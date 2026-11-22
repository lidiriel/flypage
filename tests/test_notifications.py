from pathlib import Path

from flypage import notifications


def test_report_round_trip():
    outputs = [Path("/a/Invoices/1.pdf"), Path("/a/Docs/2.pdf"), Path("/a/Invoices/3.pdf")]
    stdout = "some log line\n" + notifications.report_line(outputs) + "\n"
    assert notifications.parse_report(stdout) == {
        "created": 3, "pdfs": 3, "folders": ["/a/Invoices", "/a/Docs"],
    }


def test_no_or_broken_report():
    assert notifications.parse_report("") is None
    assert notifications.parse_report(notifications.REPORT_PREFIX + "{not json") is None


def test_summary_notification_builds(tmp_path):
    folders = [str(tmp_path / f"f{i}") for i in range(5)]
    assert notifications.summary(5, folders, "app.open-folder") is not None
    assert notifications.summary(1, folders[:1]) is not None


def test_title_counts_images_sent_without_ocr():
    assert notifications.title(3) == "3 searchable PDFs created"
    assert notifications.title(3, 3) == "3 searchable PDFs created"
    assert notifications.title(2, 0) == "2 files sent"
    assert notifications.title(4, 1) == "4 files sent, 1 searchable PDF"
    assert notifications.count_pdfs([Path("a.pdf"), Path("b.jpg")]) == 1
