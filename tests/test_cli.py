import subprocess
import sys
from pathlib import Path

import pytest

from flypage import defs
from flypage.__main__ import main

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_version_matches_version_file(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"flypage {defs.VERSION}"
    assert defs.VERSION == (ROOT / "VERSION").read_text().strip()


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage: flypage" in capsys.readouterr().out


def test_runs_as_module_from_source_tree(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "flypage", "--version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={"XDG_CONFIG_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"flypage {defs.VERSION}"


def test_process_writes_pdfs_and_prints_them(samples, tmp_path, capsys, monkeypatch):
    from flypage.ocr import tesseract

    if not tesseract.TesseractEngine().available():
        pytest.skip("ocrmypdf/tesseract not installed")
    dest = tmp_path / "Scans"
    code = main(["process", str(samples.png()), str(samples.jpeg()), "--dest", str(dest), "--merge"])
    out = capsys.readouterr()
    assert code == 0, out.err
    [line] = out.out.splitlines()
    assert line.startswith(str(dest)) and line.endswith("_Scans_001.pdf")


def test_process_uses_configured_destination_name(samples, tmp_path, capsys, monkeypatch):
    from flypage import config

    from conftest import FakeEngine

    dest = tmp_path / "invoices"
    cfg = config.Config(destinations=[config.Destination(name="Factures", path=str(dest), default=True, template="%{dest}-%n")])
    config.save(cfg)
    monkeypatch.setattr("flypage.pipeline.get_engine", lambda name: FakeEngine())
    assert main(["process", str(samples.png()), "--dest", str(dest)]) == 0
    assert capsys.readouterr().out.strip() == str(dest / "Factures-1.pdf")


def test_process_missing_file(tmp_path, capsys):
    assert main(["process", str(tmp_path / "nope.png"), "--dest", str(tmp_path)]) == 2
    assert "file not found" in capsys.readouterr().err


def test_process_failure_exit_code(samples, tmp_path, capsys, monkeypatch):
    from conftest import FakeEngine

    monkeypatch.setattr("flypage.pipeline.get_engine", lambda name: FakeEngine())
    bad = samples.root / "bad.png"
    bad.write_bytes(b"nope")
    assert main(["process", str(bad), str(samples.png()), "--dest", str(tmp_path / "o")]) == 1
    captured = capsys.readouterr()
    assert "bad.png: " in captured.err
    assert len(captured.out.splitlines()) == 1
