"""PartGraph command-line interface.

Provides the ``partgraph`` command and its ``db`` sub-command group for managing
the local Dgraph instance via the Compose plugin of the detected container
engine (Docker or Podman) and applying the DQL schema.

Design notes:
- ``app`` is a module-level :class:`typer.Typer` so the test-suite (and the
  console-script wrapper :func:`main`) can import it directly.
- The container engine is resolved at call time by
  :func:`partgraph.util.container.compose_command` (podman-first, with a
  ``PARTGRAPH_CONTAINER_ENGINE`` override). The Compose command is always
  invoked with a list argv and ``shell=False``; the compose-file path is
  resolved to an absolute path so no CWD-relative file can be injected.
- ``db down`` deliberately omits ``-v`` so the named data volume survives.
- pydgraph is imported lazily inside :func:`apply_schema` so that CLI commands
  which do not talk to Dgraph never require the gRPC stack.
"""

from __future__ import annotations

import math
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console

from partgraph import __version__
from partgraph import schema as schema_module
from partgraph.embed import get_encoder
from partgraph.refresh.links import (
    HostRateLimiter,
    format_verified_at,
    refresh_links_write,
)
from partgraph.refresh.stock import (
    build_stock_index,
    refresh_stock_write,
)
from partgraph.util.container import ContainerEngineError, compose_command
from partgraph.util.health import DGRAPH_HTTP_HEALTH_URL, probe_health
from partgraph.util.index_health import check_index_integrity

# ``get_encoder`` is imported at module level (not lazily) ON PURPOSE: the test
# suite patches it as ``patch.object(cli, "get_encoder", ...)`` for both the
# embed and the semantic-search paths. Importing partgraph.embed is cheap — it
# does NOT import sentence_transformers (that happens lazily inside get_encoder),
# so module import stays light and torch is never pulled in here.
#
# ``HostRateLimiter``/``refresh_links_write``/``format_verified_at`` are imported
# eagerly here for the same reason: the refresh-links tests patch them at
# ``partgraph.cli`` (as well as ``partgraph.refresh.links``) to spy on the
# rate-limiter construction and the leaf write call. partgraph.refresh.links is a
# thin leaf (stdlib only; httpx is imported lazily by _build_http_client), so the
# import stays light and opens no socket at import time.
#
# ``build_stock_index``/``refresh_stock_write`` (the stock/price refresh leaf,
# issue #11 PR 2) are imported eagerly for the same testability reason: the
# refresh tests patch ``refresh_stock_write`` at ``partgraph.cli`` to prove the
# stock_index is built once and threaded into every page. partgraph.refresh.stock
# is a pure-stdlib leaf (no gRPC/HTTP/source deps), so the import stays light.

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Dgraph Alpha gRPC address used for schema application and mutations.
DGRAPH_GRPC_ADDR = "127.0.0.1:9081"

#: Finite, named ceiling (256 MiB) for a single gRPC message on the shared
#: pydgraph client stub, applied symmetrically to send and receive. Raises
#: pydgraph's 4 MiB gRPC default so large read pages and vector-literal writes do
#: not trip RESOURCE_EXHAUSTED. Deliberately a bounded constant, NOT the grpc
#: ``-1`` "unlimited" sentinel — this extends ADR-0007's bounded-constant
#: precedent to the transport layer (ADR-0010).
_GRPC_MAX_MESSAGE_BYTES = 256 * 1024 * 1024

#: Absolute path to the Compose file. Resolved three levels up from this file
#: (src/partgraph/cli.py -> src/partgraph -> src -> <repo root>) so the value
#: passed to the engine's ``compose -f`` flag is always absolute and never
#: depends on the current working directory.
COMPOSE_FILE = Path(__file__).resolve().parent.parent.parent / "docker" / "docker-compose.yml"

#: Path to the canonical DQL schema file.
SCHEMA_FILE = Path(__file__).resolve().parent.parent.parent / "schema" / "partgraph.dql"

#: Repository root (src/partgraph/cli.py -> src/partgraph -> src -> <repo root>).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Default on-disk location of the JLCPCB/LCSC SQLite source file. Relative to
#: the repository root; the directory is created on demand by the fetch step.
RAW_DB_RELPATH = "data/raw/jlcpcb-components.sqlite3"
RAW_DB_PATH = _REPO_ROOT / RAW_DB_RELPATH

#: Default staging output (JSONL) and normalize checkpoint locations.
STAGED_PATH = _REPO_ROOT / "data" / "staged" / "jlcparts.jsonl"
NORMALIZE_CHECKPOINT_PATH = _REPO_ROOT / "data" / "state" / "normalize.json"

#: Resumable-load checkpoint location (load-robustness-v2, AC-A). Ties a load
#: run to the staged file via a cheap fingerprint so a crash can resume the
#: remaining batches instead of re-sending the whole staged set.
LOAD_CHECKPOINT_PATH = _REPO_ROOT / "data" / "state" / "load_checkpoint.json"

#: HTTPS URL of the CDFER single-file JLCPCB/LCSC component database (~1 GB).
#: Verified upstream GitHub Pages asset published by the cdfer
#: jlcpcb-parts-database project. The fetch step additionally verifies the
#: SQLite magic header so a wrong/substituted file still fails fast and safely.
JLCPARTS_DB_URL = (
    "https://cdfer.github.io/jlcpcb-parts-database/jlcpcb-components.sqlite3"
)

#: Node types reported by `partgraph stats`, mirroring schema/partgraph.dql.
_STATS_NODE_TYPES = (
    "Part",
    "Manufacturer",
    "Category",
    "Package",
    "Datasheet",
    "Tag",
    "AttrValue",
)

#: Provenance stamp applied to ingested records (deterministic, date-tagged).
SOURCE_REF = "jlcparts@2026-06-11"

_console = Console()
_err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Typer applications
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="partgraph",
    help=(
        "PartGraph: a local Dgraph graph database for electronic components. "
        "Manage the database and apply the schema with the 'db' command group."
    ),
    no_args_is_help=True,
    add_completion=False,
)

db_app = typer.Typer(
    name="db",
    help="Manage the local Dgraph database (container lifecycle and schema).",
    no_args_is_help=True,
)
app.add_typer(db_app, name="db")

