"""The shadow timeline: the cut as it plays, as text.

A read-only view derived from the FCPXML and rebuilt whenever it is asked
for. Nothing here is ever written back. One row per change of picture or
start of a narration line, so a reader sees which words land on which shot,
where the screen is black and what was switched off, without doing offset
arithmetic. The checks (fcpxml/checks.py) and the comparison with the base
build sit above the rows: asking for the view runs them.

render() is PURE. The command line at the bottom reads files:

    python -m fcpxml.shadow CUT.fcpxml [--base PREVIOUS.fcpxml]
        [--scope 0:19-1:33] [--from 1:00 --to 2:30] [--checks] [--markers]

It exits 1 when a check fails, so a hook or a script can act on it.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

from . import checks, coverage
from .checks import clock, signed

PICTURE_WIDTH = 44
NARRATION_WIDTH = 56
SOUND_WIDTH = 26
MAX_SOUND_PER_ROW = 3
MAX_NAMED = 6
MAX_MOVES = 14


def parse_time(value: Any) -> Fraction:
    """Seconds from ``m:ss.s``, ``h:mm:ss``, plain seconds or ``n/d``."""
    if isinstance(value, (int, Fraction)):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(str(value))
    text = str(value).strip().rstrip("s")
    if not text:
        raise ValueError("empty time")
    try:
        if ":" in text:
            total = Fraction(0)
            for part in text.split(":"):
                total = total * 60 + Fraction(part)
            return total
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        raise ValueError(
            f"not a time: {value!r} (use m:ss.s, seconds, or n/d)"
        ) from None


def parse_scope(value: Any) -> list:
    """``"0:19-1:33, 5:00-5:10"`` (or a list of such) as (start, end) pairs."""
    if not value:
        return []
    parts = value if isinstance(value, (list, tuple)) else str(value).split(",")
    windows = []
    for part in parts:
        if isinstance(part, (list, tuple)) and len(part) == 2:
            a, b = part
        else:
            text = str(part).strip()
            sep = " to " if " to " in text else "-"
            a, _, b = text.partition(sep)
        start, end = parse_time(a), parse_time(b)
        if end <= start:
            raise ValueError(f"scope window ends before it starts: {part!r}")
        windows.append((start, end))
    return windows


def _cut(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "~"


def _names(found: list, limit: int = MAX_NAMED) -> str:
    names = [i.name for i in found]
    shown = "; ".join(_cut(n, 40) for n in names[:limit])
    return shown + (f"; and {len(names) - limit} more" if len(names) > limit else "")


def _check_lines(report: checks.Report) -> list[str]:
    fails, kept = report.failures, report.inherited
    head = f"CHECKS  {len(fails)} FAIL"
    if report.has_base:
        head += f", {len(kept)} inherited from the base"
    lines = [head]
    for finding in fails + kept:
        if finding.start is not None and finding.end is not None:
            when = f"{clock(finding.start)} to {clock(finding.end)}"
        elif finding.start is not None:
            when = clock(finding.start)
        else:
            when = ""
        lines.append(f"{finding.level:<9} {finding.code:<13} {when:<17} {finding.text}")
    if not fails and not kept:
        lines.append("nothing found")
    if not report.has_base:
        lines.append("(no base given: every finding counts, none can be marked inherited)")
    return lines


def _base_lines(report: checks.Report) -> list[str]:
    lines = ["AGAINST BASE" if report.has_base else "MEASURED"]
    for label, before, now in report.facts:
        lines.append(f"{label:<26} {before + '  ->  ' if before else ''}{now}")
    if not report.has_base:
        return lines

    by_kind: dict = {}
    for change in report.changes:
        by_kind.setdefault(change.item.kind, []).append(change)
    runs = checks.blocks(report.changes)
    for kind in (coverage.PICTURE, coverage.TITLE, coverage.NARRATION,
                 coverage.MUSIC, coverage.EFFECTS, coverage.SOUND):
        changed = by_kind.get(kind, [])
        still = report.unmoved.get(kind, 0)
        if not changed and not still:
            continue
        lines.append(f"{kind}: {still} unmoved, {len(changed)} changed")
        mine = [r for r in runs if r[0] == kind]
        for run_kind, delta, moved in mine[:MAX_MOVES]:
            if len(moved) == 1:
                lines.append(f"  moved {signed(delta)}: {_cut(moved[0].name, 60)}")
            else:
                lines.append(
                    f"  moved {signed(delta)} as one block, {len(moved)} clips: "
                    f"{_cut(moved[0].name, 34)}  to  {_cut(moved[-1].name, 34)}"
                )
        if len(mine) > MAX_MOVES:
            rest = mine[MAX_MOVES:]
            lines.append(
                f"  and {len(rest)} more separate moves ({sum(len(r[2]) for r in rest)} clips), "
                f"between {signed(min(r[1] for r in rest))} and {signed(max(r[1] for r in rest))}"
            )
        for what in ("added", "removed", "changed", "switched off", "switched on"):
            hits = [c.item for c in changed if c.what == what]
            if hits:
                lines.append(f"  {what} ({len(hits)}): {_names(hits)}")
    return lines


def _rows(timeline: Any, pool: list, report: checks.Report,
          start: Optional[Fraction], end: Optional[Fraction], markers: bool) -> list[str]:
    spans = coverage.picture(timeline, pool)
    narration = coverage.sound(timeline, coverage.NARRATION, pool)
    other = [i for i in coverage.sound(timeline, None, pool) if i.kind != coverage.NARRATION]
    over_black = {
        f.start for f in report.findings if f.code == "vo-uncovered" and f.level == checks.FAIL
    }

    times = sorted({s.start for s in spans} | {i.start for i in narration})
    span_at = {s.start: s for s in spans}
    said_at: dict = {}
    for item in narration:
        said_at.setdefault(item.start, []).append(item)

    lines = [
        f"{'time':>7}  {'dur':>5}  {'picture':<{PICTURE_WIDTH}}  "
        f"{'narration':<{NARRATION_WIDTH}}  sound starting here"
    ]
    notes = []
    if markers:
        for item in pool:
            if item.enabled:
                notes += [(t, text, item.name) for t, text in item.markers]
        for gap in getattr(timeline, "gaps", None) or ():
            for marker in gap.markers:
                where = getattr(marker, "timeline_start", None) or marker.start
                notes.append((coverage.seconds(where), marker.name, gap.name or "gap"))
        notes.sort()

    for index, t in enumerate(times):
        nxt = times[index + 1] if index + 1 < len(times) else coverage.total(timeline, pool)
        if (start is not None and nxt <= start) or (end is not None and t >= end):
            continue
        span = span_at.get(t)
        if span is None:
            shot, length = "  .", ""
        elif span.black:
            shot = "-- BLACK: " + coverage.why_black(span, timeline, pool)
            length = f"{float(span.duration):.1f}"
        else:
            base = span.item
            shot = base.name if base is not None else "(no full-frame picture)"
            if base is not None and not base.primary:
                shot += f" [lane {base.lane}]"
            if base is not None and base.retimed:
                shot += " [retimed]"
            if span.over:
                shot += " + " + ", ".join(i.name for i in span.over)
            length = f"{float(span.duration):.1f}"
        said = said_at.get(t, [])
        line = " / ".join(i.name for i in said)
        flag = ""
        for item in said:
            if any(item.start <= when < item.end for when in over_black):
                flag = "  << over black"
        starting = [i for i in other if t <= i.start < nxt]
        heard = " | ".join(_cut(i.name, SOUND_WIDTH) for i in starting[:MAX_SOUND_PER_ROW])
        if len(starting) > MAX_SOUND_PER_ROW:
            heard += f" | +{len(starting) - MAX_SOUND_PER_ROW}"
        lines.append(
            f"{clock(t):>7}  {length:>5}  {_cut(shot, PICTURE_WIDTH):<{PICTURE_WIDTH}}  "
            f"{_cut(line, NARRATION_WIDTH):<{NARRATION_WIDTH}}  {heard}{flag}".rstrip()
        )
        if markers:
            for when, text, host in notes:
                if t <= when < nxt:
                    lines.append(f"{'':>7}  {'':>5}    marker {clock(when)} on {_cut(host, 30)}: {text}")
    return lines


def _off_lines(pool: list) -> list[str]:
    off = [i for i in pool if not i.enabled]
    if not off:
        return []
    lines = [f"SWITCHED OFF  {len(off)} clip(s): in the file, not seen or heard"]
    pictures = [i for i in off if i.kind == coverage.PICTURE]
    for item in pictures:
        where = "primary" if item.primary else f"lane {item.lane}"
        lines.append(f"  {clock(item.start)} to {clock(item.end)}  {where:<8} {item.name}")
    groups: dict = {}
    for item in off:
        if item.kind != coverage.PICTURE:
            groups.setdefault((item.kind, item.storyline or f"lane {item.lane}"), []).append(item)
    for (kind, home), found in sorted(groups.items()):
        lines.append(
            f"  {len(found)} {kind} clip(s) in {home!r}, "
            f"{clock(min(i.start for i in found))} to {clock(max(i.end for i in found))}"
        )
    return lines


def render(
    timeline: Any,
    *,
    name: str = "",
    sha256: str = "",
    base: Any = None,
    base_name: str = "",
    scope: Optional[list] = None,
    start: Optional[Fraction] = None,
    end: Optional[Fraction] = None,
    exists=None,
    checks_only: bool = False,
    markers: bool = False,
) -> tuple[str, checks.Report]:
    """The shadow view as text, and the report it was built from."""
    pool = coverage.items(timeline)
    report = checks.run(timeline, base, scope=scope, exists=exists)
    length = coverage.total(timeline, pool)
    off = sum(1 for i in pool if not i.enabled)
    primary = sum(1 for i in pool if i.primary)

    head = [
        f"SHADOW  {name or getattr(timeline, 'name', 'timeline')}"
        + (f"  sha256 {sha256[:12]}" if sha256 else "")
        + f"  {clock(length)}  {float(getattr(timeline, 'frame_rate', 0) or 0):g} fps"
        + f"  {primary} primary clips, {len(pool) - primary} connected ({off} switched off)"
    ]
    if base is not None:
        head.append(f"base    {base_name or getattr(base, 'name', 'base')}")
    if scope:
        head.append("scope   " + ", ".join(f"{clock(a)} to {clock(b)}" for a, b in scope))
    head.append(
        "Read from the XML, not from Final Cut. Picture is the top enabled full-frame "
        "clip; opacity, crops, keyframed moves and dissolves are not read."
    )

    parts = [head, _check_lines(report), _base_lines(report)]
    if not checks_only:
        window = ""
        if start is not None or end is not None:
            window = f"  {clock(start or Fraction(0))} to {clock(end if end is not None else length)}"
        parts.append([f"THE CUT{window}"] + _rows(timeline, pool, report, start, end, markers))
        tail = _off_lines(pool)
        if tail:
            parts.append(tail)
    return "\n\n".join("\n".join(block) for block in parts) + "\n", report


# ---------------------------------------------------------------------------
# Reading files (the command line, and what tools/see.py shares with it)
# ---------------------------------------------------------------------------

def document_path(path: str) -> Path:
    """The XML file behind a path: itself, or Info.fcpxml inside a bundle."""
    p = Path(path).expanduser()
    return p / "Info.fcpxml" if p.is_dir() else p


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(document_path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: str) -> Any:
    from .parser import parse_fcpxml

    project = parse_fcpxml(str(document_path(path)))
    if not project.timelines:
        raise ValueError(f"no timeline in {path}")
    return project.timelines[0]


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fcpxml.shadow",
        description="The cut as it plays, as text, with its checks.",
    )
    parser.add_argument("filepath")
    parser.add_argument("--base", help="the build this one was made from")
    parser.add_argument("--scope", help='what the pass meant to touch, e.g. "0:19-1:33"')
    parser.add_argument("--from", dest="start", help="first moment to list")
    parser.add_argument("--to", dest="end", help="last moment to list")
    parser.add_argument("--checks", action="store_true", help="checks and measurements only")
    parser.add_argument("--markers", action="store_true", help="list markers under their rows")
    args = parser.parse_args(argv)
    try:
        timeline = load(args.filepath)
        base = load(args.base) if args.base else None
        text, report = render(
            timeline,
            name=Path(args.filepath).name,
            sha256=file_sha256(args.filepath),
            base=base,
            base_name=Path(args.base).name if args.base else "",
            scope=parse_scope(args.scope),
            start=parse_time(args.start) if args.start else None,
            end=parse_time(args.end) if args.end else None,
            exists=lambda p: Path(p).is_file(),
            checks_only=args.checks,
            markers=args.markers,
        )
    except (OSError, ValueError) as exc:
        print(f"shadow: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(text)
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
