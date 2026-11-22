"""Load and save the user configuration (``~/.config/flypage/config.toml``).

Loading never fails: a missing file gives the defaults, and a bad value is
logged and replaced by its default so that one typo does not lose the rest
of the settings.
"""

from __future__ import annotations

import contextlib
import logging
import os
import string
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from flypage import naming, paths
from flypage.i18n import SUPPORTED_UI_LANGUAGES

log = logging.getLogger(__name__)

OCR_ENGINES = ("tesseract",)


# Desktop language → Tesseract model, for the default OCR language.
_TESSERACT_CODES = {
    "en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita", "nl": "nld", "pt": "por",
    "ca": "cat", "da": "dan", "fi": "fin", "nb": "nor", "no": "nor", "pl": "pol", "sv": "swe",
}  # fmt: skip


def default_languages() -> list[str]:
    """Only the desktop's language: each extra language slows OCR down (PLAN.md §7.1)."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "")
        if value and value not in ("C", "POSIX") and not value.startswith("C."):
            code = value.split(":")[0].split("_")[0].split(".")[0].lower()
            return [_TESSERACT_CODES.get(code, "eng")]
    return ["eng"]


@dataclass
class OcrConfig:
    engine: str = "tesseract"
    # In order of preference: the first one is the processing window's default.
    languages: list[str] = field(default_factory=default_languages)
    rotate_pages: bool = False  # needs Tesseract's "osd" model; ≈+15 % time
    clean: bool = False  # needs unpaper; ≈+50 % time


@dataclass
class NamingConfig:
    template: str = "%Y-%m-%d_%{dest}_%3n"  # see flypage/naming.py for the codes


@dataclass
class Scanner:
    name: str
    vendor_id: str  # 4 hex digits, lower case
    model_id: str = ""  # empty = any model of this vendor
    serial: str = ""  # empty = any unit
    source_dir: str = ""  # relative to the mount point; empty = whole memory
    delete_after_import: bool = True


AUTO_ICONS = ("", "folder-symbolic")  # "folder-symbolic": the default of older versions


@dataclass
class Destination:
    path: str  # as written by the user, may start with "~"
    icon: str = ""  # icon name or image path; "" = the folder's own icon (see display_icon)
    template: str = ""  # overrides naming.template when set
    table_layout: bool = False  # read tables row by row (e.g. invoices)

    @property
    def name(self) -> str:
        """The folder's name: shown under its icon, and the value of %{dest}."""
        return self.expanded_path().name or self.path

    def expanded_path(self) -> Path:
        return Path(self.path).expanduser()

    @property
    def automatic_icon(self) -> bool:
        return self.icon in AUTO_ICONS

    def display_icon(self) -> str:
        """The icon chosen in Flypage, else the folder's icon in the file manager."""
        if not self.automatic_icon:
            return self.icon
        from flypage.icons import folder_icon

        return folder_icon(self.expanded_path())

    def same_folder(self, other: Destination) -> bool:
        return os.path.normpath(self.expanded_path()) == os.path.normpath(other.expanded_path())


def _default_scanners() -> list[Scanner]:
    # As seen on a real unit: the USB controller reports OLYMPUS 07b4:0109
    # ("FE210/X775"); scans are in DCIM/100MEDIA. The serial number is a
    # constant shared by units, so it is not used to match.
    return [Scanner(name="IRIScan Book", vendor_id="07b4", model_id="0109", source_dir="DCIM")]


def pictures_dir() -> str:
    """The desktop's Pictures folder (~/Images on a French desktop), written with "~"."""
    path = None
    try:
        from gi.repository import GLib

        path = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
    except (ImportError, ValueError):
        pass
    return paths.contract_home(path or str(Path.home() / "Pictures"))


def _default_destinations() -> list[Destination]:
    return [Destination(path=pictures_dir())]


@dataclass
class Config:
    ui_language: str = "system"
    ocr: OcrConfig = field(default_factory=OcrConfig)
    naming: NamingConfig = field(default_factory=NamingConfig)
    scanners: list[Scanner] = field(default_factory=_default_scanners)
    destinations: list[Destination] = field(default_factory=_default_destinations)


def config_path() -> Path:
    return paths.config_dir() / "config.toml"


# --- loading ---------------------------------------------------------------


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return Config()
    except (OSError, tomllib.TOMLDecodeError) as err:
        log.warning("cannot read %s (%s), using default settings", path, err)
        return Config()
    return from_dict(data)


