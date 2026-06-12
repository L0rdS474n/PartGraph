"""Embedding pipeline for PartGraph PR4 semantic search.

This module turns Part rows into sentence-embedding vectors and writes them back
to Dgraph keyed by ``uid`` (the embedding source of truth is the GRAPH).

Lazy-import contract (ADR-0008)
-------------------------------
``sentence_transformers`` (which pulls in torch) is an OPTIONAL extra installed
only via ``pip install -e ".[embed]"``. It is imported **lazily inside
:func:`get_encoder` only**, never at module import time, so importing
``partgraph.embed`` (and running the unit suite, which mocks the encoder) never
loads the heavy model stack.

Write contract (AC-EW)
----------------------
Each successfully embedded part produces a mutation object that is *exactly*
``{"uid": "<resolved_uid>", "embedding": "[...]"}`` — the resolved uid string
(never a blank node) and the embedding encoded as Dgraph's ``float32vector``
string literal (see :func:`_vector_literal`), and nothing else. A part without an
``xid`` (so it cannot be resolved to an existing node) or whose embedding text is
empty produces no mutation, so the run never mints new nodes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

__all__ = [
    "EMBED_DIM",
    "build_embed_text",
    "embed_write",
    "generate_embeddings",
    "get_encoder",
]

#: Required embedding dimension. Pinned to all-MiniLM-L6-v2 (ADR-0008). Every
#: vector produced/consumed by the pipeline must be exactly this long.
EMBED_DIM = 384

#: Sentence-transformers model id used for semantic search (ADR-0008).
_MODEL_NAME = "all-MiniLM-L6-v2"

#: Default batch size for embedding generation when the caller does not override.
_DEFAULT_BATCH_SIZE = 64


def build_embed_text(part: Any) -> str:
    """Build the deterministic embedding input text for *part*.

    The text concatenates, in this fixed order, the part's ``description``,
    ``category``, ``package`` and ``tags`` (tags sorted for determinism). Only
    non-empty fields contribute, joined by a single space, so:

    - the string ``"None"`` never appears (a ``None`` field is simply omitted);
    - there are no double-space or stray-delimiter artefacts;
    - a part with no usable descriptive text yields the empty string ``""``.

    Tags supplement the descriptive fields but never stand alone: when every
    descriptive field (description/category/package) is empty the result is
    ``""`` regardless of tags, because tags by themselves carry no useful
    semantic context for similarity search (AC-ET-3).

    The function is pure and deterministic: the same part data always yields a
    byte-identical result (no randomness, timestamp or locale dependence).
    """
    base_fields: list[str] = []
    for value in (
        getattr(part, "description", None),
        getattr(part, "category", None),
        getattr(part, "package", None),
    ):
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                base_fields.append(stripped)

    # No descriptive context -> empty embed text, even if tags are present.
    if not base_fields:
        return ""

    tags = getattr(part, "tags", None) or []
    # Sort tags for a deterministic embedding input; drop empties.
    clean_tags = sorted(
        tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()
    )

    return " ".join(base_fields + clean_tags)


def generate_embeddings(
    texts: list[str],
    *,
    encoder: Callable,
    batch_size: int = 32,
    expected_dim: int = EMBED_DIM,
) -> list[list[float]]:
    """Generate embeddings for *texts* using the injected *encoder* callable.

    The encoder is called in batches of at most *batch_size* texts; the output
    order matches the input order exactly. The real sentence-transformers model
    is never loaded here — only the injected ``encoder`` is used — so this stays
    cheap and import-light (unit tests pass a fake encoder).

    Args:
        texts: Input strings to embed (may be empty).
        encoder: Callable mapping ``list[str] -> list[list[float]]``.
        batch_size: Maximum texts per encoder call (floored at 1).
        expected_dim: Required width of every output vector (default 384).

    Returns:
        One ``expected_dim``-length vector per input text, in input order. Empty
        input returns ``[]`` without calling the encoder.

    Raises:
        ValueError: If any produced vector is not ``expected_dim`` wide. The
            message names ``expected_dim`` so a misconfigured model is obvious.
    """
    if not texts:
        return []

    step = max(1, int(batch_size))
    vectors: list[list[float]] = []
    for begin in range(0, len(texts), step):
        batch = texts[begin:begin + step]
        encoded = encoder(batch)
        for vec in encoded:
            vector = list(vec)
            if len(vector) != expected_dim:
                raise ValueError(
                    f"Encoder produced a {len(vector)}-dimensional vector; "
                    f"expected {expected_dim}. Use a {expected_dim}-dim model "
                    f"(all-MiniLM-L6-v2)."
                )
            vectors.append(vector)
    return vectors


def _resolve_uids_by_xid(
    client: Any,
    xids: list[str],
) -> dict[str, str]:
    """Resolve ``xid -> uid`` for *xids* via a single READ-ONLY Dgraph lookup.

    The lookup uses ``client.txn(read_only=True)`` and never mutates. Any failure
    to query or parse the response degrades to an empty mapping (callers then
    fall back to a part's own ``uid``), so a transient read issue never aborts
    the write phase mid-stream. The xid values bind via a Dgraph ``$``-variable,
    so no untrusted string is interpolated into the query text.
    """
    if not xids:
        return {}

    query = (
        "query resolve($xids: string) {\n"
        "  q(func: type(Part)) @filter(eq(xid, $xids)) {\n"
        "    uid\n"
        "    xid\n"
        "  }\n"
        "}"
    )
    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query, variables={"$xids": json.dumps(xids)})
        raw = getattr(resp, "json", None)
        data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else None
    except Exception:  # noqa: BLE001 — a failed lookup falls back to part.uid.
        data = None
    finally:
        txn.discard()

    mapping: dict[str, str] = {}
    if isinstance(data, dict):
        for row in data.get("q", []) or []:
            if not isinstance(row, dict):
                continue
            xid = row.get("xid")
            uid = row.get("uid")
            if isinstance(xid, str) and isinstance(uid, str) and uid:
                mapping[xid] = uid
    return mapping


def _select_eligible_parts(parts: list[Any]) -> tuple[list[tuple[Any, str]], int]:
    """Return ``(eligible, skipped)`` for *parts*.

    A part is eligible when it has a non-empty string ``xid`` (so it resolves to
    an existing node rather than minting one) and a non-empty embedding text.
    Every other part is counted as skipped.
    """
    eligible: list[tuple[Any, str]] = []
    skipped = 0
    for part in parts:
        xid = getattr(part, "xid", None)
        text = build_embed_text(part)
        if not isinstance(xid, str) or not xid or not text:
            skipped += 1
            continue
        eligible.append((part, text))
    return eligible, skipped


def _vector_literal(vector: list[float]) -> str:
    """Encode a float vector as Dgraph's ``float32vector`` string literal.

    A ``float32vector`` predicate written through a JSON mutation must receive its
    value as the bracketed *string* ``"[0.1, 0.2, ...]"`` — NOT a native JSON
    array of floats. Passing a raw list makes Dgraph read each element as a
    separate ``FLOAT`` edge and reject the mutation with *"Input for predicate
    'embedding' of type vector is not vector. Did you forget to add quotes before
    []?"*. This mirrors the inline literal the read side
    (:func:`partgraph.query.dql_builder.build_semantic_dql`) feeds to
    ``similar_to`` — both sides use the same bracketed-string form.
    """
    return "[" + ", ".join(repr(float(component)) for component in vector) + "]"


def _build_batch_payload(
    window: list[tuple[Any, str]],
    vectors: list[list[float]],
    xid_to_uid: dict[str, str],
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(payload, skipped)`` for one batch.

    Each payload object is exactly ``{"uid": <resolved_uid>, "embedding": "[...]"}``
    — the resolved uid (from the lookup, else the part's own ``uid``) and the
    embedding encoded as Dgraph's vector string literal (see
    :func:`_vector_literal`). A part that resolves to no uid is skipped (never
    minted as a new node).
    """
    payload: list[dict[str, Any]] = []
    skipped = 0
    for (part, _text), vector in zip(window, vectors, strict=False):
        resolved = xid_to_uid.get(part.xid) or getattr(part, "uid", None)
        if not isinstance(resolved, str) or not resolved:
            skipped += 1
            continue
        payload.append({"uid": resolved, "embedding": _vector_literal(vector)})
    return payload, skipped


def embed_write(  # noqa: PLR0913 — signature is the frozen AC-EW test contract.
    parts_iter: Iterator[Any],
    client: Any,
    *,
    encoder: Callable | None = None,
    controller: Any | None = None,
    sleep: Callable | None = None,
    progress: Callable | None = None,
) -> dict:
    """Embed parts and write each embedding back to Dgraph by ``uid``.

    For every part the embedding text is built (:func:`build_embed_text`); parts
    with empty text or no resolvable uid are skipped. Remaining parts are
    embedded (batched via the injected *encoder*) and written with a mutation
    payload that is exactly ``{"uid": <resolved_uid>, "embedding": [...]}`` —
    nothing else, and never a blank node, so no new nodes are created.

    The optional adaptive *controller* paces the run between batches: after each
    batch it is asked to ``regulate`` the next batch size and any pause, and the
    pause is taken via the injected *sleep* (defaulting to :func:`time.sleep`).

    Args:
        parts_iter: Iterable of Part-like objects exposing ``xid``/``uid`` and
            the text fields used by :func:`build_embed_text`.
        client: A pydgraph client (``txn(read_only=True)`` for the uid lookup;
            ``txn()`` for the write).
        encoder: Callable ``list[str] -> list[list[float]]`` (injected; required).
        controller: Optional adaptive resource controller pacing the batches.
        sleep: One-arg sleep callable (defaults to ``time.sleep``); guards None.
        progress: Optional ``progress(done, total)`` callback after each batch.

    Returns:
        A summary dict ``{"embedded": int, "skipped": int}``.
    """
    if encoder is None:
        raise ValueError("embed_write requires an 'encoder' callable.")
    # Default/guard the sleep callable so an uninjected call never crashes.
    sleep_fn: Callable[[float], None] = sleep if sleep is not None else time.sleep

    # Materialise parts and select those that can be embedded and written.
    eligible, skipped = _select_eligible_parts(list(parts_iter))
    if not eligible:
        return {"embedded": 0, "skipped": skipped}

    # Resolve uids by xid in one read-only lookup; fall back to a part's own uid.
    xid_to_uid = _resolve_uids_by_xid(client, [part.xid for part, _ in eligible])

    total = len(eligible)
    embedded = 0
    # Start from the controller's configured ceiling when one is provided.
    batch_size = max(1, int(getattr(controller, "max_batch", _DEFAULT_BATCH_SIZE)
                          or _DEFAULT_BATCH_SIZE))

    index = 0
    while index < total:
        window = eligible[index:index + batch_size]
        vectors = generate_embeddings(
            [text for _, text in window],
            encoder=encoder, batch_size=batch_size, expected_dim=EMBED_DIM,
        )
        payload, batch_skipped = _build_batch_payload(window, vectors, xid_to_uid)
        skipped += batch_skipped

        if payload:
            _write_payload(client, payload)
            embedded += len(payload)

        index += len(window)
        if progress is not None:
            progress(embedded, total)

        # Pace the next batch via the adaptive controller, if provided.
        if controller is not None and index < total:
            batch_size = _pace_batch(controller, batch_size, sleep_fn)

    return {"embedded": embedded, "skipped": skipped}


def _write_payload(client: Any, payload: list[dict[str, Any]]) -> None:
    """Write one ``uid+embedding`` payload in a single committed transaction."""
    wtxn = client.txn()
    try:
        wtxn.mutate(set_obj=payload)
        wtxn.commit()
    finally:
        wtxn.discard()


def _pace_batch(
    controller: Any,
    batch_size: int,
    sleep_fn: Callable[[float], None],
) -> int:
    """Apply the controller's directive between batches; return the next size.

    Sleeps (via the injected *sleep_fn*) for the directed pause when positive and
    returns the controller's next batch size (floored at 1).
    """
    directive = controller.regulate(batch_size, _read_snapshot(controller))
    pause = float(getattr(directive, "pause_seconds", 0.0) or 0.0)
    if pause > 0.0:
        sleep_fn(pause)
    return max(1, int(directive.next_batch_size))


def _read_snapshot(controller: Any) -> Any:
    """Return a current system snapshot for *controller*.regulate().

    Uses the controller's own reader if it exposes one, else builds a reader via
    :func:`partgraph.util.resources.get_system_reader`. Imported lazily so the
    embed module stays a thin leaf at import time.
    """
    reader = getattr(controller, "reader", None)
    if callable(reader):
        return reader()
    from partgraph.util.resources import get_system_reader  # noqa: PLC0415

    return get_system_reader()()


def get_encoder() -> Callable:
    """Return the sentence-transformers encoder callable.

    This is the **sole** place that imports ``sentence_transformers`` (lazily,
    inside the function), keeping the rest of the package free of the heavy
    optional dependency. The returned callable maps ``list[str]`` to a list of
    384-dimensional float vectors using the all-MiniLM-L6-v2 model.

    Raises:
        ImportError: If ``sentence_transformers`` is not installed. The message
            names the package and the ``[embed]`` extra install command and is
            path-free (no filesystem path is ever interpolated).
    """
    try:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "sentence-transformers is not installed; semantic embedding is "
            'unavailable. Install the optional extra with: pip install -e ".[embed]"'
        ) from exc

    model = SentenceTransformer(_MODEL_NAME)

    def _encode(texts: list[str]) -> list[list[float]]:
        # convert_to_numpy=True keeps the call dependency-light; .tolist() yields
        # plain Python floats so downstream JSON serialisation is trivial.
        return model.encode(list(texts), convert_to_numpy=True).tolist()

    return _encode
