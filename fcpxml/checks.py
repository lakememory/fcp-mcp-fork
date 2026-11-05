"""Things a cut must never do by accident, and how it differs from its base.

PURE. Works on the coverage model (fcpxml/coverage.py), so every check is
about what plays, not about how many clips there are.

Two kinds of output, kept apart on purpose:

* Findings are correctness: black on screen, narration over black, two
  narration lines at once, a narration line that went missing or changed
  order, a flash of picture too short to see, a change outside the stated
  scope, a missing file. None is ever intended without someone saying so.
  A finding that is identical in the base build is INHERITED: it was there
  before this pass, so it is listed and not counted against it.
* Facts are measurements against the base: runtime, total black, the longest
  narration silence, what moved and by how much. They carry no threshold and
  no verdict. Whether +66 s is a problem is the editor's call.

A name on a gap is a label, not permission. A build that adds black and
names it "BLACK: coconut on empty sand" has still added black.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Optional

from . import coverage
from .rational import frame_duration_seconds

FAIL = "FAIL"
INHERITED = "inherited"

# A run of picture shorter than this many frames is a flash, not a shot. The
# same boundary detect_flash_frames uses for its warning level.
FLASH_FRAMES = 6
# Sound is cut between frames and positions are floored to one; an overlap
# has to exceed this many frames before it is treated as real.
TOUCH_FRAMES = 3


@dataclass(frozen=True)
class Finding:
    code: str
    level: str
    start: Optional[Fraction]
    end: Optional[Fraction]
    text: str


@dataclass(frozen=True)
class Change:
    """One clip's fate between the base and this build."""

    what: str  # moved | added | removed | changed | switched off | switched on
    item: coverage.Item
    delta: Fraction = Fraction(0)
    was: Optional[coverage.Item] = None


@dataclass
class Report:
    findings: list = field(default_factory=list)
    facts: list = field(default_factory=list)  # (label, base text or None, new text)
    changes: list = field(default_factory=list)
    unmoved: dict = field(default_factory=dict)  # kind -> count
    has_base: bool = False

    @property
    def failures(self) -> list:
        return [f for f in self.findings if f.level == FAIL]

    @property
    def inherited(self) -> list:
        return [f for f in self.findings if f.level == INHERITED]


def clock(t: Optional[Fraction]) -> str:
    """m:ss.s, the way a note names a moment."""
    if t is None:
        return "-"
    tenths = int(Fraction(t) * 10 + Fraction(1, 2))
    return f"{tenths // 600}:{(tenths % 600) // 10:02d}.{tenths % 10}"


def secs(d: Fraction) -> str:
    return f"{float(d):.1f} s"


def signed(d: Fraction) -> str:
    return f"{float(d):+.1f} s"


def _neighbours(spans: list, index: int) -> tuple:
    before = next((s.item.name for s in reversed(spans[:index]) if s.item), "")
    after = next((s.item.name for s in spans[index + 1:] if s.item), "")
    return (before, after)


def _silence(narration: list, length: Fraction) -> tuple[Fraction, Fraction]:
    """(longest stretch with no narration, where it starts)."""
    longest, where, cursor = Fraction(0), Fraction(0), Fraction(0)
    for item in narration:
        if item.start - cursor > longest:
            longest, where = item.start - cursor, cursor
        cursor = max(cursor, item.end)
    if length - cursor > longest:
        longest, where = length - cursor, cursor
    return longest, where


def compare(base_items: list, new_items: list) -> tuple[list, dict]:
    """What happened to each enabled clip between two builds.

    A clip is the same clip when its kind, name, in-point and length match;
    the nth such clip in the base pairs with the nth in the new build. What
    is left is paired by kind and name alone (it was trimmed or retimed), and
    what is still left was added or removed.
    """
    def enabled(pool):
        return [i for i in pool if i.enabled]

    def bucket(pool, key):
        out = defaultdict(list)
        for i in pool:
            out[key(i)].append(i)
        return out

    old, new = enabled(base_items), enabled(new_items)
    changes: list[Change] = []
    unmoved: Counter = Counter()

    old_by = bucket(old, lambda i: i.key)
    left_new: list = []
    for item in new:
        twins = old_by.get(item.key)
        if twins:
            was = twins.pop(0)
            delta = item.start - was.start
            if delta == 0:
                unmoved[item.kind] += 1
            else:
                changes.append(Change("moved", item, delta, was))
        else:
            left_new.append(item)
    left_old = [i for twins in old_by.values() for i in twins]

    loose = bucket(left_old, lambda i: (i.kind, i.name))
    off_now = Counter((i.kind, i.name) for i in new_items if not i.enabled)
    off_then = Counter((i.kind, i.name) for i in base_items if not i.enabled)
    for item in left_new:
        twins = loose.get((item.kind, item.name))
        if twins:
            was = twins.pop(0)
            changes.append(Change("changed", item, item.start - was.start, was))
        elif off_then[(item.kind, item.name)]:
            off_then[(item.kind, item.name)] -= 1
            changes.append(Change("switched on", item))
        else:
            changes.append(Change("added", item))
    for twins in loose.values():
        for was in twins:
            if off_now[(was.kind, was.name)]:
                off_now[(was.kind, was.name)] -= 1
                changes.append(Change("switched off", was, was=was))
            else:
                changes.append(Change("removed", was, was=was))
    changes.sort(key=lambda c: (c.item.start, c.item.name))
    return changes, dict(unmoved)


