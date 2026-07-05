# ADR-0018: `partgraph db status` is an HTTP `/health` probe, not `compose ps`

- Status: Accepted
- Date: 2026-07-05

## Context

`partgraph db status` delegated to `<engine> compose ps` (via `_run_compose`,
routed through `compose_command()` from ADR-0009). Compose `ps` only lists
containers that **Compose itself labelled** (the `com.docker.compose.project`
labels it writes on `compose up`). That has two consequences that make it the
wrong signal for "is the database running and healthy?":

1. **False "down".** A Dgraph started outside Compose — by a systemd timer, a
   cron wrapper (ADR-0014), or a bare `podman run` / `docker run` — carries no
   Compose project label, so `compose ps` prints an **empty table** and exits
   **0** even while Dgraph is fully up and serving. The user is told nothing is
   running when in fact it is.
2. **Container liveness ≠ database liveness.** Even when Compose *did* start the
   container, "the container exists" is not "the Alpha is accepting queries".
   A container can be `Up` while Dgraph is still replaying its WAL, wedged, or
   returning 503.

On top of that, `compose ps` **requires a container engine on PATH**: on a host
where the engine was uninstalled after `db up`, or a CI box that has none,
`db status` failed at engine detection instead of reporting the database it was
asked about.

Dgraph exposes its own liveness signal at `http://127.0.0.1:8081/health`
(host `8081` -> container `8080`, loopback only — the endpoint already
documented in `docs/connecting.md` and polled by the integration fixtures). A
`200` there is an authoritative, engine-independent statement that the Alpha is
serving requests, whoever started it.

## Decision

### 1. Probe Dgraph's own HTTP `/health` endpoint

`db status` calls a new leaf, `partgraph.util.health.probe_health()`, which
issues one HTTP GET against `DGRAPH_HTTP_HEALTH_URL`
(`http://127.0.0.1:8081/health`) and classifies the outcome into a frozen
`HealthResult(reachable, healthy, http_status, status, version, message)`. The
reported state is derived **solely** from that HTTP response — never from a
container engine — so it is correct regardless of how (or whether) a container
engine started the database.

### 2. HTTP 200 is the sole liveness gate

- **200** -> `healthy=True, reachable=True`. The body is parsed best-effort for
  `status`/`version` (documented shape:
  `[{"instance":"alpha","status":"healthy","version":"v25.3.4",...}]`), and the
  version is surfaced in the message when present. A **200 with an empty /
  non-list / non-dict body, or a body whose `.json()` raises `ValueError`, is
  still healthy** — the payload is simply reported as unrecognized. HTTP 200
  alone decides liveness; a body-shape change in a future Dgraph release must
  never flip a live database to "unhealthy".
- **non-200** (e.g. 503 while the Alpha is starting) -> `reachable=True,
  healthy=False`; the message names the integer code.
- **timeout** (`requests.exceptions.Timeout`) -> `reachable=False,
  healthy=False`, with a dedicated timeout message distinct from the connection
  message.
- **connection failure** (`requests.exceptions.RequestException`) ->
  `reachable=False, healthy=False`, with a fixed hint to run `partgraph db up`.

Any exception that is **not** a `requests` timeout/connection error propagates
(it is never coerced into a misleading "unreachable" result) — no blind
`except Exception` swallows a real bug.

### 3. A finite, bounded 2.0s timeout

`HEALTH_PROBE_TIMEOUT_S = 2.0` is a finite, named, bounded request timeout
(never an unbounded/`None` wait), extending ADR-0007's bounded-constant
precedent to this outbound call: `db status` returns promptly whether Dgraph is
up, down, or wedged.

### 4. Leaf-module discipline; `requests` imported lazily

`partgraph.util.health` is a **leaf** (stdlib-only top-level imports:
`dataclasses`, `collections.abc`, `typing`). `requests` is imported **lazily
inside `probe_health`**, never at module import time, so importing
`partgraph.util` pulls in no third-party HTTP dependency (a subprocess-isolated
test pins this). `http_get` is an injectable seam defaulting to a lazily-resolved
`requests.get`, mirroring `partgraph.ingest.fetch` / `partgraph.refresh.links`;
the unit suite injects a fake and never opens a socket. `health` is deliberately
**not** re-exported from `partgraph/util/__init__.py` — a probe is imported
explicitly from its submodule.

