"""The trace tool group: label, annotate and read back the run being traced.

Every call is traced whether or not this group is used (fcpxml/trace.py).
These actions only make the record easier to read: begin_run names the task,
log_intent says what the next mutating call is meant to do, end_run closes
the run, and status and show read it back. None of them is ever required.
"""

from fcpxml import trace as _trace
from tools import _common
from tools._common import text_result

# show answers in chat; a long implicit run is cut to its most recent calls.
SHOW_MAX_SPANS = 60
_OFF = "Tracing is off (FCP_MCP_TRACE=off); nothing is recorded."


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _text_arg(args: dict, key: str) -> str:
    value = args.get(key) or ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {type(value).__name__}")
    return value.strip()


async def handle_begin_run(args: dict):
    goal = args["goal"]
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal must be a non-empty string")
    project_path = _text_arg(args, "project_path") or None
    if not _trace.enabled():
        return text_result(_OFF)
    run, replaced = _trace.begin_run(goal.strip(), project_path)
    lines = [f"Run {run['run_id']} started (explicit). Goal: {run['goal']}", f"File: {run['file']}"]
    if replaced:
        lines.append(f"Closed the previous {replaced['label']} run {replaced['run_id']} (superseded).")
    return text_result("\n".join(lines))


async def handle_log_intent(args: dict):
    step, effect = _text_arg(args, "step"), _text_arg(args, "expected_effect")
    if not step and not effect:
        raise ValueError("log_intent takes step and/or expected_effect")
    if not _trace.enabled():
        return text_result(_OFF)
    info = _trace.log_intent(step, effect)
    text = (f"Intent {info['intent_id']} recorded; it attaches to the next mutating call "
            f"in run {info['run_id']}.")
    if info["replaced"]:
        text += " It replaces an earlier intent that no call used."
    return text_result(text)


async def handle_end_run(args: dict):
    outcome = _text_arg(args, "outcome") or None
    if not _trace.enabled():
        return text_result(_OFF)
    info = _trace.end_run(outcome)
    if info is None:
        return text_result("No run is open; the next call starts one.")
    return text_result(
        f"Run {info['run_id']} ended: {_count(info['spans'], 'call')}, {_count(info['errors'], 'error')}.\n"
        f"File: {info['file']}\n"
        f"Read it back: trace show {{\"run_id\": \"{info['run_id']}\"}}, or "
        f"python scripts/fcp_trace.py show {info['run_id']}"
    )


async def handle_status(args: dict):
    s = _trace.status()
    lines = [
        "Tracing: " + ("on" if s["enabled"] else "off (FCP_MCP_TRACE=off)"),
        f"Root: {s['root']}",
    ]
    run = s["run"]
    if run is None:
        lines.append("Run: none open; the next call starts one.")
    else:
        goal = f", goal: {run['goal']}" if run["goal"] else ""
        lines += [
            f"Run: {run['run_id']} ({run['label']}{goal})",
            f"File: {run['file']}",
            f"Calls: {run['spans']} ({_count(run['errors'], 'error')})",
        ]
        if run["pending_intent"]:
            lines.append(f"Pending intent: {_trace.field_text(run['pending_intent']['step'])}")
    failures = f"Trace write failures: {s['write_failures']}"
    if s["last_failure"]:
        failures += f" (last: {s['last_failure']})"
    lines.append(failures)
    return text_result("\n".join(lines))


async def handle_show(args: dict):
    query = _text_arg(args, "run_id") or "last"
    path = _trace.find_run(query)
    records = _trace.read_run(path)
    start = next((r for r in records if r.get("type") == "run_start"), {})
    end = next((r for r in records if r.get("type") == "run_end"), None)
    done = {r.get("span_id"): r for r in records if r.get("type") == "span"}
    current = _trace.current_span_id()
    rows = []
    for rec in records:
        if rec.get("type") != "span_start":
            continue
        span = done.get(rec.get("span_id"))
        if span is not None:
            outcome = span.get("outcome") or span.get("status", {}).get("code", "?")
            ms = f"{span.get('duration_ms', 0):.0f}"
        else:
            outcome = "(this call)" if rec.get("span_id") == current else "unfinished"
            ms = ""
        intent = (span or rec).get("intent") or {}
        rows.append([str(rec.get("seq", "")), f"`{rec.get('name', '?')}`", outcome, ms,
                     _trace.field_text(intent.get("step", ""))])
    errors = sum(1 for s in done.values() if s.get("status", {}).get("code") == "ERROR")
    state = f"ended ({end.get('reason')})" if end else "open"
    out = [
        f"**Run {start.get('run_id', path.stem)}** ({start.get('label', '?')}): "
        f"{_count(len(rows), 'call')}, {_count(errors, 'error')}, {state}",
    ]
    if start.get("goal"):
        out.append(f"Goal: {_trace.field_text(start['goal'])}")
    out.append(f"File: {path}")
    if len(rows) > SHOW_MAX_SPANS:
        out.append(f"*Showing the last {SHOW_MAX_SPANS}; scripts/fcp_trace.py show lists all.*")
        rows = rows[-SHOW_MAX_SPANS:]
    srv = _common.tools.server_module()
    if rows:
        out.append("")
        out.append(srv._markdown_table(["#", "Call", "Outcome", "ms", "Intent"], rows))
    return text_result("\n".join(out))


ACTIONS = {
    "begin_run": handle_begin_run,
    "log_intent": handle_log_intent,
    "end_run": handle_end_run,
    "status": handle_status,
    "show": handle_show,
}

DESCRIPTION = (
    "Local run tracing for debugging. Every call is already recorded under "
    "~/.fcp-mcp/traces/ and nothing leaves the machine; this group only labels "
    "and reads the record. begin_run (args: goal, project_path) names a task; "
    "log_intent (args: step, expected_effect) explains the next mutating call; "
    "end_run (args: outcome) closes the run; status shows the open run; show "
    "(args: run_id, a prefix or \"last\") lists its calls. Never required."
)
