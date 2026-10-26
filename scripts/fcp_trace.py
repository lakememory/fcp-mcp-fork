#!/usr/bin/env python3
"""Read fcp-mcp-server run traces from the terminal. Standard library only.

    fcp_trace.py list [--date YYYY-MM-DD] [--limit N]
    fcp_trace.py show <run_id|prefix|last> [-v]
    fcp_trace.py export <run_id|prefix|last> --md [--out FILE|-]

--dir sets the trace root (default FCP_MCP_TRACE_DIR, else ~/.fcp-mcp/traces).
The records are the Logic Pro MCP's schema 1, so ``--dir ~/.logic-mcp/traces``
reads that server's traces too. docs/TRACE.md describes every field.

Self-contained on purpose: it reads a trace without importing the server or
its dependencies, so it works from a copy, a wheel install, or another machine.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

RUN_FILE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}\.jsonl")


def default_root() -> Path:
    raw = os.environ.get("FCP_MCP_TRACE_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".fcp-mcp" / "traces"


# -- reading -------------------------------------------------------------------

class Run:
    """One run's records, continuation files included."""

    def __init__(self, first: Path):
        self.file = first
        self.run_id = first.name[:-len(".jsonl")]
        self.start: dict = {}
        self.end: Optional[dict] = None
        self.spans: list[dict] = []
        self.unfinished: list[dict] = []
        self.intents: list[dict] = []
        self.events: list[dict] = []
        self.parts: list[Path] = []
        self.malformed = 0
        starts: dict[str, dict] = {}
        done: set[str] = set()
        part, path = 1, first
        while path.is_file():
            self.parts.append(path)
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        self.malformed += 1
                        continue
                    kind = rec.get("type")
                    if kind == "run_start":
                        self.start = rec
                    elif kind == "run_end":
                        self.end = rec
                    elif kind == "span_start":
                        starts.setdefault(rec.get("span_id"), rec)
                    elif kind == "span":
                        self.spans.append(rec)
                        done.add(rec.get("span_id"))
                    elif kind == "intent":
                        self.intents.append(rec)
                    else:
                        self.events.append(rec)
            part += 1
            path = first.with_name(f"{self.run_id}.{part}.jsonl")
        self.spans.sort(key=lambda s: s.get("seq") or 0)
        self.unfinished = [s for sid, s in starts.items() if sid not in done]

    @property
    def label(self) -> str:
        return self.start.get("label", "?")

    @property
    def goal(self) -> str:
        return text(self.start.get("goal"))

    @property
    def errors(self) -> int:
        return sum(1 for s in self.spans if (s.get("status") or {}).get("code") == "ERROR")


def newest_first(path: Path) -> tuple[str, float]:
    try:
        return path.name[:16], path.stat().st_mtime
    except OSError:
        return path.name[:16], 0.0


def run_files(root: Path, date: Optional[str] = None) -> list[Path]:
    if not root.is_dir():
        return []
    files = [p for day in root.iterdir() if day.is_dir() and (date is None or day.name == date)
             for p in day.iterdir() if RUN_FILE.fullmatch(p.name)]
    return sorted(files, key=newest_first, reverse=True)


def find(query: str, root: Path) -> Path:
    files = run_files(root)
    if not files:
        raise LookupError(f"no traces under {root}")
    if query in ("last", "latest"):
        return files[0]
    hits = [p for p in files if p.name.startswith(query)]
    exact = [p for p in hits if p.name == f"{query}.jsonl"]
    if exact or len(hits) == 1:
        return (exact or hits)[0]
    if not hits:
        raise LookupError(f"no run {query} under {root}")
    names = ", ".join(p.name[:-len(".jsonl")] for p in hits[:5])
    raise LookupError(f"{len(hits)} runs match {query}: {names}")


# -- formatting -----------------------------------------------------------------

