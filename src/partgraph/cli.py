"""PartGraph command-line interface.

Provides the ``partgraph`` command and its ``db`` sub-command group for managing
the local Dgraph instance via Docker Compose and applying the DQL schema.

Design notes:
- ``app`` is a module-level :class:`typer.Typer` so the test-suite (and the
  console-script wrapper :func:`main`) can import it directly.
- Docker Compose is always invoked with a list argv and ``shell=False``; the
  compose-file path is resolved to an absolute path so no CWD-relative file can
  be injected.
- ``db down`` deliberately omits ``-v`` so the named data volume survives.
- pydgraph is imported lazily inside :func:`apply_schema` so that CLI commands
  which do not talk to Dgraph never require the gRPC stack.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer
from rich.console import Console

from partgraph import __version__
from partgraph import schema as schema_module

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Dgraph Alpha gRPC address used for schema application and mutations.
DGRAPH_GRPC_ADDR = "127.0.0.1:9081"

#: Absolute path to the Docker Compose file. Resolved three levels up from this
#: file (src/partgraph/cli.py -> src/partgraph -> src -> <repo root>) so the
#: value passed to ``docker compose -f`` is always absolute and never depends on
#: the current working directory.
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
    help="Manage the local Dgraph database (Docker Compose lifecycle and schema).",
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
    """Run ``docker compose -f <COMPOSE_FILE> <compose_args>`` safely.

    Args:
        compose_args: Trailing Docker Compose arguments (e.g. ``["up", "-d"]``).
        action: Human-readable description used in error messages.

    Raises:
        typer.Exit: With the subprocess return code if Docker Compose exits
            non-zero, after printing a clear English error message to stderr.
    """
    argv = ["docker", "compose", "-f", str(COMPOSE_FILE), *compose_args]
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
            f"(docker compose exited with code {result.returncode})."
        )
        if result.stderr:
            _err_console.print(result.stderr, end="")
        raise typer.Exit(code=result.returncode)


# ---------------------------------------------------------------------------
# db sub-commands
# ---------------------------------------------------------------------------

@db_app.command("up")
def up() -> None:
    """Start the local Dgraph database in the background (docker compose up -d)."""
    _run_compose(["up", "-d"], action="start")
    _console.print("[green]Dgraph is starting.[/green] "
                   "Health: http://127.0.0.1:8081/health")


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
    """Show the status of the local Dgraph container (docker compose ps)."""
    _run_compose(["ps"], action="query the status of")


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


def _build_dgraph_client():  # pragma: no cover — thin pydgraph wiring
    """Create a pydgraph client connected to the local Dgraph Alpha.

    pydgraph is imported lazily so commands that do not touch Dgraph never
    require the gRPC stack. Returns ``(client, stub)``; the caller closes the
    stub.
    """
    import pydgraph  # noqa: PLC0415 — lazy import keeps the CLI import-light

    stub = pydgraph.DgraphClientStub(DGRAPH_GRPC_ADDR)
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


@app.command()
def search(
    query: str = typer.Argument(
        ...,
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
) -> None:
    """Search the component graph by MPN, parameters and package.

    The query is parsed into numeric parameters (e.g. 10k -> resistance),
    a package code (e.g. 0402) and free-text MPN tokens, then matched with an
    exact / trigram / full-text cascade. When no exact parametric match exists,
    a relaxed pass returns the nearest parts by parameter distance.

    Examples:
      partgraph search "MAX232"
      partgraph search "10k 0402 1%"
      partgraph search "100nF 0603"
      partgraph search "1.2V MAX232"

    All reads are read-only; this command never modifies the database. Use
    --limit to bound the result count and --no-truncate to print full URLs.
    The command searches related parts by MPN similarity.
    """
    from partgraph.query.dql_builder import build_search_dql  # noqa: PLC0415
    from partgraph.query.parser import ParsedQuery, parse_query  # noqa: PLC0415
    from partgraph.query.ranker import rank_results  # noqa: PLC0415
    from partgraph.query.renderer import render_search_results  # noqa: PLC0415

    if not query.strip():
        _err_console.print("[red]Error:[/red] search query cannot be empty.")
        raise typer.Exit(code=1)

    parsed = parse_query(query)

    stub = None
    try:
        client, stub = _build_dgraph_client()

        # Pass 1 (hard): full parametric + text filter.
        query_text, variables = build_search_dql(parsed, limit=limit)
        data = _run_block_query(client, query_text, variables)
        result = rank_results(data, parsed)

        if not result.rows and parsed.quantities:
            # Pass 2 (relaxed): drop parametric filters, keep text + package, and
            # merge the relaxed rows under the "nearest" key for the ranker.
            relaxed = ParsedQuery(
                quantities=[],
                package=parsed.package,
                text_tokens=parsed.text_tokens,
                raw_query=parsed.raw_query,
            )
            relaxed_text, relaxed_vars = build_search_dql(relaxed, limit=limit)
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
            result = rank_results(merged, parsed)
    except typer.Exit:
        raise
    except Exception as exc:
        _err_console.print(_DB_QUERY_ERROR)
        raise typer.Exit(code=1) from exc
    finally:
        if stub is not None:
            stub.close()

    render_search_results(result, parsed, _console, no_truncate=no_truncate)


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


def main() -> None:
    """Console-script entry point that invokes the Typer application."""
    app()


if __name__ == "__main__":
    main()
