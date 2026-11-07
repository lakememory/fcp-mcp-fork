"""checks.py: what a cut must never do by accident, and how it differs from its base."""

from fractions import Fraction

from fcpxml import checks
from tests.cuts import BASE_SPINE, MASKED_SPINE, WORSE_SPINE, base, cut, is_black, masked, worse


def _codes(report, level=checks.FAIL):
    return [f.code for f in report.findings if f.level == level]


def _fact(report, label):
    return next((before, now) for name, before, now in report.facts if name == label)


def test_a_clean_cut_has_no_findings():
    report = checks.run(base())
    assert report.findings == []
    assert _fact(report, "runtime") == (None, "0:30.0")
    assert _fact(report, "black on screen") == (None, "0.0 s")


def test_new_black_fails_even_when_the_gap_is_named():
    # The build named its slot "BLACK: a shot still to come". A name is a
    # label, not permission: the base had picture there.
    report = checks.run(worse(), base())
    black = [f for f in report.findings if f.code == "black"]
    assert len(black) == 1 and black[0].level == checks.FAIL
    assert (black[0].start, black[0].end) == (Fraction(10), Fraction(16))
    assert "BLACK: a shot still to come" in black[0].text


def test_narration_over_black_fails_and_names_the_line():
    report = checks.run(worse(), base())
    over = [f for f in report.findings if f.code == "vo-uncovered"]
    assert len(over) == 1 and over[0].level == checks.FAIL
    assert "N02 second line" in over[0].text and "4.0 s" in over[0].text


def test_black_that_was_already_in_the_base_is_inherited_not_failed():
    report = checks.run(worse(), worse())
    assert _codes(report) == []
    assert sorted(_codes(report, checks.INHERITED)) == ["black", "vo-uncovered"]


def test_black_that_grew_fails_and_says_what_it_was():
    longer = WORSE_SPINE.replace(
        'offset="10s" start="0s" duration="6s"', 'offset="10s" start="0s" duration="9s"'
    ).replace('offset="16s" name="Shot B"', 'offset="19s" name="Shot B"').replace(
        'offset="26s" name="Shot C"', 'offset="29s" name="Shot C"'
    )
    report = checks.run(cut(longer, "39s"), worse())
    black = [f for f in report.findings if f.code == "black"]
    assert black[0].level == checks.FAIL
    assert "was 6.0 s in the base" in black[0].text


def test_a_named_hole_is_the_same_hole_when_a_neighbour_changes():
    # The base's named gap, now followed by a different shot and shorter.
    renamed = WORSE_SPINE.replace('name="Shot B" start="10s"', 'name="Another shot" start="40s"').replace(
        'offset="10s" start="0s" duration="6s"', 'offset="10s" start="0s" duration="4s"'
    ).replace('offset="16s" name="Another shot"', 'offset="14s" name="Another shot"').replace(
        'offset="26s" name="Shot C"', 'offset="24s" name="Shot C"')
    report = checks.run(cut(renamed, "34s"), worse())
    assert [f.level for f in report.findings if f.code == "black"] == [checks.INHERITED]


def test_without_a_base_every_finding_counts():
    report = checks.run(worse())
    assert sorted(_codes(report)) == ["black", "vo-uncovered"]
    assert not report.has_base


def test_the_facts_measure_the_build_against_its_base():
    report = checks.run(worse(), base())
    assert _fact(report, "runtime") == ("0:30.0", "0:36.0  (+6.0 s)")
    assert _fact(report, "black on screen") == ("0.0 s", "6.0 s  (+6.0 s)")
    before, now = _fact(report, "longest narration silence")
    assert before.startswith("7.0 s from 0:05.0")
    assert now.startswith("13.0 s from 0:15.0") and "(+6.0 s)" in now


def test_clips_that_travelled_together_are_one_block():
    report = checks.run(worse(), base())
    runs = {(kind, float(delta)): [i.name for i in moved]
            for kind, delta, moved in checks.blocks(report.changes)}
    assert runs[("picture", 6.0)] == ["Shot B", "Shot C"]
    assert runs[("narration", -1.0)] == ["N02 second line"]
    assert runs[("narration", 6.0)] == ["N03 third line"]
    assert report.unmoved == {"picture": 1, "narration": 1, "music": 1}


