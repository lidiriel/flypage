"""Human-readable names for OCR language codes (``fra`` → "French"/"français").

Names and their translations come from the iso-codes package; without it,
the code itself is shown.
"""

import functools
import gettext
import json
from pathlib import Path

ISO_639_3 = Path("/usr/share/iso-codes/json/iso_639-3.json")
_DOMAIN = "iso_639-3"


@functools.cache
def _names() -> dict[str, str]:
    try:
        entries = json.loads(ISO_639_3.read_text(encoding="utf-8"))["639-3"]
    except (OSError, ValueError, KeyError):
        return {}
    names = {}
    for entry in entries:
        # Tesseract mostly uses ISO 639-2/T codes (fra, deu), sometimes /B (chi).
        for key in ("alpha_3", "bibliographic"):
            if key in entry:
                names.setdefault(entry[key], entry["name"])
    return names


def language_name(code: str) -> str:
    """Translated name for a Tesseract code; variants like ``chi_sim`` use the base name."""
    name = _names().get(code.partition("_")[0])
    return gettext.dgettext(_DOMAIN, name) if name else code
