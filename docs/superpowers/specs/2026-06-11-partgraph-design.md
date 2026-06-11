# PartGraph — Design Document

- Date: 2026-06-11
- Status: Approved (basis for PR-by-PR execution)
- Owner: L0rdS474n

This is the canonical design specification for PartGraph. It is derived from the
approved project plan and is the source of truth for the architecture, schema,
ingestion pipeline, search CLI, deployment, and the PR roadmap.

## 1. Context and goals

The goal is a fast, local, searchable **graph database for electronic
components**. A user should be able to:

- Look up a part (for example `MAX232`) and get its manufacturers, variants,
  parameters and **datasheet PDF links**.
- Search by protocol (`RS-232`, `I2C`, `SPI`, `UART`, ...).
- Search by parametric expression (`1.2V MAX232`, `10k 0402 1%`).
- Search by free-text description, and later by semantic similarity.

The database is intended to become the foundation for future AI plugins (such
as circuit suggestions). **No plugins are built in this project.** PartGraph is
published as a public GitHub repository with a solo-admin setup that scales to
more contributors.

Confirmed user decisions:

- Open-dataset-first sourcing strategy.
- Datasheet **URLs only** — no PDF storage.
- Full-text **and** semantic vector search in v1.
- MIT license; GitHub account `L0rdS474n`.
- All project documentation in **English**.

## 2. Research facts that constrain the design

Researched on 2026-06-11.

### 2.1 Distributor API terms restrict local persistence

Several distributor APIs forbid building a local database from their data:
Digi-Key forbids "create your own database"; Nexar/Octopart allows at most a
24-hour cache and forbids self-hosting datasheets; Mouser and Farnell state
their data "will not [be] cache[d] ... or otherwise store[d]". **TME**
(developers.tme.eu) is the exception: free, 5 requests/second, storage allowed
with the attribution "powered by TME.eu Data" (delete-on-termination). TME's
exact field coverage is unverified and will be tested with a real key in PR5.
Distributor APIs are therefore **not** used in v1, except TME as an enrichment.

### 2.2 Open data sources (bootstrap)

- **`yaqwsx/jlcparts`** (MIT *code*): ~800k+ components. SQLite schema verified
  in `jlcparts/partLib.py`: a `components` table with `lcsc` (PK), `mfr` (the
  MPN), `manufacturer_id`, `category_id`, `package`, `description`, `datasheet`
  (URL, not null), `stock`, `price`, `basic`, `preferred`, and an `extra` JSON
  column with parametric attributes. Multi-volume `cache.zip` (~10 GB).
- **`CDFER/jlcpcb-parts-database`** (MIT *code*, daily build): a single-file
  SQLite (~1 GB, in-stock parts). This is the **quickstart source**.
- **KiCad symbol libraries** (CC-BY-SA 4.0): one `.kicad_sym` file per symbol
  with a `Datasheet` URL, `Description`, and `ki_keywords`. Verified example:
  MAX232 maps to the TI datasheet URL. A quality overlay, not a bulk source.
- **Wikidata** (CC0): only ~350 IC models — an identity layer for well-known
  circuits.
- **Data-license policy:** the jlcparts *data* is an unlicensed scrape of the
  JLCPCB/LCSC catalogue. It is used **locally only and never redistributed** in
  this repository. KiCad-derived data is CC-BY-SA and is attributed in the
  README.

### 2.3 Dgraph and the Dgraph MCP plugin

- The plugin's `dgraph_docker_start` runs `dgraph/standalone:*` **without a
  volume**, and `dgraph_docker_stop` removes the container, deleting data. The
  plugin's container is therefore **not** used; PartGraph ships its own Compose
  file with a named volume.
- Host ports `8080`/`9080` are reserved by another local stack on the
  development machine. PartGraph uses `8081`/`9081`/`8001`, all bound to
  `127.0.0.1`.
- The plugin endpoint is configurable (`dgraph_set_endpoint`), giving
  AI-assisted read access against the PartGraph endpoint.
- The plugin's `dgraph_mutate` has a deny-list (blocking characters such as
  `;` `|` `&&` in values — which component descriptions contain) and a 1 MiB
  cap. The pipeline therefore **writes via pydgraph/gRPC directly**; the plugin
  is used for reading and exploration only.
- Vector search requires Dgraph >= v24. The image is pinned to
  `dgraph/standalone:v25.3.4`; fallback `v24.1.x` if an image or licensing
  problem appears at setup (verified in PR1).
- Deduplication uses an upsert block on `xid: string @index(exact) @upsert`.

## 3. Architecture

