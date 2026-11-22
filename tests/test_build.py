"""The package installs what the source tree has (flypage/meson.build lists files by hand)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "flypage"
SHIPPED = {".py", ".css", ".svg"}


def test_every_source_file_is_installed():
    meson = (PACKAGE / "meson.build").read_text(encoding="utf-8")
    listed = set(re.findall(r"'([^']+\.(?:py|css|svg))'", meson))
    present = {
        p.relative_to(PACKAGE).as_posix()
        for p in PACKAGE.rglob("*")
        if p.suffix in SHIPPED and "__pycache__" not in p.parts and p.name != "_build.py"
    }
    assert sorted(present - listed) == [], "add these to flypage/meson.build"
