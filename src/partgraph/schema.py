"""Schema loading and application helpers for PartGraph.

This module is intentionally import-light: it does not import pydgraph at module
load time so that the CLI can import it without pulling in the gRPC stack for
commands that do not touch Dgraph. The pydgraph import happens lazily inside
:func:`apply_schema`.
"""

from __future__ import annotations

from pathlib import Path

# Default location of the canonical DQL schema relative to the repository root.
# The repository root is three levels up from this file:
#   src/partgraph/schema.py -> src/partgraph -> src -> <repo root>
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SCHEMA_PATH = _REPO_ROOT / "schema" / "partgraph.dql"


def load_schema(path: str | Path = DEFAULT_SCHEMA_PATH) -> str:
    """Read and return the DQL schema text from *path*.

    Args:
        path: Filesystem path to the ``.dql`` schema file. Defaults to the
            canonical ``schema/partgraph.dql`` shipped with the project.

    Returns:
        The full schema as a UTF-8 string.

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValueError: If the schema file is empty.
    """
    schema_path = Path(path)
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    text = schema_path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"Schema file is empty: {schema_path}")
    return text


def apply_schema(schema_text: str, grpc_addr: str) -> None:
    """Apply *schema_text* to a Dgraph instance over gRPC.

    The pydgraph dependency is imported lazily here so that importing this
    module (and the CLI) never requires the gRPC stack unless a schema is
    actually applied.

    Args:
        schema_text: The DQL schema to apply via an ``alter`` operation.
        grpc_addr: The Dgraph Alpha gRPC address, e.g. ``"127.0.0.1:9081"``.

    Raises:
        ImportError: If pydgraph is not installed.
        Exception: Any error raised by pydgraph while altering the schema is
            propagated to the caller (errors are never swallowed).
    """
    import pydgraph  # noqa: PLC0415 — lazy import keeps the module import-light

    stub = pydgraph.DgraphClientStub(grpc_addr)
    try:
        client = pydgraph.DgraphClient(stub)
        operation = pydgraph.Operation(schema=schema_text)
        client.alter(operation)
    finally:
        stub.close()
