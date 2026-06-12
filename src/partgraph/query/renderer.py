"""Rich rendering of search and detail (``show``) results.

Two entry points:
- :func:`render_search_results` — a compact table of ranked rows, with an
  explicit nearest-match banner when the result came from the relaxed pass and
  a stable footer.
- :func:`render_show_result` — labelled detail sections for a single part.

All copy is English. Datasheet URLs are rendered without wrapping so they stay
copy-pasteable under a wide terminal; the long-tail attribute and related-parts
sections are labelled "All attributes" and "Related parts (by MPN)" — never
"family" (PartFamily/variant_of are UNPOPULATED).

Honesty (ADR-0008): semantic (embedding-similarity) hits are a fuzzy match, not
an exact part-number match. They are labelled with a textual ``[Semantic]``
match-type marker (a real column value, never colour-only) so a similarity hit
can never be mistaken for an exact MPN match — consistent with the nearest-match
banner.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from partgraph.query.parser import ParsedQuery
from partgraph.query.ranker import RankedResults, RankedRow

__all__ = ["render_search_results", "render_show_result"]

#: Compact unit suffixes for the "Key params" column, per promoted predicate.
_PARAM_DISPLAY: tuple[tuple[str, str], ...] = (
    ("resistance", "Ω"),  # ohm sign
    ("capacitance", "F"),
    ("inductance", "H"),
    ("voltage_max", "V"),
    ("voltage_min", "V"),
    ("current_max", "A"),
    ("power", "W"),
    ("frequency_max", "Hz"),
    ("tolerance_pct", "%"),
)

#: Human-readable match-type label per RankedRow tier. Lexical tiers share the
#: plain "Match" label; the semantic tier is called out distinctly so a fuzzy
#: embedding hit is never confused with an exact part-number match (ADR-0008).
_MATCH_LABELS: dict[str, str] = {
    "exact": "Exact",
    "trig": "Match",
    "fts": "Text",
    "semantic": "[Semantic]",
    "nearest": "Nearest",
}


def _match_label(tier: str) -> str:
    """Return the textual match-type label for a row *tier*."""
    return _MATCH_LABELS.get(tier, tier)


#: Promoted predicates shown in the detail "Key parameters" section.
_SHOW_PARAMS: tuple[tuple[str, str], ...] = (
    ("resistance", "Resistance"),
    ("capacitance", "Capacitance"),
    ("inductance", "Inductance"),
    ("voltage_min", "Voltage min"),
    ("voltage_max", "Voltage max"),
    ("current_max", "Current max"),
    ("power", "Power"),
    ("frequency_max", "Frequency max"),
    ("tolerance_pct", "Tolerance %"),
)


def _format_number(value: float) -> str:
    """Return a compact, locale-invariant string for *value* (drops .0)."""
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def _key_params_compact(row: RankedRow) -> str:
    """Return a short "10000Ω 1%"-style summary of a row's promoted params."""
    parts: list[str] = []
    for pred, suffix in _PARAM_DISPLAY:
        value = getattr(row, pred, None)
        if value is not None:
            parts.append(f"{_format_number(value)}{suffix}")
    return " ".join(parts)


def render_search_results(
    results: RankedResults,
    parsed: ParsedQuery,
    console: Console,
    *,
    no_truncate: bool = False,
) -> None:
    """Render ranked search results to *console*.

    Args:
        results: The ranked, deduplicated results.
        parsed: The parsed query (used for the nearest-match banner copy).
        console: The Rich console to print to.
        no_truncate: When ``True``, the datasheet column is allowed to fold so
            full URLs are always shown; otherwise long URLs are cropped to the
            column width (but never wrapped).
    """
    if not results.rows:
        console.print("No matches found")
        return

    if results.nearest_match:
        # Plain, ANSI-strippable banner lines.
        console.print(f"No exact match for: {parsed.raw_query}")
        console.print("Nearest matches (by parameter distance):")
    elif results.rows and all(row.tier == "semantic" for row in results.rows):
        # Pure semantic result: be explicit that these are embedding-similarity
        # matches, not exact part-number matches (ADR-0008 honesty banner).
        console.print("Semantic matches (by embedding similarity):")

    overflow = "fold" if no_truncate else "crop"
    table = Table(show_lines=False)
    table.add_column("Match", no_wrap=True)
    table.add_column("MPN", no_wrap=True, overflow=overflow)
    table.add_column("Manufacturer", overflow=overflow)
    table.add_column("Package", overflow=overflow)
    table.add_column("Key params", overflow=overflow)
    table.add_column("Stock", justify="right")
    table.add_column("Datasheet", no_wrap=not no_truncate, overflow=overflow)

    for row in results.rows:
        urls = row.datasheet_urls or []
        datasheet = urls[0] if urls else ""
        stock = str(row.stock) if row.stock is not None else "-"
        table.add_row(
            _match_label(row.tier),
            row.mpn or row.mpn_norm,
            row.manufacturer or "",
            row.package_name or "",
            _key_params_compact(row),
            stock,
            datasheet,
        )

    console.print(table)
    count = len(results.rows)
    console.print(f"Showing {count} result(s).")


