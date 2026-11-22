import logging
from pathlib import Path

import pytest

from flypage import config
from flypage.config import Config, Destination, Scanner


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_path_follows_xdg(config_home):
    assert config.config_path() == config_home / "flypage" / "config.toml"


def test_relative_xdg_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.config_path() == tmp_path / ".config" / "flypage" / "config.toml"


def test_missing_file_gives_defaults():
    cfg = config.load()
    assert cfg == Config()
    [pictures] = cfg.destinations  # the desktop's Pictures folder
    assert pictures.path == config.pictures_dir() and pictures.name == Path(pictures.path).name
    assert cfg.scanners[0].vendor_id == "07b4"


def test_round_trip():
    cfg = Config(
        ui_language="fr",
        scanners=[Scanner(name="Mine", vendor_id="07b4", model_id="0281", delete_after_import=False)],
        destinations=[
            Destination(path="~/Invoices", icon="folder-documents-symbolic"),
            Destination(path="/srv/taxes", template="%Y_tax"),
        ],
    )
    cfg.ocr.languages = ["fra"]
    config.save(cfg)
    assert config.load() == cfg


def test_save_omits_empty_template_override():
    config.save(Config())
    data = config.tomllib.loads(config.config_path().read_text())
    assert "template" not in data["destinations"][0]


def test_save_leaves_no_temp_file():
    config.save(Config())
    assert [p.name for p in config.config_path().parent.iterdir()] == ["config.toml"]


def test_malformed_file_gives_defaults(caplog):
    write(config.config_path(), "this is = = not toml")
    with caplog.at_level(logging.WARNING):
        assert config.load() == Config()
    assert "cannot read" in caplog.text


def test_bad_values_fall_back_one_by_one(caplog):
    write(
        config.config_path(),
        """
        ui_language = "de"
        colour = "blue"
        [ocr]
        engine = "magic"
        languages = ["fra"]
        [processing]
        sort_mode = "all_same"
        [naming]
        template = "%{dest}"
        """,
    )
    with caplog.at_level(logging.WARNING):
        cfg = config.load()
    assert cfg.ui_language == "system"
    assert cfg.ocr.engine == "tesseract"
    assert cfg.ocr.languages == ["fra"]  # the valid value next to a bad one is kept
    assert cfg.naming.template == "%{dest}"
    assert "colour: unknown setting" in caplog.text
    assert "processing" not in caplog.text  # the old sort mode is dropped quietly


@pytest.mark.parametrize("languages", ["[]", '"eng"', '["eng", 3]', '[""]'])
def test_invalid_language_lists(languages):
    cfg = config.from_dict(config.tomllib.loads(f"[ocr]\nlanguages = {languages}"))
    assert cfg.ocr.languages == config.default_languages()


def test_bool_is_not_accepted_for_strings_and_vice_versa():
    cfg = config.from_dict(
        {"scanners": [{"name": "S", "vendor_id": "07B4", "delete_after_import": 1}]}
    )
    assert cfg.scanners == [Scanner(name="S", vendor_id="07b4", delete_after_import=True)]


@pytest.mark.parametrize(
    "raw",
    [
        {"vendor_id": "07b4"},  # no name
        {"name": "S", "vendor_id": "7b4"},
        {"name": "S", "vendor_id": "zzzz"},
        {"name": "S", "vendor_id": "07b4", "model_id": "12345"},
        "not a table",
    ],
)
def test_invalid_scanners_are_dropped(raw):
    assert config.from_dict({"scanners": [raw]}).scanners == []


def test_empty_scanner_list_is_kept():
    assert config.from_dict({"scanners": []}).scanners == []


def test_old_destination_name_and_default_are_dropped_quietly(caplog):
    with caplog.at_level(logging.WARNING):
        cfg = config.from_dict({"destinations": [{"name": "Docs", "path": "~/Scans", "default": True}]})
    assert cfg.destinations == [Destination(path="~/Scans")]
    assert cfg.destinations[0].name == "Scans"  # named after the folder
    assert caplog.text == ""


def test_invalid_and_duplicate_destinations_are_dropped():
    cfg = config.from_dict(
        {
            "destinations": [
                {"path": "/a"},
                {"path": "/a/"},  # the same folder
                {"path": "/other/a"},  # same name, another folder: kept
                {"path": " "},
                {"icon": "x"},
            ]
        }
    )
    assert [(d.name, d.path) for d in cfg.destinations] == [("a", "/a"), ("a", "/other/a")]


def test_no_valid_destination_restores_the_default():
    assert config.from_dict({"destinations": []}).destinations == Config().destinations


def test_expanded_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Destination(path="~/Scans").expanded_path() == tmp_path / "Scans"


def test_invalid_templates_fall_back(caplog):
    cfg = config.from_dict(
        {
            "naming": {"template": "%q"},
            "destinations": [{"name": "A", "path": "/a", "template": "%{nope}"}],
        }
    )
    assert cfg.naming.template == config.NamingConfig.template
    assert cfg.destinations[0].template == ""
    assert "naming.template" in caplog.text and "destinations[0].template" in caplog.text


def test_old_date_format_keys_are_reported(caplog):
    with caplog.at_level(logging.WARNING):
        config.from_dict({"naming": {"date_format": "%Y"}})
    assert "naming.date_format: unknown setting" in caplog.text


def test_easyocr_falls_back_to_tesseract(caplog):
    with caplog.at_level(logging.WARNING):
        cfg = config.from_dict(config.tomllib.loads('[ocr]\nengine = "easyocr"\nlanguages = ["fra"]'))
    assert cfg.ocr.engine == "tesseract"
    assert cfg.ocr.languages == ["fra"]
    assert "EasyOCR is no longer supported" in caplog.text


@pytest.mark.parametrize(
    "env, expected",
    [({"LANGUAGE": "fr:en"}, ["fra"]), ({"LANG": "de_DE.UTF-8"}, ["deu"]), ({"LANG": "C.UTF-8"}, ["eng"]),
     ({"LANG": "xx_YY"}, ["eng"]), ({}, ["eng"])],
)  # fmt: skip
def test_default_ocr_language_is_the_desktop_one(monkeypatch, env, expected):
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    assert config.default_languages() == expected


def test_ocr_options_and_table_layout_round_trip():
    cfg = Config(destinations=[Destination(path="~/I", table_layout=True),
                               Destination(path="~/D")])  # fmt: skip
    cfg.ocr.rotate_pages = True
    config.save(cfg)
    assert config.load() == cfg
    data = config.tomllib.loads(config.config_path().read_text())
    assert "table_layout" not in data["destinations"][1]  # only written when set