```text
data/raw/ (git-ignored)        src/partgraph/                 Dgraph (Docker, own compose)
  CDFER .sqlite3  ─┐    fetch → sources/ adapters → normalize   127.0.0.1:8081 HTTP / health
  jlcparts cache  ─┼───────────→ JSONL (data/staged/) ─embed─→  127.0.0.1:9081 gRPC ← pydgraph loader
  kicad-symbols/  ─┤                                            volume: partgraph_dgraph_data
  wikidata.json   ─┘                                                 ↑
  TME API (key)                                       Dgraph MCP plugin (set_endpoint :8081) = AI read access
                              partgraph CLI (typer) ──── DQL/gRPC ───┘
```

- **Language/runtime:** Python 3.12. The repository ships `pyproject.toml` and
  `environment.yml` for external contributors.
- **Dependencies (v1 fundament):** `pydgraph`, `typer`, `rich`, `httpx`,
  `pyyaml`, `requests`, plus `pytest`/`ruff` for development. Later PRs add
  `sentence-transformers` and a KiCad S-expression parser as optional extras.
- **Staged and idempotent:** raw data is fetched once; normalisation,
  embedding, and load can be re-run without re-fetching. Work is resumable via
  checkpoint files.

## 4. Schema rationale (`schema/partgraph.dql`)

Node types: `Part`, `PartFamily`, `Manufacturer`, `Category` (hierarchical via
`parent`), `Package`, `Datasheet`, `Tag`, `AttrValue`. Edges from `Part`:
`made_by`, `variant_of`, `in_category`, `in_package`, `datasheet` (multiple, to
survive link rot), `tagged`, `attr`, and `equivalent_to` (prepared, unused in
v1).

Design choices:

- **`xid`** (`string @index(exact) @upsert`) is the deduplication key,
  `"mpn_norm|mfr_norm"`. It enables conflict-free upserts during ingestion.
- **`mpn` / `mpn_norm`** carry `exact` + `trigram` indexes for exact and fuzzy
  part-number lookup. `mpn_norm` is uppercase, `[A-Z0-9]` only.
- **`description`** uses a `fulltext` index for free-text search.
- **Promoted numeric parameters** (`voltage_min`, `resistance`, `capacitance`,
  ...) are typed `float` with a `float` index and **SI-normalised** at
  ingestion (`"5V"` → 5.0, `"100nF"` → 1e-7, `"10kΩ"` → 10000.0). The long tail
  of attributes is modelled as `AttrValue` nodes (`attr_name`, `attr_value`,
  `attr_value_num`).
- **`embedding`** is a `float32vector` with an `hnsw` index using the `cosine`
  metric — the contract for semantic similarity (`similar_to`).
- Edges are `[uid] @reverse` (multi-valued, reverse-traversable) except
  `variant_of` and the Category `parent`, which are singular `uid @reverse`.
- **`source_refs`** (`[string]`) records provenance markers
  (`"jlcparts@2026-06-11"`).

Tag extraction matches a protocol lexicon (RS-232/RS-485/I2C/SPI/UART/USB/CAN/
LIN/Ethernet/HDMI/LVDS/PCIe ...) against description, category and KiCad
keywords. PartFamily extraction is a best-effort heuristic (alphanumeric prefix
before a suffix series, for example `MAX232ACPE+` → `MAX232`), improved
iteratively.

## 5. Ingestion pipeline (`src/partgraph/`)

1. **fetch** (`partgraph ingest --fetch <source>`): download the CDFER SQLite
   (default) or the full jlcparts archive (`--full`, requires 7z); clone/pull
   kicad-symbols; fetch the Wikidata SPARQL result; query TME if credentials
   are set.
2. **normalize**: one adapter per source (`sources/jlcparts.py`,
   `sources/kicad.py`, `sources/wikidata.py`, `sources/tme.py`) into a common
   `StagedPart` JSONL format. A standalone unit parser is tested against real
   values from the source data. Merge priority at the same `xid`: description
   kicad > tme > jlcparts; datasheets and tags are a source-tagged union.
3. **embed**: sentence-transformers (candidate `all-MiniLM-L6-v2`, 384-dim;
   final choice validated with real similarity tests in PR4). Embedding text is
   description + category + package + tags; batched and cached in the staged
   files.
4. **load**: pydgraph against `127.0.0.1:9081`; `xid`-keyed upsert blocks,
   batched; the schema is applied first. One code path serves both the initial
   and incremental load. Plan B if the measured initial load is too slow:
   `dgraph live` via Docker — decided on a measurement, not a guess.
5. **verify**: node counts, random sampling against the source data, and the
   acceptance tests.

## 6. Search CLI

- `partgraph search "1.2V MAX232"`: the query parser extracts quantities
  (regex value+unit → SI + field) and a text part (trigram on
  `mpn`/`mpn_norm`/`family_name` plus full-text on `description`). With zero
  hits under a hard filter, the filter is relaxed and the **nearest match is
  shown, explicitly labelled**, sorted by parameter distance. Output is a rich
  table (MPN, manufacturer, package, key parameters, stock, datasheet URL).