def _first_name(items: object) -> str | None:
    """Return ``items[0]['name']`` if present and well-formed, else ``None``."""
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            name = first.get("name")
            if isinstance(name, str):
                return name
    return None


def _render_key_parameters(part: dict, console: Console) -> None:
    """Print the promoted "Key parameters" section (or a (none) placeholder)."""
    console.print("Key parameters:")
    any_param = False
    for pred, label in _SHOW_PARAMS:
        value = part.get(pred)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            console.print(f"  {label}: {_format_number(float(value))}")
            any_param = True
    if not any_param:
        console.print("  (none)")


def _render_attributes(part: dict, console: Console) -> None:
    """Print the long-tail "All attributes" section."""
    console.print("All attributes:")
    printed = False
    for attr in part.get("attr") or []:
        if not isinstance(attr, dict):
            continue
        name = attr.get("attr_name")
        if name is None:
            continue
        val = attr.get("attr_value")
        console.print(f"  {name}: {val if val is not None else ''}")
        printed = True
    if not printed:
        console.print("  (none)")


def _render_datasheets(part: dict, console: Console) -> None:
    """Print the "Datasheets" section with full, non-wrapping URLs."""
    console.print("Datasheets:")
    printed = False
    for ds in part.get("datasheet") or []:
        if not isinstance(ds, dict):
            continue
        url = ds.get("url")
        if not (isinstance(url, str) and url):
            continue
        source = ds.get("source")
        suffix = f" ({source})" if source else ""
        # A no_wrap grid cell keeps the URL on one line even when very long.
        ds_table = Table.grid()
        ds_table.add_column(no_wrap=True, overflow="ignore")
        ds_table.add_row(f"  {url}{suffix}")
        console.print(ds_table)
        printed = True
    if not printed:
        console.print("  (none)")


def _render_related(part: dict, console: Console) -> None:
    """Print the "Related parts (by MPN)" section (never family traversal)."""
    console.print("Related parts (by MPN):")
    printed = False
    for rel in part.get("_related") or []:
        if not isinstance(rel, dict):
            continue
        rel_mpn = rel.get("mpn") or rel.get("mpn_norm")
        if rel_mpn:
            console.print(f"  {rel_mpn}")
            printed = True
    if not printed:
        console.print("  (none)")


def render_show_result(part: dict, console: Console) -> None:
    """Render a single part's detail view with labelled sections.

    Args:
        part: The raw DQL part dict (with ``made_by``/``in_package``/
            ``in_category``/``datasheet``/``tagged``/``attr`` children and an
            optional ``_related`` list of related-part dicts).
        console: The Rich console to print to.
    """
    mpn = part.get("mpn") or part.get("mpn_norm") or "?"
    console.print(f"Part: {mpn}")

    console.print(f"Manufacturer: {_first_name(part.get('made_by')) or '-'}")
    console.print(f"Package: {_first_name(part.get('in_package')) or '-'}")
    console.print(f"Category: {_first_name(part.get('in_category')) or '-'}")

    stock = part.get("stock")
    console.print(f"Stock: {stock if stock is not None else '-'}")
    console.print(f"Is basic: {bool(part.get('is_basic'))}")

    _render_key_parameters(part, console)
    _render_attributes(part, console)
    _render_datasheets(part, console)
    _render_related(part, console)
