from datetime import datetime

import pytest

from flypage import naming
from flypage.naming import NameValues, TemplateError

WHEN = datetime(2026, 11, 8, 9, 5, 7)


def values(**overrides):
    base = {"when": WHEN, "dest": "Invoices", "n": 3, "pages": 2, "original": "IMG_0001", "scanner": "IRIScan"}
    return NameValues(**{**base, **overrides})


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("%Y-%m-%d_%{dest}_%3n", "2026-11-08_Invoices_003"),
        ("%Y-%m-%d-ocr-%n", "2026-11-08-ocr-3"),
        ("%H%M%S", "090507"),
        ("%-d.%-m.%y", "8.11.26"),  # glibc flag: no padding
        ("%n-%{pages}p", "3-2p"),
        ("%10n", "0000000003"),
        ("%{original} from %{scanner}", "IMG_0001 from IRIScan"),
        ("%{scan_date}", "2026-11-08"),  # falls back to the processing date
        ("100%% scan", "100% scan"),
        ("{braces} are text", "{braces} are text"),
        ("scan", "scan"),
    ],
)
def test_render(template, expected):
    assert naming.render(template, values()) == expected


def test_month_names_follow_the_locale_codes():
    assert naming.render("%b %B", values()) == WHEN.strftime("%b %B")


def test_scan_date():
    assert naming.render("%{scan_date}", values(scan_date=datetime(2025, 1, 31))) == "2025-01-31"


def test_values_cannot_create_sub_folders():
    assert naming.render("%{dest}_%{original}", values(dest="A/B", original="x/y")) == "A_B_x_y"
    assert naming.render("%D", values()) == "11_08_26"  # strftime's own "/" too


def test_result_is_stripped_and_truncated():
    assert naming.render("  %{dest}  ", values()) == "Invoices"
    long = naming.render("%{original}", values(original="é" * 300))
    assert len(long.encode()) <= naming.MAX_STEM_BYTES
    long.encode().decode()  # still valid UTF-8, no cut character


@pytest.mark.parametrize(
    "template",
    ["", "   ", "a/b", "%", "scan%", "%{dest", "%{nope}", "%{}", "%q", "%3Y", "%t", "%-n", "%123n", "%é"],
)
def test_invalid_templates(template):
    with pytest.raises(TemplateError):
        naming.validate(template)


def test_error_messages_name_the_bad_code():
    with pytest.raises(TemplateError, match="%q"):
        naming.validate("a%qb")
    with pytest.raises(TemplateError, match="%{nope}"):
        naming.validate("%{nope}")


def test_valid_templates():
    for template in ("%Y-%m-%d_%{dest}_%3n", "%{scan_date}_%H%M", "%{pages}", "fixed", "%%"):
        naming.validate(template)


def test_empty_result_is_rejected():
    with pytest.raises(TemplateError):
        naming.render("%{scanner}", values(scanner=""))


def test_example_values_render():
    assert naming.render("%{dest}-%n", naming.example("Taxes")) == "Taxes-1"
