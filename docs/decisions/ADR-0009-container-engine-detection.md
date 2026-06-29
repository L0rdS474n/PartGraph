# ADR-0009: Container-engine detection (Docker/Podman compatibility)

- Status: Accepted
- Date: 2026-06-29

## Context

Every PartGraph container/compose invocation used to hard-code the literal
`docker` as the first argv element (for example
`["docker", "compose", "-f", ...]` in the `db up`/`down`/`status` commands and
in the integration tests). The compose-file path is resolved to an absolute
path and the subprocess is always run list-style with `shell=False`, so the
invocation is safe — but the *engine name* was fixed.

On a host that ships only Podman and no `docker` shim, that fixed name fails at
`subprocess.run` time with `FileNotFoundError: [Errno 2] No such file or
directory: 'docker'`. The failure is a raw Python traceback, it surfaces deep
inside the CLI rather than at a clear decision point, and it offers the user no
hint about what to install or how to point PartGraph at the engine they already
have. Podman 4.1+ ships a Docker-compatible `podman compose` plugin and reads
the same `docker-compose.yml`, so the only thing standing between PartGraph and
a Podman host was the hard-coded engine name.

PartGraph is a local, single-developer tool whose database lifecycle is the
very first thing a new contributor runs (`partgraph db up`). A first command
that dies with `FileNotFoundError: 'docker'` on an otherwise perfectly capable
Podman machine is a poor and avoidable first impression.

## Decision

Engine selection is centralised in one leaf helper,
`src/partgraph/util/container.py`, and the CLI builds its compose argv from that
helper instead of a hard-coded name. The selection policy is:

1. **Explicit override.** If the environment variable
   `PARTGRAPH_CONTAINER_ENGINE` is set to a non-empty value, that executable
   name is used after a PATH check. This is the escape hatch for anyone who
   wants a specific engine.

2. **Auto-detection (podman-first).** With no override, the first engine found
   on PATH in the order `("podman", "docker")` wins. Podman is preferred
   because it is equally correct when only one engine is installed; on a host
   where `docker` is a Podman shim it uses the real engine directly (skipping
   the emulation layer and its stderr noise); and it selects the rootless
   engine in the rare both-installed case.

3. **Loud failure, never silent fallback.** If `PARTGRAPH_CONTAINER_ENGINE`
   names a binary that is not on PATH, detection raises a clear
   `ContainerEngineError` naming the variable and the bad value — it never
   silently falls back to an auto-detected engine, so a typo can never quietly
   select a different engine than the one the user asked for. If no override is
   set and neither engine is installed, detection raises a `ContainerEngineError`
   naming both candidates so the user knows what to install. The override value
   is looked up verbatim as a single executable name; a value carrying internal
   whitespace or shell metacharacters is simply not found on PATH and therefore
   rejected, and because the argv is always list-style with `shell=False` such a
   value can never reach a shell.

4. **Compose v2 plugin form only.** The compose prefix is the v2 plugin form
   `<engine> compose` (Docker Compose v2 / Podman 4.1+). The legacy standalone
   `docker-compose` binary is intentionally **not** supported: it is
   end-of-life, its hyphenated invocation has no Podman equivalent, and
   supporting it would add a second code path with no benefit for a greenfield
   local tool.

The CLI catches `ContainerEngineError` at the compose call site and converts it
to a clean non-zero exit with a human-readable message, so the engine-detection
failure is never a raw traceback. The absolute `-f <compose-file>` path,
`shell=False`, and the deliberate omission of `-v` on `db down` are all
unchanged by this decision.

## Consequences

- PartGraph now runs unmodified on both Docker and Podman hosts; a Podman-only
  machine no longer fails its first `partgraph db up` with
  `FileNotFoundError: 'docker'`.
- The only behaviour change for existing users is on hosts that have **both**
  engines installed: auto-detection now prefers Podman where it previously
  always used Docker. Migration is one environment variable — set
  `PARTGRAPH_CONTAINER_ENGINE=docker` to keep using Docker. Hosts with only one
  engine, or with an explicit override already set, see no change.
- A mistyped or unavailable `PARTGRAPH_CONTAINER_ENGINE`, and a host with no
  engine at all, both fail fast with an actionable message instead of a deep
  traceback, satisfying the "errors handled explicitly, never silently
  swallowed" baseline.
- Engine resolution lives in a single stdlib-only leaf module with no
  dependency on the CLI or the embed/query/load layers, so both the CLI and the
  integration tests share one detection path and there is no import cycle.
- Dropping legacy `docker-compose` support is a deliberate, documented scope
  limit rather than an oversight; if a future need for it appears it will be
  evaluated in its own ADR.
