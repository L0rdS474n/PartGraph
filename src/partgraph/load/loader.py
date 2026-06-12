"""Batched, idempotent Dgraph loader for :class:`StagedPart` records.

:class:`Loader` writes parts into Dgraph one transaction per batch using the
Dgraph **upsert** pattern: a single query resolves the UIDs of all entities the
batch touches (Parts by ``xid``; Manufacturers/Packages/Tags by name;
Categories by name and parent; Datasheets by url; AttrValues by
name+value), then a single JSON ``set_obj`` mutation creates-or-updates the
Part graph, reusing resolved UIDs and otherwise minting blank nodes.

Security:
- Only JSON mutations (``txn.mutate(set_obj=...)``) are used. Untrusted strings
  (descriptions, names, urls) are never concatenated into N-Quads, so escaping
  bugs and injection are impossible — ``json``/pydgraph handle serialization.
- The lookup query passes every value through pydgraph **query variables**
  (``$v0``, ``$v1`` ...) rather than string interpolation, so no untrusted value
  ever lands in the DQL text.

Robustness:
- ``promoted`` keys are filtered through a fixed allowlist; unknown keys and
  ``None`` values are dropped (the JSON key is simply absent).
- ``source_refs`` are appended only when not already present on an existing
  node, so reloads never duplicate provenance.

After a successful run the loader writes ``data/state/load_metrics.json`` with
``parts_loaded``, ``wall_seconds`` and ``parts_per_second``.

Resumability (load-robustness-v2, AC-A):
- When ``load(parts, checkpoint_path=..., fingerprint=...)`` is given a
  ``checkpoint_path``, the loader writes a single-marker checkpoint
  (``batches_committed``, ``parts_loaded``, ``fingerprint``) atomically (temp
  file + ``os.replace``) after **each** successfully committed batch.
- On start, if the checkpoint exists and its ``fingerprint`` matches the
  passed ``fingerprint``, the first ``batches_committed`` batches are skipped
  (never sent to the client) and processing resumes at that absolute batch
  index, continuing the ``parts_loaded`` running total from the checkpoint.
  Because batch boundaries are a deterministic function of the parts order and
  ``batch_size``, batch N maps to the same slice across runs — so skipping
  already-committed batches is gap-free and overlap-free.
- A fingerprint mismatch (the staged file that produced ``parts`` changed) or a
  missing/``None`` ``checkpoint_path`` starts from batch 0; a stale checkpoint
  is overwritten on the first new commit.
- ``load(parts)`` with no checkpoint arguments behaves exactly as before: no
  checkpoint file is read or written (back-compat).
"""

from __future__ import annotations

import json
import os
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from partgraph.normalize.model import StagedPart

__all__ = ["PROMOTED_ALLOWLIST", "Loader"]

# -- per-batch transient-error retry policy ---------------------------------
# A whole batch is an idempotent upsert, so re-running it after a transient
# Dgraph/gRPC error is safe. Backoff is exponential with full jitter, capped.
#
# load-robustness-v2 (AC-B): _MAX_ATTEMPTS 5 -> 8 and _BACKOFF_CAP_S 8.0 -> 30.0
# so a full-jitter retry window spans a typical Dgraph container restart plus a
# Raft leader election (~1-2 min worst case) instead of giving up after ~8 s.
_MAX_ATTEMPTS = 8
_BACKOFF_BASE_S = 0.5
_BACKOFF_CAP_S = 30.0

# Retryable gRPC status names (matched case-insensitively against str(exc)).
_RETRYABLE_GRPC_STATUSES = ("UNKNOWN", "UNAVAILABLE", "ABORTED")
# Substrings (lower-cased) that identify a retryable transient condition even
# when no gRPC status code is exposed (Dgraph leader churn / txn conflicts).
_RETRYABLE_SUBSTRINGS = ("only leader", "aborted")


