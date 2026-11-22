import pytest

from flypage.progress import MAX_RUNNING_FRACTION, PASSTHROUGH_SECONDS, Estimator, Speed


def test_speed_learns_and_is_saved():
    speed = Speed.load()
    assert speed.samples == 0
    speed.record(pages=2, seconds=60)  # first measure replaces the default
    assert speed.seconds_per_page == 30
    speed.record(pages=1, seconds=40)  # then a running average
    assert speed.seconds_per_page == pytest.approx(33)
    assert Speed.load().seconds_per_page == pytest.approx(33)
    speed.record(pages=0, seconds=10)  # ignored
    assert Speed.load().samples == 2


def test_broken_speed_file_gives_the_default():
    Speed.path().parent.mkdir(parents=True)
    Speed.path().write_text("{nope")
    assert Speed.load().samples == 0


def test_running_document_moves_but_never_finishes():
    est = Estimator(Speed(10), workers=1)
    est.queue("a", pages=1)
    est.queue("b", pages=1)
    est.start("a", now=100)
    snap = est.snapshot(now=105)
    assert snap.documents == {"a": 0.5} and snap.fraction == pytest.approx(0.25)
    assert snap.seconds_left == pytest.approx(15)  # 5 s for a, then 10 s for b
    snap = est.snapshot(now=200)  # far past its estimate
    assert snap.documents["a"] == MAX_RUNNING_FRACTION and snap.overdue
    est.finish("a")
    snap = est.snapshot(now=200)
    assert (snap.pages_done, snap.pages_total, snap.fraction) == (1, 2, 0.5)


def test_parallel_workers_share_the_wait():
    est = Estimator(Speed(10), workers=2)
    for key in "abcd":
        est.queue(key, pages=1)
    est.start("a", 0)
    est.start("b", 0)
    assert est.snapshot(now=0).seconds_left == pytest.approx(20)  # 40 s of work on 2 cores


def test_images_without_ocr_weigh_almost_nothing():
    est = Estimator(Speed(30))
    est.queue("pdf", pages=1)
    est.queue("jpg", pages=1, passthrough=True)
    est.finish("jpg")
    assert est.snapshot(now=0).fraction == pytest.approx(PASSTHROUGH_SECONDS / (30 + PASSTHROUGH_SECONDS))


def test_cancelled_documents_leave_the_total_and_an_empty_queue_resets():
    est = Estimator(Speed(10))
    est.queue("a", pages=1)
    est.queue("b", pages=3)
    est.finish("a")
    est.finish("b", counted=False)
    assert est.idle
    est.queue("c", pages=1)
    snap = est.snapshot(now=0)
    assert (snap.pages_done, snap.pages_total, snap.fraction) == (0, 1, 0)
