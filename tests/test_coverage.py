"""coverage.py: what is on screen and audible at each instant."""

from dataclasses import replace
from fractions import Fraction

from fcpxml import coverage
from tests.cuts import BASE_SPINE, base, cut, is_black, masked, worse


def _names(spans):
    return [(float(s.start), float(s.end), s.item.name if s.item else None) for s in spans]


def test_kinds_come_from_the_file_and_the_role():
    kinds = {i.name: i.kind for i in coverage.items(base())}
    assert kinds["Shot A"] == coverage.PICTURE
    # A .wav asset with no hasVideo attribute is sound, not picture.
    assert kinds["N01 first line"] == coverage.NARRATION
    assert kinds["MUSIC bed"] == coverage.MUSIC


def test_a_read_with_no_role_is_narration_by_its_storyline():
    spine = BASE_SPINE.replace(' audioRole="dialogue"', "")
    kinds = {i.name: i.kind for i in coverage.items(cut(spine))}
    assert kinds["N02 second line"] == coverage.NARRATION


def test_picture_runs_start_to_end_with_no_holes():
    spans = coverage.picture(base())
    assert _names(spans) == [(0, 10, "Shot A"), (10, 20, "Shot B"), (20, 30, "Shot C")]
    assert coverage.black(base()) == []


def test_a_gap_is_black_and_is_named_by_the_document():
    timeline = worse()
    holes = coverage.black(timeline)
    assert [(float(h.start), float(h.end)) for h in holes] == [(10.0, 16.0)]
    assert coverage.why_black(holes[0], timeline) == 'gap "BLACK: a shot still to come"'


def test_a_switched_off_primary_clip_is_black_and_says_which():
    spine = BASE_SPINE.replace('name="Shot B" start="10s"', 'name="Shot B" enabled="0" start="10s"')
    timeline = cut(spine)
    holes = coverage.black(timeline)
    assert [(float(h.start), float(h.end)) for h in holes] == [(10.0, 20.0)]
    assert coverage.why_black(holes[0], timeline) == "switched off: Shot B"


def test_the_top_enabled_lane_is_what_is_on_screen():
    cutaway = (
        '<asset-clip ref="pic" offset="10s" name="Shot B" start="10s" duration="10s">'
        '<asset-clip ref="pic" lane="1" offset="12s" name="Cutaway" start="50s" duration="4s"/>'
        "</asset-clip>"
    )
    spine = BASE_SPINE.replace(
        '<asset-clip ref="pic" offset="10s" name="Shot B" start="10s" duration="10s"/>', cutaway
    )
    assert _names(coverage.picture(cut(spine))) == [
        (0, 10, "Shot A"), (10, 12, "Shot B"), (12, 16, "Cutaway"), (16, 20, "Shot B"),
        (20, 30, "Shot C"),
    ]
    off = spine.replace('name="Cutaway"', 'name="Cutaway" enabled="0"')
    assert _names(coverage.picture(cut(off))) == [
        (0, 10, "Shot A"), (10, 20, "Shot B"), (20, 30, "Shot C"),
    ]


def test_a_clip_scaled_into_a_corner_does_not_cover_what_is_under_it():
    inset = (
        '<asset-clip ref="pic" offset="10s" name="Shot B" start="10s" duration="10s">'
        '<asset-clip ref="pic" lane="1" offset="12s" name="Inset" start="50s" duration="4s">'
        '<adjust-transform scale="0.4 0.4" position="30 20"/></asset-clip>'
        "</asset-clip>"
    )
    spine = BASE_SPINE.replace(
        '<asset-clip ref="pic" offset="10s" name="Shot B" start="10s" duration="10s"/>', inset
    )
    spans = coverage.picture(cut(spine))
    middle = next(s for s in spans if s.start == 12)
    assert middle.item.name == "Shot B"
    assert [i.name for i in middle.over] == ["Inset"]
    assert not middle.black


def test_narration_over_black_is_found_with_the_part_that_is_uncovered():
    found = coverage.uncovered(worse())
    assert [(i.name, float(part.start), float(part.end)) for i, part in found] == [
        ("N02 second line", 11.0, 15.0),
    ]
    assert coverage.uncovered(base()) == []


def test_at_names_the_picture_and_the_sound_at_an_instant():
    span, heard = coverage.at(base(), Fraction(13))
    assert span.item.name == "Shot B"
    assert sorted(i.name for i in heard) == ["MUSIC bed", "N02 second line"]


def test_file_time_follows_the_clips_own_clock():
    shot_b = next(i for i in coverage.items(base()) if i.name == "Shot B")
    # On the timeline at 10s, and its clock starts at 10s: 13s on the
    # timeline is 13s into the file.
    assert shot_b.file_time(Fraction(13)) == Fraction(13)


def test_file_time_subtracts_the_assets_clock_origin():
    spine = '<asset-clip ref="cam" offset="0s" name="Camera" start="3602s" duration="10s"/>'
    camera = coverage.items(cut(spine, "10s"))[0]
    # The asset's clock starts at 01:00:00:00; the file starts at 0.
    assert camera.file_time(Fraction(3)) == Fraction(5)


