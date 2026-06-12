"""Staged-part data model and identity helpers.

:class:`StagedPart` is the canonical, immutable record produced by the
normalize stage and consumed by the loader. It is a frozen dataclass with a
deterministic JSON serialization (``sort_keys=True``, ``ensure_ascii=False``)
so that two normalize runs over the same input yield byte-identical files.

Identity helpers:
- :func:`normalize_mpn` — uppercase and keep only ``[A-Z0-9]`` (strips spaces,
  separators and trailing punctuation; all-punctuation input -> ``""``).
- :func:`normalize_mfr` — same character rule applied to manufacturer names.
- :func:`make_xid` — the deduplication key ``f"{mpn_norm}|{mfr_norm}"``.

The module imports nothing beyond the standard library so it stays cheap to
import from any layer (CLI, adapter, loader, tests).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "AttrRecord",
    "StagedPart",
    "make_xid",
    "normalize_mfr",
    "normalize_mpn",
]

# Only ASCII letters and digits survive normalization. Everything else
# (whitespace, '-', '/', '+', unicode symbols) is dropped. This is intentionally
# stricter than a slug: the normalized form is an opaque identity token, not a
# display string.
_KEEP_ALNUM_RE = re.compile(r"[^A-Z0-9]")


def normalize_mpn(raw: str | None) -> str:
    """Return the normalized form of a manufacturer part number.

    Rules (deterministic, locale-independent):
    - ``None`` is treated as an empty string.
    - Uppercase all characters.
    - Keep only ``A-Z`` and ``0-9``; drop every other character (whitespace,
      ``-``, ``/``, ``+``, unicode symbols, ...).

    An input consisting solely of punctuation therefore normalizes to ``""``.
    """
    if not raw:
        return ""
    return _KEEP_ALNUM_RE.sub("", raw.upper())


def normalize_mfr(raw: str | None) -> str:
    """Return the normalized form of a manufacturer name.

    Uses the same character rule as :func:`normalize_mpn`: uppercase, then keep
    only ``[A-Z0-9]``. ``None`` becomes ``""``.
    """
    if not raw:
        return ""
    return _KEEP_ALNUM_RE.sub("", raw.upper())


def make_xid(mpn_norm: str, mfr_norm: str) -> str:
    """Return the deduplication key ``"{mpn_norm}|{mfr_norm}"``.

    The key is deterministic and contains exactly one ``|`` separator (neither
    operand may contain ``|`` because normalization strips it).
    """
    return f"{mpn_norm}|{mfr_norm}"


@dataclass(frozen=True)
class AttrRecord:
    """A single long-tail attribute extracted from a component.

    Attributes:
        name: Attribute label as found in the source (e.g. ``"Resistance"``).
        value_text: Original string value, or ``None``.
        value_num: SI-normalized numeric value, or ``None`` when not numeric.
        unit: Unit symbol associated with ``value_num``, or ``None``.
    """

    name: str
    value_text: str | None = None
    value_num: float | None = None
    unit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a plain ``dict`` representation suitable for JSON encoding."""
        return {
            "name": self.name,
            "value_text": self.value_text,
            "value_num": self.value_num,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttrRecord:
        """Reconstruct an :class:`AttrRecord` from a decoded JSON ``dict``."""
        return cls(
            name=data["name"],
            value_text=data.get("value_text"),
            value_num=data.get("value_num"),
            unit=data.get("unit"),
        )


@dataclass(frozen=True)
class StagedPart:
    """Immutable, source-stamped representation of a single component.

    Produced by the normalize stage and consumed by the loader. All optional
    fields default to ``None`` / empty so partial source rows can still be
    represented faithfully. The dataclass is frozen to guarantee immutability
    after construction.
    """

    mpn: str
    mpn_norm: str
    mfr_name: str | None
    mfr_norm: str
    xid: str
    description: str | None
    package: str | None
    category: str | None
    subcategory: str | None
    datasheet_url: str | None
    lcsc_id: str | None
    stock: int | None
    price_usd: float | None
    is_basic: bool
    promoted: dict[str, float] = field(default_factory=dict)
    attributes: list[AttrRecord] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready ``dict`` (nested ``AttrRecord``s flattened)."""
        data = asdict(self)
        # asdict already converts nested AttrRecord dataclasses to dicts, but we
        # re-derive the attribute list explicitly to keep key ordering and the
        # exact field set under our control regardless of dataclass internals.
        data["attributes"] = [a.to_dict() for a in self.attributes]
        return data

    def to_json(self) -> str:
        """Serialize to a deterministic JSON string.

        ``sort_keys=True`` guarantees stable key ordering; ``ensure_ascii=False``
        keeps unicode characters literal (e.g. ``Ω``) rather than escaped. The
        combination makes normalize output byte-reproducible.
        """
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StagedPart:
        """Reconstruct a :class:`StagedPart` from a decoded JSON ``dict``."""
        return cls(
            mpn=data["mpn"],
            mpn_norm=data["mpn_norm"],
            mfr_name=data.get("mfr_name"),
            mfr_norm=data["mfr_norm"],
            xid=data["xid"],
            description=data.get("description"),
            package=data.get("package"),
            category=data.get("category"),
            subcategory=data.get("subcategory"),
            datasheet_url=data.get("datasheet_url"),
            lcsc_id=data.get("lcsc_id"),
            stock=data.get("stock"),
            price_usd=data.get("price_usd"),
            is_basic=bool(data.get("is_basic", False)),
            promoted=dict(data.get("promoted") or {}),
            attributes=[AttrRecord.from_dict(a) for a in data.get("attributes") or []],
            tags=list(data.get("tags") or []),
            source_ref=data.get("source_ref", ""),
        )

    @classmethod
    def from_json(cls, text: str) -> StagedPart:
        """Reconstruct a :class:`StagedPart` from its JSON string form."""
        return cls.from_dict(json.loads(text))
