# ADR-0001: Defer the full multi-volume jlcparts archive

- Status: Accepted
- Date: 2026-06-11

## Context

PartGraph needs a bulk source of electronic-component data to bootstrap its
graph. Two open distributions of the JLCPCB/LCSC catalogue are available:

1. **CDFER single-file SQLite** — the `CDFER/jlcpcb-components` project
   publishes the catalogue as one ready-to-use SQLite database (~1 GB). It can
   be downloaded with a single HTTPS request and opened directly with the
   Python standard-library `sqlite3` module. No additional runtime dependency
   is required.

2. **yaqwsx multi-volume archive** — the `yaqwsx/jlcparts` project distributes
   the database as a multi-volume `7z` archive (~10 GB uncompressed). Consuming
   it requires a 7-Zip-capable extractor (an extra system/runtime dependency)
   and multi-step reassembly before the SQLite file is usable.

PR2 ("Bootstrap ingestion") delivers the first end-to-end fetch -> normalize ->
load pipeline. Its acceptance gates (row-count parity, MAX232 datasheet
presence, load metrics) are fully satisfiable with the CDFER single-file
source. Adding a 7z extraction dependency and the multi-volume reassembly logic
now would expand PR2's scope and supply-chain surface without improving the
gate outcomes.

## Decision

For PR2, ingestion uses the **CDFER single-file SQLite source only**.

The `partgraph ingest jlcparts --full` flag (intended for the yaqwsx
multi-volume archive) is shipped as an explicit stub: invoking it prints a
clear message stating the feature is not yet implemented, references this ADR,
and exits with a non-zero status. No new dependency (e.g. a 7z extractor) is
added to PR2.

Support for the full multi-volume archive is deferred to a dedicated future PR,
which will introduce the extraction dependency, the reassembly logic, and its
own tests and ADR for the added supply-chain risk.

## Consequences

- PR2 stays small, reviewable, and free of new third-party dependencies; the
  ingestion path depends only on `httpx`, `rich`, `pydgraph` and the standard
  library.
- Users get a working ~1 GB bootstrap immediately, with the download protected
  by HTTPS-only transport, a size cap, and SQLite magic-byte validation.
- `--full` fails fast and unambiguously rather than silently doing nothing,
  so the deferred capability is discoverable.
- When the multi-volume archive is implemented, the added 7z dependency and its
  risk will be evaluated in a separate ADR, keeping the supply-chain decision
  isolated and auditable.