def _is_retryable(exc: BaseException) -> bool:
    """Return ``True`` if *exc* is a transient error worth retrying.

    Retryable when any holds:
    - a gRPC status code attribute resolves to UNKNOWN/UNAVAILABLE/ABORTED, or
    - ``str(exc)`` mentions one of those statuses, or
    - the message contains "Only leader" (leader churn) or an "aborted"
      transaction conflict.
    Everything else (e.g. TypeError, ValueError, schema errors) is fatal.
    """
    # gRPC status via a code() callable or a plain attribute.
    code = getattr(exc, "code", None)
    status_text = ""
    if callable(code):
        try:
            status_text = str(code())
        except Exception:  # noqa: BLE001 — defensive: a broken code() is not retryable signal
            status_text = ""
    elif code is not None:
        status_text = str(code)

    haystack = f"{status_text} {exc}".upper()
    if any(status in haystack for status in _RETRYABLE_GRPC_STATUSES):
        return True
    low = str(exc).lower()
    return any(sub in low for sub in _RETRYABLE_SUBSTRINGS)


def _backoff_delay(attempt: int) -> float:
    """Return the (pre-jitter) backoff ceiling for a 1-based *attempt* number."""
    return min(_BACKOFF_BASE_S * (2 ** (attempt - 1)), _BACKOFF_CAP_S)

# Promoted predicates the loader is permitted to write. Anything outside this
# set is silently dropped from the mutation payload.
PROMOTED_ALLOWLIST: frozenset[str] = frozenset({
    "voltage_min",
    "voltage_max",
    "current_max",
    "resistance",
    "capacitance",
    "inductance",
    "frequency_max",
    "power",
    "tolerance_pct",
})

# Repo-root-relative location of the load metrics file (see GATE-3).
#   src/partgraph/load/loader.py -> load -> partgraph -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LOAD_METRICS_PATH = _REPO_ROOT / "data" / "state" / "load_metrics.json"

ProgressCallback = Callable[[int, int], None]


class _UidRegistry:
    """Per-batch allocator of stable blank-node labels and query variables.

    Each distinct entity key is assigned a sequential index, yielding a safe
    blank-node label ``_:n<index>`` (never derived from untrusted text) and a
    query variable name ``$v<index>``. The registry also records the value to
    bind to each variable so the lookup query stays free of interpolation.
    """

    def __init__(self) -> None:
        self._index_by_key: dict[str, int] = {}
        self._values: list[str] = []

    def intern(self, key: str, value: str) -> int:
        """Return a stable index for *key*, recording *value* for its variable."""
        idx = self._index_by_key.get(key)
        if idx is None:
            idx = len(self._values)
            self._index_by_key[key] = idx
            self._values.append(value)
        return idx

    def blank(self, idx: int) -> str:
        return f"_:n{idx}"

    def var_name(self, idx: int) -> str:
        return f"$v{idx}"

    def value_at(self, idx: int) -> str:
        return self._values[idx]

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True)
class _BatchCtx:
    """The per-batch resolution context shared by the payload builders.

    Bundles the blank-node/variable :class:`_UidRegistry` with the ``resolved``
    map (``b<index>`` -> existing uid, ``__refs__b<index>`` -> existing
    source_refs) so payload helpers take one context argument instead of two.
    """

    registry: _UidRegistry
    resolved: dict[str, Any]

    def uid_for(self, idx: int) -> str:
        """Return the existing uid for entity *idx*, else its blank-node label."""
        existing = self.resolved.get(f"b{idx}")
        return existing if existing else self.registry.blank(idx)

    def existing_refs(self, idx: int) -> list[str]:
        """Return the existing source_refs recorded for entity *idx* (or [])."""
        refs = self.resolved.get(f"__refs__b{idx}")
        return refs if isinstance(refs, list) else []