ingest_app = typer.Typer(
    name="ingest",
    help=(
        "Ingest electronic component data from external open-data sources "
        "into the local Dgraph database."
    ),
    no_args_is_help=True,
)
app.add_typer(ingest_app, name="ingest")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_compose(compose_args: list[str], *, action: str) -> None:
    """Run ``<engine> compose -f <COMPOSE_FILE> <compose_args>`` safely.

    The compose command prefix (``["docker", "compose"]`` or
    ``["podman", "compose"]``) is resolved at call time by
    :func:`partgraph.util.container.compose_command`, so PartGraph works on
    whichever container engine is installed (Docker or Podman, podman-first).

    Args:
        compose_args: Trailing Compose arguments (e.g. ``["up", "-d"]``).
        action: Human-readable description used in error messages.

    Raises:
        typer.Exit: code 1 if no usable container engine can be resolved, or the
            subprocess return code if the container engine exits non-zero, after
            printing a clear English error message to stderr.
    """
    try:
        prefix = compose_command()
    except ContainerEngineError as exc:
        # No usable engine on PATH (or a bad PARTGRAPH_CONTAINER_ENGINE
        # override): surface the detection message and exit cleanly so the raw
        # exception never propagates as a traceback to the user's terminal.
        _err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    argv = [*prefix, "-f", str(COMPOSE_FILE), *compose_args]
    # shell is never True: argv is a list and no string is interpolated by a
    # shell, eliminating shell-injection risk.
    result = subprocess.run(  # noqa: PLW1510 — return code handled explicitly below
        argv,
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.stdout:
        _console.print(result.stdout, end="")
    if result.returncode != 0:
        _err_console.print(
            f"[red]Error:[/red] failed to {action} the Dgraph database "
            f"(the container engine exited with code {result.returncode})."
        )
        if result.stderr:
            _err_console.print(result.stderr, end="")
        raise typer.Exit(code=result.returncode)


# ---------------------------------------------------------------------------
# db sub-commands
# ---------------------------------------------------------------------------

@db_app.command("up")
def up() -> None:
    """Start the local Dgraph database in the background (compose up -d)."""
    _run_compose(["up", "-d"], action="start")
    _console.print(
        f"[green]Dgraph is starting.[/green] Health: {DGRAPH_HTTP_HEALTH_URL}"
    )


@db_app.command("down")
def down() -> None:
    """Stop the local Dgraph database, preserving the named data volume.

    The '-v' flag is intentionally never passed, so the partgraph_dgraph_data
    volume (and therefore all ingested data) survives a 'db down'.
    """
    _run_compose(["down"], action="stop")
    _console.print("[green]Dgraph stopped.[/green] The data volume is preserved.")


@db_app.command("status")
def status() -> None:
    """Report whether the local Dgraph database is running and healthy.

    Probes Dgraph's OWN HTTP /health endpoint (:data:`DGRAPH_HTTP_HEALTH_URL`)
    rather than delegating to ``compose ps``, so the reported state reflects the
    DATABASE's true liveness regardless of how it was started — compose, a
    systemd timer, or a bare ``podman run`` / ``docker run`` — and needs no
    container engine at all (ADR-0018). Exits 0 iff Dgraph is healthy, else 1.
    """
    try:
        result = probe_health()
    except Exception as exc:
        # Defense-in-depth: probe_health already maps every EXPECTED network
        # outcome (timeout / connection failure / non-200) to a HealthResult; the
        # specific handled cases never reach here. Any UNEXPECTED error is turned
        # into a fixed, path-free message and a clean exit so no raw traceback
        # (which could leak an internal path) reaches the user's terminal —
        # consistent with apply_schema/stats/search. Re-raised via ``from exc``,
        # so this is not a blind, swallowing except (ruff BLE001 is satisfied).
        _err_console.print(
            "[red]Error:[/red] could not probe the Dgraph health endpoint."
        )
        raise typer.Exit(code=1) from exc

    # ``markup=False``: the probe-derived message is untrusted for Rich markup
    # (a version string or future body value could contain '[...]' and be
    # misread as a style tag), so it is printed literally.
    if result.healthy:
        _console.print(result.message, markup=False)
    else:
        _err_console.print(result.message, markup=False)
    raise typer.Exit(code=0 if result.healthy else 1)


@db_app.command("apply-schema")
def apply_schema() -> None:
    """Apply the DQL schema to the running Dgraph instance over gRPC.

    Reads schema/partgraph.dql and applies it via pydgraph against
    DGRAPH_GRPC_ADDR (127.0.0.1:9081). pydgraph is imported lazily so other
    commands do not require it.
    """
    try:
        schema_text = schema_module.load_schema(SCHEMA_FILE)
    except (FileNotFoundError, ValueError) as exc:
        _err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    try:
        schema_module.apply_schema(schema_text, DGRAPH_GRPC_ADDR)
    except ImportError as exc:
        _err_console.print(
            "[red]Error:[/red] pydgraph is not installed. "
            'Install it with `pip install -e ".[dev]"` or `pip install pydgraph`.'
        )
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        # Surface any pydgraph/gRPC failure with a clear message, then re-raise
        # as a CLI exit so the error is never silently swallowed.
        _err_console.print(
            f"[red]Error:[/red] failed to apply schema to Dgraph at "
            f"{DGRAPH_GRPC_ADDR}: {exc}"
        )
        raise typer.Exit(code=1) from exc

    _console.print(
        f"[green]Schema applied[/green] to Dgraph at {DGRAPH_GRPC_ADDR} "
        f"from {SCHEMA_FILE}."
    )


@db_app.command("check-index")
def check_index() -> None:
    """Check the live vector-index integrity against the schema file (ADR-0019).

    Calls :func:`partgraph.util.index_health.check_index_integrity`, which asks
    Dgraph's OWN HTTP ``/query`` endpoint whether the live ``hnsw`` options on the
    ``embedding`` predicate match ``schema/partgraph.dql`` and whether an embedded
    part's stored vector, replayed through ``similar_to``, still finds itself.
    Like ``db status`` (ADR-0018) this is engine-independent — it never calls a
    container engine. Exits 0 iff the database is reachable, the schema matches,
    and the self-similarity probe passed (or there is nothing embedded yet to
    check); otherwise 1.
    """
    try:
        result = check_index_integrity(schema_text=schema_module.load_schema(SCHEMA_FILE))
    except typer.Exit:
        raise
    except Exception as exc:
        # Defense-in-depth (security Finding 2, mirroring status()): the leaf maps
        # every EXPECTED network outcome to an IndexIntegrityResult and lets only
        # UNEXPECTED errors propagate. Turn any such error into a fixed, path-free
        # message and a clean exit so no raw traceback (which could leak an
        # internal path) reaches the user's terminal. Re-raised via ``from exc``,
        # so this is not a blind, swallowing except (ruff BLE001 is satisfied).
        _err_console.print(
            "[red]Error:[/red] could not run the index integrity check."
        )
        raise typer.Exit(code=1) from exc

    # Exit formula (ADR-0019): healthy iff reachable AND the schema matches AND the
    # self-similarity probe passed or was skipped for want of embedded parts
    # (None). ``markup=False``: the probe-derived message is untrusted for Rich
    # markup (an option value could contain '[...]'), so it is printed literally —
    # the healthy line to stdout, any not-healthy line to stderr (mirrors status()).
    exit_ok = bool(
        result.reachable
        and result.schema_ok
        and result.self_similarity_ok in (True, None)
    )
    # ``soft_wrap=True``: the message is a single line by contract, but it can
    # exceed the console width — without this Rich would soft-wrap it and insert a
    # newline mid-message, splitting the verbatim status text. Soft-wrap prints it
    # as one line. ``markup=False`` keeps a literal '[...]' in an option value from
    # being misread as a Rich style tag (same discipline as status()).
    if exit_ok:
        _console.print(result.message, markup=False, soft_wrap=True)
    else:
        _err_console.print(result.message, markup=False, soft_wrap=True)
    raise typer.Exit(code=0 if exit_ok else 1)


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------

@app.command()
def version() -> None:
    """Print the installed PartGraph version."""
    _console.print(__version__)


# ---------------------------------------------------------------------------
# Ingest helpers
# ---------------------------------------------------------------------------

def _validate_limit(limit: str | None) -> int | None:
    """Validate the --limit option value.

    Returns the parsed positive integer, or ``None`` when no limit was given.
    Raises :class:`typer.Exit` (code 1) with the exact, test-pinned message
    when the value is not a positive integer.
    """
    if limit is None:
        return None
    text = limit.strip()
    try:
        value = int(text)
    except ValueError:
        value = None
    if value is None or value <= 0:
        _err_console.print("[red]Error:[/red] --limit must be a positive integer.")
        raise typer.Exit(code=1)
    return value


def _build_dgraph_client():
    """Create a pydgraph client connected to the local Dgraph Alpha.

    pydgraph is imported lazily so commands that do not touch Dgraph never
    require the gRPC stack. The stub is built with a raised per-message gRPC
    ceiling (:data:`_GRPC_MAX_MESSAGE_BYTES`, send and receive symmetrically) so
    large read pages and vector-literal writes do not hit pydgraph's 4 MiB
    default (ADR-0010). Returns ``(client, stub)``; the caller closes the stub.
    """
    import pydgraph  # noqa: PLC0415 — lazy import keeps the CLI import-light

    # grpc requires ``options`` as a LIST of (key, value) 2-tuples: a dict is
    # rejected at real-channel construction with "ValueError: too many values to
    # unpack (expected 2)". Send and receive share the one finite ceiling.
    stub = pydgraph.DgraphClientStub(
        DGRAPH_GRPC_ADDR,
        options=[
            ("grpc.max_receive_message_length", _GRPC_MAX_MESSAGE_BYTES),
            ("grpc.max_send_message_length", _GRPC_MAX_MESSAGE_BYTES),
        ],
    )
    client = pydgraph.DgraphClient(stub)
    return client, stub


def _read_staged_parts(staged_path: Path) -> list:
    """Read a JSONL staging file into a list of StagedPart records."""
    from partgraph.normalize.model import StagedPart  # noqa: PLC0415

    parts = []
    if not staged_path.exists():
        return parts
    with staged_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                parts.append(StagedPart.from_json(line))
    return parts


# ---------------------------------------------------------------------------
# ingest sub-commands
# ---------------------------------------------------------------------------

@ingest_app.command("jlcparts")
def ingest_jlcparts(
    fetch: bool = typer.Option(
        False,
        "--fetch",
        help="Download the JLCPCB/LCSC component database (~1 GB) before ingesting.",
    ),
    limit: str | None = typer.Option(
        None,
        "--limit",
        help=(
            "Limit to the first N parts (development/testing only; the full "
            "ingest loads the entire catalogue). Must be a positive integer."
        ),
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help=(
            "Load the full multi-volume yaqwsx archive. Not yet implemented — "
            "see ADR-0001."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-download even if a matching cached file already exists.",
    ),
) -> None:
    """Ingest electronic component data from the JLCPCB/LCSC catalogue (CDFER
    source) into Dgraph.

    The pipeline runs in three stages — fetch (optional), normalize, load —
    aborting immediately if any stage fails.
    """
    parsed_limit = _validate_limit(limit)

    if full:
        _err_console.print(
            "[red]Error:[/red] --full (multi-volume yaqwsx archive) is not yet "
            "implemented. The CDFER single-file source is used instead; see "
            "ADR-0001 (docs/decisions/ADR-0001-defer-full-jlcparts-archive.md)."
        )
        raise typer.Exit(code=1)

    dest = RAW_DB_PATH

    if fetch:
        _stage_fetch(dest, force=force)
    _require_source_file(dest, fetched=fetch)
    _stage_normalize(dest, parsed_limit)
    loaded = _stage_load()

    _console.print(
        f"[green]Ingest complete.[/green] Loaded {loaded} parts into Dgraph."
    )


def _stage_fetch(dest: Path, *, force: bool) -> None:
    """Download the source database, showing a progress bar. Exits 1 on error."""
    from rich.progress import (  # noqa: PLC0415
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
    )

    import partgraph.ingest.fetch as fetch_module  # noqa: PLC0415

    try:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            console=_console,
            transient=True,
        ) as progress_bar:
            task = progress_bar.add_task("Downloading jlcparts DB", total=None)

            def _on_progress(received: int, total: int | None) -> None:
                progress_bar.update(task, completed=received, total=total)

            fetch_module.fetch_cdfer(
                JLCPARTS_DB_URL, dest, force=force, progress=_on_progress
            )
    except typer.Exit:
        raise
    except Exception as exc:
        _err_console.print(f"[red]Error:[/red] failed to download the database: {exc}")
        raise typer.Exit(code=1) from exc


def _require_source_file(dest: Path, *, fetched: bool) -> None:
    """Exit 1 with a clear message if the source database is missing."""
    if dest.exists():
        return
    if fetched:
        _err_console.print(
            "[red]Error:[/red] the download did not produce the expected file "
            f"at {RAW_DB_RELPATH}."
        )
    else:
        _err_console.print(
            "[red]Error:[/red] source database not found at "
            f"{RAW_DB_RELPATH}. Run this command with --fetch to download it "
            "first (~1 GB)."
        )
    raise typer.Exit(code=1)


def _stage_normalize(dest: Path, parsed_limit: int | None) -> None:
    """Introspect the source DB and write the staged JSONL. Exits 1 on error."""
    try:
        import partgraph.normalize.run as normalize_module  # noqa: PLC0415
        from partgraph.sources.jlcparts import (  # noqa: PLC0415
            JlcpartsAdapter,
            open_jlcparts_db,
        )

        conn = open_jlcparts_db(dest)
        adapter: object = JlcpartsAdapter(conn)
        if parsed_limit is not None:
            adapter = _LimitedAdapter(adapter, parsed_limit)

        normalize_module.normalize(
            adapter=adapter,
            source_ref=SOURCE_REF,
            output_path=STAGED_PATH,
            checkpoint_path=NORMALIZE_CHECKPOINT_PATH,
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _err_console.print(f"[red]Error:[/red] normalization failed: {exc}")
        raise typer.Exit(code=1) from exc


def _file_fingerprint(path: Path) -> str:
    """Return a cheap, stable identity token for *path* (``"<size>:<mtime_ns>"``).

    Size plus nanosecond mtime is enough to detect that the staged JSONL was
    re-generated between load runs without hashing ~hundreds of MB. The load
    checkpoint stores this token; a mismatch on resume means the staged file
    changed, so the loader safely restarts from batch 0 instead of skipping.
    """
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _stage_load() -> int:
    """Load the staged parts into Dgraph. Returns the count loaded; exits 1 on error."""
    from rich.progress import (  # noqa: PLC0415
        BarColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
    )

    from partgraph.load.loader import Loader  # noqa: PLC0415

    parts = _read_staged_parts(STAGED_PATH)
    fingerprint = _file_fingerprint(STAGED_PATH)
    LOAD_CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    stub = None
    try:
        client, stub = _build_dgraph_client()
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=_console,
            transient=True,
        ) as progress_bar:
            task = progress_bar.add_task("Loading into Dgraph", total=len(parts) or None)

            def _on_load(current: int, total_count: int) -> None:
                progress_bar.update(task, completed=current, total=total_count or None)

            Loader(client, progress=_on_load).load(
                parts,
                checkpoint_path=LOAD_CHECKPOINT_PATH,
                fingerprint=fingerprint,
            )
    except typer.Exit:
        raise
    except Exception as exc:
        _err_console.print(
            f"[red]Error:[/red] failed to load parts into Dgraph: {exc}. "
            "Is the database running? Start it with `partgraph db up`."
        )
        raise typer.Exit(code=1) from exc
    finally:
        if stub is not None:
            stub.close()
    return len(parts)


class _LimitedAdapter:
    """Wrap an adapter to yield at most ``limit`` parts (dev/testing use)."""

    def __init__(self, inner, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    def iter_parts(self):
        for i, part in enumerate(self._inner.iter_parts()):
            if i >= self._limit:
                break
            yield part


# ---------------------------------------------------------------------------
# stats command
# ---------------------------------------------------------------------------

@app.command()
def stats() -> None:
    """Show node counts per type in the local Dgraph database.

    Uses the Dgraph v25-safe named-block aggregation form
    ``{ q(func: type(X)) { count(uid) } }`` (never the broken root-level
    ``count(func: ...)`` form) and renders the result as a table.
    """
    import json as _json  # noqa: PLC0415

    from rich.table import Table  # noqa: PLC0415

    stub = None
    try:
        client, stub = _build_dgraph_client()
        counts: dict[str, int] = {}
        for node_type in _STATS_NODE_TYPES:
            query = f"{{ q(func: type({node_type})) {{ count(uid) }} }}"
            txn = client.txn(read_only=True)
            try:
                resp = txn.query(query)
                data = _json.loads(resp.json)
                block = data.get("q", [])
                counts[node_type] = block[0]["count"] if block else 0
            finally:
                txn.discard()
    except typer.Exit:
        raise
    except Exception as exc:
        _err_console.print(
            f"[red]Error:[/red] failed to query Dgraph: {exc}. "
            "Is the database running? Start it with `partgraph db up`."
        )
        raise typer.Exit(code=1) from exc
    finally:
        if stub is not None:
            stub.close()

    table = Table(title="PartGraph node counts")
    table.add_column("Type", justify="left")
    table.add_column("Count", justify="right")
    for node_type in _STATS_NODE_TYPES:
        table.add_row(node_type, str(counts.get(node_type, 0)))
    _console.print(table)


# ---------------------------------------------------------------------------
# search / show commands (read-only)
# ---------------------------------------------------------------------------

#: Fixed, path-free error shown when a read-only Dgraph query fails. The raw
#: exception is never interpolated so internal paths cannot leak (B1).
_DB_QUERY_ERROR = (
    "[red]Error:[/red] could not query Dgraph. Is the database running? "
    "Start it with `partgraph db up`."
)

#: Fixed, path-free hint shown when sentence-transformers (the optional [embed]
#: extra) is not installed. Never interpolates an exception or path.
_EMBED_EXTRA_HINT = (
    "[red]Error:[/red] semantic embedding requires the optional 'embed' extra "
    '(sentence-transformers). Install it with: pip install -e ".[embed]".'
)

#: Actionable hint when a semantic search returns nothing — usually because the
#: embedding predicate has not been populated yet.
_NO_EMBEDDINGS_HINT = (
    "No semantic matches. The embedding index may be empty — run "
    "`partgraph embed` first to generate embeddings."
)

#: Fixed, path-free error shown when the embed run fails (DB down / runtime
#: error). The raw exception is never interpolated so no internal path leaks.
_EMBED_DB_ERROR = (
    "[red]Error:[/red] could not embed parts. Is the database running? "
    "Start it with `partgraph db up`."
)


def _run_block_query(client, query_text: str, variables: dict[str, str]) -> dict:
    """Run a single read-only DQL query and return the parsed JSON response.

    The transaction is always read-only and always discarded; this function
    never mutates, commits, or alters the database.
    """
    import json as _json  # noqa: PLC0415

    txn = client.txn(read_only=True)
    try:
        resp = txn.query(query_text, variables=variables)
        return _json.loads(resp.json)
    finally:
        txn.discard()


#: Fixed, path-free error shown when a package is supplied BOTH in the positional
#: query text and via --package. Names the conflict without echoing either value.
_PACKAGE_TWICE_ERROR = (
    "[red]Error:[/red] package given twice (once in the query text and once via "
    "--package); use only one."
)


def _validate_filter_text_flag(value: str | None, *, flag: str) -> None:
    """Validate a --manufacturer/--category free-text filter value.

    A no-op when *value* is ``None``. Emits the fixed, path-free "<flag> must be
    ..." error and exits 1 when the value is empty/whitespace-only or exceeds the
    shared length cap. Enforced at the CLI boundary (mirroring
    ``dql_builder._validate_filter_term``) so a bad value never builds a Dgraph
    client (AC-SF-28).
    """
    if value is None:
        return
    from partgraph.query.dql_builder import MAX_FILTER_TERM_LEN  # noqa: PLC0415

    if not value.strip() or len(value) > MAX_FILTER_TERM_LEN:
        _err_console.print(
            f"[red]Error:[/red] {flag} must be a non-empty value of at most "
            f"{MAX_FILTER_TERM_LEN} characters."
        )
        raise typer.Exit(code=1)


def _validate_min_stock_flag(min_stock: str | None, *, in_stock: bool) -> int | None:
    """Resolve the effective minimum-stock threshold from --min-stock/--in-stock.

    ``--in-stock`` is sugar for ``--min-stock 1``; the two are mutually exclusive.
    ``--min-stock`` is accepted as a string and parsed here (not by Typer's native
    ``int``) so a bad value hits OUR fixed exit-1 message rather than Click's
    exit-2 usage error. A non-integer/negative value emits the fixed, path-free
    error and exits 1. Returns the integer threshold, or ``None`` when neither
    flag is given.
    """
    if in_stock and min_stock is not None:
        _err_console.print(
            "[red]Error:[/red] --in-stock and --min-stock cannot be combined; "
            "use only one."
        )
        raise typer.Exit(code=1)
    if in_stock:
        return 1
    if min_stock is None:
        return None
    text = min_stock.strip()
    try:
        value = int(text)
    except ValueError:
        value = None
    if value is None or value < 0:
        _err_console.print("[red]Error:[/red] --min-stock must be a non-negative integer.")
        raise typer.Exit(code=1)
    return value


def _resolve_is_basic_flag(*, basic: bool, extended: bool) -> bool | None:
    """Resolve the tri-state basic/extended filter from the two boolean flags.

    ``--basic`` and ``--extended`` are mutually exclusive (emits a fixed,
    path-free error and exits 1). Returns ``True`` for ``--basic``, ``False`` for
    ``--extended``, and ``None`` when neither is given (no filter).
    """
    if basic and extended:
        _err_console.print(
            "[red]Error:[/red] --basic and --extended cannot be combined; use only one."
        )
        raise typer.Exit(code=1)
    if basic:
        return True
    if extended:
        return False
    return None


def _validate_max_price_flag(max_price: str | None) -> float | None:
    """Resolve the --max-price ceiling (USD) as a non-negative finite float.

    Accepted as a string and parsed here (not by Typer's native ``float``) so a
    bad value hits OUR fixed exit-1 message rather than Click's exit-2 usage
    error. A non-numeric/negative/non-finite value emits the fixed, path-free
    error and exits 1. Returns the float, or ``None`` when the flag is not given.
    """
    if max_price is None:
        return None
    text = max_price.strip()
    try:
        value = float(text)
    except ValueError:
        value = None
    if value is None or not math.isfinite(value) or value < 0:
        _err_console.print("[red]Error:[/red] --max-price must be a non-negative number (USD).")
        raise typer.Exit(code=1)
    return value


def _validate_package_flag(package: str | None) -> str | None:
    """Upper-case and charset-validate a --package flag value.

    Returns the upper-cased package (ready to bind as ``$pkg``), or ``None`` when
    the flag was not given. The caller is responsible for upper-casing before the
    builder (the builder re-validates but does not upper-case); doing it here lets
    ``--package soic-16`` succeed as ``SOIC-16``. A charset/length violation emits
    the fixed, path-free "--package must be ..." error and exits 1 at the CLI
    boundary, so a bad value never builds a Dgraph client (AC-SF-28).
    """
    if package is None:
        return None
    from partgraph.query.dql_builder import validate_package  # noqa: PLC0415

    candidate = package.strip().upper()
    try:
        return validate_package(candidate)
    except ValueError as exc:
        _err_console.print(
            "[red]Error:[/red] --package must be 1-20 characters of A-Z, 0-9 or "
            "'-' (e.g. SOIC-16)."
        )
        raise typer.Exit(code=1) from exc


#: The three valid ``--sort`` keys, in help/error order. ``relevance`` is the
#: default (today's tier/stock/is_basic order); ``stock`` is most-in-stock first;
#: ``price`` is cheapest first.
_VALID_SORT_KEYS: tuple[str, ...] = ("relevance", "stock", "price")


def _validate_sort_flag(sort: str) -> str:
    """Validate the ``--sort`` value against the fixed key set; return it unchanged.

    ``--sort`` is a plain ``str`` option validated in OUR code (never a Typer
    ``Enum``/``Literal``/``click.Choice``, which would make Click reject a bad
    value with its generic exit-2 usage error). A value outside
    :data:`_VALID_SORT_KEYS` emits the fixed, path-free error and exits 1 — the
    same boundary contract as the other structured-filter flags (AC-SF-40 /
    Sec MUST-1). Validated in the shared block BEFORE any Dgraph client is built.
    """
    if sort not in _VALID_SORT_KEYS:
        _err_console.print(
            "[red]Error:[/red] --sort must be one of: relevance, stock, price."
        )
        raise typer.Exit(code=1)
    return sort


def _emit_search_json(result, parsed) -> None:
    """Serialise *result* to the machine-readable JSON envelope and write stdout.

    Emits with the stdlib ``print``/``json.dumps`` — never Rich — so stdout
    carries EXACTLY one JSON object with no markup, table, banner, footer or ANSI
    (Arch MUST-3). Only ever called on the SUCCESS path: every error path prints a
    fixed, path-free message to stderr and emits no JSON at all, so a machine
    consumer never receives a half-JSON blob or a traceback (Sec MUST-2).
    """
    import json as _json  # noqa: PLC0415

    from partgraph.query.renderer import render_search_results_json  # noqa: PLC0415

    envelope = render_search_results_json(result, parsed)
    print(_json.dumps(envelope, ensure_ascii=False))


@app.command()
def search(  # noqa: PLR0913 — Typer command surface: one option per filter flag
    query: str = typer.Argument(
        "",
        help="Free-text component query, e.g. 'MAX232' or '10k 0402 1%'.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        help="Maximum number of results to show (capped server-side at 200).",
    ),
    no_truncate: bool = typer.Option(
        False,
        "--no-truncate",
        help="Show full datasheet URLs and fields without cropping wide columns.",
    ),
    semantic: str | None = typer.Option(
        None,
        "--semantic",
        help=(
            "Semantic search: embed this free-text description and rank parts by "
            "embedding similarity (e.g. 'rs232 transceiver'). Any positional "
            "query is then used only for parametric/package filters. Requires the "
            'optional [embed] extra (pip install -e ".[embed]").'
        ),
    ),
    manufacturer: str | None = typer.Option(
        None,
        "--manufacturer",
        help=(
            "Filter by manufacturer name (case-insensitive; matches all tokens, "
            "e.g. 'Texas Instruments')."
        ),
    ),
    package: str | None = typer.Option(
        None,
        "--package",
        help=(
            "Filter by exact package code, e.g. 'SOIC-16' (case-insensitive; "
            "cannot also be given as a package token in the query)."
        ),
    ),
    category: str | None = typer.Option(
        None,
        "--category",
        help=(
            "Filter by category name (case-insensitive; matches all tokens, "
            "e.g. 'RS232 ICs')."
        ),
    ),
    in_stock: bool = typer.Option(
        False,
        "--in-stock",
        help=(
            "Only show parts in stock (stock > 0). Mutually exclusive with "
            "--min-stock."
        ),
    ),
    min_stock: str | None = typer.Option(
        None,
        "--min-stock",
        help=(
            "Only show parts with at least N units in stock (a non-negative "
            "integer). Mutually exclusive with --in-stock."
        ),
    ),
    basic: bool = typer.Option(
        False,
        "--basic",
        help="Only show JLCPCB 'basic' parts. Mutually exclusive with --extended.",
    ),
    extended: bool = typer.Option(
        False,
        "--extended",
        help="Only show JLCPCB 'extended' parts. Mutually exclusive with --basic.",
    ),
    max_price: str | None = typer.Option(
        None,
        "--max-price",
        help=(
            "Only show parts priced at or below this amount in USD (a "
            "non-negative number)."
        ),
    ),
    sort: str = typer.Option(
        "relevance",
        "--sort",
        help=(
            "Order results: 'relevance' (default — best match first: tier, then "
            "in-stock and basic parts), 'stock' (most in stock first) or 'price' "
            "(cheapest first; parts with no price last). Ignored in nearest-match "
            "mode, where parameter distance always wins."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Output results as a single machine-readable JSON object (stable "
            "keys, version 1, no internal uids) instead of the human table. "
            "Errors still exit non-zero and print no JSON."
        ),
    ),
) -> None:
    """Search the component graph by MPN, parameters and package.

    The query is parsed into numeric parameters (e.g. 10k -> resistance),
    a package code (e.g. 0402) and free-text MPN tokens, then matched with an
    exact / trigram / full-text cascade. When no exact parametric match exists,
    a relaxed pass returns the nearest parts by parameter distance.

    With --semantic, the supplied description is embedded and parts are ranked by
    embedding similarity; the positional query (if any) then contributes only its
    parametric/package filters (a hybrid search). Semantic hits are labelled
    "[Semantic]" so a fuzzy match is never mistaken for an exact part number.

    Examples:
      partgraph search "MAX232"
      partgraph search "10k 0402 1%"
      partgraph search "100nF 0603"
      partgraph search "1.2V MAX232"
      partgraph search --semantic "rs232 transceiver"

    All reads are read-only; this command never modifies the database. Use
    --limit to bound the result count and --no-truncate to print full URLs.
    The command searches related parts by MPN similarity.
    """
    # Shared structured-filter validation (AC-SF-28): validate every new filter
    # flag BEFORE the --semantic branch split and BEFORE any Dgraph client is
    # built, so both paths share one contract and a bad value never opens a
    # connection. Each helper emits a fixed, path-free error and exits 1.
    _validate_filter_text_flag(manufacturer, flag="--manufacturer")
    _validate_filter_text_flag(category, flag="--category")
    min_stock_val = _validate_min_stock_flag(min_stock, in_stock=in_stock)
    is_basic_val = _resolve_is_basic_flag(basic=basic, extended=extended)
    max_price_val = _validate_max_price_flag(max_price)
    package_flag = _validate_package_flag(package)
    sort_key = _validate_sort_flag(sort)

    if semantic is not None:
        _run_semantic_search(
            query,
            semantic,
            limit=limit,
            no_truncate=no_truncate,
            manufacturer=manufacturer,
            category=category,
            min_stock=min_stock_val,
            is_basic=is_basic_val,
            max_price=max_price_val,
            package_flag=package_flag,
            sort=sort_key,
            json_output=json_output,
        )
        return

    from partgraph.query.dql_builder import build_search_dql  # noqa: PLC0415
    from partgraph.query.parser import ParsedQuery, parse_query  # noqa: PLC0415
    from partgraph.query.ranker import rank_results  # noqa: PLC0415
    from partgraph.query.renderer import render_search_results  # noqa: PLC0415

    if not query.strip():
        _err_console.print("[red]Error:[/red] search query cannot be empty.")
        raise typer.Exit(code=1)

    parsed = parse_query(query)

    # A package may come from the query text (parsed.package) OR --package, never
    # both (AC-SF-5). Reject the collision here — after parsing, before the client
    # is built — so no query is issued on a contradictory request.
    if parsed.package is not None and package_flag is not None:
        _err_console.print(_PACKAGE_TWICE_ERROR)
        raise typer.Exit(code=1)

    # Hard user constraints threaded into BOTH the hard pass and the relaxed
    # (nearest-match) pass — only the query-derived parametric quantities relax
    # (AC-SF-17). package_flag is None when the package came from the query text,
    # in which case the builder falls back to parsed.package.
    filter_kwargs = {
        "package": package_flag,
        "manufacturer": manufacturer,
        "category": category,
        "min_stock": min_stock_val,
        "is_basic": is_basic_val,
        "max_price": max_price_val,
    }

    stub = None
    try:
        client, stub = _build_dgraph_client()

        # Pass 1 (hard): full parametric + text filter + all structured filters.
        query_text, variables = build_search_dql(parsed, limit=limit, **filter_kwargs)
        data = _run_block_query(client, query_text, variables)
        result = rank_results(data, parsed, sort=sort_key)

        if not result.rows and parsed.quantities:
            # Pass 2 (relaxed): drop parametric filters, keep text + package + the
            # hard structured filters, and merge the relaxed rows under the
            # "nearest" key for the ranker.
            relaxed = ParsedQuery(
                quantities=[],
                package=parsed.package,
                text_tokens=parsed.text_tokens,
                raw_query=parsed.raw_query,
            )
            relaxed_text, relaxed_vars = build_search_dql(
                relaxed, limit=limit, **filter_kwargs
            )
            relaxed_data = _run_block_query(client, relaxed_text, relaxed_vars)

            hard_uids = {
                r.get("uid")
                for key in ("exact", "trig", "fts")
                for r in data.get(key, []) or []
                if isinstance(r, dict)
            }
            nearest_rows = [
                r
                for block in relaxed_data.values()
                if isinstance(block, list)
                for r in block
                if isinstance(r, dict) and r.get("uid") not in hard_uids
            ]
            merged = {
                "exact": data.get("exact", []) or [],
                "trig": data.get("trig", []) or [],
                "fts": data.get("fts", []) or [],
                "nearest": nearest_rows,
            }
            # ``sort`` is threaded for consistency; rank_results ignores it in
            # nearest-match mode (parameter-distance order always wins).
            result = rank_results(merged, parsed, sort=sort_key)
    except typer.Exit:
        raise
    except Exception as exc:
        _err_console.print(_DB_QUERY_ERROR)
        raise typer.Exit(code=1) from exc
    finally:
        if stub is not None:
            stub.close()

    # Success path only: under --json emit exactly one JSON envelope (Arch
    # MUST-3) — including the empty-result envelope, never the human "No matches
    # found" banner (AC-SF-26); otherwise render the human Rich table unchanged.
    if json_output:
        _emit_search_json(result, parsed)
    else:
        render_search_results(result, parsed, _console, no_truncate=no_truncate)


def _run_semantic_search(  # noqa: PLR0913 — keyword-only filter passthrough
    query: str,
    semantic_text: str,
    *,
    limit: int,
    no_truncate: bool,
    manufacturer: str | None = None,
    category: str | None = None,
    min_stock: int | None = None,
    is_basic: bool | None = None,
    max_price: float | None = None,
    package_flag: str | None = None,
    sort: str = "relevance",
    json_output: bool = False,
) -> None:
    """Run a read-only semantic (embedding-similarity) search.

    *semantic_text* is embedded into a query vector; the positional *query* (if
    any) is parsed only for parametric/package filters layered onto the vector
    search (a hybrid query). The structured filters (already validated by the
    caller) compose into the SAME similar_to block exactly as on the lexical path
    (AC-SF-16). The path is strictly read-only and never mutates.

    Error handling (path-free, never leaks an exception or filesystem path):
    - empty *semantic_text* is rejected before the encoder or DB is touched;
    - a missing [embed] extra (ImportError) exits 1 with the install hint and
      issues NO Dgraph query;
    - a package supplied both in the query text and via --package exits 1 with the
      fixed collision message, before the client is built;
    - a DB failure exits 1 with the fixed "partgraph db up" message;
    - an empty result prints an actionable "run `partgraph embed` first" hint.
    """
    from partgraph.query.dql_builder import build_semantic_dql  # noqa: PLC0415
    from partgraph.query.parser import ParsedQuery, parse_query  # noqa: PLC0415
    from partgraph.query.ranker import rank_results  # noqa: PLC0415
    from partgraph.query.renderer import render_search_results  # noqa: PLC0415

    # Reject an empty semantic query BEFORE the encoder or DB is touched.
    if not semantic_text.strip():
        _err_console.print("[red]Error:[/red] --semantic query cannot be empty.")
        raise typer.Exit(code=1)

    # Acquire the encoder first: if the [embed] extra is absent we must exit
    # WITHOUT issuing any Dgraph query.
    try:
        encoder = get_encoder()
    except ImportError as exc:
        _err_console.print(_EMBED_EXTRA_HINT)
        raise typer.Exit(code=1) from exc

    # Embed the semantic description into a single query vector.
    vectors = encoder([semantic_text])
    query_vector = list(vectors[0])

    # The positional query contributes parametric/package filters only — its free
    # text is NOT embedded (the --semantic text drives the embedding).
    parsed = parse_query(query) if query.strip() else None

    # Thread --package into the hybrid parsed filters (build_semantic_dql reads the
    # package from parsed.package). Reject the same package-given-twice collision
    # as the lexical path, before the client is built.
    if package_flag is not None:
        if parsed is not None and parsed.package is not None:
            _err_console.print(_PACKAGE_TWICE_ERROR)
            raise typer.Exit(code=1)
        base = parsed if parsed is not None else parse_query("")
        parsed = ParsedQuery(
            quantities=base.quantities,
            package=package_flag,
            text_tokens=base.text_tokens,
            raw_query=base.raw_query,
        )

    stub = None
    try:
        client, stub = _build_dgraph_client()
        query_text, variables = build_semantic_dql(
            query_vector,
            limit,
            parsed=parsed,
            manufacturer=manufacturer,
            category=category,
            min_stock=min_stock,
            is_basic=is_basic,
            max_price=max_price,
        )
        data = _run_block_query(client, query_text, variables)
        result = rank_results(
            data, parsed if parsed is not None else parse_query(""), sort=sort
        )
    except typer.Exit:
        raise
    except Exception as exc:
        _err_console.print(_DB_QUERY_ERROR)
        raise typer.Exit(code=1) from exc
    finally:
        if stub is not None:
            stub.close()

    envelope_parsed = parsed if parsed is not None else parse_query("")

    # Under --json the empty-result path emits the empty envelope and NEVER the
    # human "run `partgraph embed` first" hint (AC-SF-27): the short-circuit that
    # prints _NO_EMBEDDINGS_HINT on the human path is bypassed entirely.
    if not result.rows:
        if json_output:
            _emit_search_json(result, envelope_parsed)
            return
        _console.print(_NO_EMBEDDINGS_HINT)
        return

    if json_output:
        _emit_search_json(result, envelope_parsed)
    else:
        render_search_results(
            result,
            envelope_parsed,
            _console,
            no_truncate=no_truncate,
        )


@app.command()
def show(
    mpn: str = typer.Argument(
        ...,
        help="Manufacturer part number to look up, e.g. 'MAX232'.",
    ),
) -> None:
    """Show full detail for a single part and its related parts (by MPN).

    Looks the part up by its normalised MPN and prints manufacturer, package,
    category, stock, promoted key parameters, the long-tail attributes, all
    datasheet URLs and related parts found by MPN similarity. This is a
    read-only operation; it never modifies the database.
    """
    from partgraph.normalize.model import normalize_mpn  # noqa: PLC0415
    from partgraph.query.dql_builder import build_show_dql  # noqa: PLC0415
    from partgraph.query.renderer import render_show_result  # noqa: PLC0415

    mpn_norm = normalize_mpn(mpn)

    stub = None
    try:
        client, stub = _build_dgraph_client()
        query_text, variables = build_show_dql(mpn_norm)
        data = _run_block_query(client, query_text, variables)
    except typer.Exit:
        raise
    except Exception as exc:
        _err_console.print(_DB_QUERY_ERROR)
        raise typer.Exit(code=1) from exc
    finally:
        if stub is not None:
            stub.close()

    part_block = data.get("part", []) or []
    if not part_block:
        _console.print(f"Part '{mpn}' not found.")
        raise typer.Exit(code=0)

    part = part_block[0]
    part["_related"] = data.get("related", []) or []
    render_show_result(part, _console)


# ---------------------------------------------------------------------------
# embed command (read parts, generate embeddings, write back by uid)
# ---------------------------------------------------------------------------

#: Maximum Part nodes fetched from Dgraph in a single embedding selection query.
#: The default pydgraph/gRPC receive limit is 4 MiB; fetching tens of thousands
#: of parts in one response easily exceeds it. Keep read pages comfortably below
#: that limit and let the embed command loop over pages.
_EMBED_SELECT_PAGE_SIZE = 10_000

#: Shape of a real Dgraph uid (``0x`` + hex digits). The embed pagination cursor
#: is validated against this before it is interpolated into a DQL ``after:``
#: clause: a missing or malformed uid is excluded from cursor computation and
#: never reaches query text (validate-before-interpolate; mirrors
#: partgraph.query.dql_builder's ADR-INJECT convention; ADR-0010).
_UID_RE = re.compile(r"^0x[0-9a-fA-F]+$")

#: Informational (NOT error) notice printed when the keyset cursor fails to
#: advance between pages — the defensive guard against re-fetching the same rows
#: forever. Path-free and deliberately distinct from :data:`_EMBED_DB_ERROR`
#: (this is not a failure; the run keeps whatever it embedded so far; ADR-0010).
_EMBED_CURSOR_STALL = (
    "pagination cursor did not advance; stopping early to avoid re-fetching."
)


def _page_max_uid(parts: list) -> str | None:
    """Return the numerically-largest valid uid among *parts*, or ``None``.

    Only uids matching :data:`_UID_RE` are considered; a missing or malformed uid
    is excluded (validate-before-interpolate — it must never become a raw
    ``after:`` cursor). The maximum is taken NUMERICALLY via ``int(uid, 16)``,
    never lexicographically: ``max("0x9", "0x10")`` as strings wrongly yields
    ``"0x9"`` (the character ``'9'`` sorts after ``'1'``). The winning uid's
    ORIGINAL string is returned so its exact ``0x...`` form is preserved for the
    next query's cursor.
    """
    valid = [
        part.uid
        for part in parts
        if isinstance(getattr(part, "uid", None), str) and _UID_RE.match(part.uid)
    ]
    if not valid:
        return None
    return max(valid, key=lambda uid: int(uid, 16))


def _select_parts_for_embed(client, limit: int | None, *, after: str | None = None) -> list:
    """Select one page of Part nodes without embeddings via a READ-ONLY query.

    Returns a list of namespace objects exposing the fields
    :func:`partgraph.embed.build_embed_text` needs (``uid``/``xid``/
    ``description``/``category``/``package``/``tags``). The transaction is
    ``read_only=True`` and always discarded — selection never mutates.

    When *after* is a valid uid it is emitted as a keyset cursor
    (``after: <uid>``) so the next page starts strictly past the previous page's
    max uid. That cursor — not ``@filter(NOT has(embedding))`` — is what
    guarantees forward progress: permanently skip-only parts (no xid / no embed
    text) never gain an embedding, so the filter alone would re-select them
    forever (ADR-0010). The first page (*after* is ``None``) OMITS the clause
    entirely, staying byte-identical to the pre-cursor query. An *after* value
    that fails :data:`_UID_RE` is dropped rather than interpolated raw
    (validate-before-interpolate; mirrors dql_builder's ADR-INJECT convention).
    """
    from types import SimpleNamespace  # noqa: PLC0415

    first = limit if limit is not None else _EMBED_SELECT_PAGE_SIZE
    first = max(1, min(int(first), _EMBED_SELECT_PAGE_SIZE))
    # Validate-before-interpolate: only a well-formed uid may reach query text.
    after_clause = f", after: {after}" if after is not None and _UID_RE.match(after) else ""
    query = (
        f"{{ q(func: type(Part), first: {first}{after_clause}) @filter(NOT has(embedding)) {{ "
        "uid xid description stock "
        "in_category { name } in_package { name } tagged { name } "
        "} }"
    )
    data = _run_block_query(client, query, {})
    parts = []
    for raw in data.get("q", []) or []:
        if not isinstance(raw, dict):
            continue
        category = (raw.get("in_category") or [{}])
        package = (raw.get("in_package") or [{}])
        tags = [
            t.get("name")
            for t in (raw.get("tagged") or [])
            if isinstance(t, dict) and t.get("name")
        ]
        parts.append(
            SimpleNamespace(
                uid=raw.get("uid"),
                xid=raw.get("xid"),
                description=raw.get("description"),
                category=category[0].get("name") if category else None,
                package=package[0].get("name") if package else None,
                tags=tags,
            )
        )
    return parts


def _embed_all_pages(
    client,
    *,
    encoder,
    controller,
    remaining: int | None,
    progress_bar,
) -> int:
    """Embed every eligible part across cursor-paged selection queries.

    Loops selection pages under a uid keyset cursor (``after:``) so each page
    starts strictly past the previous page's max uid — this is what guarantees
    forward progress past permanently skip-only parts that
    ``@filter(NOT has(embedding))`` would otherwise re-select forever (ADR-0010).

    ``remaining`` bounds the run. A finite ``int`` caps it at that many rows; a
    ``remaining`` of ``None`` means *unbounded* — drive to exhaustion over the
    whole eligible catalogue (a no-``--limit`` run; ADR-0011, superseding
    ADR-0010's repeated-runs model).

    The loop terminates on ANY of: (a) a zero-row page, (b) a short page
    (fewer rows than requested), (c) a cursor that fails to strictly advance
    (defensive guard — emits a path-free stall notice), or (d) ``remaining``
    reaching 0. Condition (d) applies to BOUNDED runs only: when ``remaining``
    is ``None`` there is no countdown, so termination rests entirely on
    (a)/(b)/(c). The cursor tracks the max uid *selected*, never the count
    *embedded*, so a full page of skip-only parts still advances past its block.

    Memory profile is per-page regardless of ``remaining``: each page is
    selected, embedded and released before the next is fetched — nothing
    accumulates across pages, so an unbounded run costs no more resident memory
    than a bounded one.

    Returns the total number of parts embedded (written) across all pages.
    """
    from partgraph.embed import embed_write  # noqa: PLC0415

    embedded_total = 0
    selected_total = 0
    target_total = remaining
    task = progress_bar.add_task("Embedding parts", total=target_total)

    def _on_progress(done: int, total_count: int) -> None:
        progress_bar.update(
            task,
            completed=selected_total + done,
            total=target_total or total_count or None,
        )

    last_uid: str | None = None  # uid keyset cursor; None on page 1.
    while remaining is None or remaining > 0:
        page_limit = (
            _EMBED_SELECT_PAGE_SIZE
            if remaining is None
            else min(remaining, _EMBED_SELECT_PAGE_SIZE)
        )
        parts = _select_parts_for_embed(client, page_limit, after=last_uid)
        if not parts:
            break  # (a) no more rows match the filter.

        page_size = len(parts)
        # Cursor = the max uid SELECTED this page, never the count embedded: a
        # full page of permanently skip-only parts must still advance past its
        # block, or it would sticky-loop forever.
        page_max_uid = _page_max_uid(parts)

        # (c) Defensive guard: on any page after the first, the cursor MUST
        # strictly advance numerically. A max uid that does not exceed the
        # previous cursor means the server re-served rows we already processed —
        # stop rather than re-fetch them forever.
        if last_uid is not None and (
            page_max_uid is None or int(page_max_uid, 16) <= int(last_uid, 16)
        ):
            _console.print(_EMBED_CURSOR_STALL)
            break

        summary = embed_write(
            iter(parts),
            client,
            encoder=encoder,
            controller=controller,
            progress=_on_progress,
        )
        embedded_total += int(summary.get("embedded", 0) or 0)
        selected_total += page_size
        progress_bar.update(task, completed=selected_total)

        if remaining is not None:
            remaining -= page_size  # (d) bounded run: count each row exactly once.
        if page_size < page_limit:
            break  # (b) short page: fewer rows than asked means no more.

        # Advance the cursor past this page. A page with no valid uid yields no
        # cursor, so stop rather than re-fetch page 1 forever.
        if page_max_uid is None:
            break
        last_uid = page_max_uid

    return embedded_total


# ---------------------------------------------------------------------------
# embed --changed: incremental re-embedding reconcile pass (issue #11 PR 4;
# ADR-0015). Walks the has(embedding) partition (disjoint from the missing
# pass's NOT has(embedding) partition) and re-embeds only parts whose source
# text drifted, backfilling content hashes for parts embedded before the hash
# predicate existed. Reuses the embed section's uid-cursor primitives
# (_UID_RE, _page_max_uid, _EMBED_SELECT_PAGE_SIZE, _EMBED_CURSOR_STALL) — same
# pipeline — rather than copying them.
# ---------------------------------------------------------------------------


def _select_parts_for_reembed(
    client, limit: int | None, *, after: str | None = None
) -> list:
    """Select one page of already-embedded Part nodes via a READ-ONLY query.

    Mirrors :func:`_select_parts_for_embed` but roots on
    ``@filter(has(embedding))`` (the complement partition — parts already
    embedded) and additionally PROJECTS the stored ``embed_text_hash`` plus the
    ``build_embed_text`` source fields. The reconcile pass recomputes each part's
    embed text and its sha256 CLIENT-SIDE and compares that to the stored hash in
    Python, so the query NEVER filters or looks up by a hash VALUE (no
    ``eq(embed_text_hash, ...)`` and no hash literal in the query text — the
    predicate is index-free by design; ADR-0015).

    The transaction is ``read_only=True`` and always discarded — selection never
    mutates. When *after* is a valid uid it is emitted as a keyset cursor
    (``after: <uid>``); the first page (*after* is ``None``) omits it entirely.
    An *after* value that fails :data:`_UID_RE` is dropped rather than
    interpolated raw (validate-before-interpolate; the same guard, and the same
    ``_UID_RE`` domain, as :func:`_select_parts_for_embed`).
    """
    from types import SimpleNamespace  # noqa: PLC0415

    first = limit if limit is not None else _EMBED_SELECT_PAGE_SIZE
    first = max(1, min(int(first), _EMBED_SELECT_PAGE_SIZE))
    # Validate-before-interpolate: only a well-formed uid may reach query text.
    after_clause = f", after: {after}" if after is not None and _UID_RE.match(after) else ""
    query = (
        f"{{ q(func: type(Part), first: {first}{after_clause}) @filter(has(embedding)) {{ "
        "uid embed_text_hash description "
        "in_category { name } in_package { name } tagged { name } "
        "} }"
    )
    data = _run_block_query(client, query, {})
    parts = []
    for raw in data.get("q", []) or []:
        if not isinstance(raw, dict):
            continue
        category = (raw.get("in_category") or [{}])
        package = (raw.get("in_package") or [{}])
        tags = [
            t.get("name")
            for t in (raw.get("tagged") or [])
            if isinstance(t, dict) and t.get("name")
        ]
        parts.append(
            SimpleNamespace(
                uid=raw.get("uid"),
                embed_text_hash=raw.get("embed_text_hash"),
                description=raw.get("description"),
                category=category[0].get("name") if category else None,
                package=package[0].get("name") if package else None,
                tags=tags,
            )
        )
    return parts


def _reconcile_page(page_parts, client, *, encoder, controller) -> int:
    """Dispatch one reconcile page across the ADR-0015 D2 cases and write it.

    For each part carrying a valid uid the embed text is rebuilt and, when
    non-empty (the empty-text precondition — skip in ANY hash state, checked
    BEFORE the case split), its stored hash is compared CLIENT-SIDE against a
    freshly computed one:

    - no stored hash  -> case (ii):  backfill ``{uid, embed_text_hash}`` (no encoder).
    - stored == fresh -> case (iii): skip (no mutate, no encoder).
    - stored != fresh -> case (iv):  re-embed ``{uid, embedding, embed_text_hash}``.

    Case (i) — a part with no embedding at all — is out of scope here: the
    selection roots on ``has(embedding)`` and the existing missing pass owns it.
    Both write paths live in :mod:`partgraph.embed` (:func:`stamp_hashes` /
    :func:`reembed_write`), which write by the already-resolved uid directly (no
    xid round-trip) — cli.py never mutates directly. Returns the number of rows
    written (backfilled + re-embedded).
    """
    from partgraph.embed import (  # noqa: PLC0415
        build_embed_text,
        compute_embed_text_hash,
        reembed_write,
        stamp_hashes,
    )

    backfill: list[tuple[str, str]] = []  # (uid, text) — case (ii)
    reembed: list[tuple[str, str]] = []   # (uid, text) — case (iv)
    for part in page_parts:
        uid = getattr(part, "uid", None)
        if not isinstance(uid, str) or not _UID_RE.match(uid):
            continue  # no resolvable uid -> cannot write back
        text = build_embed_text(part)
        if not text:
            continue  # empty-text precondition: skip in ANY hash state
        stored = getattr(part, "embed_text_hash", None)
        if not isinstance(stored, str) or not stored:
            backfill.append((uid, text))            # (ii)
            continue
        if stored == compute_embed_text_hash(text):
            continue                                # (iii)
        reembed.append((uid, text))                 # (iv)

    written = 0
    if backfill:
        written += stamp_hashes(client, backfill)
    if reembed:
        written += reembed_write(reembed, client, encoder=encoder, controller=controller)
    return written


def _reembed_all_pages(
    client,
    *,
    encoder,
    controller,
    remaining: int | None,
    progress_bar,
) -> int:
    """Reconcile already-embedded parts across cursor-paged selection queries.

    The reconcile pass of ``partgraph embed --changed``: it walks the
    ``has(embedding)`` partition (disjoint from the missing pass's
    ``NOT has(embedding)`` partition) under a uid keyset cursor and, per page,
    dispatches the ADR-0015 D2 cases via :func:`_reconcile_page`.

    ``remaining`` bounds the run exactly as in :func:`_embed_all_pages`: a finite
    ``int`` caps it at that many rows; ``None`` means unbounded (drive to
    exhaustion). The loop terminates on ANY of: (a) a zero-row page, (b) a page
    yielding fewer rows than the next page could still consume, (c) a cursor that
    fails to strictly advance (defensive guard — emits the path-free
    :data:`_EMBED_CURSOR_STALL` notice), or (d) ``remaining`` reaching 0. The
    cursor tracks the max uid *selected*, never the count written, so a full page
    of case-(iii) skip rows still advances past its block.

    Termination (b) compares the page yield against the NEXT page's target
    (``min(remaining, PAGE_SIZE)`` after the countdown), not the current page's
    request: a bounded run whose page exactly satisfies the shrinking budget must
    not be mistaken for a short (end-of-data) page. For an unbounded run this
    reduces to the plain "fewer rows than a full page" test. Memory is per-page:
    each page is selected, reconciled and released before the next is fetched.

    Returns the number of parts written (backfilled + re-embedded) across all
    pages.
    """
    reembedded_total = 0
    selected_total = 0
    target_total = remaining
    task = progress_bar.add_task("Reconciling embeddings", total=target_total)

    last_uid: str | None = None  # uid keyset cursor; None on page 1.
    while remaining is None or remaining > 0:
        page_limit = (
            _EMBED_SELECT_PAGE_SIZE
            if remaining is None
            else min(remaining, _EMBED_SELECT_PAGE_SIZE)
        )
        parts = _select_parts_for_reembed(client, page_limit, after=last_uid)
        if not parts:
            break  # (a) no more embedded rows match the filter.

        page_size = len(parts)
        # Cursor = the max uid SELECTED this page, never the count written: a
        # full page of case-(iii) skip rows must still advance past its block.
        page_max_uid = _page_max_uid(parts)

        # (c) Defensive guard: after the first page the cursor MUST strictly
        # advance numerically, or the server re-served rows we already handled.
        if last_uid is not None and (
            page_max_uid is None or int(page_max_uid, 16) <= int(last_uid, 16)
        ):
            _console.print(_EMBED_CURSOR_STALL)
            break

        reembedded_total += _reconcile_page(
            parts, client, encoder=encoder, controller=controller,
        )
        selected_total += page_size
        progress_bar.update(task, completed=selected_total)

        if remaining is not None:
            remaining -= page_size  # (d) bounded run: count each row exactly once.

        # (b) Short page: fewer rows than the NEXT page could still consume.
        page_budget = (
            _EMBED_SELECT_PAGE_SIZE
            if remaining is None
            else min(remaining, _EMBED_SELECT_PAGE_SIZE)
        )
        if page_size < page_budget:
            break

        # Advance the cursor past this page. A page with no valid uid yields no
        # cursor, so stop rather than re-fetch page 1 forever.
        if page_max_uid is None:
            break
        last_uid = page_max_uid

    return reembedded_total


@app.command()
def embed(
    limit: str | None = typer.Option(
        None,
        "--limit",
        help=(
            "Limit embedding to the first N parts (development/testing; the full "
            "run embeds the whole catalogue). Must be a positive integer."
        ),
    ),
    changed: bool = typer.Option(
        False,
        "--changed",
        help=(
            "Also reconcile already-embedded parts: re-embed parts whose source "
            "text changed since they were last embedded and backfill missing "
            "content hashes, BEFORE the usual missing-only pass (issue #11 PR 4)."
        ),
    ),
) -> None:
    """Generate semantic embeddings for parts and write them back to Dgraph.

    Reads parts read-only, builds an embedding text per part (description +
    category + package + tags), encodes them with the sentence-transformers
    model and writes the embedding back by uid (uid + embedding + content hash
    payload only — never a new node). This is a heavy, one-off run; embeddings
    persist in the graph and power `partgraph search --semantic`.

    With ``--changed`` a reconcile pass runs FIRST over already-embedded parts
    (issue #11 PR 4; ADR-0015): parts whose source text drifted since they were
    embedded are re-embedded and parts missing a content hash are backfilled,
    then the usual missing-only pass runs. The two passes cover the disjoint
    ``has(embedding)`` / ``NOT has(embedding)`` partitions. Plain ``embed`` (no
    ``--changed``) is unchanged: the missing-only pass alone.

    Requires the optional [embed] extra (pip install -e ".[embed]"). Errors are
    path-free: a missing extra or a stopped database exits 1 with a clear hint.
    """
    import time as _time  # noqa: PLC0415

    from rich.progress import (  # noqa: PLC0415
        BarColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
    )

    from partgraph.util.resources import (  # noqa: PLC0415
        ResourceController,
        get_system_reader,
    )

    parsed_limit = _validate_limit(limit)

    # Acquire the encoder first: a missing [embed] extra must exit WITHOUT
    # touching the database (no selection query, no mutation).
    try:
        encoder = get_encoder()
    except ImportError as exc:
        _err_console.print(_EMBED_EXTRA_HINT)
        raise typer.Exit(code=1) from exc

    controller = ResourceController()
    # Attach a live reader so the controller paces the run off real system load.
    controller.reader = get_system_reader()  # type: ignore[attr-defined]

    start = _time.monotonic()
    stub = None
    reembedded_total = 0
    embedded_total = 0
    try:
        client, stub = _build_dgraph_client()
        # None (no --limit) drives the pages unbounded — to exhaustion over the
        # whole eligible catalogue; a finite --limit N bounds each pass to N rows
        # exactly, never re-capped at any default (ADR-0011). --changed runs the
        # reconcile pass FIRST, then the (unchanged) missing pass (ADR-0015).
        remaining = parsed_limit
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=_console,
            transient=True,
        ) as progress_bar:
            if changed:
                reembedded_total = _reembed_all_pages(
                    client,
                    encoder=encoder,
                    controller=controller,
                    remaining=remaining,
                    progress_bar=progress_bar,
                )
            embedded_total = _embed_all_pages(
                client,
                encoder=encoder,
                controller=controller,
                remaining=remaining,
                progress_bar=progress_bar,
            )
    except typer.Exit:
        raise
    except Exception as exc:
        # Any DB/runtime failure: never interpolate the exception, so no internal
        # path can leak. Re-raised as a clean CLI exit.
        _err_console.print(_EMBED_DB_ERROR)
        raise typer.Exit(code=1) from exc
    finally:
        if stub is not None:
            stub.close()

    elapsed = _time.monotonic() - start
    if changed:
        _console.print(
            f"[green]Reconciled {reembedded_total} changed parts.[/green]"
        )
    _console.print(
        f"[green]Embedded {embedded_total} parts in {elapsed:.1f}s.[/green]"
    )


# ---------------------------------------------------------------------------
# refresh-links command (HTTP-check datasheet URLs, stamp freshness, auto-purge)
# ---------------------------------------------------------------------------

#: Fixed, path-free error shown when the refresh-links run fails (DB down /
#: mutation error). The raw exception is never interpolated so no internal path
#: leaks; deliberately distinct from :data:`_EMBED_DB_ERROR` so the text names
#: the right operation while still hinting `partgraph db up`.
_REFRESH_DB_ERROR = (
    "[red]Error:[/red] could not refresh datasheet links. Is the database "
    "running? Start it with `partgraph db up`."
)

#: Maximum Datasheet nodes selected for a link check when no --limit is given.
#: Bounds a single run; the full catalogue is covered across repeated/cron runs.
_REFRESH_SELECT_DEFAULT = 200_000

#: Maximum Datasheet nodes fetched from Dgraph in a single selection page. Kept
#: comfortably below the gRPC message ceiling; the run loops over pages.
_REFRESH_SELECT_PAGE_SIZE = 10_000

#: Shape of a real Dgraph uid (``0x`` + hex). COPIED from the embed section on
#: purpose (never imported/aliased from it) so the refresh selection path stays
#: fully decoupled from the embed pipeline the design forbids modifying. The
#: keyset cursor is validated against this before it can reach a DQL ``after:``
#: clause (validate-before-interpolate; ADR-0010 / dql_builder's ADR-INJECT).
_REFRESH_UID_RE = re.compile(r"^0x[0-9a-fA-F]+$")

#: Informational (NOT error) notice printed when the keyset cursor fails to
#: advance between pages — the defensive guard against re-fetching the same rows
#: forever. Path-free and distinct from :data:`_REFRESH_DB_ERROR` (this is not a
#: failure; the run keeps whatever it checked so far; mirrors
#: :data:`_EMBED_CURSOR_STALL`).
_REFRESH_CURSOR_STALL = (
    "pagination cursor did not advance; stopping early to avoid re-fetching."
)

#: Per-host politeness interval (seconds) between two datasheet checks against
#: the SAME host — most datasheet URLs share a handful of hosts (lcsc.com), so a
#: bounded delay keeps the checker a good citizen without adding a config knob.
_REFRESH_HOST_MIN_INTERVAL = 0.5

#: The summary keys the refresh run aggregates across pages.
_REFRESH_SUMMARY_KEYS = ("checked", "alive", "dead", "purged")


def _utcnow() -> datetime:
    """Return the current UTC time (patchable "now" seam).

    Mirrors the reason ``get_encoder`` is imported at module level "ON PURPOSE
    ... the test suite patches it": the refresh-links tests replace this with a
    fixed instant so the staleness cutoff ``T = now - stale_days`` is
    deterministic and never reads the real wall clock.
    """
    return datetime.now(UTC)


def _build_http_client():
    """Create an ``httpx.Client`` for datasheet link checking (lazy import).

    httpx is imported lazily (mirroring :mod:`partgraph.ingest.fetch`) so CLI
    commands that never check links never construct an HTTP stack. Redirects are
    deliberately NOT followed: a 3xx is observed and classified as-is (the link
    is served), matching the leaf's 2xx/3xx = alive policy. The unit suite
    patches this factory so no real socket is ever opened.
    """
    import httpx  # noqa: PLC0415 — lazy import keeps the HTTP stack optional.

    return httpx.Client(follow_redirects=False)


def _close_http_client(http_client) -> None:
    """Close *http_client* if it exposes a ``close`` method (best effort)."""
    close = getattr(http_client, "close", None)
    if callable(close):
        close()


def _refresh_page_max_uid(rows: list) -> str | None:
    """Return the numerically-largest valid uid among *rows*, or ``None``.

    Only uids matching :data:`_REFRESH_UID_RE` are considered; a missing or
    malformed uid is excluded (validate-before-interpolate — it must never become
    a raw ``after:`` cursor). The maximum is taken NUMERICALLY via
    ``int(uid, 16)``, never lexicographically (``max("0x9", "0x10")`` as strings
    wrongly yields ``"0x9"``). The winning uid's ORIGINAL string is returned so
    its exact ``0x...`` form is preserved for the next query's cursor.

    A dedicated copy of the embed cursor-max logic — the embed ``_page_max_uid``
    is deliberately NOT called, keeping the refresh path decoupled from the embed
    pipeline the design forbids modifying.
    """
    valid = [
        row.uid
        for row in rows
        if isinstance(getattr(row, "uid", None), str) and _REFRESH_UID_RE.match(row.uid)
    ]
    if not valid:
        return None
    return max(valid, key=lambda uid: int(uid, 16))


def _select_datasheets_for_refresh(
    client,
    limit: int | None,
    *,
    stale_days: int = 30,
    after: str | None = None,
) -> list:
    """Select one page of stale Datasheet nodes via a READ-ONLY query.

    Roots at ``type(Datasheet)`` and carries the staleness filter
    ``@filter(NOT has(verified_at) OR lt(verified_at, "<T>"))`` where
    ``T = _utcnow() - stale_days`` — a datasheet is (re)checked only when it was
    never verified or was verified before the cutoff (cross-run idempotency).
    This is an INDEPENDENT selection: it never roots at ``type(Part)``, never
    carries ``NOT has(embedding)``, and never calls the embed selection helper.

    When *after* is a valid uid it is emitted as a keyset cursor (``after:
    <uid>``) so the next page starts strictly past the previous page's max uid
    (intra-run forward progress); the first page OMITS the clause. An *after*
    value that fails :data:`_REFRESH_UID_RE` is dropped rather than interpolated
    raw. The transaction is ``read_only=True`` and always discarded.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    first = limit if limit is not None else _REFRESH_SELECT_PAGE_SIZE
    first = max(1, min(int(first), _REFRESH_SELECT_PAGE_SIZE))
    cutoff = format_verified_at(_utcnow() - timedelta(days=stale_days))
    # Validate-before-interpolate: only a well-formed uid may reach query text.
    after_clause = (
        f", after: {after}"
        if after is not None and _REFRESH_UID_RE.match(after)
        else ""
    )
    query = (
        f"{{ q(func: type(Datasheet), first: {first}{after_clause}) "
        f'@filter(NOT has(verified_at) OR lt(verified_at, "{cutoff}")) {{ '
        "uid url http_status fail_count "
        "} }"
    )
    data = _run_block_query(client, query, {})
    rows = []
    for raw in data.get("q", []) or []:
        if not isinstance(raw, dict):
            continue
        rows.append(
            SimpleNamespace(
                uid=raw.get("uid"),
                url=raw.get("url"),
                http_status=raw.get("http_status"),
                fail_count=raw.get("fail_count"),
            )
        )
    return rows


def _refresh_all_pages(  # noqa: PLR0913 — one keyword-only seam per orchestration knob.
    client,
    *,
    http_client,
    max_failures: int,
    timeout: float,
    stale_days: int,
    remaining: int,
    progress_bar,
) -> dict:
    """Check every stale datasheet link across cursor-paged selection queries.

    Loops selection pages under a uid keyset cursor (``after:``), mirroring the
    embed loop's termination conditions — (a) a zero-row page, (b) a short page,
    (c) a cursor that fails to strictly advance (emits a path-free stall notice),
    or (d) ``remaining`` reaching 0 — but never calls the embed helpers, keeping
    the two paths decoupled. A single
    :class:`~partgraph.refresh.links.HostRateLimiter` is constructed once and
    threaded into every page's write so per-host politeness state persists across
    pages; a :class:`~partgraph.util.resources.ResourceController` paces local
    load between pages (as embed). Returns the aggregated
    ``{"checked","alive","dead","purged"}`` summary.
    """
    import time  # noqa: PLC0415 — monotonic clock/sleep for the rate limiter.

    from partgraph.util.resources import (  # noqa: PLC0415
        ResourceController,
        get_system_reader,
    )

    totals = dict.fromkeys(_REFRESH_SUMMARY_KEYS, 0)
    task = progress_bar.add_task("Checking datasheet links", total=remaining)

    controller = ResourceController()
    # Attach a live reader so the controller paces the run off real system load.
    controller.reader = get_system_reader()  # type: ignore[attr-defined]

    # One limiter per run: per-host timing must persist across pages.
    rate_limiter = HostRateLimiter(
        _REFRESH_HOST_MIN_INTERVAL, clock=time.monotonic, sleep=time.sleep
    )

    def _on_purge(datasheet_uid: str, fail_count: int, parts_unlinked: int) -> None:
        # Path-free destructive notice: plain uid + ints only, no exception/path.
        _console.print(
            f"[yellow]Purged dead datasheet[/yellow] {datasheet_uid} after "
            f"{fail_count} consecutive failures; unlinked from "
            f"{parts_unlinked} part(s)."
        )

    selected_total = 0
    last_uid: str | None = None  # uid keyset cursor; None on page 1.
    while remaining > 0:
        page_limit = min(remaining, _REFRESH_SELECT_PAGE_SIZE)
        rows = _select_datasheets_for_refresh(
            client, page_limit, stale_days=stale_days, after=last_uid
        )
        if not rows:
            break  # (a) no more stale datasheets.

        page_size = len(rows)
        page_max_uid = _refresh_page_max_uid(rows)

        # (c) Defensive guard: the cursor MUST strictly advance after page 1. A
        # max uid that does not exceed the previous cursor means the server
        # re-served processed rows — stop rather than re-fetch them forever.
        if last_uid is not None and (
            page_max_uid is None or int(page_max_uid, 16) <= int(last_uid, 16)
        ):
            _console.print(_REFRESH_CURSOR_STALL)
            break

        summary = refresh_links_write(
            iter(rows),
            client,
            http_client=http_client,
            clock=_utcnow,
            max_failures=max_failures,
            timeout=timeout,
            rate_limiter=rate_limiter,
            on_purge=_on_purge,
        )
        for key, value in summary.items():
            if key in totals:
                totals[key] += int(value or 0)

        selected_total += page_size
        progress_bar.update(task, completed=selected_total)
        remaining -= page_size  # (d) count each returned row exactly once.
        if page_size < page_limit:
            break  # (b) short page: fewer rows than asked means no more.
        if page_max_uid is None:
            break

        last_uid = page_max_uid
        # Pace local resources before the next page (healthy box -> no-op).
        controller.wait_until_healthy(reader=controller.reader, sleep=time.sleep)

    return totals


@app.command("refresh-links")
def refresh_links(
    stale_days: int = typer.Option(
        30,
        "--stale-days",
        help=(
            "Only (re)check datasheet links whose verified_at is missing or "
            "older than N days."
        ),
    ),
    limit: str | None = typer.Option(
        None,
        "--limit",
        help=(
            "Limit to the first N datasheets (development/testing; the full run "
            "covers the whole catalogue across runs). Must be a positive integer."
        ),
    ),
    max_failures: int = typer.Option(
        3,
        "--max-failures",
        help=(
            "Auto-purge a datasheet link after N consecutive failed checks "
            "(drops the datasheet edge from every referencing part and the "
            "Datasheet node). Must be a positive integer."
        ),
    ),
    timeout: float = typer.Option(
        10.0,
        "--timeout",
        help="Per-request HTTP timeout (seconds) for each datasheet link check.",
    ),
) -> None:
    """HTTP-check datasheet links, stamp freshness, and auto-purge dead links.

    Pages stale Datasheet nodes (uid keyset cursor + a ``verified_at`` staleness
    filter), HTTP-checks each URL (HEAD, GET fallback on 405/501) and writes a
    narrow ``verified_at``/``http_status``/``fail_count`` update back by uid —
    ``fail_count`` reset to 0 when alive, else incremented. A link that reaches
    ``--max-failures`` consecutive failures is auto-purged in a separate
    transaction, with a path-free destructive notice.

    This is a one-shot command bounded per run (schedule it via cron/systemd; see
    PR 3). Errors are path-free: a stopped database exits 1 with a clear hint.
    """
    from rich.progress import (  # noqa: PLC0415
        BarColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
    )

    parsed_limit = _validate_limit(limit)
    if max_failures <= 0:
        _err_console.print(
            "[red]Error:[/red] --max-failures must be a positive integer."
        )
        raise typer.Exit(code=1)
    if timeout <= 0:
        _err_console.print(
            "[red]Error:[/red] --timeout must be a positive number."
        )
        raise typer.Exit(code=1)

    http_client = _build_http_client()
    stub = None
    totals = dict.fromkeys(_REFRESH_SUMMARY_KEYS, 0)
    try:
        client, stub = _build_dgraph_client()
        remaining = parsed_limit if parsed_limit is not None else _REFRESH_SELECT_DEFAULT
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=_console,
            transient=True,
        ) as progress_bar:
            totals = _refresh_all_pages(
                client,
                http_client=http_client,
                max_failures=max_failures,
                timeout=timeout,
                stale_days=stale_days,
                remaining=remaining,
                progress_bar=progress_bar,
            )
    except typer.Exit:
        raise
    except Exception as exc:
        # Any DB/runtime failure (selection read, write-back or purge mutation):
        # never interpolate the exception, so no internal path can leak. Re-raised
        # as a clean CLI exit.
        _err_console.print(_REFRESH_DB_ERROR)
        raise typer.Exit(code=1) from exc
    finally:
        if stub is not None:
            stub.close()
        _close_http_client(http_client)

    _console.print(
        f"[green]Checked {totals['checked']} datasheet links:[/green] "
        f"{totals['alive']} alive, {totals['dead']} dead, "
        f"{totals['purged']} purged."
    )


# ---------------------------------------------------------------------------
# refresh command (re-check LCSC stock/price/is_basic, stamp freshness)
# ---------------------------------------------------------------------------

#: Fixed, path-free error shown when the stock/price refresh run fails against
#: Dgraph (DB down / selection or write-back mutation error). The raw exception
#: is never interpolated so no internal path leaks; deliberately TEXTUALLY
#: DISTINCT from both :data:`_EMBED_DB_ERROR` and :data:`_REFRESH_DB_ERROR` (the
#: refresh-links error) so the message names the right operation while still
#: hinting `partgraph db up`.
_REFRESH_STOCK_DB_ERROR = (
    "[red]Error:[/red] could not refresh part stock/price. Is the database "
    "running? Start it with `partgraph db up`."
)

#: Fixed, path-free error shown when the local source snapshot cannot be read
#: (a corrupt/unreadable cached SQLite file, or an unrecognized schema). The raw
#: exception and the absolute file path are never interpolated; --fetch is hinted
#: as the remedy for a corrupt cache.
_REFRESH_STOCK_SOURCE_ERROR = (
    "[red]Error:[/red] could not read the component source database. The "
    "cached file may be corrupt or incomplete; re-run with --fetch to "
    "re-download it."
)

#: Fixed, path-free error shown when a `refresh --fetch` download fails. The raw
#: exception is never interpolated so no internal path/URL detail leaks.
_REFRESH_STOCK_FETCH_ERROR = (
    "[red]Error:[/red] failed to download the component database. Check your "
    "network connection and try again."
)

#: Maximum Part nodes selected for a stock/price refresh when no --limit is
#: given. Bounds a single run; the full catalogue is covered across runs.
_REFRESH_STOCK_SELECT_DEFAULT = 200_000

#: Maximum Part nodes fetched from Dgraph in a single selection page. Kept
#: comfortably below the gRPC message ceiling; the run loops over pages. Its own
#: constant, never an alias of the embed / refresh-links page-size constants.
_REFRESH_STOCK_SELECT_PAGE_SIZE = 10_000

#: Shape of a real Dgraph uid (``0x`` + hex). COPIED (never imported/aliased)
#: from the embed / refresh-links sections so the stock-refresh selection path
#: stays fully decoupled from the pipelines the design forbids modifying. The
#: keyset cursor is validated against this before it can reach a DQL ``after:``
#: clause (validate-before-interpolate; ADR-0010 / dql_builder's ADR-INJECT).
_REFRESH_STOCK_UID_RE = re.compile(r"^0x[0-9a-fA-F]+$")

#: Informational (NOT error) notice printed when the keyset cursor fails to
#: advance between pages — the defensive guard against re-fetching the same rows
#: forever. Path-free and distinct from :data:`_REFRESH_STOCK_DB_ERROR` (this is
#: not a failure; the run keeps whatever it refreshed so far).
_REFRESH_STOCK_CURSOR_STALL = (
    "pagination cursor did not advance; stopping early to avoid re-fetching."
)

#: The summary keys the stock/price refresh run aggregates across pages.
_REFRESH_STOCK_SUMMARY_KEYS = ("checked", "matched", "absent")


def _load_stock_index(dest: Path) -> dict:
    """Parse the JLC source snapshot at *dest* into an in-memory stock index.

    Opens the SQLite source via the already-shipped
    :func:`~partgraph.sources.jlcparts.open_jlcparts_db` /
    :class:`~partgraph.sources.jlcparts.JlcpartsAdapter` (mirroring
    :func:`_stage_normalize`'s reuse) and joins the adapter's row stream into a
    ``lcsc_id -> (stock, price_usd, is_basic)`` dict via
    :func:`partgraph.refresh.stock.build_stock_index`. Built ONCE per run and
    threaded into every page's write-back.

    Any failure (a corrupt cache raising ``sqlite3.DatabaseError``, an
    unrecognized schema raising ``ValueError``, etc.) propagates to the caller,
    which catches BROADLY and renders it as a single path-free error — no raw
    exception text or absolute path is surfaced.
    """
    from partgraph.sources.jlcparts import (  # noqa: PLC0415
        JlcpartsAdapter,
        open_jlcparts_db,
    )

    conn = open_jlcparts_db(dest)
    adapter = JlcpartsAdapter(conn)
    return build_stock_index(adapter.iter_parts())


def _refresh_stock_page_max_uid(rows: list) -> str | None:
    """Return the numerically-largest valid uid among *rows*, or ``None``.

    Only uids matching :data:`_REFRESH_STOCK_UID_RE` are considered; a missing or
    malformed uid is excluded (validate-before-interpolate — it must never become
    a raw ``after:`` cursor). The maximum is taken NUMERICALLY via ``int(uid,
    16)``, never lexicographically (``max("0x9", "0x10")`` as strings wrongly
    yields ``"0x9"``). The winning uid's ORIGINAL string is returned so its exact
    ``0x...`` form is preserved for the next query's cursor.

    A dedicated copy of the cursor-max logic, kept decoupled from the pipelines
    the design forbids modifying.
    """
    valid = [
        row.uid
        for row in rows
        if isinstance(getattr(row, "uid", None), str)
        and _REFRESH_STOCK_UID_RE.match(row.uid)
    ]
    if not valid:
        return None
    return max(valid, key=lambda uid: int(uid, 16))


def _select_parts_for_refresh(
    client,
    limit: int | None,
    *,
    stale_days: int = 7,
    after: str | None = None,
) -> list:
    """Select one page of stale Part nodes (with an lcsc_id) via a READ-ONLY query.

    Roots at ``type(Part)`` and carries the EXACT parenthesized filter
    ``@filter(has(lcsc_id) AND (NOT has(stock_checked_at) OR
    lt(stock_checked_at, "<T>")))`` where ``T = _utcnow() - stale_days`` — a Part
    is (re)checked only when it carries an lcsc_id AND was either never checked or
    checked before the cutoff. The inner parentheses bind ``has(lcsc_id)`` to the
    whole staleness OR, so an lcsc_id-less Part is never selected via the OR's
    second arm. This is an INDEPENDENT selection: it never roots at
    ``type(Datasheet)``, never carries ``NOT has(embedding)``, and never calls the
    embed or link-refresh selection helpers.

    When *after* is a valid uid it is emitted as a keyset cursor (``after:
    <uid>``) so the next page starts strictly past the previous page's max uid;
    the first page OMITS the clause. An *after* value that fails
    :data:`_REFRESH_STOCK_UID_RE` is dropped rather than interpolated raw. The
    transaction is ``read_only=True`` and always discarded.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    first = limit if limit is not None else _REFRESH_STOCK_SELECT_PAGE_SIZE
    first = max(1, min(int(first), _REFRESH_STOCK_SELECT_PAGE_SIZE))
    cutoff = format_verified_at(_utcnow() - timedelta(days=stale_days))
    # Validate-before-interpolate: only a well-formed uid may reach query text.
    after_clause = (
        f", after: {after}"
        if after is not None and _REFRESH_STOCK_UID_RE.match(after)
        else ""
    )
    query = (
        f"{{ q(func: type(Part), first: {first}{after_clause}) "
        f'@filter(has(lcsc_id) AND (NOT has(stock_checked_at) OR '
        f'lt(stock_checked_at, "{cutoff}"))) {{ '
        "uid lcsc_id "
        "} }"
    )
    data = _run_block_query(client, query, {})
    rows = []
    for raw in data.get("q", []) or []:
        if not isinstance(raw, dict):
            continue
        rows.append(
            SimpleNamespace(
                uid=raw.get("uid"),
                lcsc_id=raw.get("lcsc_id"),
            )
        )
    return rows


def _refresh_stock_all_pages(
    client,
    *,
    stock_index: dict,
    stale_days: int,
    remaining: int,
    progress_bar,
) -> dict:
    """Refresh every stale Part's stock/price across cursor-paged selection queries.

    Loops selection pages under a uid keyset cursor (``after:``), terminating on
    (a) a zero-row page, (b) a short page, (c) a cursor that fails to strictly
    advance (emits a path-free stall notice), or (d) ``remaining`` reaching 0 —
    the same termination shape as the sibling paging loops, but with its own
    copied helpers so the stock-refresh path never touches the pipelines the
    design forbids modifying. The SAME pre-built *stock_index* object is threaded
    into every page's write-back (built once, never per page). A
    :class:`~partgraph.util.resources.ResourceController` paces local load between
    pages. Returns the aggregated ``{"checked","matched","absent"}`` summary.
    """
    import time  # noqa: PLC0415 — monotonic sleep for inter-page pacing.

    from partgraph.util.resources import (  # noqa: PLC0415
        ResourceController,
        get_system_reader,
    )

    totals = dict.fromkeys(_REFRESH_STOCK_SUMMARY_KEYS, 0)
    task = progress_bar.add_task("Refreshing part stock/price", total=remaining)

    controller = ResourceController()
    # Attach a live reader so the controller paces the run off real system load.
    controller.reader = get_system_reader()  # type: ignore[attr-defined]

    selected_total = 0
    last_uid: str | None = None  # uid keyset cursor; None on page 1.
    while remaining > 0:
        page_limit = min(remaining, _REFRESH_STOCK_SELECT_PAGE_SIZE)
        rows = _select_parts_for_refresh(
            client, page_limit, stale_days=stale_days, after=last_uid
        )
        if not rows:
            break  # (a) no more stale parts.

        page_size = len(rows)
        page_max_uid = _refresh_stock_page_max_uid(rows)

        # (c) Defensive guard: the cursor MUST strictly advance after page 1. A
        # max uid that does not exceed the previous cursor means the server
        # re-served processed rows — stop rather than re-fetch them forever.
        if last_uid is not None and (
            page_max_uid is None or int(page_max_uid, 16) <= int(last_uid, 16)
        ):
            _console.print(_REFRESH_STOCK_CURSOR_STALL)
            break

        summary = refresh_stock_write(
            iter(rows),
            client,
            stock_index=stock_index,
            clock=_utcnow,
        )
        for key, value in summary.items():
            if key in totals:
                totals[key] += int(value or 0)

        selected_total += page_size
        progress_bar.update(task, completed=selected_total)
        remaining -= page_size  # (d) count each returned row exactly once.
        if page_size < page_limit:
            break  # (b) short page: fewer rows than asked means no more.

        # Advance the cursor only when this page yielded a valid max uid. A real
        # Dgraph page always does; keeping the previous cursor on the (test-only)
        # all-malformed-uid case cannot lose real progress, and the stall guard
        # above still fires on the next page if the cursor fails to advance — so
        # a full page whose uids cannot form a cursor continues rather than
        # ending the run one page early.
        if page_max_uid is not None:
            last_uid = page_max_uid
        # Pace local resources before the next page (healthy box -> no-op).
        controller.wait_until_healthy(reader=controller.reader, sleep=time.sleep)

    return totals


@app.command("refresh")
def refresh(
    stale_days: int = typer.Option(
        7,
        "--stale-days",
        help=(
            "Only re-check parts whose stock_checked_at is missing or older "
            "than N days."
        ),
    ),
    limit: str | None = typer.Option(
        None,
        "--limit",
        help=(
            "Limit to the first N parts (development/testing; the full run "
            "covers the whole catalogue across runs). Must be a positive integer."
        ),
    ),
    fetch: bool = typer.Option(
        False,
        "--fetch",
        help="Download the JLCPCB/LCSC component database (~1 GB) before refreshing.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-download even if a matching cached file already exists (with --fetch).",
    ),
) -> None:
    """Refresh each part's LCSC stock/price/basic status and stamp freshness.

    Pages Part nodes carrying an lcsc_id (uid keyset cursor + a stock_checked_at
    staleness filter), looks each up in the source snapshot's stock/price index
    and writes a narrow stock/price_usd/is_basic/stock_checked_at update back by
    uid — a part absent from the snapshot this run is stamp-only, leaving its
    volatile fields untouched. The source snapshot is loaded once up front
    (optionally re-downloaded with --fetch) and reused across every page.

    This is a one-shot command bounded per run (schedule it via cron/systemd; see
    PR 3). Errors are path-free: a missing source file, a corrupt cache or a
    stopped database each exit 1 with a clear, path-free hint.
    """
    from rich.progress import (  # noqa: PLC0415
        BarColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
    )

    parsed_limit = _validate_limit(limit)
    if stale_days < 0:
        _err_console.print("[red]Error:[/red] --stale-days must not be negative.")
        raise typer.Exit(code=1)

    dest = RAW_DB_PATH
    if fetch:
        try:
            _stage_fetch(dest, force=force)
        except typer.Exit:
            raise
        except Exception as exc:
            # Path-free download-failure handling for this NEW call site: the raw
            # exception is never interpolated, so no URL/path detail can leak.
            _err_console.print(_REFRESH_STOCK_FETCH_ERROR)
            raise typer.Exit(code=1) from exc
    _require_source_file(dest, fetched=fetch)

    try:
        stock_index = _load_stock_index(dest)
    except typer.Exit:
        raise
    except Exception as exc:
        # Broad catch: a corrupt cache (sqlite3.DatabaseError), an unrecognized
        # schema (ValueError) or any other parse failure is rendered path-free —
        # never a raw exception or an absolute file path.
        _err_console.print(_REFRESH_STOCK_SOURCE_ERROR)
        raise typer.Exit(code=1) from exc

    stub = None
    totals = dict.fromkeys(_REFRESH_STOCK_SUMMARY_KEYS, 0)
    try:
        client, stub = _build_dgraph_client()
        remaining = (
            parsed_limit if parsed_limit is not None else _REFRESH_STOCK_SELECT_DEFAULT
        )
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=_console,
            transient=True,
        ) as progress_bar:
            totals = _refresh_stock_all_pages(
                client,
                stock_index=stock_index,
                stale_days=stale_days,
                remaining=remaining,
                progress_bar=progress_bar,
            )
    except typer.Exit:
        raise
    except Exception as exc:
        # Any DB/runtime failure (selection read or write-back mutation): never
        # interpolate the exception, so no internal path can leak.
        _err_console.print(_REFRESH_STOCK_DB_ERROR)
        raise typer.Exit(code=1) from exc
    finally:
        if stub is not None:
            stub.close()

    _console.print(
        f"[green]Refreshed stock/price for {totals['checked']} parts:[/green] "
        f"{totals['matched']} matched, {totals['absent']} absent."
    )


def main() -> None:
    """Console-script entry point that invokes the Typer application."""
    app()


if __name__ == "__main__":
    main()
