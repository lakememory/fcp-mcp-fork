"""Contact sheets on the timeline's clock.

preview_sheet gives one frame per spine cut. That is a list of shots. This
is the cut as it plays: a frame every *step* seconds from the top enabled
picture at that instant, plus one for any shot or stretch of black that falls
between two samples, so nothing on screen is missing from the sheet. Each
tile carries its timecode, the shot's name and the narration line being
spoken, because the question a sheet answers is "what is on screen while
this is said".

plan() is PURE: it decides which instants to show and what plays there.
compose() draws; frames come through an injected function so the layout is
testable without ffmpeg. The default frame function reads the SOURCE file at
the position the timeline maps to (asset clock and <timeMap> included), so a
sheet costs twenty seeks, not a render of the whole cut. It shows the top
full-frame clip as it is in the file: no transforms, opacity or dissolves.
Give it a movie (a proxy render, or an export from Final Cut) and it samples
that instead, which is the ground truth when those matter.

Drawing needs Pillow (the ``see`` extra). ffmpeg has a drawtext filter only
when built with libfreetype, and stock builds are not.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Optional

from . import coverage, render
from .checks import clock

COLUMNS = 4
ROWS = 5
PER_SHEET = COLUMNS * ROWS
# 1568 px is the longest edge a vision model reads without rescaling.
SHEET_WIDTH = 1568
TILE_WIDTH = SHEET_WIDTH // COLUMNS
TILE_HEIGHT = TILE_WIDTH * 9 // 16
LABEL_HEIGHT = 40
HEADER_HEIGHT = 30
FRAME_SHORT_SIDE = 240
DEFAULT_STEP = Fraction(2)

FrameFn = Callable[[str, Fraction], Optional[str]]


@dataclass(frozen=True)
class Tile:
    """One frame of a sheet: an instant, and what plays at it."""

    at: Fraction
    shot: str  # the picture's name, or why there is none
    said: str  # the narration line playing, "" when none
    source: str = ""  # file to read the frame from, "" for black
    file_time: Fraction = Fraction(0)
    black: bool = False
    cut: bool = False  # the picture changed since the previous tile
    extra: bool = False  # added between samples so a short run is not missed

    @property
    def line(self) -> str:
        said = f" | {self.said}" if self.said else ""
        return f"{clock(self.at):>7}  {self.shot}{said}"


def plan(
    timeline: Any,
    start: Optional[Fraction] = None,
    end: Optional[Fraction] = None,
    step: Fraction = DEFAULT_STEP,
    *,
    movie: str = "",
    found: Optional[list] = None,
) -> list[Tile]:
    """The tiles for [start, end): a sample every *step*, and no run skipped."""
    step = Fraction(step)
    if step <= 0:
        raise ValueError("step must be more than 0 seconds")
    pool = found if found is not None else coverage.items(timeline)
    length = coverage.total(timeline, pool)
    a = max(Fraction(0), Fraction(start)) if start is not None else Fraction(0)
    b = min(length, Fraction(end)) if end is not None else length
    if b <= a:
        raise ValueError(f"nothing to show between {clock(a)} and {clock(b)}")

    spans = coverage.picture(timeline, pool)
    narration = coverage.sound(timeline, coverage.NARRATION, pool)

    instants = []
    t = a
    while t < b:
        instants.append((t, False))
        t += step
    sampled = {t for t, _ in instants}
    for span in spans:
        lo, hi = max(span.start, a), min(span.end, b)
        if hi > lo and not any(lo <= t < hi for t in sampled):
            instants.append(((lo + hi) / 2, True))
    instants.sort()

    tiles: list[Tile] = []
    previous = object()
    for at, extra in instants:
        span = next((s for s in spans if s.start <= at < s.end), None)
        said = next((i.name for i in narration if i.start <= at < i.end), "")
        item = span.item if span is not None else None
        if span is None or span.black:
            shot = "NO PICTURE: " + (coverage.why_black(span, timeline, pool) if span else "past the end")
            source, file_time, is_black = "", Fraction(0), True
        elif item is None:
            shot = " + ".join(i.name for i in span.over)
            source, file_time, is_black = "", Fraction(0), False
        else:
            shot = item.name + (f" [lane {item.lane}]" if not item.primary else "")
            source, file_time, is_black = item.media, item.file_time(at), False
        if movie:
            source, file_time = movie, at
        identity = item if item is not None else (span.start if span is not None else None)
        tiles.append(Tile(
            at=at, shot=shot, said=said, source=source, file_time=file_time,
            black=is_black and not movie, cut=bool(tiles) and identity != previous,
            extra=extra,
        ))
        previous = identity
    return tiles


def pages(tiles: list[Tile], per_sheet: int = PER_SHEET) -> list[list[Tile]]:
    return [tiles[i:i + per_sheet] for i in range(0, len(tiles), per_sheet)]


def cached_frame(source: str, at: Fraction) -> Optional[str]:
    """A small JPEG of *source* at *at* file seconds, from the private cache."""
    if not source or not Path(source).is_file():
        return None
    try:
        st = os.stat(source)
    except OSError:
        return None
    key = hashlib.sha256(
        f"{source}|{st.st_mtime_ns}|{st.st_size}|{at}|{FRAME_SHORT_SIDE}".encode("utf-8")
    ).hexdigest()[:24]
    directory = render.cache_dir() / "frames"
    directory.mkdir(exist_ok=True)
    target = directory / f"{key}.jpg"
    if target.is_file():
        return str(target)
    return render.render_frame(source, at, str(target), max_short_side=FRAME_SHORT_SIDE)


def _pillow():
    try:
        import PIL.Image
        import PIL.ImageDraw
        import PIL.ImageFont
    except ImportError:
        raise RuntimeError(
            "Contact sheets need Pillow. Install it into the server's "
            "environment: uv pip install 'fcp-mcp-server[see]'"
        ) from None
    return PIL.Image, PIL.ImageDraw, PIL.ImageFont


def _font(image_font, size: int):
    try:
        return image_font.load_default(size=size)
    except TypeError:  # Pillow before 10.1 has only the fixed bitmap font
        return image_font.load_default()


def _fit(draw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text + "..."


def compose(
    tiles: list[Tile],
    out_path: str,
    *,
    title: str = "",
    frame: Optional[FrameFn] = None,
) -> tuple[str, list[str]]:
    """Draw one sheet. Returns (path, shots whose frame could not be read)."""
    image, image_draw, image_font = _pillow()
    frame = frame or cached_frame
    rows = max(1, -(-len(tiles) // COLUMNS))
    cell = TILE_HEIGHT + LABEL_HEIGHT
    sheet = image.new("RGB", (SHEET_WIDTH, HEADER_HEIGHT + rows * cell), (24, 24, 26))
    draw = image_draw.Draw(sheet)
    head, label, big = _font(image_font, 16), _font(image_font, 14), _font(image_font, 30)
    draw.text((8, 6), _fit(draw, title, head, SHEET_WIDTH - 16), font=head, fill=(235, 235, 235))

    unread: list[str] = []
    for index, tile in enumerate(tiles):
        x = (index % COLUMNS) * TILE_WIDTH
        y = HEADER_HEIGHT + (index // COLUMNS) * cell
        box = (x + 2, y + 2, x + TILE_WIDTH - 2, y + TILE_HEIGHT - 2)
        if tile.black:
            draw.rectangle(box, fill=(0, 0, 0), outline=(220, 40, 40), width=4)
            draw.text((x + TILE_WIDTH // 2, y + TILE_HEIGHT // 2), "BLACK",
                      font=big, fill=(220, 40, 40), anchor="mm")
        else:
            path = frame(tile.source, tile.file_time) if tile.source else None
            picture = None
            if path:
                try:
                    with image.open(path) as opened:
                        picture = opened.convert("RGB")
                except OSError:
                    picture = None
            if picture is None:
                unread.append(tile.shot)
                draw.rectangle(box, fill=(70, 70, 74))
                draw.text((x + TILE_WIDTH // 2, y + TILE_HEIGHT // 2), "NO FRAME",
                          font=big, fill=(200, 200, 200), anchor="mm")
            else:
                picture.thumbnail((TILE_WIDTH - 4, TILE_HEIGHT - 4))
                sheet.paste(picture, (x + (TILE_WIDTH - picture.width) // 2,
                                      y + (TILE_HEIGHT - picture.height) // 2))
        if tile.cut:
            draw.rectangle((x, y, x + 5, y + TILE_HEIGHT), fill=(250, 200, 40))
        stamp = clock(tile.at)
        draw.text((x + 8, y + TILE_HEIGHT + 2), stamp, font=label,
                  fill=(250, 200, 40) if tile.extra else (255, 255, 255))
        offset = int(draw.textlength(stamp + "  ", font=label))
        draw.text((x + 8 + offset, y + TILE_HEIGHT + 2),
                  _fit(draw, tile.shot, label, TILE_WIDTH - 16 - offset),
                  font=label, fill=(220, 60, 60) if tile.black else (200, 200, 205))
        if tile.said:
            draw.text((x + 8, y + TILE_HEIGHT + 20),
                      _fit(draw, tile.said, label, TILE_WIDTH - 16),
                      font=label, fill=(140, 200, 255))

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, "JPEG", quality=82)
    return out_path, unread


def sheet_dir() -> Path:
    directory = render.cache_dir() / "sheets"
    directory.mkdir(exist_ok=True)
    return directory


def render_sheets(
    timeline: Any,
    *,
    name: str,
    sha256: str = "",
    start: Optional[Fraction] = None,
    end: Optional[Fraction] = None,
    step: Fraction = DEFAULT_STEP,
    max_sheets: int = 2,
    movie: str = "",
    frame: Optional[FrameFn] = None,
    dark=None,
) -> dict:
    """Plan, draw and describe the sheets for a range.

    Returns ``sheets`` (each with ``path``, ``tiles`` and ``unread``) and
    ``resume``: the instant the next call should start from when the range
    needed more than *max_sheets*, else None. ``dark`` is the black-file test
    (see coverage.items): with it a black clip is drawn and named as black.
    """
    found = coverage.items(timeline, dark) if dark is not None else None
    tiles = plan(timeline, start, end, step, movie=movie, found=found)
    groups = pages(tiles)
    drawn = groups[:max(1, int(max_sheets))]
    stem = Path(name).stem.replace("/", "_")
    tag = sha256[:8] or "nosha"
    out = []
    for group in drawn:
        first, last = group[0].at, group[-1].at
        label = f"{clock(first)}-{clock(last)}".replace(":", "m")
        target = sheet_dir() / f"{stem}.{tag}.{label}.step{float(step):g}{'.movie' if movie else ''}.jpg"
        title = (
            f"{name}  {clock(first)} to {clock(last)}  every {float(step):g} s"
            + ("  from the movie file" if movie else "  from source files (no transforms or dissolves)")
            + "   yellow bar = the picture changed   yellow time = added so a short run is not missed"
        )
        path, unread = compose(group, str(target), title=title, frame=frame)
        out.append({"path": path, "tiles": group, "unread": unread})
    resume = groups[len(drawn)][0].at if len(groups) > len(drawn) else None
    return {"sheets": out, "resume": resume, "total_sheets": len(groups)}
