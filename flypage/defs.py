"""Build-time constants.

An installed copy reads them from ``_build.py``, which Meson generates. When
running from the source tree that module does not exist, so we fall back to
values derived from the checkout.
"""

from pathlib import Path

try:
    from flypage._build import LOCALEDIR, PKGDATADIR, VERSION
except ImportError:
    _ROOT = Path(__file__).resolve().parent.parent
    VERSION = (_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    LOCALEDIR = None  # gettext's default search path
    PKGDATADIR = str(_ROOT / "data")
