"""Small cuts for the coverage, checks, shadow and sheet tests.

BASE is three ten-second shots with a three-line read and a music bed in
connected storylines, the shape of a narrated documentary. WORSE is what a
bad notes pass makes of it: a named black slot inserted after the first shot,
everything after it pushed six seconds later, and one line dragged onto the
black. MASKED is BASE with a black movie laid over everything from 0:10 on a
lane above the picture, the way a pass hides a half it is not working on.
"""

from fcpxml.parser import FCPXMLParser

PICTURE = "file:///nowhere/picture.mov"
READ = "file:///nowhere/read.wav"
MUSIC = "file:///nowhere/bed.wav"
BLACK = "file:///nowhere/black.mov"

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
    <resources>
        <format id="r1" name="FFVideoFormat1080p24" frameDuration="1/24s" width="1920" height="1080"/>
        <asset id="pic" name="picture" start="0s" duration="600s" hasVideo="1" format="r1">
            <media-rep kind="original-media" src="{picture}"/>
        </asset>
        <asset id="cam" name="camera" start="3600s" duration="600s" hasVideo="1" format="r1">
            <media-rep kind="original-media" src="{picture}"/>
        </asset>
        <asset id="slug" name="black" start="0s" duration="600s" hasVideo="1" format="r1">
            <media-rep kind="original-media" src="{black}"/>
        </asset>
        <asset id="vo" name="read" start="0s" duration="600s">
            <media-rep kind="original-media" src="{read}"/>
        </asset>
        <asset id="mus" name="bed" start="0s" duration="600s" hasAudio="1" audioSources="1" audioChannels="2">
            <media-rep kind="original-media" src="{music}"/>
        </asset>
    </resources>
    <library>
        <event name="event">
            <project name="{name}">
                <sequence format="r1" duration="{duration}" tcStart="0s" tcFormat="NDF">
                    <spine>
{spine}
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
"""

BASE_SPINE = """
<asset-clip ref="pic" offset="0s" name="Shot A" start="0s" duration="10s">
    <spine lane="-1" offset="0s" name="NEW READ">
        <gap name="Gap" offset="0s" start="0s" duration="1s"/>
        <asset-clip ref="vo" offset="1s" name="N01 first line" start="0s" duration="4s" audioRole="dialogue"/>
        <gap name="Gap" offset="5s" start="0s" duration="7s"/>
        <asset-clip ref="vo" offset="12s" name="N02 second line" start="4s" duration="4s" audioRole="dialogue"/>
        <gap name="Gap" offset="16s" start="0s" duration="6s"/>
        <asset-clip ref="vo" offset="22s" name="N03 third line" start="8s" duration="4s" audioRole="dialogue"/>
    </spine>
    <spine lane="-2" offset="0s" name="Music">
        <asset-clip ref="mus" offset="0s" name="MUSIC bed" start="0s" duration="30s" audioRole="music"/>
    </spine>
</asset-clip>
<asset-clip ref="pic" offset="10s" name="Shot B" start="10s" duration="10s"/>
<asset-clip ref="pic" offset="20s" name="Shot C" start="20s" duration="10s"/>
"""

WORSE_SPINE = """
<asset-clip ref="pic" offset="0s" name="Shot A" start="0s" duration="10s">
    <spine lane="-1" offset="0s" name="NEW READ">
        <gap name="Gap" offset="0s" start="0s" duration="1s"/>
        <asset-clip ref="vo" offset="1s" name="N01 first line" start="0s" duration="4s" audioRole="dialogue"/>
        <gap name="Gap" offset="5s" start="0s" duration="6s"/>
        <asset-clip ref="vo" offset="11s" name="N02 second line" start="4s" duration="4s" audioRole="dialogue"/>
        <gap name="Gap" offset="15s" start="0s" duration="13s"/>
        <asset-clip ref="vo" offset="28s" name="N03 third line" start="8s" duration="4s" audioRole="dialogue"/>
    </spine>
    <spine lane="-2" offset="0s" name="Music">
        <asset-clip ref="mus" offset="0s" name="MUSIC bed" start="0s" duration="30s" audioRole="music"/>
    </spine>
</asset-clip>
<gap name="BLACK: a shot still to come" offset="10s" start="0s" duration="6s">
    <marker start="1s" duration="1/24s" value="SHOT LIST 1: something goes here"/>
</gap>
<asset-clip ref="pic" offset="16s" name="Shot B" start="10s" duration="10s"/>
<asset-clip ref="pic" offset="26s" name="Shot C" start="20s" duration="10s"/>
"""

BLACKOUT = """
    <spine lane="2" offset="0s" name="BLACKOUT (delete to see picture)">
        <gap name="Gap" offset="0s" start="0s" duration="10s"/>
        <asset-clip ref="slug" offset="10s" name="BLACKOUT from 0:10" start="0s" duration="20s"/>
    </spine>
</asset-clip>
<asset-clip ref="pic" offset="10s" name="Shot B" """

MASKED_SPINE = BASE_SPINE.replace(
    '</asset-clip>\n<asset-clip ref="pic" offset="10s" name="Shot B" ', BLACKOUT.lstrip("\n")
)
assert MASKED_SPINE != BASE_SPINE


def xml(spine: str, duration: str = "30s", name: str = "cut", picture: str = PICTURE,
        black: str = BLACK) -> str:
    return TEMPLATE.format(
        spine=spine, duration=duration, name=name, picture=picture, read=READ, music=MUSIC,
        black=black,
    )


def is_black(path, instants=()):
    """The stand-in for blackness.Probe: the fixture's black movie, and only it."""
    return str(path).endswith("black.mov")


def cut(spine: str, duration: str = "30s", name: str = "cut"):
    """A parsed timeline with *spine* as its primary storyline."""
    return FCPXMLParser().parse_string(xml(spine, duration, name)).timelines[0]


def base():
    return cut(BASE_SPINE, "30s", "base")


def worse():
    return cut(WORSE_SPINE, "36s", "worse")


def masked():
    return cut(MASKED_SPINE, "30s", "masked")
