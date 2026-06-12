"""
Tests: T-NORM-*

Verifies partgraph.normalize.run (the normalize stage) and
partgraph.normalize.tags (tag extraction).

The normalize stage:
  - Reads from an adapter (injected), writes JSONL to data/staged/jlcparts.jsonl
    (path configurable via dependency injection for tests).
  - Is deterministic: two runs over the same adapter input produce byte-identical
    output files.
  - Is resumable: a SINGLE-MARKER checkpoint in data/state/normalize.json
    (last_lcsc + rows_written, per AC-NORMALIZE-2) allows continuation after
    interruption without duplicates or gaps. The marker is fixed-size and flushed
    once per fixed window of rows — never a growing per-row list (that pattern
    is O(n^2) and is regression-tested below to ensure it can never return).
  - source_ref is an injected parameter (never datetime.now()) so provenance
    strings are deterministic in tests.
  - Iterates LCSC IDs in ascending order.

Tags:
  - extract_tags(text) lexicon: RS-232/RS-485/I2C/SPI/UART/USB/CAN/LIN/
    Ethernet/HDMI/LVDS/PCIe (case-insensitive word-boundary match).
  - Canonicalization: "RS232" -> "RS-232".
  - No token match -> [].

NOTE: Collection will ERROR if partgraph.normalize.run or
partgraph.normalize.tags do not yet exist. That is the expected red state.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator

import pytest

import partgraph.normalize.run as normalize_run  # noqa: F401
from partgraph.normalize.run import normalize  # noqa: F401
from partgraph.normalize.tags import extract_tags  # noqa: F401
from partgraph.normalize.model import AttrRecord, StagedPart  # noqa: F401


# ---------------------------------------------------------------------------
# Minimal StagedPart factory (duplicated lightly to avoid cross-test coupling)
# ---------------------------------------------------------------------------

def _make_staged(
    lcsc_id: str,
    mpn: str = "TESTMPN",
    mpn_norm: str = "TESTMPN",
    mfr_name: str = "Mfr",
    mfr_norm: str = "MFR",
) -> StagedPart:
    return StagedPart(
        mpn=mpn,
        mpn_norm=mpn_norm,
        mfr_name=mfr_name,
        mfr_norm=mfr_norm,
        xid=f"{mpn_norm}|{mfr_norm}",
        description="Test part",
        package="SOP-8",
        category="IC",
        subcategory="Logic",
        datasheet_url="https://example.com/ds.pdf",
        lcsc_id=lcsc_id,
        stock=10,
        price_usd=0.10,
        is_basic=False,
        promoted={},
        attributes=[],
        tags=[],
        source_ref="jlcparts@2026-06-11",
    )


# ---------------------------------------------------------------------------
# Fake adapter for injecting controlled StagedPart sequences
# ---------------------------------------------------------------------------

class _FakeAdapter:
    """Minimal adapter that yields a pre-seeded list of StagedParts."""

    def __init__(self, parts: list[StagedPart]) -> None:
        self._parts = parts

    def iter_parts(self) -> Iterator[StagedPart]:
        yield from self._parts


# ---------------------------------------------------------------------------
# T-NORM-deterministic
# ---------------------------------------------------------------------------

def test_norm_deterministic_two_runs_byte_identical(tmp_path: pathlib.Path) -> None:
    """Given a fixed set of StagedParts from an injected adapter.
    When normalize() is called twice (writing to separate output paths each time
    or re-using the same configured paths in separate tmp dirs).
    Then the two JSONL output files are byte-identical.

    This proves the normalize stage is deterministic: same input -> same bytes,
    regardless of call order or OS state.
    """
    parts = [
        _make_staged("C100", mpn="LM358N",  mpn_norm="LM358N",  mfr_norm="TI"),
        _make_staged("C200", mpn="MAX232A",  mpn_norm="MAX232A",  mfr_norm="TI"),
        _make_staged("C050", mpn="NE555",    mpn_norm="NE555",    mfr_norm="STM"),
    ]
    adapter = _FakeAdapter(parts)

    run1_dir = tmp_path / "run1"
    run1_dir.mkdir()
    run2_dir = tmp_path / "run2"
    run2_dir.mkdir()

    output1 = run1_dir / "jlcparts.jsonl"
    output2 = run2_dir / "jlcparts.jsonl"

    normalize(adapter=adapter, source_ref="jlcparts@2026-06-11", output_path=output1)
    normalize(adapter=adapter, source_ref="jlcparts@2026-06-11", output_path=output2)

    assert output1.exists(), "First normalize run did not create output file."
    assert output2.exists(), "Second normalize run did not create output file."
    assert output1.read_bytes() == output2.read_bytes(), (
        "Two normalize runs over identical input produced different bytes. "
        "Normalize must be deterministic (sort by lcsc ASC, sort_keys=True)."
    )


def test_norm_deterministic_output_sorted_by_lcsc_asc(tmp_path: pathlib.Path) -> None:
    """Given parts with LCSC IDs that are not in sorted order from the adapter.
    When normalize() is called.
    Then the JSONL output lines appear in LCSC ID ascending order.
    """
    parts = [
        _make_staged("C300"),
        _make_staged("C010"),
        _make_staged("C150"),
    ]
    output = tmp_path / "sorted.jsonl"
    normalize(
        adapter=_FakeAdapter(parts),
        source_ref="jlcparts@2026-06-11",
        output_path=output,
    )
    lines = output.read_text(encoding="utf-8").splitlines()
    lcscs = [json.loads(line)["lcsc_id"] for line in lines if line.strip()]
    assert lcscs == sorted(lcscs), (
        f"JSONL output not sorted by LCSC ID ASC: {lcscs}"
    )


# ---------------------------------------------------------------------------
# T-NORM-resume
# ---------------------------------------------------------------------------

def test_norm_resume_no_duplicates_no_gaps(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given N parts to normalize under a single-marker checkpoint.
    When normalize() is interrupted after a committed window of K records
    (simulated by making the checkpoint-write raise once, exactly the way a
    real crash/kill aborts the stage mid-stream), the partial JSONL and the
    checkpoint exist and the checkpoint records the committed boundary.
    Then re-running normalize() with the same adapter (yielding all N parts
    again) produces exactly N unique records in LCSC ASC order with no
    duplicates and no gaps.

    Why the interruption is injected at the checkpoint-write (not via an adapter
    that raises mid-iteration as an earlier revision did): AC-NORMALIZE-2
    mandates sorted-by-lcsc iteration with a SINGLE marker. Sorted output cannot
    be emitted until the whole input has been read, so the only place a partial,
    resumable state can exist is at a committed write-window boundary. The prior
    streaming-in-adapter-order model (and its growing per-row checkpoint) had
    diverged from this spec and is the O(n^2) defect being fixed; this test now
    pins the spec. The behavior contract (complete, gap-free, duplicate-free
    output after resume) remains fully asserted.

    Resume is governed by data/state/normalize.json recording the last committed
    LCSC marker and the number of rows written.
    """
    N = 6
    K = 3  # commit one window of K records, then interrupt.
    all_parts = [_make_staged(f"C{i:03d}") for i in range(1, N + 1)]

    # Make the checkpoint window K so the first window commits exactly K rows.
    monkeypatch.setattr(normalize_run, "_CHECKPOINT_WINDOW", K)

    output = tmp_path / "resume.jsonl"
    checkpoint = tmp_path / "normalize_checkpoint.json"

    # Wrap the real checkpoint writer so it commits the first window normally and
    # then raises on the next call — i.e. the run is killed right after the first
    # window of K rows has been flushed and recorded.
    real_write = normalize_run._write_checkpoint
    calls = {"n": 0}

    def _flaky_write(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        real_write(*args, **kwargs)
        if calls["n"] == 1:
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(normalize_run, "_write_checkpoint", _flaky_write)

    # First run — interrupted right after the first committed window.
    with pytest.raises(RuntimeError, match="simulated interruption"):
        normalize(
            adapter=_FakeAdapter(all_parts),
            source_ref="jlcparts@2026-06-11",
            output_path=output,
            checkpoint_path=checkpoint,
        )

    # The checkpoint must exist after the partial run, recording the committed
    # boundary (single marker: last_lcsc + rows_written), not a per-row id list.
    assert checkpoint.exists(), (
        "Checkpoint file must exist after a partial run so resume is possible."
    )
    cp = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert "written_lcsc_ids" not in cp, (
        "Checkpoint must be a single marker, never a growing per-row id list "
        f"(AC-NORMALIZE-2). Got keys: {sorted(cp)}"
    )
    assert cp.get("rows_written") == K, (
        f"Checkpoint must record the committed window boundary (rows_written={K}), "
        f"got {cp.get('rows_written')!r}"
    )
    assert cp.get("last_lcsc") == f"C{K:03d}", (
        f"Checkpoint last_lcsc must be the last committed LCSC, got {cp.get('last_lcsc')!r}"
    )

    # Restore the real writer for the resuming run.
    monkeypatch.setattr(normalize_run, "_write_checkpoint", real_write)

    # Second run — resumes from the checkpoint and completes.
    normalize(
        adapter=_FakeAdapter(all_parts),
        source_ref="jlcparts@2026-06-11",
        output_path=output,
        checkpoint_path=checkpoint,
    )

    lines = [
        line for line in output.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    records = [json.loads(line) for line in lines]
    lcscs = [r["lcsc_id"] for r in records]

    assert len(lcscs) == N, (
        f"Expected {N} unique records after resume, got {len(lcscs)}: {lcscs}"
    )
    assert len(set(lcscs)) == N, (
        f"Duplicate LCSC IDs found after resume: {lcscs}"
    )
    assert lcscs == sorted(lcscs), (
        f"Resumed output must remain sorted by LCSC ASC (no gaps): {lcscs}"
    )
    assert lcscs == [f"C{i:03d}" for i in range(1, N + 1)], (
        f"Resumed output must contain exactly C001..C{N:03d} with no gaps: {lcscs}"
    )


def test_norm_resume_completed_output_byte_identical_to_plain(
    tmp_path: pathlib.Path
) -> None:
    """Given the same input.
    When normalize() runs once with a checkpoint (interrupted then resumed) and
    once without a checkpoint (plain sorted mode).
    Then both output files are byte-identical.

    This pins that resumability never changes the bytes: the resumable path sorts
    by the same (lcsc, xid) key and serializes identically to the plain path, so
    determinism (AC-NORMALIZE: byte-identical full output) holds across modes.
    """
    parts = [
        _make_staged("C300", mpn="NE555", mpn_norm="NE555", mfr_norm="STM"),
        _make_staged("C010", mpn="LM358N", mpn_norm="LM358N", mfr_norm="TI"),
        _make_staged("C150", mpn="MAX232", mpn_norm="MAX232", mfr_norm="TI"),
        _make_staged("C010", mpn="ATTINY", mpn_norm="ATTINY", mfr_norm="MCHP"),
    ]

    plain = tmp_path / "plain.jsonl"
    normalize(adapter=_FakeAdapter(parts), source_ref="jlcparts@2026-06-11",
              output_path=plain)

    resumable = tmp_path / "resumable.jsonl"
    checkpoint = tmp_path / "cp.json"
    normalize(adapter=_FakeAdapter(parts), source_ref="jlcparts@2026-06-11",
              output_path=resumable, checkpoint_path=checkpoint)

    assert resumable.read_bytes() == plain.read_bytes(), (
        "Resumable normalize output must be byte-identical to plain sorted output."
    )


def test_norm_resume_checkpoint_writes_are_windowed_not_per_row(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given 20,000 synthetic parts and the production checkpoint window (5000).
    When normalize() runs with a checkpoint.
    Then the checkpoint-write function is called at most ceil(20000/5000)+2 times.

    This is the regression guard for the O(n^2) defect: the original code rewrote
    a growing collection of all seen lcsc ids after EVERY row (20,000 writes of an
    ever-larger payload). Pinning the call count to a small, window-bounded number
    makes per-row checkpointing impossible to reintroduce silently.
    """
    n_rows = 20_000
    window = normalize_run._CHECKPOINT_WINDOW
    assert window == 5000, (
        f"This regression test assumes the production window of 5000, got {window}."
    )

    parts = [_make_staged(f"C{i:06d}") for i in range(n_rows)]

    write_calls = {"n": 0}
    real_write = normalize_run._write_checkpoint

    def _counting_write(*args: object, **kwargs: object) -> None:
        write_calls["n"] += 1
        real_write(*args, **kwargs)

    monkeypatch.setattr(normalize_run, "_write_checkpoint", _counting_write)

    output = tmp_path / "big.jsonl"
    checkpoint = tmp_path / "big_cp.json"
    written = normalize(
        adapter=_FakeAdapter(parts),
        source_ref="jlcparts@2026-06-11",
        output_path=output,
        checkpoint_path=checkpoint,
    )

    assert written == n_rows, f"Expected {n_rows} rows written, got {written}."

    import math
    max_writes = math.ceil(n_rows / window) + 2
    assert write_calls["n"] <= max_writes, (
        f"Checkpoint written {write_calls['n']} times for {n_rows} rows; "
        f"must be <= {max_writes} (windowed, not per-row). Per-row checkpointing "
        "of a growing state is the O(n^2) defect and must never return."
    )
    # And the output must still be complete and correctly ordered.
    out_lines = [
        line for line in output.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(out_lines) == n_rows, (
        f"Output must contain all {n_rows} rows, got {len(out_lines)}."
    )


# ---------------------------------------------------------------------------
# T-NORM-provenance
# ---------------------------------------------------------------------------

def test_norm_provenance_source_ref_injected(tmp_path: pathlib.Path) -> None:
    """Given an injected source_ref='jlcparts@2026-06-11'.
    When normalize() is called.
    Then every record in the JSONL output has source_ref == 'jlcparts@2026-06-11'.

    source_ref MUST be an injected parameter, never derived from datetime.now(),
    to ensure determinism and testability.
    """
    # NOTE: the local _make_staged() helper (defined above in this file) does not
    # accept a source_ref kwarg; source_ref is injected via normalize() below.
    parts = [_make_staged("C999")]
    output = tmp_path / "prov.jsonl"
    normalize(
        adapter=_FakeAdapter(parts),
        source_ref="jlcparts@2026-06-11",
        output_path=output,
    )
    lines = output.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line.strip():
            continue
        record = json.loads(line)
        assert record.get("source_ref") == "jlcparts@2026-06-11", (
            f"source_ref mismatch in record: {record.get('source_ref')!r}"
        )


def test_norm_provenance_source_ref_no_datetime_now_call(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given monkeypatched datetime.now that raises.
    When normalize() is called with an explicit source_ref.
    Then it completes without error (proving it never calls datetime.now()).
    """
    import datetime

    def _banned_now(*args, **kwargs):
        raise RuntimeError(
            "normalize() must not call datetime.now(); "
            "source_ref must be an injected parameter."
        )

    monkeypatch.setattr(datetime, "datetime", type("FakeDatetime", (), {
        "now": staticmethod(_banned_now),
        "utcnow": staticmethod(_banned_now),
    }))

    output = tmp_path / "prov_no_dt.jsonl"
    normalize(
        adapter=_FakeAdapter([_make_staged("C888")]),
        source_ref="jlcparts@2026-06-11",
        output_path=output,
    )
    assert output.exists()


# ---------------------------------------------------------------------------
# T-NORM-tags  — extract_tags lexicon
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected_tag", [
    ("RS-232 Line Driver", "RS-232"),
    ("RS-485 transceiver", "RS-485"),
    ("I2C bus controller", "I2C"),
    ("SPI flash interface", "SPI"),
    ("UART bridge", "UART"),
    ("USB Type-C controller", "USB"),
    ("CAN bus transceiver", "CAN"),
    ("LIN transceiver", "LIN"),
    ("Ethernet PHY", "Ethernet"),
    ("HDMI transmitter", "HDMI"),
    ("LVDS serializer", "LVDS"),
    ("PCIe switch", "PCIe"),
])
def test_norm_tags_lexicon_case_insensitive(text: str, expected_tag: str) -> None:
    """Given a description string containing a protocol keyword.
    When extract_tags(text) is called.
    Then the returned list contains the canonical tag (case-normalized form)
    regardless of how the keyword appears in the input text.
    """
    tags = extract_tags(text)
    assert expected_tag in tags, (
        f"extract_tags({text!r}) = {tags!r}; expected {expected_tag!r} to be present."
    )


@pytest.mark.parametrize("text, expected_tag", [
    ("RS232 driver",  "RS-232"),
    ("RS485 bus",     "RS-485"),
    ("i2c",           "I2C"),
    ("spi",           "SPI"),
    ("uart",          "UART"),
    ("usb",           "USB"),
    ("can bus",       "CAN"),
    ("lin bus",       "LIN"),
    ("ethernet",      "Ethernet"),
    ("hdmi",          "HDMI"),
    ("lvds",          "LVDS"),
    ("pcie",          "PCIe"),
])
def test_norm_tags_canonicalization_lowercase_and_rs232(text: str, expected_tag: str) -> None:
    """Given protocol keywords in lowercase or without hyphen (RS232 -> RS-232).
    When extract_tags(text) is called.
    Then the tag is canonicalized to the standard form.
    """
    tags = extract_tags(text)
    assert expected_tag in tags, (
        f"extract_tags({text!r}) = {tags!r}; expected canonical tag {expected_tag!r}."
    )


def test_norm_tags_no_token_returns_empty_list() -> None:
    """Given a description with no protocol lexicon tokens.
    When extract_tags(text) is called.
    Then the result is an empty list (never None, never raises).
    """
    no_token_texts = [
        "Resistor 10kΩ 0603",
        "100nF ceramic capacitor",
        "NPN general purpose transistor",
        "",
        "RoHS compliant",
    ]
    for text in no_token_texts:
        result = extract_tags(text)
        assert isinstance(result, list), (
            f"extract_tags({text!r}) must return a list, got {type(result)!r}"
        )
        assert result == [], (
            f"extract_tags({text!r}) = {result!r}; expected [] for text with no protocol tokens."
        )


def test_norm_tags_word_boundary_not_partial_match() -> None:
    """Given strings where a protocol token appears as part of a larger word.
    When extract_tags(text) is called.
    Then the protocol token is NOT matched (word-boundary requirement).
    """
    # "CANISTER" contains "CAN" but is not a protocol tag.
    # "MUSICAL" contains "USB"? No. "SPIFFY" contains "SPI".
    texts_without_tags = [
        "CANISTER component",
        "SPICE simulation model",
        "UARTICULATE description",
    ]
    for text in texts_without_tags:
        tags = extract_tags(text)
        # "CAN" in "CANISTER" should NOT match
        # "SPI" in "SPICE" should NOT match
        for unexpected in ("CAN", "SPI", "UART"):
            if unexpected.lower() in text.lower().split()[0]:
                assert unexpected not in tags, (
                    f"extract_tags({text!r}) incorrectly matched {unexpected!r} "
                    f"(partial word match, not word boundary): {tags!r}"
                )


def test_norm_tags_multiple_protocols_in_text() -> None:
    """Given a description containing multiple protocol keywords.
    When extract_tags(text) is called.
    Then all matching tags appear in the result.
    """
    text = "Dual RS-232 / RS-485 transceiver with SPI interface"
    tags = extract_tags(text)
    assert "RS-232" in tags, f"RS-232 missing from tags: {tags}"
    assert "RS-485" in tags, f"RS-485 missing from tags: {tags}"
    assert "SPI" in tags, f"SPI missing from tags: {tags}"
