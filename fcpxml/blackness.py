"""Is a clip's picture black where the cut uses it?

The coverage model reads the XML, and the XML cannot say what is in a file.
A black movie laid over the back half of a cut is "picture" to it, so the
cut measures as fully covered while the screen is dark for two and a half
minutes. A name cannot settle it either way ("BLACKOUT" is a label, like a
gap's name). So this looks: three frames of the file, inside the range the
clip plays, each shrunk to 64x36 grey. The clip is black when no pixel in
any of the three is brighter than BLACK_PEAK.

Probe is the callable coverage.items() takes as ``dark``. It asks for the
middle frame first and stops at the first frame that is not black, so an
ordinary shot costs one seek. Answers are cached per (file, mtime, size,
instant) in the private preview cache; a re-exported file is looked at again.

What it cannot see: a clip that is black for only part of its length, and
anything a transform, opacity or an effect does to the picture. A frame that
cannot be read is not called black.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import render
from .trace import traced_run

# Brightest pixel (0 to 255) a black frame may hold. Video black is 0 or 16
# depending on range; compression noise adds a little.
BLACK_PEAK = 32
SAMPLE_SIZE = "64:36"
TIMEOUT_SECONDS = 30

PeakFn = Callable[[str, Fraction], Optional[int]]


def peak(source: str, at: Fraction) -> Optional[int]:
    """The brightest pixel of *source* at *at* file seconds, or None if unread."""
    if shutil.which("ffmpeg") is None or not Path(source).is_file():
        return None
    args = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
        "-ss", f"{float(at):.3f}", "-i", str(source),
        "-frames:v", "1", "-vf", f"scale={SAMPLE_SIZE},format=gray",
        "-f", "rawvideo", "-",
    ]
    try:
        result = traced_run(args, capture_output=True, timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    data = getattr(result, "stdout", b"") or b""
    if getattr(result, "returncode", 1) != 0 or not isinstance(data, bytes) or not data:
        return None
    return max(data)


def cache_file() -> Path:
    return render.cache_dir() / "black.json"


class Probe:
    """``probe(path, instants) -> bool``: black at every one of *instants*."""

    def __init__(self, cache_path: Optional[Path] = None, peak_fn: Optional[PeakFn] = None):
        self._path = Path(cache_path) if cache_path is not None else None
        self._peak = peak_fn or peak
        self._known: Optional[dict] = None
        self._dirty = False
        self.looked = 0  # frames read this session, not served from the cache

    def _load(self) -> dict:
        if self._known is None:
            if self._path is None:
                self._path = cache_file()
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                self._known = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                self._known = {}
        return self._known

    def _peak_at(self, source: str, stamp: str, at: Fraction) -> Optional[int]:
        known = self._load()
        key = f"{source}|{stamp}|{float(at):.3f}"
        if key in known:
            return known[key]
        self.looked += 1
        value = self._peak(source, at)
        if value is not None:
            known[key] = int(value)
            self._dirty = True
        return value

    def __call__(self, source: str, instants: Iterable[Fraction]) -> bool:
        try:
            st = os.stat(source)
        except OSError:
            return False
        stamp = f"{st.st_mtime_ns}|{st.st_size}"
        seen = False
        for at in dict.fromkeys(Fraction(t) for t in instants):
            value = self._peak_at(source, stamp, at)
            if value is None or value > BLACK_PEAK:
                return False
            seen = True
        return seen

    def save(self) -> None:
        """Write what was learned. Never raises: a cache is not worth a failure."""
        if not self._dirty or self._known is None or self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            scratch = self._path.with_suffix(".tmp")
            scratch.write_text(json.dumps(self._known), encoding="utf-8")
            os.replace(scratch, self._path)
            self._dirty = False
        except OSError:
            pass
