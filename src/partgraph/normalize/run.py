"""The normalize stage: adapter rows -> enriched, source-stamped JSONL.

:func:`normalize` consumes an adapter's ``iter_parts()`` stream, enriches each
:class:`StagedPart` (protocol tags from the description; numeric attributes
promoted into the SI ``promoted`` map via a fixed allowlist), stamps the
injected ``source_ref`` and writes one JSON object per line.

Both write modes are deterministic and produce byte-identical output for the
same input, because both buffer, sort by ``(lcsc_id, xid)`` ascending and write
the same serialization (``sort_keys=True``, ``ensure_ascii=False``):

- **No checkpoint** (default): parts are written atomically via a temp file.
- **With ``checkpoint_path``**: parts are written in fixed-size windows, and a
  **single-marker** checkpoint (``last_lcsc`` + ``rows_written``) is flushed
  once per window, on clean completion, and on abort. A re-run truncates the
  output back to the last committed window boundary (``rows_written`` lines) and
  skips the already-written sorted prefix, so an interruption loses at most the
  last partial window and resume produces no duplicates and no gaps. Because the
  sort order is deterministic, ``rows_written`` is an exact, stable cursor into
  the sorted stream.

The checkpoint is a single marker by design (see AC-NORMALIZE-2): it records the
last committed ``lcsc`` and the number of rows written, never a growing list of
every seen id. Per-row checkpoint rewrites of a growing collection are O(n^2)
and are forbidden — see :data:`_CHECKPOINT_WINDOW` and the regression test
``test_norm_resume_checkpoint_writes_are_windowed_not_per_row``.

``source_ref`` is always an injected parameter — the module never reads the
clock — so provenance strings are deterministic and testable.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from partgraph.normalize.model import StagedPart
from partgraph.normalize.promote import enrich_attributes, promote
from partgraph.normalize.tags import extract_tags

__all__ = ["normalize"]

# Number of rows written between checkpoint flushes (and the maximum number of
# rows an interruption may force a resume to redo). A constant, bounded window
# makes checkpointing O(n / window) writes overall instead of O(n) — and each
# write is O(1) in state size because the checkpoint is a single marker, not a
# growing collection. This is the structural guard against the O(n^2)
# per-row-rewrite defect.
_CHECKPOINT_WINDOW = 5000


class _Adapter(Protocol):
    """Structural type for anything with an ``iter_parts()`` method."""

    def iter_parts(self) -> Iterable[StagedPart]: ...


def _sort_key(part: StagedPart) -> tuple[str, str]:
    """Return the deterministic sort key for *part* (lcsc ASC, xid tie-break)."""
    return (part.lcsc_id or "", part.xid)


def _enrich(part: StagedPart, source_ref: str) -> StagedPart:
    """Return a copy of *part* with attributes enriched, tags + promoted params
    and ``source_ref`` set.

    Deterministic and clock-free: attribute ``value_num``/``unit`` are derived by
    re-parsing ``value_text`` (with range / multi-value expansion), tags derive
    from the description lexicon, and promotion reads only the enriched
    attributes. Any ``promoted`` value already present on the part is preserved
    (a pre-seeded key wins over an attribute-derived one).
    """
    tags = list(part.tags)
    derived = extract_tags(part.description or "")
    for tag in derived:
        if tag not in tags:
            tags.append(tag)

    enriched_attrs = enrich_attributes(part.attributes)

    promoted = dict(part.promoted)
    for key, value in promote(enriched_attrs).items():
        if key not in promoted:
            promoted[key] = value

    return dataclasses.replace(
        part,
        attributes=enriched_attrs,
        tags=tags,
        promoted=promoted,
        source_ref=source_ref,
    )


def _enriched_sorted(adapter: _Adapter, source_ref: str) -> list[StagedPart]:
    """Return all parts enriched and sorted by ``(lcsc_id, xid)`` ascending.

    This is the single source of deterministic ordering shared by both the
    plain and resumable write paths, so their outputs are byte-identical.
    """
    enriched = [_enrich(part, source_ref) for part in adapter.iter_parts()]
    enriched.sort(key=_sort_key)
    return enriched


def _load_checkpoint(checkpoint_path: Path) -> tuple[str | None, int]:
    """Return ``(last_lcsc, rows_written)`` from a single-marker checkpoint.

    A missing or unreadable checkpoint, or one missing the ``rows_written``
    marker, yields ``(None, 0)`` (treated as a fresh run). ``last_lcsc`` is the
    last committed ``lcsc`` value and is informational; ``rows_written`` is the
    authoritative resume cursor into the deterministic sorted stream.
    """
    if not checkpoint_path.exists():
        return (None, 0)
    try:
        data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return (None, 0)
    if not isinstance(data, dict):
        return (None, 0)
    rows = data.get("rows_written")
    if not isinstance(rows, int) or rows < 0:
        return (None, 0)
    last = data.get("last_lcsc")
    last_lcsc = str(last) if isinstance(last, str) else None
    return (last_lcsc, rows)


def _write_checkpoint(
    checkpoint_path: Path,
    *,
    last_lcsc: str | None,
    rows_written: int,
    source_ref: str,
) -> None:
    """Persist the single-marker checkpoint atomically.

    The payload is fixed-size (a marker and two counters) regardless of how many
    rows have been processed, so each write is O(1) — never O(rows). Written via
    a temp file + ``os.replace`` so a crash mid-write cannot corrupt the marker.
    """
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "source_ref": source_ref,
        "last_lcsc": last_lcsc,
        "rows_written": rows_written,
    }
    tmp = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, checkpoint_path)


def _truncate_to_lines(output_path: Path, line_count: int) -> None:
    """Truncate *output_path* in place to its first *line_count* lines.

    Used on resume to rewind the output to the last committed window boundary,
    discarding any partial-window lines an interrupted run may have flushed past
    the checkpoint. A missing file (or ``line_count == 0``) leaves nothing to do
    beyond ensuring an empty file exists for subsequent appends.
    """
    if line_count <= 0:
        # Nothing committed: start the output empty so appends rebuild from scratch.
        with output_path.open("w", encoding="utf-8"):
            pass
        return
    if not output_path.exists():
        return
    kept = 0
    tmp = output_path.with_name(output_path.name + ".rewind")
    with (
        output_path.open("r", encoding="utf-8") as src,
        tmp.open("w", encoding="utf-8") as dst,
    ):
        for line in src:
            if kept >= line_count:
                break
            dst.write(line)
            kept += 1
    os.replace(tmp, output_path)


def _normalize_sorted(
    adapter: _Adapter,
    source_ref: str,
    output_path: Path,
) -> int:
    """Buffer, sort by lcsc ASC and write atomically. Returns the row count."""
    enriched = _enriched_sorted(adapter, source_ref)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_name(output_path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for part in enriched:
            fh.write(part.to_json())
            fh.write("\n")
    os.replace(tmp, output_path)
    return len(enriched)


def _normalize_resumable(
    adapter: _Adapter,
    source_ref: str,
    output_path: Path,
    checkpoint_path: Path,
) -> int:
    """Sort, then window-append with a single-marker checkpoint.

    Determinism: the enriched parts are sorted by ``(lcsc_id, xid)`` exactly as
    in :func:`_normalize_sorted`, so a completed resumable run is byte-identical
    to a plain run over the same input.

    Resume: ``rows_written`` from the checkpoint is the count of rows already
    committed to the output. The output is truncated back to that many lines
    (dropping any partial-window remainder) and the first ``rows_written`` sorted
    parts are skipped, so the run continues exactly where the last committed
    window ended — no duplicates, no gaps.

    Durability: the checkpoint is flushed once per :data:`_CHECKPOINT_WINDOW`
    rows, on clean completion, and in a ``finally`` on abort. An interruption
    therefore loses at most one partial window of work. Each flush writes a
    fixed-size marker (O(1)), never a growing collection (which would be O(n^2)
    across the run).

    Returns the number of records written during this invocation.
    """
    enriched = _enriched_sorted(adapter, source_ref)
    total = len(enriched)

    _last_lcsc, already = _load_checkpoint(checkpoint_path)
    resuming = checkpoint_path.exists()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if resuming:
        # Rewind output to the last committed window boundary, then continue.
        _truncate_to_lines(output_path, already)
        start = min(already, total)
        mode = "a"
    else:
        start = 0
        mode = "w"

    written_this_run = 0
    committed = start
    last_lcsc = enriched[start - 1].lcsc_id if start > 0 else _last_lcsc

    try:
        with output_path.open(mode, encoding="utf-8") as fh:
            buf: list[str] = []
            for part in enriched[start:]:
                buf.append(part.to_json())
                buf.append("\n")
                written_this_run += 1

                # Flush a full window together with its checkpoint marker so the
                # on-disk output and the marker advance atomically as a pair.
                if written_this_run % _CHECKPOINT_WINDOW == 0:
                    fh.write("".join(buf))
                    buf.clear()
                    fh.flush()
                    committed = start + written_this_run
                    last_lcsc = part.lcsc_id
                    _write_checkpoint(
                        checkpoint_path,
                        last_lcsc=last_lcsc,
                        rows_written=committed,
                        source_ref=source_ref,
                    )

            # Flush the trailing partial window and record clean completion.
            if buf:
                fh.write("".join(buf))
                buf.clear()
                fh.flush()
            committed = total
            last_lcsc = enriched[-1].lcsc_id if enriched else last_lcsc
        _write_checkpoint(
            checkpoint_path,
            last_lcsc=last_lcsc,
            rows_written=committed,
            source_ref=source_ref,
        )
    except BaseException:
        # On any abort, persist the last *committed* window boundary so a resume
        # rewinds to a clean line count. Buffered-but-unflushed rows are not
        # counted, so the marker never claims more than what reached the file.
        _write_checkpoint(
            checkpoint_path,
            last_lcsc=last_lcsc,
            rows_written=committed,
            source_ref=source_ref,
        )
        raise

    return written_this_run


def normalize(
    *,
    adapter: _Adapter,
    source_ref: str,
    output_path: str | Path,
    checkpoint_path: str | Path | None = None,
) -> int:
    """Run the normalize stage over *adapter* into *output_path* (JSONL).

    Args:
        adapter: Any object exposing ``iter_parts()`` yielding StagedParts.
        source_ref: Provenance string (e.g. ``"jlcparts@2026-06-11"``) stamped
            on every record. Injected — never derived from the clock.
        output_path: Destination JSONL file.
        checkpoint_path: Optional checkpoint file enabling resumable streaming.
            When ``None``, the stage runs in deterministic sorted mode.

    Returns:
        The number of records written during this invocation.
    """
    output_path = Path(output_path)
    if checkpoint_path is None:
        return _normalize_sorted(adapter, source_ref, output_path)
    return _normalize_resumable(
        adapter, source_ref, output_path, Path(checkpoint_path)
    )
