"""The see tool group: look at the cut before judging it.

Every other reporting tool describes clips. These two describe what plays:
``shadow`` is the cut as text with its checks, ``contact_sheet`` is frames
on the timeline's clock, returned as images the model can look at. Both are
read-only and derived: nothing here writes to the project, and what they
return is rebuilt from the file each time, named by its sha256 so a stale
view cannot pass for the current one.
"""

from __future__ import annotations

import base64
from fractions import Fraction
from pathlib import Path

import tools
from fcpxml import journal, render, shadow, sheet
from tools._common import parse_project, text_result

MAX_SHEETS_CEILING = 6


def _timeline(filepath, what: str = "filepath"):
    """(validated path, timeline, None) or (None, None, message)."""
    if not filepath:
        return None, None, f"This action requires '{what}'."
    try:
        validated = tools.server_module()._validate_filepath(filepath, (".fcpxml", ".fcpxmld"))
        _project, timeline = parse_project(validated)
    except (ValueError, OSError) as exc:
        return None, None, f"Could not read {filepath}: {exc}"
    if timeline is None:
        return None, None, f"No timelines found in {filepath}"
    return validated, timeline, None


def _optional_time(args: dict, key: str):
    value = args.get(key)
    return None if value in (None, "") else shadow.parse_time(value)


def _image(path: str):
    """An MCP image block for a JPEG on disk."""
    from mcp.types import ImageContent

    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    try:
        return ImageContent(type="image", data=data, mimeType="image/jpeg")
    except Exception:  # mcp 2.x spells the field mime_type
        return ImageContent(type="image", data=data, mime_type="image/jpeg")


async def handle_shadow(args: dict):
    validated, timeline, message = _timeline(args.get("filepath"))
    if timeline is None:
        return text_result(message)
    base, base_name = None, ""
    if args.get("base"):
        base_path, base, message = _timeline(args.get("base"), "base")
        if base is None:
            return text_result(message)
        base_name = Path(base_path).name
    try:
        scope = shadow.parse_scope(args.get("scope"))
        start, end = _optional_time(args, "start"), _optional_time(args, "end")
    except ValueError as exc:
        return text_result(str(exc))

    sha = journal.file_hash(validated) or ""
    text, report = shadow.render(
        timeline,
        name=Path(validated).name,
        sha256=sha,
        base=base,
        base_name=base_name,
        scope=scope,
        start=start,
        end=end,
        exists=lambda p: Path(p).is_file(),
        checks_only=args.get("checks_only") is True,
        markers=args.get("markers") is True,
    )
    directory = render.cache_dir() / "shadow"
    directory.mkdir(exist_ok=True)
    saved = directory / f"{Path(validated).stem}.{sha[:8] or 'nosha'}.shadow.txt"
    try:
        saved.write_text(text, encoding="utf-8")
        text += f"\nSaved: {saved}\n"
    except OSError:
        pass
    if report.failures:
        text += (
            f"\n{len(report.failures)} check(s) FAIL. Each one is fixed, or named in the "
            "handoff with the reason it stands. Look at it before deciding which: "
            "see contact_sheet over the same times.\n"
        )
    return text_result(text)


async def handle_contact_sheet(args: dict):
    validated, timeline, message = _timeline(args.get("filepath"))
    if timeline is None:
        return text_result(message)
    movie = args.get("movie") or ""
    if movie and not Path(movie).expanduser().is_file():
        return text_result(f"movie not found: {movie}")
    try:
        start, end = _optional_time(args, "start"), _optional_time(args, "end")
        step = shadow.parse_time(args.get("step", sheet.DEFAULT_STEP))
        max_sheets = min(MAX_SHEETS_CEILING, max(1, int(args.get("max_sheets", 2))))
    except (ValueError, TypeError) as exc:
        return text_result(str(exc))

    try:
        result = sheet.render_sheets(
            timeline,
            name=Path(validated).name,
            sha256=journal.file_hash(validated) or "",
            start=start, end=end, step=Fraction(step), max_sheets=max_sheets,
            movie=str(Path(movie).expanduser()) if movie else "",
        )
    except (ValueError, RuntimeError) as exc:
        return text_result(str(exc))

    lines = []
    for number, page in enumerate(result["sheets"], 1):
        tiles = page["tiles"]
        lines.append(
            f"Sheet {number} of {result['total_sheets']}: {sheet.clock(tiles[0].at)} to "
            f"{sheet.clock(tiles[-1].at)}  {page['path']}"
        )
        lines += [tile.line for tile in tiles]
        if page["unread"]:
            lines.append("No frame could be read for: " + ", ".join(sorted(set(page["unread"]))))
        lines.append("")
    if result["resume"] is not None:
        lines.append(
            f"{result['total_sheets'] - len(result['sheets'])} more sheet(s) in this range. "
            f"Continue with start={sheet.clock(result['resume'])}, or raise max_sheets "
            f"(up to {MAX_SHEETS_CEILING})."
        )
    lines.append(
        "Frames are read from the source files at the position the timeline maps to: "
        "the top enabled full-frame clip, with no transforms, opacity or dissolves. "
        "Pass movie= to sample an export or a proxy render instead."
    )
    content = text_result("\n".join(lines))
    if args.get("inline", True) is not False:
        for page in result["sheets"]:
            try:
                content.append(_image(page["path"]))
            except OSError:
                pass
    return content


ACTIONS = {
    "shadow": handle_shadow,
    "contact_sheet": handle_contact_sheet,
}

DESCRIPTION = (
    "Look at the cut as it plays, before judging or reporting on it. Read-only. "
    "shadow(filepath, base?, scope?, start?, end?, markers?, checks_only?) returns "
    "the film as text: one row per change of picture or start of a narration line, "
    "showing what is on screen while each line is said, where the screen is black "
    "and what is switched off. Above the rows it runs the checks (black on screen, "
    "narration over black, two narration lines at once, lost or reordered narration, "
    "flash frames, missing media) and, given base (the build this one was made "
    "from), measures runtime, black and silence against it and lists what moved. "
    "scope (\"0:19-1:33\") is what the pass meant to touch; a change outside it "
    "fails. contact_sheet(filepath, start?, end?, step=2, max_sheets=2, movie?, "
    "inline=true) returns the picture itself: a frame every step seconds with "
    "timecode, shot and the narration line under each tile, as images. Times are "
    "m:ss.s or seconds. A report should not describe a moment that has not been "
    "looked at with these."
)
