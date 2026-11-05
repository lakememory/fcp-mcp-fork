"""Connected storylines, disabled clips and auditions.

Final Cut writes a connected (secondary) storyline as ``<spine lane="N">``
hanging off a primary clip; the DTD has no ``<storyline>`` element. The parser
matched only ``<storyline>``, so on a film whose narration, music and effects
each sit in a storyline it returned the primary storyline and 0 connected
clips. It also ignored ``enabled="0"`` (a switched-off clip read as picture)
and read an ``<audition>``, which has no duration of its own, as zero time, so
the pick never appeared and every clip after it came early by its length.

Clock, from a Final Cut 12 export: a storyline anchored at ``437/125s`` writes
its first child at ``0s``. Children count from the storyline's own first frame.
"""

from fractions import Fraction

import pytest

from fcpxml import dtd, filtergraph, timeline_app
from fcpxml.parser import FCPXMLParser

HAS_DTD = dtd.find_apple_dtd("1.13") is not None

FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
    <resources>
        <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
        <asset id="a1" name="picture" start="0s" duration="100s" hasVideo="1" format="r1">
            <media-rep kind="original-media" src="file:///tmp/does-not-exist/picture.mov"/>
        </asset>
        <asset id="a2" name="read" start="0s" duration="100s" hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000">
            <media-rep kind="original-media" src="file:///tmp/does-not-exist/read.wav"/>
        </asset>
    </resources>
    <library>
        <event name="event">
            <project name="project">
                <sequence format="r1" duration="30s" tcStart="0s" tcFormat="NDF">
                    <spine>
                        <asset-clip ref="a1" offset="0s" name="Shot 1" start="10s" duration="10s">
                            <spine lane="-1" offset="12s" name="NEW READ">
                                <gap name="Gap" offset="0s" start="0s" duration="3s"/>
                                <asset-clip ref="a2" offset="3s" name="N01" start="0s" duration="4s" audioRole="dialogue"/>
                                <asset-clip ref="a2" offset="7s" name="N02" start="4s" duration="2s" enabled="0" audioRole="dialogue"/>
                                <audition offset="9s">
                                    <asset-clip ref="a2" offset="9s" name="N03 pick" start="6s" duration="1s" audioRole="dialogue"/>
                                    <asset-clip ref="a2" offset="9s" name="N03 alt" start="8s" duration="1s" audioRole="dialogue"/>
                                </audition>
                            </spine>
                            <spine lane="1" offset="437/125s" name="CUTAWAY">
                                <gap name="Gap" offset="0s" start="0s" duration="31188000/720000s"/>
                                <asset-clip ref="a1" offset="31188000/720000s" name="Cutaway" start="20s" duration="2s">
                                    <asset-clip ref="a2" lane="-1" offset="21s" name="Cutaway sound" start="0s" duration="1s"/>
                                </asset-clip>
                            </spine>
                        </asset-clip>
                        <asset-clip ref="a1" offset="10s" name="Shot 2 off" start="0s" duration="5s" enabled="0">
                            <asset-clip ref="a1" lane="1" offset="1s" name="Rides a disabled parent" start="0s" duration="2s"/>
                        </asset-clip>
                        <audition offset="15s">
                            <asset-clip ref="a1" offset="15s" name="Pick" start="0s" duration="6s"/>
                            <asset-clip ref="a1" offset="15s" name="Alt" start="0s" duration="6s"/>
                        </audition>
                        <asset-clip ref="a1" offset="21s" name="Shot 4" start="0s" duration="9s"/>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