def test_file_time_goes_through_the_time_map_of_a_retimed_clip():
    spine = (
        '<asset-clip ref="pic" offset="0s" name="Slow" start="0s" duration="6s">'
        '<timeMap><timept time="0s" value="0s" interp="linear"/>'
        '<timept time="6s" value="9/2s" interp="linear"/></timeMap></asset-clip>'
    )
    slow = coverage.items(cut(spine, "6s"))[0]
    assert slow.retimed
    # Six seconds of screen time show four and a half seconds of source.
    assert slow.file_time(Fraction(3)) == Fraction(9, 4)
    assert slow.file_time(Fraction(6)) == Fraction(9, 2)


def test_file_time_sits_on_the_files_frame_grid():
    # A freeze written at 8.04s of a 24 fps file (V19's Hickey shot: the
    # file is 193 frames, 8.0417s long). 8.04s is inside the last frame,
    # which began at 8.0s; a seek to 8.04s finds no frame at all.
    spine = (
        '<asset-clip ref="pic" offset="0s" name="Freeze" start="0s" duration="20s">'
        '<timeMap><timept time="0s" value="0s" interp="linear"/>'
        '<timept time="193/12s" value="201/25s" interp="linear"/>'
        '<timept time="20s" value="201/25s" interp="linear"/></timeMap></asset-clip>'
    )
    freeze = coverage.items(cut(spine, "20s"))[0]
    assert freeze.frame == Fraction(1, 24)
    assert freeze.file_time(Fraction(18)) == Fraction(8)
    assert freeze.file_time(Fraction(20)) == Fraction(8)
    # Mid-frame instants show the frame that began before them (the clip
    # plays at half speed, so timeline 3/24s is file 3/48s: the second frame).
    assert freeze.file_time(Fraction(1, 48)) == Fraction(0)
    assert freeze.file_time(Fraction(3, 24)) == Fraction(1, 24)


def test_without_a_declared_frame_rate_file_time_is_not_snapped():
    shot = coverage.items(cut(
        '<asset-clip ref="pic" offset="0s" name="Shot" start="0s" duration="2s"/>', "2s"
    ))[0]
    mid = replace(shot, clip_start=Fraction(1, 48))
    assert mid.file_time(Fraction(0)) == Fraction(0)
    assert replace(mid, frame=None).file_time(Fraction(0)) == Fraction(1, 48)


def test_total_is_the_sequences_own_duration():
    assert coverage.total(worse()) == Fraction(36)


def test_gaps_keep_their_name_and_markers():
    gap = worse().gaps[0]
    assert gap.name == "BLACK: a shot still to come"
    assert gap.start.seconds == 10.0
    assert [m.name for m in gap.markers] == ["SHOT LIST 1: something goes here"]
    assert gap.markers[0].timeline_start.seconds == 11.0


def test_a_black_movie_on_top_is_black_and_hides_what_is_under_it():
    timeline = masked()
    pool = coverage.items(timeline, is_black)
    slug = next(i for i in pool if i.name == "BLACKOUT from 0:10")
    assert slug.black and not any(i.black for i in pool if i is not slug)
    spans = coverage.picture(timeline, pool)
    assert _names(spans) == [(0, 10, "Shot A"), (10, 30, None)]
    assert spans[1].black and spans[1].slug is slug
    assert coverage.why_black(spans[1], timeline, pool) == "black clip: BLACKOUT from 0:10"
    over = coverage.uncovered(timeline, coverage.NARRATION, pool)
    assert [item.name for item, _ in over] == ["N02 second line", "N03 third line"]


def test_without_looking_at_files_a_black_movie_is_picture():
    # The limit the XML has on its own, and why dark exists.
    timeline = masked()
    assert _names(coverage.picture(timeline)) == [(0, 10, "Shot A"), (10, 30, "BLACKOUT from 0:10")]
    assert coverage.black(timeline) == []


def test_a_clip_is_asked_about_inside_the_range_it_plays():
    asked = {}

    def dark(path, instants):
        asked[path.rsplit("/", 1)[-1]] = [float(t) for t in instants]
        return False

    spine = BASE_SPINE.replace(
        '<asset-clip ref="pic" offset="20s" name="Shot C" start="20s" duration="10s"/>',
        '<asset-clip ref="cam" offset="20s" name="Shot C" start="3640s" duration="10s"/>',
    ).replace(
        '<asset-clip ref="pic" offset="10s" name="Shot B" start="10s" duration="10s"/>',
        '<asset-clip ref="slug" offset="10s" name="Shot B" start="100s" duration="10s" enabled="0"/>',
    )
    coverage.items(cut(spine), dark)
    # Shot C plays file seconds 40 to 50 (its asset clock starts at 3600):
    # the middle first, then a tenth and nine tenths of the way through.
    assert asked == {"picture.mov": [45.0, 41.0, 49.0]}  # the switched-off clip is not asked about