def _in_scope(change: Change, windows: list) -> bool:
    spots = [(change.item.start, change.item.end)]
    if change.was is not None:
        spots.append((change.was.start, change.was.end))
    return all(any(a < w1 and b > w0 for w0, w1 in windows) for a, b in spots)


def run(
    timeline: Any,
    base: Any = None,
    *,
    scope: Optional[list] = None,
    exists: Optional[Callable[[str], bool]] = None,
) -> Report:
    """Check *timeline*, against *base* when one is given.

    ``scope`` is a list of (start, end) windows in seconds the pass said it
    would touch; a change outside every window is a finding. ``exists`` tests
    a file path; leave it None to skip the missing-media check (this module
    does no I/O of its own).
    """
    report = Report(has_base=base is not None)
    frame = frame_duration_seconds(getattr(timeline, "frame_rate", 24.0) or 24.0)
    touch = TOUCH_FRAMES * frame

    pool = coverage.items(timeline)
    spans = coverage.picture(timeline, pool)
    length = coverage.total(timeline, pool)
    narration = coverage.sound(timeline, coverage.NARRATION, pool)

    base_pool = coverage.items(base) if base is not None else []
    base_spans = coverage.picture(base, base_pool) if base is not None else []
    base_length = coverage.total(base, base_pool) if base is not None else None
    base_narration = (
        coverage.sound(base, coverage.NARRATION, base_pool) if base is not None else []
    )

    # --- black, and narration over it ----------------------------------
    # A hole is the same hole as one in the base when it sits between the
    # same two shots, or is the same named gap or the same switched-off clip
    # (a hole can keep its name while a neighbour changes).
    base_black: dict = {}
    for index, span in enumerate(base_spans):
        if span.black:
            cause = coverage.why_black(span, base, base_pool)
            keys = [_neighbours(base_spans, index)]
            if cause.startswith(('gap "', "switched off:")):
                keys.append(cause)
            for key in keys:
                base_black[key] = max(base_black.get(key, Fraction(0)), span.duration)

    black_level: dict = {}
    for index, span in enumerate(spans):
        if not span.black:
            continue
        cause = coverage.why_black(span, timeline, pool)
        known = [base_black[k] for k in (_neighbours(spans, index), cause) if k in base_black]
        before = max(known) if known else None
        if before is not None and span.duration <= before + frame:
            level, note = INHERITED, ""
        elif before is not None:
            level, note = FAIL, f" (was {secs(before)} in the base)"
        else:
            level, note = FAIL, ""
        black_level[(span.start, span.end)] = level
        report.findings.append(Finding(
            "black", level, span.start, span.end,
            f"{secs(span.duration)} with no picture{note}: {cause}",
        ))

    for item, part in coverage.uncovered(timeline, coverage.NARRATION, pool):
        if part.duration <= touch:
            continue
        hole = next(
            (lvl for (a, b), lvl in black_level.items() if a <= part.start and b >= part.end),
            FAIL,
        )
        report.findings.append(Finding(
            "vo-uncovered", hole, part.start, part.end,
            f"{item.name!r} plays over black for {secs(part.duration)}",
        ))

    # --- narration against itself --------------------------------------
    base_pairs = set()
    for a, b in zip(base_narration, base_narration[1:]):
        if a.end - b.start > touch:
            base_pairs.add((a.name, b.name))
    for a, b in zip(narration, narration[1:]):
        overlap = min(a.end, b.end) - b.start
        if overlap > touch:
            report.findings.append(Finding(
                "vo-double", INHERITED if (a.name, b.name) in base_pairs else FAIL,
                b.start, b.start + overlap,
                f"{a.name!r} and {b.name!r} play at once for {secs(overlap)}",
            ))

    # --- flashes ---------------------------------------------------------
    base_flashes = {s.item.name for s in base_spans
                    if s.item is not None and s.duration < FLASH_FRAMES * frame}
    for span in spans:
        if span.item is not None and span.duration < FLASH_FRAMES * frame:
            frames = int(span.duration / frame)
            report.findings.append(Finding(
                "flash", INHERITED if span.item.name in base_flashes else FAIL,
                span.start, span.end,
                f"{span.item.name!r} is on screen for {frames} frame(s)",
            ))

    # --- missing files ---------------------------------------------------
    if exists is not None:
        gone = sorted({i.name for i in pool if i.enabled and i.media and not exists(i.media)})
        if gone:
            shown = ", ".join(gone[:5]) + (f" and {len(gone) - 5} more" if len(gone) > 5 else "")
            report.findings.append(Finding(
                "media", FAIL, None, None, f"{len(gone)} enabled clip(s) have no file: {shown}",
            ))

    # --- facts -----------------------------------------------------------
    def fact(label, new_text, base_text=None):
        report.facts.append((label, base_text, new_text))

    black_total = sum((s.duration for s in spans if s.black), Fraction(0))
    quiet, quiet_at = _silence(narration, length)
    if base is None:
        fact("runtime", clock(length))
        fact("black on screen", secs(black_total))
        fact("longest narration silence", f"{secs(quiet)} from {clock(quiet_at)}")
        fact("picture runs", str(sum(1 for s in spans if s.item is not None)))
        fact("narration lines", str(len(narration)))
        return report

    base_black_total = sum((s.duration for s in base_spans if s.black), Fraction(0))
    base_quiet, base_quiet_at = _silence(base_narration, base_length)
    fact("runtime", f"{clock(length)}  ({signed(length - base_length)})", clock(base_length))
    fact("black on screen",
         f"{secs(black_total)}  ({signed(black_total - base_black_total)})",
         secs(base_black_total))
    fact("longest narration silence",
         f"{secs(quiet)} from {clock(quiet_at)}  ({signed(quiet - base_quiet)})",
         f"{secs(base_quiet)} from {clock(base_quiet_at)}")
    fact("picture runs", str(sum(1 for s in spans if s.item is not None)),
         str(sum(1 for s in base_spans if s.item is not None)))
    fact("narration lines", str(len(narration)), str(len(base_narration)))

    # --- narration lost or reordered --------------------------------------
    then, now = Counter(i.name for i in base_narration), Counter(i.name for i in narration)
    for name in sorted(then):
        if now[name] < then[name]:
            where = next(i.start for i in base_narration if i.name == name)
            report.findings.append(Finding(
                "vo-lost", FAIL, None, None,
                f"{name!r} is in the base at {clock(where)} and "
                + ("not in this build" if not now[name] else "here fewer times"),
            ))
    shared = [n for n in dict.fromkeys(i.name for i in base_narration) if now[n]]
    order = {n: k for k, n in enumerate(dict.fromkeys(i.name for i in narration))}
    for a, b in zip(shared, shared[1:]):
        if order[a] > order[b]:
            where = next(i.start for i in narration if i.name == b)
            report.findings.append(Finding(
                "vo-order", FAIL, where, None,
                f"{b!r} now comes before {a!r}; the base has them the other way round",
            ))

    # --- what changed, and whether it stayed in scope ----------------------
    report.changes, report.unmoved = compare(base_pool, pool)
    if scope:
        windows = [(Fraction(a), Fraction(b)) for a, b in scope]
        outside = [c for c in report.changes if not _in_scope(c, windows)]
        if outside:
            by_kind = Counter(c.item.kind for c in outside)
            first = outside[0]
            named = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
            report.findings.append(Finding(
                "scope", FAIL, first.item.start, None,
                f"{len(outside)} clip(s) changed outside the stated scope ({named}); "
                f"first is {first.item.name!r} ({first.what})",
            ))
    return report


def blocks(changes: list) -> list[tuple]:
    """Group moved clips into runs that travelled together.

    Returns (kind, delta, [items]) for each run of same-kind clips, adjacent
    in the base, that moved by the same amount: "N02 to N09 moved -10.4 s as
    one block" is one decision, not eight.
    """
    moved = sorted((c for c in changes if c.what == "moved"),
                   key=lambda c: (c.item.kind, c.was.start))
    runs: list = []
    for change in moved:
        if runs and runs[-1][0] == change.item.kind and runs[-1][1] == change.delta:
            runs[-1][2].append(change.item)
        else:
            runs.append((change.item.kind, change.delta, [change.item]))
    return runs