def text(value: Any) -> str:
    """Plain text of a field that may be a truncation or redaction marker."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "preview" in value:
            return f"{value['preview']}... [truncated, {value.get('length', 0)} bytes]"
        if "_redacted" in value:
            return f"[{value['_redacted']}, {value.get('length', '?')} bytes]"
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def one_line(s: str, limit: int) -> str:
    flat = " ".join(s.split())
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def local_time(iso: Optional[str]) -> str:
    if not iso:
        return "?"
    try:
        stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def duration(ms: Any) -> str:
    if not isinstance(ms, (int, float)):
        return "?"
    if ms < 1000:
        return f"{ms:.0f} ms"
    seconds = ms / 1000
    return f"{seconds:.1f} s" if seconds < 60 else f"{int(seconds // 60)}m {int(seconds % 60)}s"


def count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def short_hash(digest: Optional[str]) -> str:
    return f" (sha256 {digest[:12]})" if digest else ""


def outcome_of(span: dict) -> str:
    code = (span.get("status") or {}).get("code", "?")
    outcome = span.get("outcome")
    return code if outcome in (None, "ok") else f"{code} {outcome}"


def last_frame(traceback_text: str) -> str:
    frames = re.findall(r'File "([^"]+)", line (\d+), in (\S+)', traceback_text)
    if not frames:
        return ""
    path, line, func = frames[-1]
    return f"{path}:{line} in {func}"


def headline(span: dict) -> str:
    kind = "mutating" if span.get("mutating") else "read"
    if span.get("pathway"):  # a Logic MCP span
        kind = f"{span['pathway']} · {kind}"
    return (f"#{span.get('seq', '?')} {span.get('name', '?')}  [{kind}]  "
            f"{duration(span.get('duration_ms'))}  {outcome_of(span)}")


def details(span: dict, verbose: bool) -> list[str]:
    out = []
    intent = span.get("intent")
    if intent:
        out.append(f"intent: {text(intent.get('step'))} -> expect: {text(intent.get('expected_effect'))}")
    if span.get("called_as"):
        out.append(f"called as: {span['called_as']}")
    given = span.get("input")
    if given:
        out.append(f"input: {given.get('path')}{short_hash(given.get('sha256'))}")
    for item in span.get("outputs") or []:
        out.append(f"output: {item.get('path')}{short_hash(item.get('sha256'))}")
    flags = [name for name in ("gated", "confirm_unreviewed", "autopushed") if span.get(name)]
    if flags:
        out.append("flags: " + ", ".join(flags))
    error = span.get("error")
    if isinstance(error, dict) and error.get("type"):
        out.append(f"error: {error['type']}: {one_line(text(error.get('message')), 300)}")
        frame = error.get("frame") or last_frame(text(error.get("traceback")))
        if frame:
            out.append(f"at: {frame}")
    elif (span.get("status") or {}).get("message"):
        out.append("message: " + one_line(text(span["status"]["message"]), 2000 if verbose else 200))
    if span.get("via"):
        out.append(f"via: {span['via']}")
    diff = ((span.get("state") or {}).get("diff") or {}).get("summary")
    if diff:
        out.append(f"diff: {diff}")
    procs = span.get("subprocess") or []
    if procs:
        out.append("subprocess: " + "; ".join(
            f"{p.get('argv0')} rc={p.get('returncode')}"
            + (f" {p['error']}" if p.get("error") else "") + f" {duration(p.get('duration_ms'))}"
            for p in procs))
        if verbose:
            for p in procs:
                tail = (p.get("stderr_tail") or "").strip()
                if tail:
                    out.append(f"  {p.get('argv0')} stderr: {one_line(tail[-600:], 600)}")
    logs = span.get("logs") or []
    for entry in logs if verbose else logs[:1]:
        out.append(f"log {entry.get('level')} {entry.get('logger')}: {one_line(text(entry.get('message')), 200)}")
    if len(logs) > 1 and not verbose:
        out.append(f"  (+{len(logs) - 1} more log lines, -v)")
    if verbose:
        if span.get("args") not in (None, {}):
            out.append("args: " + one_line(json.dumps(span["args"], sort_keys=True, ensure_ascii=False), 2000))
        if span.get("result") is not None:
            out.append("result: " + one_line(text(span["result"]), 2000))
        if isinstance(error, dict) and error.get("traceback"):
            out += ["traceback:"] + ["  " + ln for ln in text(error["traceback"]).rstrip().splitlines()]
    return out


# -- commands ------------------------------------------------------------------------

def render_list(root: Path, date: Optional[str], limit: int) -> str:
    files = run_files(root, date)[:limit]
    if not files:
        return f"no traces under {root}" + (f" for {date}" if date else "")
    lines = ["RUN ID                    STARTED              LABEL     SPANS  ERR  END           GOAL"]
    for path in files:
        run = Run(path)
        end = (run.end or {}).get("reason") or ("unfinished" if run.unfinished else "open")
        lines.append(" ".join([
            run.run_id.ljust(25), local_time(run.start.get("start_time")).ljust(20),
            run.label.ljust(9), str(len(run.spans)).ljust(6), str(run.errors).ljust(4),
            end.ljust(13), one_line(run.goal, 60),
        ]))
    return "\n".join(lines)


def render_show(run: Run, verbose: bool = False) -> str:
    start, end = run.start, run.end
    out = [f"Run {run.run_id}  ({run.label})  trace_id {start.get('trace_id', '?')}"]
    if run.goal:
        out.append(f"Goal: {run.goal}")
    if start.get("project_path"):
        out.append(f"Project: {start['project_path']}")
    server = start.get("server") or {}
    if server:
        sha = f" (git {server['git_sha'][:7]})" if server.get("git_sha") else ""
        out.append(f"Server: {server.get('name')} {server.get('version')}{sha}, pid {server.get('pid')}")
    timing = f"Started {local_time(start.get('start_time'))}"
    if end:
        timing += (f"  ended {local_time(end.get('end_time'))} ({duration(end.get('duration_ms'))})"
                   f"  reason {end.get('reason', '?')}")
        if end.get("outcome") is not None:
            timing += f"\nOutcome: {text(end['outcome'])}"
    else:
        timing += "  (no run_end: still open, or the server was killed)"
    out.append(timing)
    out.append(f"Calls: {len(run.spans)} ({count(run.errors, 'error')})")
    out.append("File: " + ", ".join(str(p) for p in run.parts))
    out += ["", "run"]
    items = [("span", s) for s in run.spans] + [("unfinished", s) for s in run.unfinished]
    for n, (kind, span) in enumerate(items):
        last = n == len(items) - 1 and not run.events
        branch, cont = ("└─ ", "   ") if last else ("├─ ", "│  ")
        if kind == "unfinished":
            out.append(f"{branch}#{span.get('seq', '?')} {span.get('name', '?')}  UNFINISHED "
                       "(span_start with no end: crash, kill, or still running)")
            continue
        out.append(branch + headline(span))
        out += [cont + "    " + line for line in details(span, verbose)]
    for n, event in enumerate(run.events):
        branch = "└─ " if n == len(run.events) - 1 else "├─ "
        line = f"{branch}event {event.get('name', '?')}"
        intent = event.get("intent")
        if intent:
            line += f": {text(intent.get('step'))} -> {text(intent.get('expected_effect'))}"
        if event.get("next_file"):
            line += f": continues in {event['next_file']}"
        out.append(line)
    if run.malformed:
        out.append(f"({run.malformed} malformed line(s) skipped)")
    return "\n".join(out)


def cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render_markdown(run: Run) -> str:
    start, end = run.start, run.end
    md = [f"# Trace {run.run_id}", "", "| | |", "|---|---|",
          f"| Goal | {cell(run.goal) or '(none)'} |", f"| Label | {run.label} |",
          f"| Trace ID | `{start.get('trace_id', '?')}` |"]
    if start.get("project_path"):
        md.append(f"| Project | `{start['project_path']}` |")
    server = start.get("server") or {}
    if server:
        md.append(f"| Server | {server.get('name')} {server.get('version')} "
                  f"(git `{(server.get('git_sha') or '?')[:12]}`) |")
    md.append(f"| Started | {local_time(start.get('start_time'))} |")
    if end:
        md.append(f"| Ended | {local_time(end.get('end_time'))} ({duration(end.get('duration_ms'))}, "
                  f"{end.get('reason', '?')}) |")
        if end.get("outcome") is not None:
            md.append(f"| Outcome | {cell(text(end['outcome']))} |")
    unfinished = f", {len(run.unfinished)} unfinished" if run.unfinished else ""
    md += [f"| Steps | {len(run.spans)} ({count(run.errors, 'error')}{unfinished}) |", "",
           "## Steps", "", "| # | Call | Intent | Outcome | ms | Outputs |", "|---|---|---|---|---|---|"]
    for s in run.spans:
        intent = text((s.get("intent") or {}).get("step"))
        outputs = ", ".join(Path(o.get("path", "")).name for o in s.get("outputs") or [])
        md.append(f"| {s.get('seq')} | `{s.get('name')}` | {cell(intent)} | {outcome_of(s)} | "
                  f"{(s.get('duration_ms') or 0):.0f} | {cell(outputs)} |")
    for s in run.spans:
        md += ["", f"### {s.get('seq')}. `{s.get('name')}`", ""]
        intent = s.get("intent")
        if intent:
            md.append(f"- **Step:** {text(intent.get('step'))}")
            md.append(f"- **Expected effect:** {text(intent.get('expected_effect'))}")
        kind = "mutating" if s.get("mutating") else "read-only"
        md.append(f"- **Outcome:** {outcome_of(s)} · {kind} · {(s.get('duration_ms') or 0):.0f} ms")
        if (s.get("status") or {}).get("message"):
            md.append(f"- **Message:** {cell(text(s['status']['message']))}")
        if s.get("input"):
            md.append(f"- **Input:** `{s['input'].get('path')}`{short_hash(s['input'].get('sha256'))}")
        for o in s.get("outputs") or []:
            md.append(f"- **Output:** `{o.get('path')}`{short_hash(o.get('sha256'))}")
        flags = [name for name in ("gated", "confirm_unreviewed", "autopushed") if s.get(name)]
        if flags:
            md.append("- **Flags:** " + ", ".join(flags))
        md += ["", "Arguments:", "```json", json.dumps(s.get("args"), indent=2, sort_keys=True,
                                                        ensure_ascii=False), "```"]
        error = s.get("error")
        if isinstance(error, dict) and error.get("type"):
            md += ["", f"Error: `{error['type']}`: {cell(text(error.get('message')))}", "",
                   "```", text(error.get("traceback")).rstrip(), "```"]
        procs = s.get("subprocess") or []
        if procs:
            md += ["", "| Process | rc | ms | stderr (tail) |", "|---|---|---|---|"]
            for p in procs:
                tail = one_line((p.get("stderr_tail") or "")[-300:], 300)
                md.append(f"| {p.get('argv0')} | {p.get('returncode')} | {(p.get('duration_ms') or 0):.0f} | "
                          f"{cell(tail)} |")
        for entry in s.get("logs") or []:
            md.append(f"- log {entry.get('level')} `{entry.get('logger')}`: {cell(text(entry.get('message')))}")
        if s.get("result") is not None:
            md += ["", "<details><summary>Result</summary>", "", "```", text(s["result"]).rstrip(),
                   "```", "</details>"]
    if run.unfinished:
        md += ["", "## Unfinished spans", ""]
        md += [f"- #{s.get('seq')} `{s.get('name')}` started {local_time(s.get('start_time'))}"
               for s in run.unfinished]
    if run.events:
        md += ["", "## Events", ""]
        for e in run.events:
            line = f"- {e.get('name', '?')}"
            if e.get("intent"):
                line += f": {text(e['intent'].get('step'))} -> {text(e['intent'].get('expected_effect'))}"
            md.append(line)
    md.append("")
    return "\n".join(md)


def write_private(path: Path, body: str) -> None:
    """A report holds arguments and replies: mode 600, like the trace itself."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)


