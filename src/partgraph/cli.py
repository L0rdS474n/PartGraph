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


def main() -> None:
    """Console-script entry point that invokes the Typer application."""
    app()


if __name__ == "__main__":
    main()
