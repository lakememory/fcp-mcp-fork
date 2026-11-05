"""blackness.py: is a clip's picture black where the cut uses it?"""

import json
import shutil
import subprocess
from fractions import Fraction

import pytest

from fcpxml import blackness, render

HAS_FFMPEG = shutil.which("ffmpeg") is not None
MIDDLE, EARLY, LATE = Fraction(5), Fraction(1), Fraction(9)


@pytest.fixture(autouse=True)
def _private_cache(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(render, "cache_dir", lambda: cache)
    return cache


@pytest.fixture
def movie(tmp_path):
    path = tmp_path / "clip.mov"
    path.write_bytes(b"not really a movie")
    return str(path)


def _peaks(values):
    """A peak function that answers from *values* by instant and counts its calls."""
    calls = []

    def fake(source, at):
        calls.append(at)
        return values.get(at)

    return fake, calls


def test_black_at_every_instant_is_black(movie):
    fake, calls = _peaks({MIDDLE: 0, EARLY: 16, LATE: blackness.BLACK_PEAK})
    assert blackness.Probe(peak_fn=fake)(movie, [MIDDLE, EARLY, LATE]) is True
    assert calls == [MIDDLE, EARLY, LATE]


def test_an_ordinary_shot_is_settled_by_its_first_frame(movie):
    fake, calls = _peaks({MIDDLE: 200, EARLY: 0, LATE: 0})
    assert blackness.Probe(peak_fn=fake)(movie, [MIDDLE, EARLY, LATE]) is False
    assert calls == [MIDDLE]


def test_one_bright_frame_is_enough_to_be_picture(movie):
    # A shot that opens on black and comes up is not a black clip.
    fake, _ = _peaks({MIDDLE: 0, EARLY: 0, LATE: blackness.BLACK_PEAK + 1})
    assert blackness.Probe(peak_fn=fake)(movie, [MIDDLE, EARLY, LATE]) is False


def test_a_frame_that_cannot_be_read_is_not_called_black(movie, tmp_path):
    fake, _ = _peaks({MIDDLE: 0, EARLY: None, LATE: 0})
    assert blackness.Probe(peak_fn=fake)(movie, [MIDDLE, EARLY, LATE]) is False
    fake, calls = _peaks({MIDDLE: 0})
    assert blackness.Probe(peak_fn=fake)(str(tmp_path / "missing.mov"), [MIDDLE]) is False
    assert calls == []
    assert blackness.Probe(peak_fn=fake)(movie, []) is False


def test_answers_are_kept_and_a_changed_file_is_looked_at_again(movie, _private_cache):
    fake, calls = _peaks({MIDDLE: 0, EARLY: 0, LATE: 0})
    first = blackness.Probe(peak_fn=fake)
    assert first(movie, [MIDDLE, EARLY, LATE]) and first.looked == 3
    first.save()
    assert len(json.loads((_private_cache / "black.json").read_text())) == 3

    second = blackness.Probe(peak_fn=fake)
    assert second(movie, [MIDDLE, EARLY, LATE]) and second.looked == 0
    assert len(calls) == 3

    with open(movie, "ab") as handle:  # a re-export: same path, different file
        handle.write(b" and more")
    third = blackness.Probe(peak_fn=fake)
    assert third(movie, [MIDDLE, EARLY, LATE]) and third.looked == 3


def test_a_cache_that_cannot_be_read_or_written_costs_nothing(movie, tmp_path):
    broken = tmp_path / "black.json"
    broken.write_text("{not json")
    fake, _ = _peaks({MIDDLE: 0})
    probe = blackness.Probe(cache_path=broken, peak_fn=fake)
    assert probe(movie, [MIDDLE]) is True
    nowhere = blackness.Probe(cache_path=tmp_path / "a-file" / "black.json", peak_fn=fake)
    (tmp_path / "a-file").write_text("in the way")
    assert nowhere(movie, [MIDDLE]) is True
    nowhere.save()  # must not raise


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")
def test_real_files_a_black_movie_is_black_and_colour_bars_are_not(tmp_path):
    def make(name, source):
        path = tmp_path / name
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source,
             "-pix_fmt", "yuv420p", str(path)],
            check=True, capture_output=True,
        )
        return str(path)

    black = make("black.mp4", "color=c=black:size=320x180:rate=24:duration=2")
    bars = make("bars.mp4", "testsrc=size=320x180:rate=24:duration=2")
    instants = [Fraction(1), Fraction(1, 5), Fraction(9, 5)]
    assert blackness.peak(black, Fraction(1)) <= blackness.BLACK_PEAK
    assert blackness.peak(bars, Fraction(1)) > blackness.BLACK_PEAK
    assert blackness.peak(black, Fraction(60)) is None  # past the end: no frame
    probe = blackness.Probe()
    assert probe(black, instants) is True
    assert probe(bars, instants) is False