def test_two_narration_lines_at_once_fail():
    # N02 now starts at 4s while N01 runs to 5s.
    spine = BASE_SPINE.replace('offset="12s" name="N02 second line"', 'offset="4s" name="N02 second line"')
    report = checks.run(cut(spine))
    double = [f for f in report.findings if f.code == "vo-double"]
    assert len(double) == 1 and "N01 first line" in double[0].text
    assert (double[0].start, double[0].end) == (Fraction(4), Fraction(5))
    # The same overlap in the base is inherited, not this pass's doing.
    again = checks.run(cut(spine), cut(spine))
    assert [f.level for f in again.findings if f.code == "vo-double"] == [checks.INHERITED]


def test_a_narration_line_that_went_missing_fails():
    spine = BASE_SPINE.replace(
        '<asset-clip ref="vo" offset="12s" name="N02 second line" start="4s" duration="4s" audioRole="dialogue"/>',
        '<gap name="Gap" offset="12s" start="0s" duration="4s"/>',
    )
    report = checks.run(cut(spine), base())
    lost = [f for f in report.findings if f.code == "vo-lost"]
    assert len(lost) == 1 and "N02 second line" in lost[0].text and "0:12.0" in lost[0].text


def test_a_switched_off_narration_line_is_lost_too():
    spine = BASE_SPINE.replace('name="N02 second line"', 'name="N02 second line" enabled="0"')
    report = checks.run(cut(spine), base())
    assert "vo-lost" in _codes(report)
    assert [c.what for c in report.changes if c.item.name == "N02 second line"] == ["switched off"]


def test_narration_out_of_order_fails():
    spine = (BASE_SPINE
             .replace('offset="12s" name="N02 second line"', 'offset="22s" name="N02 second line"')
             .replace('offset="22s" name="N03 third line"', 'offset="12s" name="N03 third line"'))
    report = checks.run(cut(spine), base())
    order = [f for f in report.findings if f.code == "vo-order"]
    assert len(order) == 1
    assert "'N03 third line' now comes before 'N02 second line'" in order[0].text


def test_a_run_of_picture_too_short_to_see_is_a_flash():
    spine = (BASE_SPINE
             .replace('name="Shot B" start="10s" duration="10s"', 'name="Shot B" start="10s" duration="239/24s"')
             + '<asset-clip ref="pic" offset="719/24s" name="Sliver" start="90s" duration="1/24s"/>')
    spine = spine.replace('offset="20s" name="Shot C"', 'offset="479/24s" name="Shot C"')
    timeline = cut(spine)
    report = checks.run(timeline)
    flash = [f for f in report.findings if f.code == "flash"]
    assert len(flash) == 1 and "'Sliver' is on screen for 1 frame(s)" in flash[0].text


def test_a_change_outside_the_stated_scope_fails():
    inside = checks.run(worse(), base(), scope=[(Fraction(0), Fraction(40))])
    assert "scope" not in _codes(inside)
    outside = checks.run(worse(), base(), scope=[(Fraction(8), Fraction(18))])
    scope = [f for f in outside.findings if f.code == "scope"]
    # Shot C (20-30 -> 26-36) and N03 moved, and both sit outside 0:08-0:18.
    assert len(scope) == 1 and "2 clip(s) changed outside the stated scope" in scope[0].text


def test_missing_media_is_checked_only_when_asked():
    assert "media" not in _codes(checks.run(base()))
    report = checks.run(base(), exists=lambda path: not path.endswith("picture.mov"))
    media = [f for f in report.findings if f.code == "media"]
    assert len(media) == 1 and "3 enabled clip(s) have no file" in media[0].text


def test_clock_reads_like_a_note():
    assert checks.clock(Fraction(887, 10)) == "1:28.7"
    assert checks.clock(Fraction(0)) == "0:00.0"
    assert checks.clock(Fraction(59996, 100)) == "10:00.0"