### 5. Safe, path-free, single-line message

`HealthResult.message` never contains a raw exception string, a response body,
or any `/`-bearing path, so `db status` can print it verbatim (with Rich
`markup=False`, so a version/body value carrying `[...]` is never misread as a
style tag). The healthy message goes to stdout; every not-healthy/unreachable
message goes to stderr.

## BREAKING CHANGES

This is a deliberate, enumerated breaking change to the `db status` contract:

- **(a) stdout shape.** Was a multi-row `compose ps` table (container id, name,
  status, ports); is now a **single-line health message** (e.g.
  `Dgraph is healthy (v25.3.4).`).
- **(b) exit code.** Was **always 0** (it merely printed whatever `compose ps`
  returned); is now **0 iff Dgraph is healthy, else 1** (non-200, timeout,
  connection failure, or an unexpected probe error all exit 1). `db status` is
  now usable as a real health gate in a script.
- **(c) no container engine.** `db status` no longer requires or invokes a
  container engine at all — it never calls `compose_command()`,
  `_run_compose(["ps"])`, or `subprocess.run`. It works on a host with no
  engine installed.

## Relationship to ADR-0009

This ADR **narrows ADR-0009** (container-engine detection). ADR-0009 routed the
`db up` / `db down` / `db status` trio through `compose_command()`; ADR-0018
removes `status` from that set. `db up` and `db down` remain Compose-routed and
engine-detected exactly as before — only `status` is now engine-independent. As
a drift-elimination follow-through, the `db up` success message and the
integration lifecycle test now reference `DGRAPH_HTTP_HEALTH_URL` (the single
source of truth in `partgraph.util.health`) instead of re-declaring the literal.

## Migration

Verified at authoring time (`grep` across the repo): **no** in-repo shell script,
systemd unit, cron/wrapper (ADR-0014's scheduling artifacts), README, or docs
page parses `db status` stdout or branches on its exit code — so nothing in this
repository breaks. External consumers that previously scraped the `compose ps`
table, or that relied on `db status` always exiting 0, must update:

- To list the *container*, call `<engine> compose -f docker/docker-compose.yml
  ps` directly (the old behaviour, now explicit).
- To gate on database health, keep using `db status` and read its **exit code**
  (0 = healthy, non-zero = not), or GET `http://127.0.0.1:8081/health` directly.

## Verification

Specified test-first in `tests/unit/test_health.py` (acceptance criteria
AC-1..AC-9), all hermetic (an injected `http_get` fake; no real socket, sleep,
or wall-clock read):

- **AC-1** — 200 + well-formed healthy body -> `healthy`/`reachable` True,
  `http_status=200`, `status="healthy"`, `version="v25.3.4"`; `db status` prints
  a line containing "healthy" and exits 0.
- **AC-2** (core fix) — with `compose_command` raising `ContainerEngineError`
  and the probe healthy, `db status` exits 0 and `subprocess.run` is **never**
  called (engine-independence).
- **AC-3** — connection error -> unreachable/unhealthy; message names
  `partgraph db up`, leaks neither the exception text nor any `/`; CLI exits 1
  with no traceback.
- **AC-4** — timeout -> unreachable/unhealthy with a dedicated timeout message
  distinct from AC-3; CLI exits 1.
- **AC-5** — non-200 (503) -> reachable but unhealthy, `http_status=503`,
  message names `503` and does not echo the body; CLI exits 1.
- **AC-6** — 200 with empty/non-list/`ValueError` body -> still healthy,
  `status`/`version` None, message reports the payload unrecognized (path-free,
  no leaked `.json()` text); CLI exits 0.
- **AC-7** — `HEALTH_PROBE_TIMEOUT_S` is a finite positive float, forwarded as
  the `timeout=` kwarg (default and custom).
- **AC-8** — `DGRAPH_HTTP_HEALTH_URL` is defined in exactly one place
  (`partgraph.util.health`); `tests/conftest.py` and the lifecycle integration
  test import it rather than re-declaring the literal (no drift).
- **AC-9** — `db status --help` still exits 0 and stays English.

Additional hardening tests pin: `db status` calls `probe_health()` with **zero**
arguments (no-override contract); an unexpected error from the seam propagates
rather than being swallowed; and importing `partgraph.util.health` in a fresh
interpreter does not import `requests`. `ruff check .` is clean and the full
non-integration suite is green.
