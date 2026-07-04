# Connecting to PartGraph

This guide covers **every supported way to query your local PartGraph graph**:

1. [AI access via the Dgraph MCP plugin](#1-ai-access-via-the-dgraph-mcp-plugin)
2. [Plain Dgraph clients](#2-plain-dgraph-clients) (gRPC / HTTP / Ratel)
3. [AI/LLM query recipes (DQL)](#3-aillm-query-recipes-dql)
4. [The bundled `partgraph` CLI](#4-the-bundled-partgraph-cli)

Every example below was run against a live local instance; see
[How these examples were verified](#how-these-examples-were-verified) for the
exact conditions and which parts were *not* executed.

---

## Before you connect

### The data is built locally

This repository ships **only code, the schema, and docs** — no component data
(see the *Data policy* in the [README](../README.md)). Until you build the
graph, every query below returns an empty result. Build it once, in order (these
commands are described here for context — this guide does not run them):

```bash
partgraph db up             # start the local Dgraph container (Docker/Podman)
partgraph db apply-schema   # apply schema/partgraph.dql over gRPC
partgraph ingest jlcparts   # load the component catalogue into the graph
partgraph embed             # compute semantic vectors (enables --semantic)
```

`db up` + `apply-schema` + `ingest jlcparts` give you a graph you can query;
`embed` additionally enables semantic/vector search. Stop the database with
`partgraph db down` (data is preserved in a named volume).

### Ports and the 8080 / 9080 collision

> [!WARNING]
> **Never point a client at `8080` or `9080`.** PartGraph deliberately offsets
> every host port by **+1** and binds to `127.0.0.1` only. The standard Dgraph
> ports `8080` / `9080` are reserved on this machine by **another local stack (a
> cve-graph stack)** — connecting there will hit the wrong database. PartGraph
> lives on **`8081` / `9081` / `8001`**.

| Host (`127.0.0.1`) | Container | Protocol / purpose        | Point these clients here            |
| ------------------ | --------- | ------------------------- | ----------------------------------- |
| `8081`             | `8080`    | HTTP + `/health` (DQL over HTTP) | curl, the MCP plugin, any HTTP client |
| `9081`             | `9080`    | gRPC                      | pydgraph, dgo, dgraph4j             |
| `8001`             | `8000`    | Ratel / admin UI          | a web browser                       |

All three are bound to loopback only. This is the **entire** access-control
model — see [Safety and notes](#safety-and-notes).

Quick liveness check (HTTP, `8081`):

```bash
curl -sS http://127.0.0.1:8081/health
```

```json
[{"instance":"alpha","address":"localhost:7080","status":"healthy","group":"1","version":"v25.3.4","uptime":...,"max_assigned":...}]
```

(`uptime` and `max_assigned` are point-in-time and elided above.)

---

## 1. AI access via the Dgraph MCP plugin

> **Prerequisite:** the database must be up and populated — see
> [Before you connect](#before-you-connect).

The Dgraph MCP plugin lets an AI/LLM client explore the graph as a **read-only
consumer**.

**Zero to first query:**

1. Point the plugin at the local Alpha:

   ```text
   dgraph_set_endpoint http://localhost:8081
   ```

2. Run a DQL string through `dgraph_query_dql`:

   ```dql
   { q(func: eq(mpn_norm, "MAX232ECDR")) @filter(has(datasheet)) {
       mpn
       made_by { name }
       datasheet { url }
   } }
   ```

   ```json
   {"data":{"q":[{"mpn":"MAX232ECDR","made_by":[{"name":"Texas Instruments"}],"datasheet":[{"url":"https://www.lcsc.com/datasheet/lcsc_datasheet_1810010413_Texas-Instruments-MAX232ECDR_C138709.pdf"}]}]}}
   ```

Anything you can express in DQL you can send through `dgraph_query_dql`,
including the [expand-node](#recipe-4--expand-a-node) and
[shortest-path](#recipe-5--shortest-path-part--manufacturer) recipes below. To
expand or path-find, first look up a node's `uid` with an `eq(mpn_norm, ...)`
query (uids are instance-specific — see the recipes).

> **Writes are not done through the plugin.** All writes go through **pydgraph
> over gRPC (`127.0.0.1:9081`)** only. This is a **PartGraph policy choice**, not
> an MCP limitation: the plugin's bundled Docker-lifecycle tools run Dgraph
> without a persistent volume and would delete your data, so PartGraph keeps the
> plugin strictly read-only and owns the container lifecycle itself (via
> `partgraph db …`).

---

## 2. Plain Dgraph clients

> **Prerequisite:** the database must be up and populated — see
> [Before you connect](#before-you-connect).

Connection coordinates:

| Client                | Address                        | Notes                                   |
| --------------------- | ------------------------------ | --------------------------------------- |
| gRPC (pydgraph / dgo / dgraph4j) | `127.0.0.1:9081`    | binary gRPC; used for reads **and** writes |
| HTTP `/query` (DQL)   | `http://127.0.0.1:8081/query`  | `POST`, `Content-Type: application/dql` |
| Ratel UI              | `http://127.0.0.1:8001`        | admin/query UI (see caveat below)       |

### 2.1 gRPC — pydgraph (Python, runnable)

`pydgraph` is installed with PartGraph (`pip install -e .`). This mirrors the
CLI's own client (`cli._build_dgraph_client`) and runs a **read-only**
transaction:

```python
import pydgraph

stub = pydgraph.DgraphClientStub("127.0.0.1:9081")
client = pydgraph.DgraphClient(stub)
txn = client.txn(read_only=True)
try:
    res = txn.query(
        '{ q(func: eq(mpn_norm,"MAX232ECDR")) @filter(has(datasheet)) '
        '{ mpn made_by{name} datasheet{url} } }'
    )
    print(res.json.decode())
finally:
    txn.discard()
    stub.close()
```

```json
{"q": [{"mpn": "MAX232ECDR", "made_by": [{"name": "Texas Instruments"}], "datasheet": [{"url": "https://www.lcsc.com/datasheet/lcsc_datasheet_1810010413_Texas-Instruments-MAX232ECDR_C138709.pdf"}]}]}
```

**For untrusted input, bind — do not string-format.** Declare a typed
`$`-variable and pass the value through the `variables` map, exactly as
PartGraph's own query layer does (`src/partgraph/query/dql_builder.py` emits a
`query search($te: string, …)` header and runs it via
`txn.query(query_text, variables=variables)`):

```python
query = (
    "query lookup($mpn: string) {"
    "  q(func: eq(mpn_norm, $mpn)) @filter(has(datasheet)) {"
    "    mpn made_by { name } datasheet { url }"
    "  }"
    "}"
)
res = txn.query(query, variables={"$mpn": user_supplied_mpn})
```

Any wrapper that accepts external input must bind it as a `$`-variable, never
interpolate it into the DQL string.

For large reads (e.g. dumping `embedding` vectors), raise pydgraph's default
4 MiB per-message ceiling — the same pattern the CLI uses:

```python
stub = pydgraph.DgraphClientStub(
    "127.0.0.1:9081",
    options=[("grpc.max_receive_message_length", 256 * 1024 * 1024)],
)
```

### 2.2 HTTP `/query` (DQL) — curl or any HTTP client

**Zero to first query** — POST a DQL body to `/query`:

```bash
curl -sS http://127.0.0.1:8081/query \
  -H 'Content-Type: application/dql' \
  --data-binary '{ q(func: eq(mpn_norm,"MAX232ECDR")) @filter(has(datasheet)) { mpn made_by{name} datasheet{url} } }'
```

```json
{"data":{"q":[{"mpn":"MAX232ECDR","made_by":[{"name":"Texas Instruments"}],"datasheet":[{"url":"https://www.lcsc.com/datasheet/lcsc_datasheet_1810010413_Texas-Instruments-MAX232ECDR_C138709.pdf"}]}]}}
```

The `Content-Type: application/dql` header is required so Dgraph parses the body
as DQL (not GraphQL). All recipes in
[section 3](#3-aillm-query-recipes-dql) work verbatim over this endpoint.

### 2.3 dgo (Go) and dgraph4j (Java) — coordinates + skeleton

> These two skeletons were **not executed** in this verification session; they
> are provided as connection coordinates only. Both connect to the same gRPC
> endpoint `127.0.0.1:9081`.

**dgo (Go):**

```go
// go get github.com/dgraph-io/dgo/v240 google.golang.org/grpc
conn, _ := grpc.NewClient("127.0.0.1:9081", grpc.WithTransportCredentials(insecure.NewCredentials()))
defer conn.Close()
dg := dgo.NewDgraphClient(api.NewDgraphClient(conn))
txn := dg.NewReadOnlyTxn()
resp, _ := txn.Query(ctx, `{ q(func: eq(mpn_norm,"MAX232ECDR")) { mpn made_by{name} } }`)
fmt.Println(string(resp.Json))
```

**dgraph4j (Java):**

```java
// implementation "io.dgraph:dgraph4j:<version>"
ManagedChannel channel = ManagedChannelBuilder.forAddress("127.0.0.1", 9081).usePlaintext().build();
DgraphClient dgraph = new DgraphClient(DgraphGrpc.newStub(channel));
Transaction txn = dgraph.newReadOnlyTransaction();
Response resp = txn.query("{ q(func: eq(mpn_norm,\"MAX232ECDR\")) { mpn made_by{name} } }");
System.out.println(resp.getJson().toStringUtf8());
```

### 2.4 Ratel UI

Ratel (Dgraph's web query UI) is mapped to `http://127.0.0.1:8001` (the
`127.0.0.1:8001:8000` mapping in `docker/docker-compose.yml`).

> **Not confirmed serving in this session.** The port accepts a TCP connection
> but the HTTP probe was reset (`Connection reset by peer`). Treat the mapping as
> *configured but unverified* — if it does not load in your browser, use the
> HTTP `/query` endpoint (section 2.2) instead.

---

## 3. AI/LLM query recipes (DQL)

> **Prerequisite:** the database must be up and populated — see
> [Before you connect](#before-you-connect).

The graph contract is the DQL schema: **[`schema/partgraph.dql`](../schema/partgraph.dql)**.
Key predicates for querying:

- Identity: `mpn` and `mpn_norm` (both `exact` + `trigram` indexed),
  `xid` (`mpn_norm|mfr_norm`), `lcsc_id`.
- Edges (all reverse-traversable): `made_by`, `in_category`, `in_package`,
  `datasheet`, `tagged`, `attr`.
- Promoted numeric parameters (SI-normalised floats): `resistance`,
  `capacitance`, `voltage_min/max`, `tolerance_pct`, `stock`, …
- Commercial: `price_usd` — unit price in USD (a float; **may be absent** on a
  part) — and `is_basic` — a bool for the JLCPCB basic/extended tier.
- Semantic: `embedding` — a 384-dim vector with an HNSW cosine index.

> `variant_of` and `equivalent_to` exist in the schema but are **not exercised**
> by these recipes (the schema marks `equivalent_to` as "prepared, unused in
> v1"). Do not rely on variant/family traversal as a working query path.

All five recipes below were run over the HTTP `/query` endpoint and return the
data shown.

### Recipe 1 — exact MPN lookup (manufacturer + datasheet)

```dql
{ q(func: eq(mpn_norm, "MAX232ECDR")) @filter(has(datasheet)) {
    mpn
    made_by { name }
    datasheet { url }
} }
```

```json
{"data":{"q":[{"mpn":"MAX232ECDR","made_by":[{"name":"Texas Instruments"}],"datasheet":[{"url":"https://www.lcsc.com/datasheet/lcsc_datasheet_1810010413_Texas-Instruments-MAX232ECDR_C138709.pdf"}]}]}}
```

`mpn_norm` is the normalised key: the CLI uppercases input and strips it to
`[A-Z0-9]`, so `"max232ecdr"` and `"MAX232ECDR"` resolve to the same node.

### Recipe 2 — family / trigram MPN search

```dql
{ q(func: regexp(mpn_norm, /MAX232/), first: 10) @filter(has(datasheet)) {
    mpn
    mpn_norm
} }
```

Returns 10 rows, including:

```json
{"mpn":"MAX232EESE+T","mpn_norm":"MAX232EESET"}
{"mpn":"MAX232EDTR(XBLW)","mpn_norm":"MAX232EDTRXBLW"}
```

### Recipe 3 — parametric search (10k / 0402 / 1%)

Find 0402 resistors near 10 kΩ at 1 % tolerance that have a datasheet:

```dql
{ q(func: type(Part), first: 10) @filter(
      ge(resistance, 9900.0) AND le(resistance, 10100.0)
      AND eq(tolerance_pct, 1.0) AND has(datasheet)
  ) @cascade(in_package) {
    uid
    mpn
    resistance
    tolerance_pct
    in_package @filter(eq(name, "0402")) { name }
    datasheet { url }
} }
```

Returns 10 rows, all with `resistance: 10000`, `tolerance_pct: 1`, and
`in_package: [{"name":"0402"}]` — for example `CR0402FF1002G`, `RTT021002FTH`,
and `AC0402FR-0710KL`.

### Recipe 4 — expand a node

First resolve a `uid` (uids are **instance-specific and not stable across
rebuilds** — never hard-code them):

```dql
{ q(func: eq(mpn_norm, "MAX232ECDR")) { uid } }
```

Then expand it two hops (replace `0x…` with the uid you just got):

```dql
{ q(func: uid(0x…)) { expand(_all_) { expand(_all_) } } }
```

For a real `Part` node this dumps its scalars and edges — `stock`,
`xid` (`"MAX232ECDR|TEXASINSTRUMENTS"`), `lcsc_id` (`"C138709"`), `mpn_norm`,
`description`, the 384-float `embedding`
(`[0.02659, 0.03792, -0.01975, ...]`), plus `made_by`, `in_package`,
`in_category`, `datasheet`, `attr`, and `tagged` (`[{"name":"RS-232"}]`).

### Recipe 5 — shortest path (Part → Manufacturer)

Resolve both endpoints' uids first (as in Recipe 4), then path-find over the
populated `made_by` edge:

```dql
{
  pathvar as shortest(from: 0xPART, to: 0xMANUFACTURER) { made_by }
  pathresult(func: uid(pathvar)) { uid mpn name }
}
```

Returns the `Part → Manufacturer` path. Use placeholder uids and resolve them
for your instance first.

### Semantic (vector) search

The **verified** semantic path is the CLI, which embeds your query text with the
same all-MiniLM model used at ingest and searches the `embedding` HNSW index:

```bash
partgraph search --semantic "rs232 transceiver"
```

Dgraph can also run vector-similarity DQL directly against the `embedding`
predicate (e.g. a `similar_to(...)`-style function). That **direct-DQL form was
not exercised in this session** — confirm the exact function signature against
the Dgraph v25.3.4 docs before relying on it, or use the CLI above, which is
tested (see [section 4](#4-the-bundled-partgraph-cli)).

---

## 4. The bundled `partgraph` CLI

> **Prerequisite:** the database must be up and populated — see
> [Before you connect](#before-you-connect). (`--semantic` additionally needs
> `partgraph embed` to have run.)

The CLI is the simplest entry point — no client code, no DQL. It opens its own
read-only gRPC transaction to `127.0.0.1:9081`.

### `partgraph search "<query>"`

Free-text / MPN search (exit 0, a Rich table of up to 20 rows):

```text
$ partgraph search "MAX232"
MAX232DR    | Texas Instruments | SOIC-16 | 9690 | https://www.lcsc.com/datasheet/lcsc_datasheet_2410010330_Texas-Instruments-MAX232DR_C158068.pdf
MAX232ECDR  | Texas Instruments | SOIC-16 |  998 | https://www.lcsc.com/datasheet/lcsc_datasheet_1810010413_Texas-Instruments-MAX232ECDR_C138709.pdf
...
Showing 20 result(s).
```

Parametric expressions work too (each row tagged `Exact`):

```text
$ partgraph search "10k 0402 1%"
0402WGF1002TCE   | UNI-ROYAL  | 0402 | 4191623
CRCW040210K0FKTD | Vishay     | 0402 | ...
ERJ2RKF1002X     | PANASONIC  | 0402 |  370007
...   (20 rows)
```

#### Sorting and machine-readable output

`partgraph search` accepts two output flags (both read-only, both compose with
the filters above):

- `--sort relevance|stock|price` orders the results. `relevance` (the default)
  is best-match-first (match tier, then in-stock and basic parts); `stock` puts
  the **most in stock first**; `price` puts the **cheapest first** (parts with no
  `price_usd` sort last). In nearest-match mode `--sort` is ignored — parameter
  distance always wins.
- `--json` prints a single machine-readable JSON object instead of the human
  table: `{"version": 1, "query": …, "nearest_match": …, "count": N,
  "results": [ … ]}`. Each result row has the stable keys `mpn`, `mpn_norm`,
  `manufacturer`, `package`, `category`, `stock`, `is_basic`, `price_usd`,
  `match_type`, `datasheets`, `params`. The envelope carries **no internal
  `uid`s**; `version` is `1` and only ever bumps on a breaking key change
  (additive keys do not bump it). On any error the command exits non-zero and
  prints no JSON.

```bash
partgraph search "MAX232" --json --sort price --limit 50
```

### `partgraph search --semantic "<query>"`

Vector search by meaning (requires `partgraph embed` to have run):

```text
$ partgraph search --semantic "rs232 transceiver"
Semantic matches (by embedding similarity):
[Semantic] ADM3222ARSZ-REEL7
[Semantic] ICL3225EIAZ
[Semantic] MAX3223ECUP+
[Semantic] MAX3232IPWR(UMW)
[Semantic] ST3232BTR
...   (19 rows)
```

(On first run a one-time "Loading weights" progress line may appear while the
embedding model loads — this is benign.)

### `partgraph show "<mpn>"`

Full detail for one part:

```text
$ partgraph show "MAX232ECDR"
Part: MAX232ECDR
Manufacturer: Texas Instruments
Package: SOIC-16
Category: RS232 ICs
Stock: 998
All attributes:
  Operating Temperature (max): 0℃~+70℃ | Type: Transceiver |
  Supply Voltage (max): 4.5V~5.5V | Data Rate: 250Kbps | Driver/receiver: 2/2
Datasheets:
  https://www.lcsc.com/datasheet/lcsc_datasheet_1810010413_Texas-Instruments-MAX232ECDR_C138709.pdf (jlcparts@2026-06-11)
Related parts (by MPN):
  MAX15162AATG+, MAX20088ATPA/VY+, MAX14922ATE+, MAX20084ATEA/VY+, MAX77960EFV06+, ...
```

> **"Related parts" is MPN-prefix similarity, not functional relatedness.** The
> related rows above are unrelated power-management Maxim parts that merely share
> the `MAX` prefix. This is *not* `variant_of` / `family_name` traversal.

### `partgraph stats`

Graph-wide counts. **Approximate / illustrative** — these are point-in-time and
were captured while an `embed` job was running concurrently, so treat them as
orders of magnitude, not exact totals:

| Node type    | Count (approx.) |
| ------------ | --------------- |
| Part         | ~600,000        |
| Datasheet    | ~475,000        |
| AttrValue    | ~102,000        |
| Package      | ~15,000         |
| Manufacturer | ~2,500          |
| Category     | ~960            |
| Tag          | ~12             |

---

## Safety and notes

- **No authentication.** Dgraph standalone ships without auth; access control is
  **loopback binding only** (`127.0.0.1`). Any local process can read and write
  the graph — an accepted risk for a single-developer local tool. **Never** bind
  these ports to `0.0.0.0` or expose them to the network (see the README's
  *Security* section).
- **Injection-safe by construction.** Hostile CLI input is normalised to an
  alphanumeric `mpn_norm` before it reaches a query, and `show` echoes the raw
  argument only inside a path-free "not found" message.
- **Writes are gRPC-only** (pydgraph, `9081`); the MCP plugin is read-only by
  policy.

---

## Common issues

- **Empty search query** — the CLI exits **1** and prints
  `Error: search query cannot be empty.` Pass a non-empty query, e.g.
  `partgraph search "MAX232"`.

- **MPN not in the graph** — this is **not** an error: the CLI exits **0** and
  prints `Part '<MPN>' not found.` Re-check the MPN, or confirm
  `partgraph ingest jlcparts` has run.

- **Database not running** — raw clients get a generic transport error (curl:
  `Connection refused`; pydgraph / gRPC: `StatusCode.UNAVAILABLE` or a
  connection error — standard behaviour when `8081` / `9081` are not served).
  The bundled CLI catches this and prints: "Error: could not query Dgraph. Is
  the database running? Start it with `partgraph db up`." Start the database —
  see [Before you connect](#before-you-connect).

- **`--semantic` returns nothing** — semantic search is verified working *once
  embeddings exist*. Before you have run `partgraph embed`, the CLI prints: "No
  semantic matches. The embedding index may be empty — run `partgraph embed`
  first to generate embeddings." Run `partgraph embed` once and retry.

---

## How these examples were verified

- **Environment:** a live local PartGraph instance reached over **loopback**
  (`127.0.0.1`), using **read-only** transactions and **structural** checks
  (shape of the JSON / table, presence of the expected fields and edges).
- **Date:** 2026-07-03.
- **Point-in-time values:** `stats` counts, `stock` figures, and `/health`
  `uptime` / `max_assigned` are snapshots and will drift. Node `uid`s are
  **instance-specific and not stable across rebuilds** — always resolve them via
  an `eq(mpn_norm, ...)` lookup rather than hard-coding.
- **Not executed in this session** (marked inline where they appear): the dgo /
  dgraph4j skeletons, the Ratel UI serving HTTP, and direct vector-similarity
  DQL against `embedding`. The build commands (`db up`, `apply-schema`,
  `ingest`, `embed`) are documented for context and were **not** run as part of
  writing this guide.
