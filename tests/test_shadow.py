"""shadow.py: the cut as it plays, as text, and its command line."""

from fractions import Fraction

import pytest

from fcpxml import shadow
from tests.cuts import BASE_SPINE, WORSE_SPINE, base, cut, is_black, masked, worse, xml


def _rows(text):
    body = text.split("THE CUT", 1)[1].split("\n\n", 1)[0]
    return body.splitlines()[2:]


def test_one_row_per_change_of_picture_or_start_of_a_line():
    text, _ = shadow.render(base(), name="base.fcpxml")
    rows = _rows(text)
    assert [r.split()[0] for r in rows] == ["0:00.0", "0:01.0", "0:10.0", "0:12.0", "0:20.0", "0:22.0"]
    assert "Shot A" in rows[0] and "MUSIC bed" in rows[0]
    assert "N01 first line" in rows[1] and "Shot" not in rows[1]
    assert "Shot B" in rows[2] and rows[2].split()[1] == "10.0"


def test_the_header_names_the_file_and_what_was_read():
    text, _ = shadow.render(base(), name="base.fcpxml", sha256="ab" * 32)
    first = text.splitlines()[0]
    assert first.startswith("SHADOW  base.fcpxml  sha256 abababababab  0:30.0  24 fps")
    assert "3 primary clips, 4 connected (0 switched off)" in first
    assert "not from Final Cut" in text


def test_black_is_a_row_and_the_line_over_it_is_flagged():
    text, report = shadow.render(worse(), name="worse.fcpxml", base=base(), base_name="base.fcpxml")
    rows = _rows(text)
    black = next(r for r in rows if r.split()[0] == "0:10.0")
    assert '-- BLACK: gap "BLACK: a shot still to come"' in black
    line = next(r for r in rows if "N02 second line" in r)
    assert line.rstrip().endswith("<< over black")
    assert len(report.failures) == 2


def test_the_checks_and_the_comparison_sit_above_the_rows():
    text, _ = shadow.render(worse(), name="worse.fcpxml", base=base(), base_name="base.fcpxml")
    assert text.index("CHECKS  2 FAIL, 0 inherited from the base") < text.index("AGAINST BASE")
    assert text.index("AGAINST BASE") < text.index("THE CUT")
    assert "base    base.fcpxml" in text
    assert "runtime                    0:30.0  ->  0:36.0  (+6.0 s)" in text
    assert "moved +6.0 s as one block, 2 clips: Shot B  to  Shot C" in text
    assert "moved -1.0 s: N02 second line" in text


def test_without_a_base_it_says_nothing_can_be_inherited():
    text, _ = shadow.render(worse(), name="worse.fcpxml")
    assert "CHECKS  2 FAIL\n" in text
    assert "no base given" in text
    assert "MEASURED" in text and "AGAINST BASE" not in text


def test_what_is_switched_off_is_listed_and_not_in_the_rows():
    spine = BASE_SPINE.replace('name="N02 second line"', 'name="N02 second line" enabled="0"').replace(
        'name="Shot B" start="10s"', 'name="Shot B" enabled="0" start="10s"')
    text, _ = shadow.render(cut(spine), name="off.fcpxml")
    assert not [r for r in _rows(text) if "N02 second line" in r]
    assert "-- BLACK: switched off: Shot B" in text
    tail = text.split("SWITCHED OFF", 1)[1]
    assert "2 clip(s)" in tail
    assert "0:10.0 to 0:20.0  primary  Shot B" in tail
    assert "1 narration clip(s) in 'NEW READ'" in tail


def test_a_window_limits_the_rows_but_not_the_checks():
    text, report = shadow.render(worse(), name="worse.fcpxml", start=Fraction(20), end=Fraction(30))
    rows = _rows(text)
    assert rows and all(r.split()[0] >= "0:16.0" for r in rows)
    assert not [r for r in rows if "Shot A" in r]
    assert "THE CUT  0:20.0 to 0:30.0" in text
    assert len(report.failures) == 2


def test_checks_only_leaves_the_rows_out():
    text, _ = shadow.render(worse(), name="worse.fcpxml", checks_only=True)
    assert "CHECKS" in text and "THE CUT" not in text


def test_markers_are_listed_under_their_row_when_asked():
    plain, _ = shadow.render(worse(), name="worse.fcpxml")
    assert "SHOT LIST 1" not in plain
    text, _ = shadow.render(worse(), name="worse.fcpxml", markers=True)
    assert "marker 0:11.0 on BLACK: a shot still to come: SHOT LIST 1: something goes here" in text


