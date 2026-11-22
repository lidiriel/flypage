import pytest
from gi.repository import Gio, GLib

from flypage.config import Destination
from flypage.icons import FALLBACK, folder_icon


def set_metadata(folder, key, value):
    """Set an icon the way Nemo does; skip where GIO keeps no metadata (no gvfs)."""
    try:
        Gio.File.new_for_path(str(folder)).set_attribute_string(key, value, Gio.FileQueryInfoFlags.NONE, None)
    except GLib.Error as err:
        pytest.skip(f"no GIO metadata here: {err.message}")


def test_plain_and_missing_folders_get_a_folder(tmp_path):
    assert folder_icon(tmp_path) == FALLBACK
    assert folder_icon(tmp_path / "nope") == FALLBACK


def test_custom_icon_name_from_the_file_manager(tmp_path):
    folder = tmp_path / "Mael"
    folder.mkdir()
    set_metadata(folder, "metadata::custom-icon-name", "stock_smiley-15")
    assert folder_icon(folder) == "stock_smiley-15"


def test_custom_icon_file_from_the_file_manager(tmp_path):
    folder, image = tmp_path / "Books", tmp_path / "books.png"
    folder.mkdir()
    image.write_bytes(b"png")
    set_metadata(folder, "metadata::custom-icon", image.as_uri())
    assert folder_icon(folder) == str(image)
    image.unlink()  # the image is gone: back to a plain folder
    assert folder_icon(folder) == FALLBACK


def test_chosen_icon_wins_over_the_folders(tmp_path):
    folder = tmp_path / "Mael"
    folder.mkdir()
    set_metadata(folder, "metadata::custom-icon-name", "stock_smiley-15")
    assert Destination(path=str(folder)).display_icon() == "stock_smiley-15"
    assert Destination(path=str(folder), icon="folder-symbolic").display_icon() == "stock_smiley-15"  # old default
    assert Destination(path=str(folder), icon="starred-symbolic").display_icon() == "starred-symbolic"
