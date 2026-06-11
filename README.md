# PartGraph

PartGraph is a fast, local, searchable **graph database for electronic
components**. Look up a part such as `MAX232` and get its manufacturers,
variants, parameters and **datasheet PDF links**; search by protocol
(`RS-232`, `I2C`), by parametric expression (`10k 0402 1%`) or by free-text
description. A semantic (vector) search layer is planned so you can find parts
by what they *do*, not just by their part number.

PartGraph stores **links to datasheets, never the PDFs themselves**, and is
built on a local [Dgraph](https://dgraph.io/) instance running in Docker.

> Status: early alpha. PR1 delivers the fundament (repository skeleton, CI,
> Docker Compose, the DQL schema, and the `partgraph db` lifecycle commands).
> Ingestion and search arrive in later PRs (see the roadmap below).

## Data policy

**Component data is not redistributed in this repository.** This repo contains
only code, the schema, and documentation; the database must be **built locally**
by each user from open sources. The jlcparts-derived data (an unlicensed scrape
of the JLCPCB/LCSC catalogue) is used **only locally and is never
redistributed**. KiCad-derived data is CC-BY-SA 4.0 and is attributed below.

## Attribution

- **jlcparts** — bootstrap component data. MIT-licensed *code* by
  [yaqwsx](https://github.com/yaqwsx/jlcparts). The underlying catalogue data
  is used locally only (see the data policy above).
- **KiCad symbol libraries** — descriptions, keywords and canonical datasheet
  URLs. Licensed **CC-BY-SA 4.0**
  ([kicad-symbols](https://gitlab.com/kicad/libraries/kicad-symbols)).
- **TME.eu** — when the optional TME enrichment is enabled, the resulting data
  is **"powered by TME.eu Data"** and is subject to TME's developer terms.

## Quickstart

Requirements: Python 3.12, Docker with the Compose plugin, and (for the full
ingestion in later PRs) ~10 GB of free disk for raw data plus a few GB for the
Dgraph volume and embedding models.

```bash
# 1. Create and activate a Python 3.12 environment (conda shown; venv works too)
conda env create -f environment.yml
conda activate partgraph

# 2. Install PartGraph (editable) with development extras
pip install -e ".[dev]"

# 3. Start the local Dgraph database
partgraph db up

# 4. Apply the DQL schema
partgraph db apply-schema

# 5. Run the tests (unit only; integration needs the database running)
pytest -m "not integration"        # unit tests
pytest -m integration              # integration tests (requires `db up`)

# Stop the database when finished (data is preserved)
partgraph db down
```

## Ports

PartGraph binds every port to `127.0.0.1` and offsets the host ports by +1
because `8080`/`9080` are commonly reserved by other local stacks.

| Host (127.0.0.1) | Container | Purpose                 |
| ---------------- | --------- | ----------------------- |
| 8081             | 8080      | Alpha HTTP / health     |
| 9081             | 9080      | Alpha gRPC (pydgraph)   |
| 8001             | 8000      | Ratel / admin UI        |

> The standard Dgraph ports `8080`/`9080` are intentionally **not** used, as
> they are reserved by other local stacks.

## Security

- Dgraph **standalone has no authentication**. Access control is provided
  entirely by binding every port to `127.0.0.1` only.
- **Any local process can read and write the graph.** For a single-developer
  local tool this is an **accepted risk**.
- **Do NOT expose these ports** to other interfaces or to the network. Never
  change the bindings to `0.0.0.0`.

## Dgraph MCP plugin integration

PartGraph is designed to be queried by the Dgraph MCP plugin (for AI-assisted
exploration). Point the plugin at this instance:

```text
dgraph_set_endpoint http://localhost:8081
```

The plugin is used as a **read-only consumer** (queries/exploration). All
**writes go through pydgraph over gRPC** (`127.0.0.1:9081`); the plugin's own
Docker lifecycle tools are **not** used because they run Dgraph without a
volume and delete data on stop.

## Disk requirements

- ~10 GB for raw bootstrap data (full jlcparts archive; the default CDFER
  single-file source is ~1 GB).
- A few GB for the Dgraph named volume as the graph grows.
- ~1 GB for the sentence-embedding model cache (PR4 onward).

## Roadmap

- **PR1 — Fundament** (this release): repo skeleton, CI, Docker Compose, DQL
  schema, `partgraph db up/down/status`, schema application, design doc.
- **PR2 — Bootstrap ingestion**: CDFER/jlcparts adapter, unit parser,
  normalisation, pydgraph loader with `xid` upserts.
- **PR3 — Search CLI**: query parser, structured + full-text search,
  `show`/`stats`, ranking, nearest-match.
- **PR4 — Semantics**: embeddings for the whole catalogue, HNSW load,
  `--semantic` and hybrid search.
- **PR5 — Enrichment**: KiCad overlay, Wikidata identity links, TME adapter.
- **PR6 — Acceptance & release**: full acceptance suite, datasheet-URL health
  sampling, README polish, tag `v0.1.0`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, the test-first
policy, and pull-request rules.

## License

PartGraph is released under the **MIT License**. See [LICENSE](LICENSE).