def test_a_long_list_of_separate_moves_is_capped_with_a_summary():
    lines = []
    shifted = []
    for n in range(20):
        lines.append(f'<asset-clip ref="vo" lane="-1" offset="{n}s" name="L{n:02d}" start="{n}s" duration="1/2s" audioRole="dialogue"/>')
        shifted.append(f'<asset-clip ref="vo" lane="-1" offset="{n * 26}/24s" name="L{n:02d}" start="{n}s" duration="1/2s" audioRole="dialogue"/>')
    shot = '<asset-clip ref="pic" offset="0s" name="Shot" start="0s" duration="30s">{}</asset-clip>'
    text, _ = shadow.render(cut(shot.format("".join(shifted))), base=cut(shot.format("".join(lines))))
    assert "and 5 more separate moves (5 clips)" in text


def test_times_are_read_the_way_notes_write_them():
    assert shadow.parse_time("1:28.7") == Fraction(887, 10)
    assert shadow.parse_time("88.7") == Fraction(887, 10)
    assert shadow.parse_time("3600/24s") == Fraction(150)
    assert shadow.parse_time("1:02:03") == Fraction(3723)
    assert shadow.parse_time(12) == Fraction(12)
    with pytest.raises(ValueError, match="not a time"):
        shadow.parse_time("soon")


def test_scope_reads_one_or_several_windows():
    assert shadow.parse_scope("0:19-1:33") == [(Fraction(19), Fraction(93))]
    assert shadow.parse_scope("0:19 to 1:33, 300-310") == [
        (Fraction(19), Fraction(93)), (Fraction(300), Fraction(310)),
    ]
    assert shadow.parse_scope(None) == []
    with pytest.raises(ValueError, match="ends before it starts"):
        shadow.parse_scope("1:33-0:19")


def test_the_command_line_exits_1_on_a_failure_and_0_when_clean(tmp_path, capsys):
    good, bad = tmp_path / "base.fcpxml", tmp_path / "worse.fcpxml"
    present = tmp_path / "picture.mov"
    present.write_bytes(b"")
    good.write_text(xml(BASE_SPINE, "30s", picture=present.as_uri()))
    bad.write_text(xml(WORSE_SPINE, "36s", picture=present.as_uri()))

    assert shadow.main([str(bad), "--base", str(good)]) == 1
    out = capsys.readouterr().out
    assert "SHADOW  worse.fcpxml  sha256 " in out and "base    base.fcpxml" in out
    assert "FAIL      black" in out

    # The read and the bed do not exist on disk, and the command line checks.
    assert shadow.main([str(good), "--checks"]) == 1
    assert "media" in capsys.readouterr().out


def test_the_command_line_reports_a_file_it_cannot_read(tmp_path, capsys):
    assert shadow.main([str(tmp_path / "missing.fcpxml")]) == 2
    assert "shadow:" in capsys.readouterr().err


def test_a_bundle_is_read_through_its_info_file(tmp_path):
    bundle = tmp_path / "cut.fcpxmld"
    bundle.mkdir()
    (bundle / "Info.fcpxml").write_text(xml(BASE_SPINE))
    assert shadow.document_path(str(bundle)) == bundle / "Info.fcpxml"
    assert len(shadow.load(str(bundle)).clips) == 3
    assert len(shadow.file_sha256(str(bundle))) == 64


def test_a_black_movie_is_a_black_row_and_every_line_under_it_is_flagged():
    text, report = shadow.render(masked(), name="masked.fcpxml", base=base(), dark=is_black)
    rows = _rows(text)
    black = next(r for r in rows if r.split()[0] == "0:10.0")
    assert "-- BLACK: black clip: BLACKOUT from 0:10" in black
    for name in ("N02 second line", "N03 third line"):
        assert next(r for r in rows if name in r).rstrip().endswith("<< over black")
    assert not next(r for r in rows if "N01 first line" in r).rstrip().endswith("<< over black")
    assert "FAIL      black         0:10.0 to 0:30.0  20.0 s with no picture: black clip" in text
    assert "A clip whose file is black where it plays counts as black." in text
    assert len(report.failures) == 3


def test_the_view_says_when_files_were_not_looked_at():
    text, report = shadow.render(masked(), name="masked.fcpxml", base=base())
    assert "Files were not looked at: a black clip counts as picture." in text
    assert report.failures == []
