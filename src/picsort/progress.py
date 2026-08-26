from __future__ import annotations

import sys
import threading
import time


class Progress:
    def __init__(self, total: int, label: str, verbose: bool = False):
        self.total = total
        self.label = label
        self.verbose = verbose
        self.completed = 0
        self.started = time.monotonic()
        if self.verbose:
            print(f"[0/{self.total}] {self.label}: started")

    def update(self, item: str = "") -> None:
        self.completed += 1
        elapsed = max(time.monotonic() - self.started, 0.001)
        rate = self.completed / elapsed
        remaining = max(self.total - self.completed, 0)
        eta = remaining / rate if rate else 0
        if self.verbose:
            print(f"[{self.completed}/{self.total}] {self.label}: {item}")
            return
        width = 30
        filled = int(width * self.completed / self.total) if self.total else width
        bar = "#" * filled + "." * (width - filled)
        message = f"\r{self.label} [{bar}] {self.completed}/{self.total} {rate:.1f}/s ETA {format_duration(eta)}"
        print(message, end="", file=sys.stderr, flush=True)

    def finish(self) -> None:
        if not self.verbose:
            print(file=sys.stderr)


class Spinner:
    def __init__(self, label: str):
        self.label = label
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        sys.stderr.write("\033[2K\r")
        sys.stderr.flush()

    def _run(self) -> None:
        frames = "|/-\\"
        index = 0
        while not self._stop.wait(0.2):
            sys.stderr.write(f"\033[2K\r{frames[index % len(frames)]} {self.label}")
            sys.stderr.flush()
            index += 1


class ScanProgress:
    def __init__(self, label: str):
        self.label = label
        self.directories = 0
        self.entries = 0
        self.matches = 0
        self._stop = threading.Event()
        self._thread = None
        self.started = time.monotonic()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update(self, directories: int, entries: int, matches: int) -> None:
        self.directories = directories
        self.entries = entries
        self.matches = matches

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        sys.stderr.write("\033[2K\r")
        sys.stderr.flush()

    def _run(self) -> None:
        frames = "|/-\\"
        index = 0
        while not self._stop.wait(0.2):
            elapsed = max(time.monotonic() - self.started, 0.001)
            rate = self.entries / elapsed
            sys.stderr.write(
                f"\033[2K\r{frames[index % len(frames)]} {self.label}: "
                f"dirs={self.directories} entries={self.entries} "
                f"matches={self.matches} {rate:.0f}/s elapsed={format_duration(elapsed)}"
            )
            sys.stderr.flush()
            index += 1


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
