"""Estimated OCR progress (PLAN.md §9.1).

ocrmypdf reports progress only page by page, and most scans are one page
of ≈30 s: without an estimate the bar would sit empty, then jump to full.
So each document gets an expected duration (pages × this computer's
seconds per page, learned from the documents already done and saved in
``~/.local/state/flypage/ocr-speed.json``), and a running document's
progress is its elapsed time over that estimate, held below 100 % until it
really ends.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from flypage import paths

log = logging.getLogger(__name__)

DEFAULT_SECONDS_PER_PAGE = 20.0  # until this computer has been measured
PASSTHROUGH_SECONDS = 0.5  # an image written as it is, without OCR
MAX_RUNNING_FRACTION = 0.95  # a running document never shows as finished
SMOOTHING = 0.3  # weight of the newest measure in the running average


class Speed:
    """Seconds of OCR per page on this computer, as a running average saved on disk."""

    def __init__(self, seconds_per_page: float = DEFAULT_SECONDS_PER_PAGE, samples: int = 0) -> None:
        self.seconds_per_page = seconds_per_page
        self.samples = samples

    @staticmethod
    def path():
        return paths.state_dir() / "ocr-speed.json"

    @classmethod
    def load(cls) -> Speed:
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
            value = float(data["seconds_per_page"])
            if value > 0:
                return cls(value, int(data.get("samples", 1)))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return cls()

    def record(self, pages: int, seconds: float) -> None:
        """Learn from one document's OCR time, and save."""
        if pages <= 0 or seconds <= 0:
            return
        measured = seconds / pages
        if self.samples == 0:
            self.seconds_per_page = measured
        else:
            self.seconds_per_page += SMOOTHING * (measured - self.seconds_per_page)
        self.samples += 1
        self._save()

    def _save(self) -> None:
        path = self.path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"seconds_per_page": self.seconds_per_page, "samples": self.samples}))
            os.replace(tmp, path)
        except OSError as err:
            log.warning("cannot save the OCR speed: %s", err)


@dataclass
class _Doc:
    pages: int
    estimate: float  # seconds
    started: float | None = None  # time.monotonic() when OCR began


@dataclass
class Snapshot:
    fraction: float  # 0…1 for the whole queue
    seconds_left: float
    pages_done: int
    pages_total: int
    documents: dict = field(default_factory=dict)  # key → 0…1 while running (absent: waiting)
    overdue: bool = False  # a running document has passed its estimate


class Estimator:
    """Progress of the documents queued since the queue was last empty.

    Work is counted in estimated seconds, so an image written without OCR
    weighs almost nothing next to a page of OCR. Time is passed in (``now``)
    to keep this testable.
    """

    def __init__(self, speed: Speed, workers: int = 1) -> None:
        self.speed = speed
        self.workers = max(1, workers)
        self._docs: dict[object, _Doc] = {}
        self._done_work = 0.0
        self._done_pages = 0

    @property
    def idle(self) -> bool:
        return not self._docs

    def queue(self, key, pages: int, passthrough: bool = False) -> None:
        estimate = PASSTHROUGH_SECONDS if passthrough else max(1, pages) * self.speed.seconds_per_page
        self._docs[key] = _Doc(pages, estimate)

    def start(self, key, now: float) -> None:
        if key in self._docs:
            self._docs[key].started = now

    def finish(self, key, counted: bool = True) -> None:
        """A document ended; ``counted=False`` (cancelled) takes it out of the total."""
        doc = self._docs.pop(key, None)
        if doc is not None and counted:
            self._done_work += doc.estimate
            self._done_pages += doc.pages
        if not self._docs:  # the next drop starts a new bar
            self._done_work = 0.0
            self._done_pages = 0

    def snapshot(self, now: float) -> Snapshot:
        total = self._done_work + sum(d.estimate for d in self._docs.values())
        work = self._done_work
        running_left = queued_left = 0.0
        documents = {}
        overdue = False
        for key, doc in self._docs.items():
            if doc.started is None:
                queued_left += doc.estimate
                continue
            elapsed = now - doc.started
            fraction = min(elapsed / doc.estimate, MAX_RUNNING_FRACTION) if doc.estimate else MAX_RUNNING_FRACTION
            overdue = overdue or elapsed > doc.estimate
            documents[key] = fraction
            work += doc.estimate * fraction
            running_left += doc.estimate * (1 - fraction)
        seconds_left = max((running_left + queued_left) / self.workers, max_running_left(self._docs, now))
        pages_total = self._done_pages + sum(d.pages for d in self._docs.values())
        return Snapshot(
            fraction=work / total if total else 0.0,
            seconds_left=seconds_left,
            pages_done=self._done_pages,
            pages_total=pages_total,
            documents=documents,
            overdue=overdue,
        )


def max_running_left(docs: dict, now: float) -> float:
    """The longest wait for a document already running (they run side by side)."""
    lefts = [d.estimate - (now - d.started) for d in docs.values() if d.started is not None]
    return max([0.0] + lefts)
