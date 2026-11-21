"""Output file names built from the user's template, e.g. ``%Y-%m-%d_%{dest}_%3n``.

Codes:

- any ``strftime`` code (``%Y`` ``%m`` ``%d`` ``%H`` ``%M`` ``%S`` ``%b`` …) for the
  processing date and time, optionally with a glibc flag (``%-d`` = day
  without leading zero);
- ``%n``: sequence number in the destination for this session; ``%3n`` pads
  it with zeros to 3 digits;
- ``%{dest}`` ``%{original}`` ``%{scanner}`` ``%{pages}`` ``%{scan_date}``;
- ``%%``: a literal ``%``.

The ``.pdf`` extension is added by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from flypage.i18n import _

VALUE_CODES = ("dest", "original", "scanner", "pages", "scan_date")
# glibc strftime conversions, minus %n (ours: sequence) and %t (a tab).
STRFTIME_CODES = frozenset("aAbBcCdDeFgGhHIjklmMpPrRsSTuUVwWxXyYzZ")
SCAN_DATE_FORMAT = "%Y-%m-%d"

# Leaves room for ".pdf", a "-NN" collision suffix and the ".part" temp name
# within the usual 255-byte file-name limit.
MAX_STEM_BYTES = 200

_CODE = re.compile(r"(?P<width>\d{1,2})?n|(?P<flag>[-_0^#])?(?P<letter>[A-Za-z])")


class TemplateError(ValueError):
    """The template, or the name it produces, cannot be used."""


@dataclass
class NameValues:
    when: datetime  # processing date/time
    dest: str
    n: int
    pages: int
    original: str = ""
    scanner: str = ""
    scan_date: datetime | None = None  # falls back to ``when``


def example(dest: str = "Documents") -> NameValues:
    """Plausible values, for previews and validation."""
    now = datetime.now()
    return NameValues(when=now, dest=dest, n=1, pages=1, original="IMG_0001", scanner="IRIScan Book", scan_date=now)


def _parse(template: str) -> list[tuple[str, str | int]]:
    """Split ``template`` into ("text", s), ("time", code), ("seq", width) and ("value", name)."""
    if not template.strip():
        raise TemplateError(_("The file name template is empty."))
    if "/" in template:
        raise TemplateError(_("The file name template cannot contain “/”."))
    parts: list[tuple[str, str | int]] = []
    text: list[str] = []
    i = 0
    while i < len(template):
        char = template[i]
        i += 1
        if char != "%":
            text.append(char)
            continue
        if i == len(template):
            raise TemplateError(_("The template ends with a lone “%”. Write “%%” for a percent sign."))
        if template[i] == "%":
            text.append("%")
            i += 1
            continue
        if text:
            parts.append(("text", "".join(text)))
            text = []
        if template[i] == "{":
            end = template.find("}", i)
            if end < 0:
                raise TemplateError(_("Missing “}” after “%{”."))
            name = template[i + 1 : end]
            if name not in VALUE_CODES:
                raise TemplateError(_("Unknown code “%{{{name}}}”.").format(name=name))
            parts.append(("value", name))
            i = end + 1
            continue
        match = _CODE.match(template, i)
        if not match or (match["letter"] and match["letter"] not in STRFTIME_CODES):
            raise TemplateError(_("Unknown code “%{code}”.").format(code=template[i : i + 2]))
        if match["letter"]:
            parts.append(("time", "%" + (match["flag"] or "") + match["letter"]))
        else:
            parts.append(("seq", int(match["width"] or 0)))
        i = match.end()
    if text:
        parts.append(("text", "".join(text)))
    return parts


def validate(template: str) -> None:
    """Raise ``TemplateError`` with a translated message if ``template`` is unusable."""
    render(template, example())


def render(template: str, values: NameValues) -> str:
    """Return the file name (without extension) for ``values``."""
    pieces = []
    for kind, arg in _parse(template):
        if kind == "text":
            pieces.append(arg)
        elif kind == "time":
            pieces.append(_clean(values.when.strftime(arg)))
        elif kind == "seq":
            pieces.append(f"{values.n:0{arg}d}" if arg else str(values.n))
        elif arg == "scan_date":
            pieces.append((values.scan_date or values.when).strftime(SCAN_DATE_FORMAT))
        else:
            pieces.append(_clean(str(getattr(values, arg))))
    name = "".join(pieces).strip()
    if name in ("", ".", ".."):
        raise TemplateError(_("The template gives an empty file name."))
    return _truncate(name)


def _clean(text: str) -> str:
    """Make a value safe inside a file name (e.g. %D gives "11/18/26")."""
    return text.replace("/", "_").replace("\0", "")


def _truncate(name: str) -> str:
    encoded = name.encode("utf-8")
    if len(encoded) <= MAX_STEM_BYTES:
        return name
    return encoded[:MAX_STEM_BYTES].decode("utf-8", errors="ignore").rstrip()