class Loader:
    """Loads StagedPart records into Dgraph in idempotent, upserting batches."""

    def __init__(
        self,
        client: Any,
        batch_size: int = 1000,
        *,
        progress: ProgressCallback | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        """Create a loader.

        Args:
            client: A pydgraph client exposing ``txn()`` -> transaction with
                ``query``, ``mutate``, ``commit`` and ``discard``.
            batch_size: Number of parts per transaction (default 1000).
            progress: Optional ``progress(current, total)`` callback invoked
                after each committed batch.
            sleep: Sleep function used for retry backoff (default
                ``time.sleep``). Tests inject a no-op / recording callable so no
                real wall-clock sleep occurs and the jitter values can be
                asserted. The retry loop NEVER calls ``time.sleep`` directly.
            rng: Source of full-jitter randomness for backoff (default a fresh
                ``random.Random()``). Injectable/seedable for reproducibility.
        """
        self.client = client
        self.batch_size = batch_size
        self._progress = progress
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()

    # -- public API ---------------------------------------------------------

    def load(
        self,
        parts: Sequence[StagedPart],
        *,
        checkpoint_path: str | Path | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Upsert *parts* into Dgraph and write the load-metrics file.

        Args:
            parts: The records to load.
            checkpoint_path: Optional resume marker. When given, a single-marker
                checkpoint (``batches_committed``, ``parts_loaded``,
                ``fingerprint``) is written atomically after each committed
                batch. On start, a checkpoint whose ``fingerprint`` matches
                *fingerprint* lets the loader skip the already-committed batches
                and resume at the next absolute batch index. ``None`` (the
                default) disables checkpointing entirely — no file is read or
                written (back-compat with the original ``load(parts)`` call).
            fingerprint: Identity token of the staged file that produced *parts*
                (e.g. ``"<size>:<mtime_ns>"``). A checkpoint only resumes when
                its stored fingerprint equals this value; otherwise the loader
                restarts from batch 0 and overwrites the stale checkpoint.

        Returns:
            The metrics dict that was persisted to ``load_metrics.json``. Its
            ``parts_loaded`` reflects the TOTAL parts represented by a completed
            run — including any parts resumed from a matching checkpoint — so
            it equals ``len(parts)`` on a full completion regardless of resume.
        """
        parts = list(parts)
        total = len(parts)
        cp_path = Path(checkpoint_path) if checkpoint_path is not None else None
        start = time.perf_counter()

        # Resume cursor: how many leading batches a matching checkpoint already
        # committed, and the parts_loaded running total carried over from it.
        skip_batches, loaded = self._resume_cursor(cp_path, fingerprint)

        batches_committed = skip_batches
        for batch_index, begin in enumerate(range(0, total, self.batch_size)):
            if batch_index < skip_batches:
                # Already committed on a previous run for this same fingerprint:
                # never send these parts to the client (idempotent-safe skip).
                continue
            batch = parts[begin:begin + self.batch_size]
            # Absolute batch index is passed through so retry/exhaustion
            # messages and checkpoint counts stay correct across resume.
            self._load_batch(batch, batch_index)
            loaded += len(batch)
            batches_committed += 1
            if cp_path is not None:
                self._write_checkpoint(
                    cp_path,
                    batches_committed=batches_committed,
                    parts_loaded=loaded,
                    fingerprint=fingerprint,
                )
            if self._progress is not None:
                self._progress(loaded, total)

        wall_seconds = time.perf_counter() - start
        metrics = {
            "parts_loaded": loaded,
            "wall_seconds": wall_seconds,
            "parts_per_second": loaded / max(wall_seconds, 1e-9),
        }
        self._write_metrics(metrics)
        return metrics

    # -- resume / checkpoint helpers ----------------------------------------

    def _resume_cursor(
        self,
        checkpoint_path: Path | None,
        fingerprint: str | None,
    ) -> tuple[int, int]:
        """Return ``(skip_batches, parts_already_loaded)`` for a resume.

        ``(0, 0)`` means start fresh. A non-zero result is only ever returned
        when *checkpoint_path* exists, is a well-formed single-marker checkpoint,
        and its stored ``fingerprint`` equals *fingerprint* exactly. A missing
        file, unreadable/malformed JSON, or any fingerprint mismatch yields
        ``(0, 0)`` so the loader restarts from batch 0 and overwrites the stale
        marker on its first new commit.
        """
        if checkpoint_path is None or not checkpoint_path.exists():
            return (0, 0)
        try:
            data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return (0, 0)
        if not isinstance(data, dict):
            return (0, 0)
        if data.get("fingerprint") != fingerprint:
            # Staged file changed (or no fingerprint given): unsafe to resume.
            return (0, 0)
        committed = data.get("batches_committed")
        loaded = data.get("parts_loaded")
        if not isinstance(committed, int) or committed < 0:
            return (0, 0)
        if not isinstance(loaded, int) or loaded < 0:
            loaded = 0
        return (committed, loaded)

    def _write_checkpoint(
        self,
        checkpoint_path: Path,
        *,
        batches_committed: int,
        parts_loaded: int,
        fingerprint: str | None,
    ) -> None:
        """Persist the single-marker load checkpoint atomically.

        The payload is fixed-size (three fields) regardless of how many parts
        have been processed, so each write is O(1). Written via a temp file +
        ``os.replace`` so a crash mid-write cannot corrupt the marker — mirrors
        the normalize-stage checkpoint pattern.
        """
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "batches_committed": batches_committed,
            "parts_loaded": parts_loaded,
            "fingerprint": fingerprint,
        }
        tmp = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp, checkpoint_path)

    # -- batch processing ---------------------------------------------------

    def _load_batch(self, batch: Sequence[StagedPart], batch_index: int) -> None:
        """Upsert one batch, retrying transient errors with capped backoff.

        Each attempt opens a fresh transaction (via :meth:`_load_batch_once`).
        Because the whole batch is an idempotent upsert, re-running it after a
        transient failure is safe. Up to :data:`_MAX_ATTEMPTS` attempts are made:

        - A retryable error (see :func:`_is_retryable`) triggers a full-jitter
          sleep — ``rng.uniform(0, min(0.5 * 2**(attempt-1), 30.0))`` — via the
          injected ``sleep`` callable, then another attempt.
        - A fatal error propagates immediately (no sleep).
        - Exhausting all attempts raises a single :class:`RuntimeError` naming
          the 0-based batch index and the attempt count, chained from the last
          transient error.
        """
        last_exc: BaseException | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                self._load_batch_once(batch)
                return
            except BaseException as exc:  # classify, then sleep+retry or re-raise
                if not _is_retryable(exc):
                    raise  # fatal: propagate the original immediately, no sleep
                last_exc = exc
                if attempt < _MAX_ATTEMPTS:
                    delay = self._rng.uniform(0.0, _backoff_delay(attempt))
                    self._sleep(delay)
        # All attempts exhausted on a retryable error.
        raise RuntimeError(
            f"batch {batch_index} failed after {_MAX_ATTEMPTS} attempts"
        ) from last_exc

    def _load_batch_once(self, batch: Sequence[StagedPart]) -> None:
        """Resolve UIDs then upsert one batch within a single fresh transaction.

        The batch is first collapsed on ``xid`` keeping the **last** occurrence
        (``fix/loader-batch-internal-duplicates``). The same xid appearing twice
        in one batch must map to a single Part node: ``@upsert`` deduplicates
        across transactions, not within one mutation, so two same-xid objects in
        a single ``set_obj`` would create two Part nodes. Collapsing here means
        each xid is interned, queried, blank-noded and emitted exactly once, and
        the surviving fields are the last occurrence's (deterministic merge).
        """
        batch = self._dedupe_batch_last_wins(batch)

        registry = _UidRegistry()

        # Assign a query/blank index to every entity the batch references. The
        # Part key is value-based (``part::<xid>``, no batch position), so each
        # distinct xid gets exactly one blank-node index — matching the
        # value-based keying already used for every nested entity.
        part_indices: list[int] = []
        for part in batch:
            part_indices.append(registry.intern(f"part::{part.xid}", part.xid))
            self._register_entities(part, registry)

        query, variables = self._build_lookup_query(batch, registry)

        txn = self.client.txn()
        try:
            resolved = self._run_lookup(txn, query, variables)
            ctx = _BatchCtx(registry=registry, resolved=resolved)
            payload = [
                self._build_part_obj(part, part_idx, ctx)
                for part, part_idx in zip(batch, part_indices, strict=False)
            ]
            txn.mutate(set_obj=payload)
            txn.commit()
        finally:
            txn.discard()

    @staticmethod
    def _dedupe_batch_last_wins(batch: Sequence[StagedPart]) -> list[StagedPart]:
        """Collapse same-``xid`` parts within a batch, keeping the last occurrence.

        Insertion-ordered ``dict`` semantics give a stable, last-occurrence-wins
        result: re-assigning an existing key overwrites the value but preserves
        the key's original position, so output order matches first appearance
        while the retained record is the latest one seen. Parts with distinct
        xids are unaffected.
        """
        deduped: dict[str, StagedPart] = {}
        for part in batch:
            deduped[part.xid] = part
        return list(deduped.values())

    def _register_entities(self, part: StagedPart, registry: _UidRegistry) -> None:
        """Intern every entity referenced by *part* so it gets a stable index."""
        if part.mfr_name:
            registry.intern(f"mfr::{part.mfr_name}", part.mfr_name)
        if part.category:
            registry.intern(f"cat1::{part.category}", part.category)
        if part.subcategory and part.category:
            registry.intern(
                f"cat2::{part.category}::{part.subcategory}", part.subcategory
            )
        if part.package:
            registry.intern(f"pkg::{part.package}", part.package)
        if part.datasheet_url:
            registry.intern(f"ds::{part.datasheet_url}", part.datasheet_url)
        for tag in part.tags:
            registry.intern(f"tag::{tag}", tag)
        for attr in part.attributes:
            registry.intern(
                f"attr::{attr.name}::{attr.value_text}",
                attr.value_text if attr.value_text is not None else "",
            )

    # -- lookup query (variables only; no value interpolation) --------------

    def _build_lookup_query(
        self,
        batch: Sequence[StagedPart],
        registry: _UidRegistry,
    ) -> tuple[str, dict[str, str]]:
        """Return ``(query_text, variables)`` resolving all batch entity UIDs.

        Every block is named ``b<index>`` and filters by type so name-based
        lookups never collide across node types. Values bind via ``$v<index>``
        variables — the query text contains only literal predicate/type names.
        """
        seen: set[int] = set()
        used_vars: dict[int, str] = {}
        blocks: list[str] = []

        def use_var(idx: int) -> str:
            """Mark variable *idx* as used and return its ``$v<idx>`` name."""
            used_vars[idx] = registry.value_at(idx)
            return registry.var_name(idx)

        def add_block(idx: int, body: str) -> None:
            if idx in seen:
                return
            seen.add(idx)
            blocks.append(body)

        for part in batch:
            # Part by xid (also fetch existing source_refs for dedup).
            xid_idx = registry.intern(f"part::{part.xid}::lookup", part.xid)
            add_block(
                xid_idx,
                f"  b{xid_idx}(func: eq(xid, {use_var(xid_idx)})) "
                f"{{ uid xid source_refs }}",
            )
            if part.mfr_name:
                idx = registry.intern(f"mfr::{part.mfr_name}", part.mfr_name)
                add_block(
                    idx,
                    f"  b{idx}(func: eq(name, {use_var(idx)})) "
                    f"@filter(type(Manufacturer)) {{ uid }}",
                )
            if part.category:
                idx = registry.intern(f"cat1::{part.category}", part.category)
                add_block(
                    idx,
                    f"  b{idx}(func: eq(name, {use_var(idx)})) "
                    f"@filter(type(Category) AND NOT has(parent)) {{ uid }}",
                )
            if part.subcategory and part.category:
                idx = registry.intern(
                    f"cat2::{part.category}::{part.subcategory}", part.subcategory
                )
                parent_idx = registry.intern(f"cat1::{part.category}", part.category)
                add_block(
                    idx,
                    f"  b{idx}(func: eq(name, {use_var(idx)})) "
                    f"@filter(type(Category) AND has(parent)) "
                    f"{{ uid parent @filter(eq(name, {use_var(parent_idx)})) "
                    f"{{ uid }} }}",
                )
            if part.package:
                idx = registry.intern(f"pkg::{part.package}", part.package)
                add_block(
                    idx,
                    f"  b{idx}(func: eq(name, {use_var(idx)})) "
                    f"@filter(type(Package)) {{ uid }}",
                )
            if part.datasheet_url:
                idx = registry.intern(f"ds::{part.datasheet_url}", part.datasheet_url)
                add_block(
                    idx,
                    f"  b{idx}(func: eq(url, {use_var(idx)})) "
                    f"@filter(type(Datasheet)) {{ uid }}",
                )
            for tag in part.tags:
                idx = registry.intern(f"tag::{tag}", tag)
                add_block(
                    idx,
                    f"  b{idx}(func: eq(name, {use_var(idx)})) "
                    f"@filter(type(Tag)) {{ uid }}",
                )
            for attr in part.attributes:
                idx = registry.intern(
                    f"attr::{attr.name}::{attr.value_text}",
                    attr.value_text if attr.value_text is not None else "",
                )
                add_block(
                    idx,
                    f"  b{idx}(func: eq(attr_value, {use_var(idx)})) "
                    f"@filter(type(AttrValue)) {{ uid attr_name attr_value }}",
                )

        # Declare ONLY variables actually referenced in blocks (Dgraph rejects
        # unused declared variables).
        variables = {registry.var_name(i): v for i, v in used_vars.items()}
        decl = ", ".join(f"{name}: string" for name in variables)
        header = f"query batch({decl})" if decl else "query batch()"
        body = "\n".join(blocks) if blocks else "  q(func: type(Part), first: 0) { uid }"
        query_text = header + " {\n" + body + "\n}"
        return query_text, variables

    def _run_lookup(
        self,
        txn: Any,
        query: str,
        variables: dict[str, str],
    ) -> dict[str, Any]:
        """Execute the lookup query and map ``b<index>`` -> existing uid.

        Existing ``source_refs`` are stashed under ``__refs__b<index>`` keys.
        Missing blocks (the common case for a fresh DB, and for the unit-test
        mock that returns ``{"q": []}``) simply resolve to no uid.
        """
        resp = txn.query(query, variables=variables)
        raw = getattr(resp, "json", None)
        resolved: dict[str, Any] = {}
        source_refs: dict[str, list[str]] = {}
        if raw is None:
            return resolved
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return resolved
        if not isinstance(data, dict):
            return resolved
        for block_name, rows in data.items():
            if not block_name.startswith("b") or not isinstance(rows, list) or not rows:
                continue
            first = rows[0]
            if not isinstance(first, dict):
                continue
            uid = first.get("uid")
            if uid:
                resolved[block_name] = uid
            if "source_refs" in first:
                refs = first.get("source_refs")
                if isinstance(refs, list):
                    source_refs[block_name] = [str(r) for r in refs]
                elif isinstance(refs, str):
                    source_refs[block_name] = [refs]
        # Stash source_refs alongside resolved uids under a reserved prefix.
        for name, refs in source_refs.items():
            resolved[f"__refs__{name}"] = refs
        return resolved

    # -- mutation payload ---------------------------------------------------

    def _entity_obj(
        self,
        ctx: _BatchCtx,
        idx: int,
        *,
        dgraph_type: str,
        fields: dict[str, Any],
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        """Build a nested entity object (existing-uid reuse or blank create)."""
        obj: dict[str, Any] = {"uid": ctx.uid_for(idx), "dgraph.type": dgraph_type}
        for key, value in fields.items():
            if value is not None:
                obj[key] = value
        if source_ref and source_ref not in ctx.existing_refs(idx):
            obj["source_refs"] = [source_ref]
        return obj

    def _build_part_obj(
        self,
        part: StagedPart,
        part_idx: int,
        ctx: _BatchCtx,
    ) -> dict[str, Any]:
        """Build the JSON object for a Part with all nested edges."""
        # Resolve the Part's own uid via the lookup block keyed by xid.
        lookup_idx = ctx.registry.intern(f"part::{part.xid}::lookup", part.xid)
        part_uid = ctx.resolved.get(f"b{lookup_idx}") or ctx.registry.blank(part_idx)

        obj: dict[str, Any] = {
            "uid": part_uid,
            "dgraph.type": "Part",
            "xid": part.xid,
            "mpn": part.mpn,
            "mpn_norm": part.mpn_norm,
            "is_basic": part.is_basic,
        }
        # Optional scalar fields: present only when not None.
        for key, value in (
            ("description", part.description),
            ("lcsc_id", part.lcsc_id),
            ("stock", part.stock),
            ("price_usd", part.price_usd),
        ):
            if value is not None:
                obj[key] = value

        # Promoted numeric params: allowlist + non-None only.
        for key, value in part.promoted.items():
            if key in PROMOTED_ALLOWLIST and value is not None:
                obj[key] = value

        # Provenance (dedup against existing).
        if part.source_ref and part.source_ref not in ctx.existing_refs(lookup_idx):
            obj["source_refs"] = [part.source_ref]

        self._attach_edges(obj, part, ctx)
        return obj

    def _attach_edges(
        self,
        obj: dict[str, Any],
        part: StagedPart,
        ctx: _BatchCtx,
    ) -> None:
        """Attach all relationship edges to the Part payload *obj* in place."""
        if part.mfr_name:
            idx = ctx.registry.intern(f"mfr::{part.mfr_name}", part.mfr_name)
            obj["made_by"] = [self._entity_obj(
                ctx, idx, dgraph_type="Manufacturer",
                fields={"name": part.mfr_name}, source_ref=part.source_ref,
            )]

        category_obj = self._build_category_obj(part, ctx)
        if category_obj is not None:
            obj["in_category"] = [category_obj]

        if part.package:
            idx = ctx.registry.intern(f"pkg::{part.package}", part.package)
            obj["in_package"] = [self._entity_obj(
                ctx, idx, dgraph_type="Package",
                fields={"name": part.package}, source_ref=part.source_ref,
            )]

        if part.datasheet_url:
            idx = ctx.registry.intern(f"ds::{part.datasheet_url}", part.datasheet_url)
            obj["datasheet"] = [self._entity_obj(
                ctx, idx, dgraph_type="Datasheet",
                fields={"url": part.datasheet_url, "source": part.source_ref or None},
                source_ref=part.source_ref,
            )]

        if part.tags:
            obj["tagged"] = [
                self._entity_obj(
                    ctx, ctx.registry.intern(f"tag::{tag}", tag),
                    dgraph_type="Tag", fields={"name": tag},
                )
                for tag in part.tags
            ]

        if part.attributes:
            obj["attr"] = [self._attr_obj(ctx, attr) for attr in part.attributes]

    def _attr_obj(self, ctx: _BatchCtx, attr: Any) -> dict[str, Any]:
        """Build an AttrValue payload for one attribute record."""
        idx = ctx.registry.intern(
            f"attr::{attr.name}::{attr.value_text}",
            attr.value_text if attr.value_text is not None else "",
        )
        return self._entity_obj(
            ctx, idx, dgraph_type="AttrValue",
            fields={
                "attr_name": attr.name,
                "attr_value": attr.value_text,
                "attr_value_num": attr.value_num,
            },
        )

    def _build_category_obj(
        self,
        part: StagedPart,
        ctx: _BatchCtx,
    ) -> dict[str, Any] | None:
        """Build the Part's in_category target (level-2 with parent level-1).

        Uses (name, parent) composite identity for the level-2 category so the
        same subcategory name under different parents yields distinct nodes.
        """
        if not part.category:
            return None

        cat1_idx = ctx.registry.intern(f"cat1::{part.category}", part.category)
        level1 = self._entity_obj(
            ctx, cat1_idx, dgraph_type="Category",
            fields={"name": part.category}, source_ref=part.source_ref,
        )

        if not part.subcategory:
            return level1

        cat2_idx = ctx.registry.intern(
            f"cat2::{part.category}::{part.subcategory}", part.subcategory
        )
        level2 = self._entity_obj(
            ctx, cat2_idx, dgraph_type="Category",
            fields={"name": part.subcategory}, source_ref=part.source_ref,
        )
        level2["parent"] = level1
        return level2

    # -- metrics ------------------------------------------------------------

    def _write_metrics(self, metrics: dict[str, Any]) -> None:
        """Persist load metrics to ``data/state/load_metrics.json``."""
        _LOAD_METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOAD_METRICS_PATH.write_text(
            json.dumps(metrics, sort_keys=True), encoding="utf-8"
        )
