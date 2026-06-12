"""Protocol tag extraction from free-text component descriptions.

:func:`extract_tags` scans a description for a fixed lexicon of communication
protocols and returns their canonical tag names. Matching is case-insensitive
and word-boundary aware, so ``"CANISTER"`` does not yield ``CAN`` and
``"SPICE"`` does not yield ``SPI``. Hyphen-optional forms are canonicalized
(``"RS232"`` -> ``"RS-232"``).

The lexicon order is fixed so the returned list is deterministic for a given
input. A description with no protocol token yields ``[]`` (never ``None``).
"""

from __future__ import annotations

import re

__all__ = ["extract_tags"]

# Each entry: (canonical tag, compiled case-insensitive word-boundary pattern).
# Patterns allow an optional separator for the RS-232 / RS-485 families so both
# "RS-232" and "RS232" are recognised and normalised to the hyphenated form.
#
# Ordering is deliberate and stable: it determines the order of the returned
# list when multiple protocols match.
_LEXICON: list[tuple[str, re.Pattern[str]]] = [
    ("RS-232", re.compile(r"\bRS[\s-]?232\b", re.IGNORECASE)),
    ("RS-485", re.compile(r"\bRS[\s-]?485\b", re.IGNORECASE)),
    ("I2C", re.compile(r"\bI2C\b", re.IGNORECASE)),
    ("SPI", re.compile(r"\bSPI\b", re.IGNORECASE)),
    ("UART", re.compile(r"\bUART\b", re.IGNORECASE)),
    ("USB", re.compile(r"\bUSB\b", re.IGNORECASE)),
    ("CAN", re.compile(r"\bCAN\b", re.IGNORECASE)),
    ("LIN", re.compile(r"\bLIN\b", re.IGNORECASE)),
    ("Ethernet", re.compile(r"\bEthernet\b", re.IGNORECASE)),
    ("HDMI", re.compile(r"\bHDMI\b", re.IGNORECASE)),
    ("LVDS", re.compile(r"\bLVDS\b", re.IGNORECASE)),
    ("PCIe", re.compile(r"\bPCIe\b", re.IGNORECASE)),
]


def extract_tags(text: str | None) -> list[str]:
    """Return the canonical protocol tags found in *text*.

    Args:
        text: A free-text description. ``None`` or empty yields ``[]``.

    Returns:
        A list of canonical tag names (e.g. ``["RS-232", "SPI"]``) in lexicon
        order, with no duplicates. Always a list, never ``None``; never raises.
    """
    if not text:
        return []
    return [tag for tag, pattern in _LEXICON if pattern.search(text)]
