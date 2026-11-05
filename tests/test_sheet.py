"""sheet.py: contact sheets on the timeline's clock."""

import shutil
import subprocess
from fractions import Fraction

import pytest

from fcpxml import coverage, render, sheet
from tests.cuts import BASE_SPINE, base, cut, is_black, masked, worse

HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture(autouse=True)
def _private_cache(tmp_path, monkeypatch):
    """Sheets and frames go to the preview cache; tests never write the real one."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(render, "cache_dir", lambda: cache)
    return cache


def test_a_tile_every_step_seconds_names_the_shot_and_the_line():
    tiles = sheet.plan(base(), step=Fraction(5))
    assert [float(t.at) for t in tiles] == [0, 5, 10, 15, 20, 25]
    assert [t.shot for t in tiles] == ["Shot A", "Shot A", "Shot B", "Shot B", "Shot C", "Shot C"]
    assert [t.said for t in tiles] == ["", "", "", "N02 second line", "", "N03 third line"]
    # The yellow bar: the picture changed since the previous tile.
    assert [t.cut for t in tiles] == [False, False, True, False, True, False]


def test_a_tile_reads_the_file_at_the_position_the_timeline_maps_to():
    tiles = sheet.plan(base(), start=Fraction(13), end=Fraction(14), step=Fraction(1))
    assert len(tiles) == 1
    assert tiles[0].source.endswith("/nowhere/picture.mov")
    assert tiles[0].file_time == Fraction(13)


def test_black_is_a_tile_of_its_own_with_no_source():
    tiles = sheet.plan(worse(), start=Fraction(8), end=Fraction(18), step=Fraction(2))
    black = [t for t in tiles if t.black]
    assert [float(t.at) for t in black] == [10, 12, 14]
    assert all(t.source == "" for t in black)
    assert black[0].shot == 'NO PICTURE: gap "BLACK: a shot still to come"'
    assert black[1].said == "N02 second line"
    assert black[0].cut and not black[1].cut


def test_a_run_that_falls_between_two_samples_still_gets_a_tile():
    sliver = '<asset-clip ref="pic" offset="20s" name="Shot C" start="20s" duration="10s"/>'
    spine = BASE_SPINE.replace(
        sliver,
        '<asset-clip ref="pic" offset="20s" name="Shot C" start="20s" duration="10s">'
        '<asset-clip ref="pic" lane="1" offset="45/2s" name="Sliver" start="70s" duration="1/2s"/>'
        "</asset-clip>",
    )
    tiles = sheet.plan(cut(spine), start=Fraction(20), end=Fraction(30), step=Fraction(2))
    extra = [t for t in tiles if t.extra]
    # 22.5s to 23.0s holds no sample at 20, 22, 24...; it is shown at its middle.
    assert [(t.shot, float(t.at)) for t in extra] == [("Sliver [lane 1]", 22.75)]
    assert [float(t.at) for t in tiles] == sorted(float(t.at) for t in tiles)


def test_a_movie_is_sampled_at_timeline_time_and_keeps_the_labels():
    tiles = sheet.plan(worse(), start=Fraction(10), end=Fraction(14), step=Fraction(2),
                       movie="/somewhere/export.mov")
    assert [(t.source, float(t.file_time)) for t in tiles] == [
        ("/somewhere/export.mov", 10.0), ("/somewhere/export.mov", 12.0),
    ]
    # The export is the truth about what is black, so the tile is read, not drawn.
    assert not any(t.black for t in tiles)
    assert tiles[0].shot.startswith("NO PICTURE")


def test_a_bad_range_or_step_is_refused():
    with pytest.raises(ValueError, match="nothing to show"):
        sheet.plan(base(), start=Fraction(40), end=Fraction(50))
    with pytest.raises(ValueError, match="step"):
        sheet.plan(base(), step=Fraction(0))


def test_tiles_are_paged_twenty_to_a_sheet():
    tiles = sheet.plan(base(), step=Fraction(1))
    groups = sheet.pages(tiles)
    assert [len(g) for g in groups] == [20, 10]


def test_the_tile_line_is_what_the_text_index_shows():
    tile = sheet.plan(base(), start=Fraction(13), end=Fraction(14), step=Fraction(1))[0]
    assert tile.line == " 0:13.0  Shot B | N02 second line"


# --- drawing (needs Pillow) -------------------------------------------------

PIL = pytest.importorskip("PIL")


def _solid(tmp_path, colour):
    from PIL import Image

    def frame(source, at):
        target = tmp_path / f"frame_{float(at):.2f}.jpg"
        Image.new("RGB", (426, 240), colour).save(target, "JPEG")
        return str(target)

    return frame


def test_a_sheet_is_drawn_at_the_width_a_model_reads(tmp_path):
    from PIL import Image

    tiles = sheet.plan(worse(), start=Fraction(0), end=Fraction(36), step=Fraction(2))
    path, unread = sheet.compose(tiles, str(tmp_path / "sheet.jpg"), title="worse",
                                 frame=_solid(tmp_path, (20, 160, 60)))
    assert unread == []
    with Image.open(path) as image:
        assert image.width == sheet.SHEET_WIDTH == 1568
        rows = -(-len(tiles) // sheet.COLUMNS)
        assert image.height == sheet.HEADER_HEIGHT + rows * (sheet.TILE_HEIGHT + sheet.LABEL_HEIGHT)

        def centre(index):
            x = (index % sheet.COLUMNS) * sheet.TILE_WIDTH + sheet.TILE_WIDTH // 2
            y = (sheet.HEADER_HEIGHT + (index // sheet.COLUMNS) * (sheet.TILE_HEIGHT + sheet.LABEL_HEIGHT)
                 + sheet.TILE_HEIGHT // 4)
            return image.getpixel((x, y))

        picture = centre(0)
        assert picture[1] > 120 and picture[0] < 80  # the green frame was pasted
        black_index = next(i for i, t in enumerate(tiles) if t.black)
        assert max(centre(black_index)) < 40  # drawn black, not read from a file
        # A red border marks it, so black cannot be mistaken for a dark shot.
        # Sampled on the right edge: the left one carries the yellow cut bar.
        x = (black_index % sheet.COLUMNS + 1) * sheet.TILE_WIDTH - 5
        y = (sheet.HEADER_HEIGHT + (black_index // sheet.COLUMNS) * (sheet.TILE_HEIGHT + sheet.LABEL_HEIGHT)
             + sheet.TILE_HEIGHT // 2)
        edge = image.getpixel((x, y))
        assert edge[0] > 150 and edge[1] < 90


def test_a_frame_that_cannot_be_read_is_reported_not_left_blank(tmp_path):
    tiles = sheet.plan(base(), step=Fraction(10))
    path, unread = sheet.compose(tiles, str(tmp_path / "sheet.jpg"), frame=lambda source, at: None)
    assert unread == ["Shot A", "Shot B", "Shot C"]


def test_render_sheets_stops_at_max_sheets_and_says_where_to_resume(_private_cache, tmp_path):
    result = sheet.render_sheets(
        base(), name="base.fcpxml", sha256="ab" * 32, step=Fraction(1), max_sheets=1,
        frame=_solid(tmp_path, (90, 90, 200)),
    )
    assert result["total_sheets"] == 2 and len(result["sheets"]) == 1
    assert result["resume"] == Fraction(20)
    path = result["sheets"][0]["path"]
    assert path.startswith(str(_private_cache / "sheets"))
    assert "base.abababab." in path and path.endswith(".jpg")


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")
def test_the_default_frame_is_read_from_the_source_file_and_cached(tmp_path, _private_cache):
    source = tmp_path / "bars.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=2",
         "-pix_fmt", "yuv420p", str(source)],
        check=True, capture_output=True,
    )
    first = sheet.cached_frame(str(source), Fraction(1))
    assert first and first.startswith(str(_private_cache / "frames"))
    stamp = (_private_cache / "frames").stat().st_mtime_ns
    assert sheet.cached_frame(str(source), Fraction(1)) == first
    assert (_private_cache / "frames").stat().st_mtime_ns == stamp  # nothing new written
    assert sheet.cached_frame(str(tmp_path / "missing.mp4"), Fraction(1)) is None


def test_a_black_movie_is_drawn_and_named_as_black(tmp_path):
    timeline = masked()
    plain = sheet.plan(timeline, start=Fraction(8), end=Fraction(14), step=Fraction(2))
    assert [t.black for t in plain] == [False, False, False]  # read as picture without the test
    tiles = sheet.plan(timeline, start=Fraction(8), end=Fraction(14), step=Fraction(2),
                       found=coverage.items(timeline, is_black))
    assert [t.black for t in tiles] == [False, True, True]
    assert tiles[1].shot == "NO PICTURE: black clip: BLACKOUT from 0:10" and tiles[1].source == ""
    assert tiles[2].said == "N02 second line"
    result = sheet.render_sheets(
        timeline, name="masked.fcpxml", start=Fraction(8), end=Fraction(14),
        frame=_solid(tmp_path, (90, 90, 200)), dark=is_black,
    )
    assert [t.black for t in result["sheets"][0]["tiles"]] == [False, True, True]
