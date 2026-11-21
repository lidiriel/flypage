"""Cancellation shared by the pipeline and the OCR engines.

Every external command is started through ``CancelToken.run()`` so that
``cancel()`` can kill it, along with any children it spawned.
"""

import os
import signal
import subprocess
import threading


class Cancelled(Exception):
    """The operation was cancelled by the user."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def cancel(self) -> None:
        with self._lock:
            self._event.set()
            procs = list(self._procs)
        for proc in procs:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def run(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        """Run ``cmd`` to completion, raising ``Cancelled`` if cancelled meanwhile."""
        with self._lock:
            self.check()
            # Own process group: cancel() kills the children too, and a Ctrl+C
            # in the terminal reaches us only, so we decide how to stop.
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                **kwargs,
            )
            self._procs.add(proc)
        try:
            out, err = proc.communicate()
        finally:
            with self._lock:
                self._procs.discard(proc)
        self.check()
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