def from_dict(data: dict[str, Any]) -> Config:
    cfg = Config()
    if "processing" in data:  # only held the sort mode, removed (PLAN.md §9)
        log.info("config.processing: no longer used, ignored")
        data = {k: v for k, v in data.items() if k != "processing"}
    _warn_unknown(data, {f.name for f in fields(Config)}, "config")

    cfg.ui_language = _value(data, "ui_language", cfg.ui_language, "config", SUPPORTED_UI_LANGUAGES)

    ocr = _table(data, "ocr")
    if ocr.get("engine") == "easyocr":  # offered by early versions, dropped (PLAN.md §7.1)
        log.warning("ocr.engine: EasyOCR is no longer supported, using tesseract")
        ocr = {k: v for k, v in ocr.items() if k != "engine"}
    _read_into(cfg.ocr, ocr, "ocr", choices={"engine": OCR_ENGINES})

    _read_into(cfg.naming, _table(data, "naming"), "naming")
    if not _template_ok(cfg.naming.template, "naming.template"):
        cfg.naming.template = NamingConfig.template

    if "scanners" in data:
        cfg.scanners = [s for i, raw in enumerate(_array(data, "scanners")) if (s := _scanner(raw, i))]

    if "destinations" in data:
        cfg.destinations = _destinations(_array(data, "destinations"))

    return cfg


def _value(table: dict[str, Any], key: str, default: Any, where: str, choices=None) -> Any:
    """Return ``table[key]`` if it has the type of ``default`` (and is in ``choices``)."""
    if key not in table:
        return default
    value = table[key]
    if isinstance(default, list):
        ok = isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value)
    else:
        ok = type(value) is type(default)  # not isinstance: bool is an int
    if ok and choices is not None:
        ok = value in choices
    if not ok:
        log.warning("%s.%s: invalid value %r, using %r", where, key, value, default)
        return default
    return list(value) if isinstance(value, list) else value


def _read_into(obj: Any, table: dict[str, Any], where: str, choices: dict | None = None) -> None:
    """Fill the fields of dataclass ``obj`` from ``table``, keeping defaults on bad values."""
    names = [f.name for f in fields(obj)]
    _warn_unknown(table, set(names), where)
    for name in names:
        value = _value(table, name, getattr(obj, name), where, (choices or {}).get(name))
        setattr(obj, name, value)


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        log.warning("config.%s: expected a table, ignored", key)
        return {}
    return value


def _array(data: dict[str, Any], key: str) -> list[Any]:
    value = data[key]
    if not isinstance(value, list):
        log.warning("config.%s: expected an array of tables, ignored", key)
        return []
    return value


def _warn_unknown(table: dict[str, Any], known: set[str], where: str) -> None:
    for key in table.keys() - known:
        log.warning("%s.%s: unknown setting, ignored", where, key)


def _template_ok(template: str, where: str) -> bool:
    try:
        naming.validate(template)
    except naming.TemplateError as err:
        log.warning("%s: %s, using the default", where, err)
        return False
    return True


def is_usb_id(value: str) -> bool:
    return len(value) == 4 and all(c in string.hexdigits for c in value)


def _scanner(raw: Any, index: int) -> Scanner | None:
    where = f"scanners[{index}]"
    if not isinstance(raw, dict):
        log.warning("%s: expected a table, ignored", where)
        return None
    scanner = Scanner(name="", vendor_id="")
    _read_into(scanner, raw, where)
    scanner.vendor_id = scanner.vendor_id.lower()
    scanner.model_id = scanner.model_id.lower()
    if not scanner.name or not is_usb_id(scanner.vendor_id):
        log.warning("%s: needs a name and a 4-digit hex vendor_id, ignored", where)
        return None
    if scanner.model_id and not is_usb_id(scanner.model_id):
        log.warning("%s: model_id must be 4 hex digits or empty, ignored", where)
        return None
    return scanner


def _destinations(raws: list[Any]) -> list[Destination]:
    result: list[Destination] = []
    for index, raw in enumerate(raws):
        where = f"destinations[{index}]"
        if not isinstance(raw, dict):
            log.warning("%s: expected a table, ignored", where)
            continue
        # Written by older versions: the name is now the folder's, and there is no default.
        raw = {k: v for k, v in raw.items() if k not in ("name", "default")}
        dest = Destination(path="")
        _read_into(dest, raw, where)
        if not dest.path.strip():
            log.warning("%s: needs a path, ignored", where)
            continue
        if dest.template and not _template_ok(dest.template, f"{where}.template"):
            dest.template = ""
        if any(d.same_folder(dest) for d in result):
            log.warning("%s: folder %r already listed, ignored", where, dest.path)
            continue
        result.append(dest)

    if not result:
        log.warning("config.destinations: no valid destination, using the default one")
        return _default_destinations()
    return result


# --- saving ----------------------------------------------------------------


def to_dict(cfg: Config) -> dict[str, Any]:
    data = asdict(cfg)
    for dest in data["destinations"]:
        if not dest["template"]:
            del dest["template"]  # keep the file readable: no override
        if not dest["table_layout"]:
            del dest["table_layout"]
        if not dest["icon"]:
            del dest["icon"]  # the folder's own icon
    return data


def update(change: Callable[[Config], None]) -> None:
    """Change one setting on top of what is saved now (another window may have saved since)."""
    cfg = load()
    change(cfg)
    save(cfg)


def save(cfg: Config, path: Path | None = None) -> None:
    """Write the configuration atomically (a crash never leaves a half-written file)."""
    import tomli_w  # only needed to write; reading and --version work without it

    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(to_dict(cfg), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