"""


@pytest.fixture
def timeline():
    return FCPXMLParser().parse_string(FIXTURE).timelines[0]


def _connected(timeline, name):
    hits = [c for c in timeline.connected_clips if c.name == name]
    assert len(hits) == 1, f"{name}: {len(hits)} found"
    return hits[0]


def _spine(timeline, name):
    hits = [c for c in timeline.clips if c.name == name]
    assert len(hits) == 1, f"{name}: {len(hits)} found"
    return hits[0]


def test_a_spine_with_a_lane_is_a_connected_storyline(timeline):
    names = [c.name for c in timeline.connected_clips]
    assert names == [
        "N01", "N02", "N03 pick", "Cutaway", "Cutaway sound", "Rides a disabled parent",
    ]
    read = [c for c in timeline.connected_clips if c.storyline == "NEW READ"]
    assert [c.lane for c in read] == [-1, -1, -1]
    assert _connected(timeline, "Cutaway").lane == 1
    assert _connected(timeline, "Cutaway").storyline == "CUTAWAY"


def test_storyline_children_count_from_the_storylines_own_start(timeline):
    # Shot 1 sits at 0s and its clock starts at 10s; the storyline is anchored
    # at 12s on that clock, so at 2s on the timeline. Its children are timed
    # from 0: N01 at 3s is at 5s, N02 at 7s is at 9s.
    assert _connected(timeline, "N01").timeline_start.seconds == 5.0
    assert _connected(timeline, "N02").timeline_start.seconds == 9.0
    assert _connected(timeline, "N01").duration.seconds == 4.0
    assert _connected(timeline, "N01").role == "dialogue"


def test_a_spacer_gap_in_a_storyline_is_not_a_clip(timeline):
    assert not [c for c in timeline.connected_clips if c.name == "Gap"]


def test_a_sub_frame_position_is_floored_once_not_twice(timeline):
    # Anchored at 437/125s on a clock starting at 10s: -6.504s. The clip is
    # 43.31666s into the storyline: 36.81266s = frame 883.5. Flooring the two
    # offsets separately (-157 + 1039) gives 882.
    assert _connected(timeline, "Cutaway").timeline_start.frames == 883


def test_a_clip_connected_to_a_storyline_clip_is_read_on_its_clock(timeline):
    # The cutaway starts at frame 883 and its clock at 20s; its sound is at
    # 21s on that clock, one second in.
    assert _connected(timeline, "Cutaway sound").timeline_start.frames == 883 + 24


def test_enabled_is_read_and_inherited(timeline):
    assert _connected(timeline, "N01").enabled is True
    assert _connected(timeline, "N02").enabled is False
    assert _spine(timeline, "Shot 1").enabled is True
    assert _spine(timeline, "Shot 2 off").enabled is False
    assert _connected(timeline, "Rides a disabled parent").enabled is False


def test_an_audition_plays_its_first_child(timeline):
    assert [c.name for c in timeline.clips] == ["Shot 1", "Shot 2 off", "Pick", "Shot 4"]
    assert _spine(timeline, "Pick").start.seconds == 15.0
    # The clip after the audition is where the document puts it, not early by
    # the audition's length.
    assert _spine(timeline, "Shot 4").start.seconds == 21.0
    assert _connected(timeline, "N03 pick").timeline_start.seconds == 11.0
    assert not [c for c in timeline.connected_clips if c.name == "N03 alt"]


def test_the_legacy_storyline_element_is_still_read():
    xml = FIXTURE.replace('<spine lane="-1" offset="12s" name="NEW READ">', '<storyline lane="-1">', 1)
    head, _, tail = xml.partition('<storyline lane="-1">')
    body, _, rest = tail.partition("</spine>")
    legacy = FCPXMLParser().parse_string(head + '<storyline lane="-1">' + body + "</storyline>" + rest)
    names = [c.name for c in legacy.timelines[0].connected_clips]
    assert "N01" in names and "N02" in names


def test_a_disabled_clip_is_not_rendered(timeline):
    graph = filtergraph.compile_timeline(timeline)
    labels = [s.label for s in graph.segments]
    assert "Shot 2 off" not in labels
    assert "Rides a disabled parent" not in labels
    # The disabled spine clip keeps its time, as black.
    hole = [s for s in graph.segments if s.kind == "gap" and s.tl_in == Fraction(10)]
    assert len(hole) == 1 and hole[0].duration == Fraction(5)
    assert graph.total == Fraction(30)
    assert any("disabled" in note for note in graph.substitutions)


def test_the_render_still_draws_an_enabled_storyline_clip(timeline):
    graph = filtergraph.compile_timeline(timeline)
    assert "Cutaway" in [s.label for s in graph.segments]


def test_the_view_payload_says_what_is_off_and_where_it_sits(timeline):
    payload = timeline_app.build_payload(timeline)
    by_name = {c["name"]: c for c in payload["clips"]}
    assert by_name["N02"]["enabled"] is False
    assert by_name["N01"]["enabled"] is True
    assert by_name["N01"]["storyline"] == "NEW READ"
    assert by_name["Shot 1"]["storyline"] == ""
    assert Fraction(by_name["N01"]["start"]) == Fraction(5)


@pytest.mark.skipif(not HAS_DTD, reason="Apple's FCPXML DTD is not installed")
def test_the_fixture_is_a_document_final_cut_accepts(tmp_path):
    path = tmp_path / "storylines.fcpxml"
    path.write_text(FIXTURE)
    ok, detail = dtd.validate_against_dtd(str(path))
    assert ok, detail