def main(argv: Optional[list[str]] = None) -> int:
    # SUPPRESS, so a subcommand's unset --dir cannot overwrite one given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", type=Path, default=argparse.SUPPRESS,
                        help="trace root (default FCP_MCP_TRACE_DIR or ~/.fcp-mcp/traces)")
    parser = argparse.ArgumentParser(prog="fcp_trace.py", description=__doc__.split("\n\n")[0],
                                     parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", parents=[common], help="recent runs, newest first")
    p_list.add_argument("--date", help="only runs whose folder is this YYYY-MM-DD")
    p_list.add_argument("--limit", type=int, default=20)
    p_show = sub.add_parser("show", parents=[common], help="span tree of one run")
    p_show.add_argument("run")
    p_show.add_argument("-v", "--verbose", action="store_true")
    p_export = sub.add_parser("export", parents=[common], help="markdown report of one run")
    p_export.add_argument("run")
    p_export.add_argument("--md", action="store_true", required=True)
    p_export.add_argument("--out", help="FILE, or - for stdout (default: next to the trace)")
    args = parser.parse_args(argv)
    root = (getattr(args, "dir", None) or default_root()).expanduser()

    try:
        if args.command == "list":
            print(render_list(root, args.date, args.limit))
            return 0
        run = Run(find(args.run, root))
    except LookupError as exc:
        print(f"fcp_trace: {exc}", file=sys.stderr)
        return 1
    if args.command == "show":
        print(render_show(run, args.verbose))
        return 0
    body = render_markdown(run)
    if args.out == "-":
        print(body)
        return 0
    out = Path(args.out) if args.out else run.file.with_name(f"{run.run_id}.md")
    try:
        write_private(out, body)
    except OSError as exc:
        print(f"fcp_trace: could not write {out}: {exc}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