def test_a_black_movie_laid_over_the_picture_is_new_black():
    report = checks.run(masked(), base(), dark=is_black)
    black = [f for f in report.findings if f.code == "black"]
    assert len(black) == 1 and black[0].level == checks.FAIL
    assert (black[0].start, black[0].end) == (Fraction(10), Fraction(30))
    assert "black clip: BLACKOUT from 0:10" in black[0].text
    assert _fact(report, "black on screen") == ("0.0 s", "20.0 s  (+20.0 s)")
    assert _codes(report).count("vo-uncovered") == 2


def test_without_the_file_test_the_same_cut_passes():
    # Mutation check for the one above: take dark away and the mask is "picture".
    report = checks.run(masked(), base())
    assert report.findings == []
    assert _fact(report, "black on screen") == ("0.0 s", "0.0 s  (+0.0 s)")


def test_a_black_movie_that_was_already_in_the_base_is_inherited():
    report = checks.run(masked(), masked(), dark=is_black)
    assert _codes(report) == []
    assert sorted(_codes(report, checks.INHERITED)) == ["black", "vo-uncovered", "vo-uncovered"]


def _many_lines(count):
    """BASE's read replaced by *count* two-second lines from 0:11, under the mask."""
    lines = ['<gap name="Gap" offset="0s" start="0s" duration="11s"/>']
    for n in range(count):
        lines.append(
            f'<asset-clip ref="vo" offset="{11 + 3 * n}s" name="L{n + 1:02d} line" '
            f'start="{3 * n}s" duration="2s" audioRole="dialogue"/>'
        )
        lines.append(f'<gap name="Gap" offset="{13 + 3 * n}s" start="0s" duration="1s"/>')
    head, _, rest = MASKED_SPINE.partition('<spine lane="-1" offset="0s" name="NEW READ">')
    _, _, tail = rest.partition("</spine>")
    return head + '<spine lane="-1" offset="0s" name="NEW READ">' + "".join(lines) + "</spine>" + tail


def test_many_lines_over_one_black_are_one_finding_that_names_the_first_and_last():
    report = checks.run(cut(_many_lines(4)), dark=is_black)
    over = [f for f in report.findings if f.code == "vo-uncovered"]
    assert len(over) == 1 and over[0].level == checks.FAIL
    assert (over[0].start, over[0].end) == (Fraction(11), Fraction(22))
    assert over[0].text == (
        "4 narration lines play over this black for 8.0 s in all: 'L01 line' to 'L04 line'"
    )
    # The report still knows each line, for the rows of the shadow view.
    assert [item.name for item, _, _ in report.over_black] == [
        "L01 line", "L02 line", "L03 line", "L04 line"
    ]


def test_up_to_three_lines_over_one_black_keep_a_row_each():
    report = checks.run(cut(_many_lines(checks.LINES_PER_BLACK)), dark=is_black)
    over = [f for f in report.findings if f.code == "vo-uncovered"]
    assert [f.text.split("'")[1] for f in over] == ["L01 line", "L02 line", "L03 line"]


def test_a_black_movie_is_the_same_black_when_the_shot_before_it_changes():
    swapped = MASKED_SPINE.replace('name="Shot A"', 'name="Shot A, a different take"')
    report = checks.run(cut(swapped), masked(), dark=is_black)
    black = [f for f in report.findings if f.code == "black"]
    assert len(black) == 1 and black[0].level == checks.INHERITED


def test_a_gap_stays_inherited_when_the_shot_after_it_is_switched_off():
    # V19 against V16: the orange slot was V16's; the chicken hash after it
    # is new black. Before holes split at their cause, the two read as one
    # grown hole and the line over the old slot failed with it.
    spine = WORSE_SPINE.replace('name="Shot B"', 'name="Shot B" enabled="0"')
    report = checks.run(cut(spine, "36s"), worse())
    black = {(f.start, f.end): (f.level, f.text) for f in report.findings if f.code == "black"}
    assert black[(Fraction(10), Fraction(16))][0] == checks.INHERITED
    level, text = black[(Fraction(16), Fraction(26))]
    assert level == checks.FAIL and "switched off: Shot B" in text
    assert _codes(report, checks.INHERITED) == ["black", "vo-uncovered"]
