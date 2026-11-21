import logging

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
    assert cfg.default_destination().name == "Documents"
    assert cfg.scanners[0].vendor_id == "07b4"


def test_round_trip():
    cfg = Config(
        ui_language="fr",
        scanners=[Scanner(name="Mine", vendor_id="07b4", model_id="0281", delete_after_import=False)],
        destinations=[
            Destination(name="Invoices", path="~/Invoices", icon="folder-documents-symbolic"),
            Destination(name="Taxes", path="/srv/taxes", default=True, template="%Y_tax"),
        ],
    )
    cfg.ocr.languages = ["fra"]
    cfg.processing.sort_mode = "all_same"
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
        sort_mode = 3
        [naming]
        template = "%{dest}"
        """,
    )
    with caplog.at_level(logging.WARNING):
        cfg = config.load()
    assert cfg.ui_language == "system"
    assert cfg.ocr.engine == "tesseract"
    assert cfg.ocr.languages == ["fra"]  # the valid value next to a bad one is kept
    assert cfg.processing.sort_mode == "per_file"
    assert cfg.naming.template == "%{dest}"
    assert "colour: unknown setting" in caplog.text


@pytest.mark.parametrize("languages", ["[]", '"eng"', '["eng", 3]', '[""]'])
def test_invalid_language_lists(languages):
    cfg = config.from_dict(config.tomllib.loads(f"[ocr]\nlanguages = {languages}"))
    assert cfg.ocr.languages == ["eng", "fra"]


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


def test_destinations_without_default_get_the_first():
    cfg = config.from_dict({"destinations": [{"name": "A", "path": "/a"}, {"name": "B", "path": "/b"}]})
    assert [d.default for d in cfg.destinations] == [True, False]


def test_destinations_keep_only_the_first_default():
    cfg = config.from_dict(
        {
            "destinations": [
                {"name": "A", "path": "/a"},
                {"name": "B", "path": "/b", "default": True},
                {"name": "C", "path": "/c", "default": True},
            ]
        }
    )
    assert [d.default for d in cfg.destinations] == [False, True, False]
    assert cfg.default_destination().name == "B"


def test_invalid_and_duplicate_destinations_are_dropped():
    cfg = config.from_dict(
        {
            "destinations": [
                {"name": "A", "path": "/a"},
                {"name": "A", "path": "/other"},
                {"name": " ", "path": "/blank"},
                {"name": "NoPath"},
            ]
        }
    )
    assert [(d.name, d.path) for d in cfg.destinations] == [("A", "/a")]


def test_no_valid_destination_restores_the_default():
    assert config.from_dict({"destinations": []}).destinations == Config().destinations


def test_expanded_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Destination(name="D", path="~/Scans").expanded_path() == tmp_path / "Scans"


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
