"""What a viewer sees and hears at every instant of a timeline.

PURE: no subprocess, no file access. Everything is derived from a parsed
Timeline and every time is an exact Fraction of a second on the sequence
clock, from 0.

The reporting tools count clips. This answers a different question: at
1:28.7, what is on screen, and what is being said? A clip that is switched
off, a hole in the primary storyline under a narration line, a three-frame
sliver of a shot left showing between two cutaways: none of those is a
property of one clip. They are properties of the stack at an instant.

What is read: which clips are enabled, their lanes, their times, whether they
carry picture, and a static transform that shrinks or moves the frame. What
is NOT read: opacity, crops, masks, blend modes, keyframed transforms and
transitions. A clip is taken to fill the frame unless its transform says it
does not, and the top enabled full-frame clip is what is "on screen".

What is in a file is not in the XML either. items() takes ``dark``, a
callable that says whether a file is black at given instants (the one that
looks is fcpxml/blackness.py). A top clip that is black hides what is under
it and shows nothing: the stretch is black, and the clip is named as why.
Without ``dark`` every file counts as picture.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Any, Callable, Iterable, Optional

from .media_intel import media_src_to_path

NARRATION_ROLES = ("dialogue", "narration", "vo")
MUSIC_ROLES = ("music",)
EFFECTS_ROLES = ("effects", "sfx", "fx")
NARRATION_STORYLINES = ("vo", "narr", "new read", "read")

AUDIO_EXTENSIONS = (
    ".wav", ".mp3", ".aif", ".aiff", ".m4a", ".aac", ".flac", ".caf", ".ogg", ".opus",
)

PICTURE = "picture"
TITLE = "title"
NARRATION = "narration"
MUSIC = "music"
EFFECTS = "effects"
SOUND = "sound"
SOUND_KINDS = (NARRATION, MUSIC, EFFECTS, SOUND)

# Where in a clip's length its file is looked at to decide it is black: the
# middle first, so a shot that is not black is settled by one frame.
DARK_SAMPLES = (Fraction(1, 2), Fraction(1, 10), Fraction(9, 10))

DarkFn = Callable[[str, list], bool]


def seconds(tc: Any) -> Fraction:
    """Exact seconds for a Timecode (or anything with ``seconds``)."""
    if tc is None:
        return Fraction(0)
    exact = getattr(tc, "_exact_seconds", None)
    if exact is not None:
        return Fraction(exact)
    return Fraction(tc.seconds).limit_denominator(1_000_000)


@dataclass(frozen=True)
class Item:
    """One clip placed on the sequence clock."""

    name: str
    start: Fraction
    end: Fraction
    lane: int
    kind: str
    enabled: bool = True
    primary: bool = False
    storyline: str = ""
    role: str = ""
    media: str = ""  # filesystem path, "" when the clip has no file
    still: bool = False
    covers: bool = False
    clip_start: Fraction = Fraction(0)  # the clip's own clock at its first frame
    origin: Fraction = Fraction(0)  # where the asset's clock begins
    time_map: tuple = ()
    markers: tuple = ()  # ((timeline seconds, text), ...)
    black: bool = False  # its file is black where the cut plays it

    @property
    def duration(self) -> Fraction:
        return self.end - self.start

    @property
    def is_picture(self) -> bool:
        return self.kind in (PICTURE, TITLE)

    @property
    def is_sound(self) -> bool:
        return self.kind in SOUND_KINDS

    @property
    def retimed(self) -> bool:
        return bool(self.time_map)

    def file_time(self, at: Fraction) -> Fraction:
        """Seconds into the FILE that play at timeline instant *at*.

        The clip's clock is the asset's clock (a camera file with 01:00:00:00
        timecode starts at 3600s, not 0), and a retimed clip reaches its
        source through its ``<timeMap>``. A still has one picture.
        """
        if self.still:
            return Fraction(0)
        local = self.clip_start + (Fraction(at) - self.start)
        source = _mapped(self.time_map, local) if self.time_map else local
        return max(Fraction(0), source - self.origin)

    @property
    def key(self) -> tuple:
        """Identity for comparing two builds: what it is, not where it sits."""
        return (self.kind, self.name, self.clip_start, self.duration)


@dataclass(frozen=True)
class Span:
    """A stretch of time during which the picture does not change.

    ``item`` is the top enabled full-frame clip, or None. ``over`` holds
    enabled picture above it that does not fill the frame (titles, a clip
    scaled into a corner). Nothing in either is black. ``slug`` is the top
    clip when its file is black: it hides what is under it, so ``item`` is
    None and the stretch is black because of it.
    """

    start: Fraction
    end: Fraction
    item: Optional[Item] = None
    over: tuple = ()
    slug: Optional[Item] = None

    @property
    def duration(self) -> Fraction:
        return self.end - self.start

    @property
    def black(self) -> bool:
        return self.item is None and not self.over


def _mapped(points: tuple, local: Fraction) -> Fraction:
    """Piecewise-linear ``<timeMap>`` lookup, extended past either end."""
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if local <= t1 or (t1, v1) == points[-1]:
            if t1 == t0:
                return v0
            return v0 + (local - t0) * (v1 - v0) / (t1 - t0)
    return local


def _sound_kind(role: str, storyline: str) -> str:
    low = (role or "").lower()
    if low.startswith(NARRATION_ROLES):
        return NARRATION
    if low.startswith(MUSIC_ROLES):
        return MUSIC
    if low.startswith(EFFECTS_ROLES):
        return EFFECTS
    if not low:
        name = (storyline or "").lower()
        if name.startswith(NARRATION_STORYLINES):
            return NARRATION
        if "music" in name:
            return MUSIC
        if "sfx" in name or "effect" in name:
            return EFFECTS
    return SOUND


def _covers(obj: Any) -> bool:
    transform = getattr(obj, "transform", None)
    if transform is None or getattr(transform, "animated", False):
        return True
    smallest = min(transform.scale)
    if smallest < 1.0:
        return False
    return tuple(transform.position) == (0.0, 0.0) or smallest > 1.0


def _item(obj: Any, *, connected: bool) -> Item:
    tag = getattr(obj, "clip_type", "") if connected else ""
    media = getattr(obj, "media", None)
    src = getattr(obj, "media_path", "") or ""
    low = src.split("?", 1)[0].lower()
    role = (getattr(obj, "role", "") or getattr(obj, "audio_role", "")
            or getattr(obj, "video_role", "") or "")
    storyline = getattr(obj, "storyline", "") or ""

    if tag in ("title", "caption"):
        kind = TITLE
    elif tag == "audio" or low.endswith(AUDIO_EXTENSIONS):
        kind = _sound_kind(role, storyline)
    elif media is not None and not (media.has_video or media.still):
        kind = _sound_kind(role, storyline)
    else:
        kind = PICTURE

    if connected:
        placed = getattr(obj, "timeline_start", None) or getattr(obj, "offset", None)
        start = seconds(placed)
    else:
        start = seconds(obj.start)
    clip_start = seconds(getattr(obj, "source_start", None))
    marks = []
    for marker in getattr(obj, "markers", None) or ():
        where = getattr(marker, "timeline_start", None) or getattr(marker, "start", None)
        marks.append((seconds(where), getattr(marker, "name", "") or ""))

    return Item(
        name=obj.name,
        start=start,
        end=start + seconds(obj.duration),
        lane=int(getattr(obj, "lane", 0) or 0) if connected else 0,
        kind=kind,
        enabled=bool(getattr(obj, "enabled", True)),
        primary=not connected,
        storyline=storyline,
        role=role,
        media=media_src_to_path(src) if src else "",
        still=bool(media.still) if media is not None else False,
        covers=kind == PICTURE and _covers(obj),
        clip_start=clip_start,
        origin=Fraction(media.origin) if media is not None else Fraction(0),
        time_map=tuple(getattr(obj, "time_map", ()) or ()),
        markers=tuple(marks),
    )


def items(timeline: Any, dark: Optional[DarkFn] = None) -> list[Item]:
    """Every clip on the sequence clock, enabled or not, in time order.

    ``dark(path, [file seconds, ...])`` says whether a file is black at those
    instants. Each enabled full-frame clip with a file is asked about, at
    DARK_SAMPLES of its length, and marked ``black`` when it is.
    """
    found = [_item(c, connected=False) for c in getattr(timeline, "clips", None) or ()]
    found += [_item(c, connected=True) for c in getattr(timeline, "connected_clips", None) or ()]
    if dark is not None:
        found = [_looked_at(i, dark) for i in found]
    return sorted(found, key=lambda i: (i.start, -i.lane, i.name))


def _looked_at(item: Item, dark: DarkFn) -> Item:
    if not (item.enabled and item.covers and item.media and item.end > item.start):
        return item
    instants = [item.file_time(item.start + item.duration * part) for part in DARK_SAMPLES]
    return replace(item, black=True) if dark(item.media, instants) else item


def total(timeline: Any, found: Optional[Iterable[Item]] = None) -> Fraction:
    """The cut's length: the sequence's own duration, or where its picture ends."""
    declared = seconds(getattr(timeline, "duration", None))
    if declared > 0:
        return declared
    pool = list(found) if found is not None else items(timeline)
    return max((i.end for i in pool if i.enabled), default=Fraction(0))


def picture(timeline: Any, found: Optional[list[Item]] = None) -> list[Span]:
    """What is on screen, start to end, with no holes: black is a Span too."""
    pool = found if found is not None else items(timeline)
    length = total(timeline, pool)
    seen = [i for i in pool if i.enabled and i.is_picture and i.end > i.start]
    edges = {Fraction(0), length}
    for i in seen:
        edges.update((max(Fraction(0), i.start), min(length, i.end)))
    cuts = sorted(e for e in edges if 0 <= e <= length)

    spans: list[Span] = []
    for a, b in zip(cuts, cuts[1:]):
        here = [i for i in seen if i.start <= a and i.end >= b]
        full = [i for i in here if i.covers]
        top = max(full, key=lambda i: i.lane) if full else None
        floor = top.lane if top is not None else None
        slug = None
        if top is not None and top.black:
            slug, top = top, None
        over = tuple(sorted(
            (i for i in here if not i.covers and (floor is None or i.lane > floor)),
            key=lambda i: i.lane,
        ))
        last = spans[-1] if spans else None
        if last is not None and last.item is top and last.slug is slug and last.over == over:
            spans[-1] = Span(last.start, b, top, over, slug)
        else:
            spans.append(Span(a, b, top, over, slug))
    return spans


def black(timeline: Any, found: Optional[list[Item]] = None) -> list[Span]:
    return [s for s in picture(timeline, found) if s.black]


def sound(timeline: Any, kind: Optional[str] = None,
          found: Optional[list[Item]] = None) -> list[Item]:
    """Enabled sound in time order; *kind* narrows it to one of SOUND_KINDS."""
    pool = found if found is not None else items(timeline)
    return [i for i in pool
            if i.enabled and i.is_sound and (kind is None or i.kind == kind)]


def uncovered(timeline: Any, kind: str = NARRATION,
              found: Optional[list[Item]] = None) -> list[tuple[Item, Span]]:
    """(sound item, the part of it that plays over black), in time order."""
    pool = found if found is not None else items(timeline)
    holes = black(timeline, pool)
    out = []
    for item in sound(timeline, kind, pool):
        for hole in holes:
            a, b = max(item.start, hole.start), min(item.end, hole.end)
            if b > a:
                out.append((item, Span(a, b)))
    return out


def at(timeline: Any, instant: Fraction,
       found: Optional[list[Item]] = None) -> tuple[Optional[Span], list[Item]]:
    """(the picture span containing *instant*, the sound playing then)."""
    pool = found if found is not None else items(timeline)
    instant = Fraction(instant)
    spans = picture(timeline, pool)
    span = next((s for s in spans if s.start <= instant < s.end), None)
    if span is None and spans and instant == spans[-1].end:
        span = spans[-1]
    heard = [i for i in sound(timeline, None, pool) if i.start <= instant < i.end]
    return span, heard


def why_black(span: Span, timeline: Any, found: Optional[list[Item]] = None) -> str:
    """What the hole is, in the editor's own words where the document has them."""
    if span.slug is not None:
        return f"black clip: {span.slug.name}"
    pool = found if found is not None else items(timeline)
    off = [i.name for i in pool
           if not i.enabled and i.kind == PICTURE and i.primary
           and i.start < span.end and i.end > span.start]
    if off:
        return "switched off: " + ", ".join(off)
    for gap in getattr(timeline, "gaps", None) or ():
        g0 = seconds(gap.start)
        g1 = g0 + seconds(gap.duration)
        if g0 < span.end and g1 > span.start:
            name = (gap.name or "").strip()
            return f'gap "{name}"' if name and name != "Gap" else "an unnamed gap"
    return "nothing on the primary storyline"