- `partgraph search --semantic "<free description>"`: embed the query →
  `similar_to(embedding, k)` → hybrid with optional parametric filters.
- `partgraph show <MPN>`: full detail plus family variants and all datasheets.
- `partgraph stats`, `partgraph db up|down|status`, `partgraph ingest <source>`.
- Ranking: exact MPN > trigram MPN > family > full-text > semantic; boosted for
  `stock > 0` and `is_basic`.

## 7. Deployment

- **Docker Compose** (`docker/docker-compose.yml`): image
  `dgraph/standalone:v25.3.4`; ports `127.0.0.1:8081:8080`,
  `127.0.0.1:9081:9080`, `127.0.0.1:8001:8000`; named volume
  `partgraph_dgraph_data` mounted at `/dgraph`; explicit
  `container_name: partgraph-dgraph`.
- **Ports:** every port binds to `127.0.0.1` only. Dgraph standalone has no
  authentication, so the loopback binding is the access control. The ports must
  not be exposed publicly.
- **Volume:** the named volume preserves all data across `db down` (which never
  passes `-v`) and restarts.
- **Why not the plugin's Docker tools:** the plugin starts Dgraph without a
  volume and deletes data on stop. PartGraph ships its own Compose file so data
  survives, and connects the plugin (read-only) via
  `dgraph_set_endpoint http://localhost:8081`.

## 8. Repository and GitHub setup

Structure: `src/partgraph/` (`cli.py`, `schema.py`, and later `query/`,
`sources/`, `normalize/`, `embed.py`, `load.py`), `schema/partgraph.dql`,
`docker/docker-compose.yml`, `tests/` (unit + integration marked
`@pytest.mark.integration`), `docs/`, plus `README.md`, `LICENSE` (MIT),
`CONTRIBUTING.md`, `.github/workflows/ci.yml` (ruff + unit pytest),
`.github/ISSUE_TEMPLATE/`, `PULL_REQUEST_TEMPLATE.md`, `CODEOWNERS`
(`* @L0rdS474n`), and `.gitignore`.

Branch protection on `main`: require a pull request, required status check
`CI`, **required approvals 0** while solo (the admin merges their own PRs),
linear history. Scaling to more contributors raises approvals to 1 (CODEOWNERS
is already in place).

## 9. PR roadmap and gate criteria

1. **PR1 — Fundament:** repo skeleton, CI, Docker Compose, DQL schema,
   `partgraph db up/down/status`, schema application, this design doc.
   *Gate:* container up on 8081/9081 with a volume; vector predicate accepted by
   v25.3.4 (`similar_to` smoke test); restart without data loss; the plugin
   endpoint reconnects and answers DQL.
2. **PR2 — Bootstrap ingestion:** CDFER/jlcparts adapter, unit parser,
   normalisation, pydgraph loader with `xid` upserts. *Gate:* loaded Part count
   equals the source row count (order 10^5–10^6); `MAX232` DQL returns variants
   with datasheet URLs; load time measured.
3. **PR3 — Search CLI:** query parser, structured + full-text search,
   `show`/`stats`, ranking, nearest-match. *Gate:* the real-world searches in
   the verification list are green.
4. **PR4 — Semantics:** embeddings for the whole catalogue, HNSW load,
   `--semantic`, hybrid search. *Gate:* semantic verification green; embedding
   time measured.
5. **PR5 — Enrichment:** KiCad overlay, Wikidata links, TME adapter (field
   coverage verified with a real key; if TME lacks datasheet fields it is
   documented honestly and de-prioritised). *Gate:* the MAX232 node has a KiCad
   description, a TI datasheet URL, and tags from `ki_keywords`.
6. **PR6 — Acceptance & release:** full acceptance suite, datasheet-URL health
   check (HEAD sampling), README polish, tag `v0.1.0`. *Gate:* the whole
   verification list is green, shown with real output.

## 10. Risks and open points

- **jlcparts data license** is unclear (a scrape) → local use only;
  redistribution forbidden by the README policy. (Accepted risk.)
- **Catalogue size:** v1 reaches ~0.5–1 M components (the JLC catalogue). The
  path forward is the adapter pattern + TME + future open sources, stated in the
  README roadmap.
- **TME field coverage** is unverified → verified with a real key in PR5; a free
  account at developers.tme.eu is required when PR5 starts.
- **`dgraph/standalone:v25.3.4`** image licensing/behaviour verified at PR1
  setup; fallback `v24.1.x`.
- **Embedding time** for ~800k texts on CPU measured in PR4; the model choice
  may be adjusted on measured similarity results.
- **Disk needs:** ~10 GB raw (full jlcparts) + a few GB for Dgraph + ~1 GB for
  models — documented in the README.
