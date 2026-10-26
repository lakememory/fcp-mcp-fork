"""Run tracing: every tool call as JSON lines under ``~/.fcp-mcp/traces/``.

The journal records what a call WROTE. This records what was asked and what
came back, for every call: reads, refusals and crashes included, with timing,
the subprocesses it ran and the warnings it logged. An agent's action sequence
can be read back in order, and a failure read in place instead of
reconstructed. The records are the Logic Pro MCP's schema 1, so one viewer
(``scripts/fcp_trace.py``) reads both servers' traces. docs/TRACE.md is the
schema.

On by default and local only. ``FCP_MCP_TRACE=off`` (or 0/false/no) disables
it: no file, no directory, every result byte-identical. ``FCP_MCP_TRACE_DIR``
moves the root. Nothing here may fail a call: each entry point catches its own
exceptions and counts them for ``trace status``.
"""

from __future__ import annotations

import atexit
import contextvars
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from fcpxml import journal as _journal

SCHEMA = 1
SERVER_NAME = "fcp-mcp-server"
FIELD_LIMIT = 8192
MAX_FILE_BYTES = 50 * 1024 * 1024
IDLE_SECONDS = 1800      # an implicit run closes after this long without a call
STDERR_TAIL = 2000       # characters of each subprocess's stderr kept
SPAN_LIST_CAP = 100      # subprocess and log entries kept per span

INSTRUCTIONS = (
    "Calls are traced locally for debugging; optionally call trace begin_run {goal} "
    "at the start of a task and trace log_intent {step, expected_effect} before a "
    "mutating call (never required)."
)

_OFF = {"off", "0", "false", "no"}


def _table(*rows: tuple[str, bool, str]) -> dict[str, bool]:
    return {f"{group}.{action}": flag for group, flag, actions in rows for action in actions.split()}


# Which calls change something: an edited, generated or exported file, files
# moved by undo, Final Cut Pro itself, or money. Analysis that leaves a cache
# or sidecar behind (transcripts, beats JSON, previews, index rows) is a read;
# a span's `outputs` lists those files whatever this says. tests/test_trace.py
# compares the keys with TOOL_GROUPS, so an action upstream adds fails a test
# until it is classified here instead of reading as a silent `false`.
MUTATING: dict[str, bool] = _table(
    ("inspect", False, "list_projects analyze_timeline analyze_pacing list_clips list_markers "
                       "list_roles list_keywords list_effects list_templates list_library_clips "
                       "list_compound_clips list_connected_clips filter_by_role list_keyframes "
                       "list_speed_points"),
    ("diagnose", False, "validate_timeline detect_gaps detect_duplicates detect_flash_frames "
                        "detect_silence_candidates detect_media_silence detect_beats "
                        "find_short_cuts find_long_clips diff_timelines"),
    ("edit", True, "insert_clip delete_clips trim_clip split_clip reorder_clips change_speed "
                   "rapid_trim add_transition add_audio add_connected_clip assign_role fill_gaps "
                   "fix_flash_frames remove_silence_candidates remove_media_silence "
                   "set_keyframes delete_keyframes batch_keyframes set_animation_curve "
                   "set_speed_curve reset_speed"),
    ("mark", True, "add_marker batch_add_markers import_srt_markers import_transcript_markers "
                   "import_beat_markers snap_to_beats"),
    ("generate", True, "auto_rough_cut generate_ab_roll generate_montage apply_template "
                       "create_compound_clip flatten_compound_clip import_edl_json"),
    ("transcript", False, "transcribe_media transcript_pack"),
    ("transcript", True, "edit_by_transcript remove_filler_words"),
    # export_csv, export_edl and export_role_stems answer in chat and write nothing.
    ("deliver", False, "export_csv export_edl export_role_stems list_fcp_libraries"),
    ("deliver", True, "export_fcp7_xml export_resolve_xml reformat_timeline relink_media "
                      "push_to_fcp"),
    ("preview", False, "preview_render preview_sheet preview_frame preview_check "
                       "preview_timeline"),
    ("watch", False, "watch_start watch_status watch_stop watch_pull"),
    ("index", False, "index_status index_build index_clear"),
    ("scenes", False, "detect_scenes"),
    ("scenes", True, "scenes_to_markers scenes_split"),
    ("organize", False, "history"),
    ("organize", True, "organize_auto organize_keywords organize_rate organize_roles undo"),
    ("find", False, "find_index find_shots"),
    ("find", True, "find_to_timeline"),
    ("view", False, "view_timeline view_clip"),
    ("gen", False, "gen_quote"),
    ("gen", True, "gen_fill_gap gen_broll gen_extend_clip"),
    ("trace", False, "begin_run log_intent end_run status show"),
)


def enabled() -> bool:
    return os.environ.get("FCP_MCP_TRACE", "").strip().lower() not in _OFF


def trace_root() -> Path:
    """The trace root. Never created here; the first write creates it."""
    raw = os.environ.get("FCP_MCP_TRACE_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".fcp-mcp" / "traces"


def _field_limit() -> int:
    try:
        value = int(os.environ.get("FCP_MCP_TRACE_FIELD_LIMIT", ""))
    except ValueError:
        return FIELD_LIMIT
    return value if value >= 256 else FIELD_LIMIT


# -- state --------------------------------------------------------------------
# One open run per process, shared by every request; one span per call, in a
# contextvar so concurrent requests each see their own (the journal's _LEDGER
# does the same). _LOCK serialises run bookkeeping and file appends.

_LOCK = threading.RLock()
_SPAN: contextvars.ContextVar[Optional["_Span"]] = contextvars.ContextVar(
    "fcp_trace_span", default=None
)
_state: dict[str, Any] = {
    "run": None, "failures": 0, "last_failure": None, "version": "", "groups": {},
    "atexit": False, "hooked": False,
}


def bind(version: str, groups: dict) -> None:
    """Hand over the server version and the live TOOL_GROUPS. Called by server.py."""
    _state["version"] = version
    _state["groups"] = groups


def _failed(exc: BaseException) -> None:
    _state["failures"] += 1
    _state["last_failure"] = f"{type(exc).__name__}: {exc}"


class _Run:
    def __init__(self, root: Path, label: str, goal: str, project_path: Optional[str],
                 start_ns: Optional[int] = None):
        # A run starts when the call that opened it did, so no span precedes it.
        self.start_ns = start_ns or time.time_ns()
        seconds = self.start_ns // 1_000_000_000
        self.run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(seconds)) + "-" + secrets.token_hex(3)
        self.trace_id = secrets.token_hex(16)
        self.span_id = secrets.token_hex(8)
        self.root = root
        self.day_dir = root / time.strftime("%Y-%m-%d", time.localtime(seconds))
        self.label, self.goal, self.project_path = label, goal, project_path
        self.field_limit = _field_limit()
        self.part = 1
        self.bytes = 0
        self.span_count = 0
        self.error_count = 0
        self.last_activity = time.monotonic()
        self.pending_intent: Optional[dict] = None
        self.closing: Optional[tuple[Optional[str], str]] = None
        self.finished = False

    def file(self, part: Optional[int] = None) -> Path:
        part = part or self.part
        return self.day_dir / (f"{self.run_id}.jsonl" if part == 1 else f"{self.run_id}.{part}.jsonl")


class _Span:
    def __init__(self, tool: str, command: Optional[str], called_as: Optional[str], args: Any):
        self.tool, self.command, self.called_as = tool, command, called_as
        self.name = f"{tool}.{command}" if command else tool
        self.mutating = MUTATING.get(self.name, False)
        self.span_id = secrets.token_hex(8)
        self.start_ns = time.time_ns()
        self.t0 = time.perf_counter()
        self.limit = _field_limit()
        self.secrets = _env_secrets()
        self.args = _field(_redact(args, None, self.secrets), self.limit)
        self.run: Optional[_Run] = None
        self.seq = 0
        self.intent: Optional[dict] = None
        self.outcome: Optional[str] = None
        self.fields: dict[str, Any] = {}
        self.subprocess: list[dict] = []
        self.logs: list[dict] = []
        self.token: Optional[contextvars.Token] = None
        self.closed = False


# -- dispatch hooks -------------------------------------------------------------

def start_span(name: str, arguments: Any) -> Optional[_Span]:
    """Open the span for one call to *name*. ``None`` when tracing is off."""
    if not enabled():
        return None
    try:
        tool, command, called_as = _resolve(name, arguments)
        span = _Span(tool, command, called_as, arguments)
        _hook_logging()
        # begin_run's span belongs to the run it starts, so it opens when it closes.
        if span.name != "trace.begin_run":
            _open(span)
        span.token = _SPAN.set(span)
        return span
    except Exception as exc:
        _failed(exc)
        return None


def end_span(span: Optional[_Span], result: Any, exc: Optional[BaseException] = None) -> Any:
    """Close *span* and return *result* untouched, whatever happens here."""
    if span is None:
        return result
    try:
        _close(span, result, exc)
    except Exception as failure:
        _failed(failure)
    finally:
        if span.token is not None:
            try:
                _SPAN.reset(span.token)
            except ValueError:  # closed from another context; nothing to restore
                pass
            span.token = None
    return result


def outcome(name: str, exc: Optional[BaseException] = None) -> None:
    """Name how dispatch is ending this call, with the exception if there is one."""
    span = _SPAN.get()
    if span is None:
        return
    try:
        span.outcome = name
        if exc is not None:
            span.fields["error"] = _error(exc, span)
    except Exception as failure:
        _failed(failure)


def ref() -> str:
    """`` (trace <run_id>:<seq>)`` for the current call, or ``""``."""
    span = _SPAN.get()
    if span is None or span.run is None:
        return ""
    return f" (trace {span.run.run_id}:{span.seq})"


def note(**fields: Any) -> None:
    """Attach FCP facts (``gated``, ``autopushed``, ...) to the current span."""
    span = _SPAN.get()
    if span is not None:
        span.fields.update(fields)


def note_input() -> None:
    """Copy the input path and hash from the journal ledger ``begin`` just opened.

    ``_journal.begin`` hashes the input on every call, reads included; reading
    it back here is what keeps the trace from hashing the file a second time.
    """
    span = _SPAN.get()
    if span is None:
        return
    try:
        ledger = _journal._LEDGER.get()
        given = (ledger or {}).get("input") or {}
        if given.get("path"):
            span.fields["input"] = {"path": given["path"], "sha256": given.get("sha256")}
    except Exception as failure:
        _failed(failure)


def note_outputs(written: list, hashes: Optional[dict] = None) -> None:
    """Record what the request wrote; the FCPXML hashes once _written_hashes has them."""
    span = _SPAN.get()
    if span is None:
        return
    try:
        outputs = []
        for path in written:
            entry: dict[str, Any] = {"path": str(path)}
            if hashes and str(path).endswith((".fcpxml", ".fcpxmld")):
                digest = hashes.get(Path(path).name)
                if digest:
                    entry["sha256"] = digest
            outputs.append(entry)
        span.fields["outputs"] = outputs
    except Exception as failure:
        _failed(failure)


def _resolve(name: str, arguments: Any) -> tuple[str, Optional[str], Optional[str]]:
    """(group, action, called_as): a flat legacy name is recorded under its group."""
    groups = _state["groups"]
    if name in groups:
        action = arguments.get("action") if isinstance(arguments, dict) else None
        return name, action if isinstance(action, str) and action else None, None
    for group, spec in groups.items():
        if name in spec.get("actions", ()):
            return group, name, name
    return name, None, None


# -- subprocesses -----------------------------------------------------------------

def traced_run(args, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run``, recorded on the current span.

    Same arguments, same return value, same exceptions. ``subprocess.run`` is
    looked up at call time, so a test that patches it still patches this.
    """
    span = _SPAN.get()
    if span is None:
        return subprocess.run(args, **kwargs)
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(args, **kwargs)
    except BaseException as exc:
        _note_subprocess(span, args, t0, getattr(exc, "returncode", None),
                         getattr(exc, "stderr", None), exc)
        raise
    _note_subprocess(span, args, t0, getattr(proc, "returncode", None),
                     getattr(proc, "stderr", None), None)
    return proc


def _note_subprocess(span, args, t0, returncode, stderr, exc) -> None:
    try:
        if len(span.subprocess) >= SPAN_LIST_CAP:
            span.fields["subprocess_dropped"] = span.fields.get("subprocess_dropped", 0) + 1
            return
        argv = args.split() if isinstance(args, str) else list(args)
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        entry = {
            "argv0": Path(str(argv[0])).name if argv else "",
            "returncode": returncode if isinstance(returncode, int) else None,
            "stderr_tail": _scrub(stderr[-STDERR_TAIL:], span.secrets) if isinstance(stderr, str) else None,
            "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        if exc is not None:
            entry["error"] = type(exc).__name__
        span.subprocess.append(entry)
    except Exception as failure:
        _failed(failure)


# -- logging ----------------------------------------------------------------------

class _SpanLogHandler(logging.Handler):
    """Copies ``fcpxml.*`` records onto the current span, and changes nothing else.

    A handler on the ``fcpxml`` logger is "a handler found", so Python would
    stop sending those records to ``logging.lastResort`` (stderr, WARNING and
    up), which is where they go today because nothing configures logging. emit
    hands a record to lastResort in exactly the case Python would have.
    """

    def emit(self, record: logging.LogRecord) -> None:
        span = _SPAN.get()
        if span is not None:
            try:
                if len(span.logs) < SPAN_LIST_CAP:
                    span.logs.append({
                        "level": record.levelname, "logger": record.name,
                        "message": _field(_scrub(record.getMessage(), span.secrets), span.limit),
                    })
                else:
                    span.fields["logs_dropped"] = span.fields.get("logs_dropped", 0) + 1
            except Exception as failure:
                _failed(failure)
        last = logging.lastResort
        if last is not None and record.levelno >= last.level and not self._another_handler(record):
            last.handle(record)

    def _another_handler(self, record: logging.LogRecord) -> bool:
        logger: Optional[logging.Logger] = logging.getLogger(record.name)
        while logger is not None:
            if any(h is not self for h in logger.handlers):
                return True
            logger = logger.parent if logger.propagate else None
        return False


_LOG_HANDLER = _SpanLogHandler()


def _hook_logging() -> None:
    if not _state["hooked"]:
        logging.getLogger("fcpxml").addHandler(_LOG_HANDLER)
        _state["hooked"] = True


# -- runs and spans ----------------------------------------------------------------

def _iso(ns: int) -> str:
    seconds, ms = divmod(ns // 1_000_000, 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{ms:03d}Z"


def _base(kind: str, run: _Run, span_id: str, ns: Optional[int] = None) -> dict:
    return {
        "schema": SCHEMA, "type": kind, "run_id": run.run_id, "trace_id": run.trace_id,
        "span_id": span_id, "ts": _iso(ns if ns is not None else time.time_ns()),
    }


def _active_run(start_ns: Optional[int] = None) -> _Run:
    root = trace_root()
    run = _state["run"]
    if run is not None:
        if run.root != root:
            _finish(run, None, "relocated")
        elif run.label == "implicit" and time.monotonic() - run.last_activity > IDLE_SECONDS:
            _finish(run, None, "idle_timeout")
        else:
            return run
    return _start("implicit", "", None, root, start_ns)


def _start(label: str, goal: str, project_path: Optional[str], root: Optional[Path] = None,
           start_ns: Optional[int] = None) -> _Run:
    with _LOCK:
        run = _Run(root or trace_root(), label, goal, project_path, start_ns)
        _state["run"] = run
        if not _state["atexit"]:
            atexit.register(_at_exit)
            _state["atexit"] = True
        rec = _base("run_start", run, run.span_id, run.start_ns)
        rec.update({
            "label": label,
            "goal": _field(goal, run.field_limit),
            "server": {"name": SERVER_NAME, "version": _state["version"], "pid": os.getpid(),
                       "git_sha": _git_sha()},
            "config": {
                "field_limit": run.field_limit, "max_file_bytes": MAX_FILE_BYTES,
                "idle_timeout_s": IDLE_SECONDS, "stderr_tail": STDERR_TAIL,
                "span_list_cap": SPAN_LIST_CAP,
                "log_level": logging.getLevelName(logging.getLogger("fcpxml").getEffectiveLevel()),
                "redact_keys": "key|token|secret|password|api",
            },
            "start_time": _iso(run.start_ns),
            "start_ns": run.start_ns,
        })
        if project_path:
            rec["project_path"] = project_path
        _append(run, rec)
        return run


def _finish(run: _Run, outcome_text: Optional[str], reason: str) -> None:
    with _LOCK:
        if run.finished:
            return
        run.finished = True
        if run.pending_intent is not None:
            _append(run, {**_base("event", run, run.span_id), "name": "intent_unused",
                          "intent": run.pending_intent})
            run.pending_intent = None
        end_ns = time.time_ns()
        rec = _base("run_end", run, run.span_id, end_ns)
        rec.update({
            "reason": reason, "end_time": _iso(end_ns), "end_ns": end_ns,
            "duration_ms": round((end_ns - run.start_ns) / 1e6, 1),
            "span_count": run.span_count, "error_count": run.error_count,
            "status": {"code": "ERROR" if run.error_count else "OK"},
        })
        if outcome_text is not None:
            rec["outcome"] = _field(outcome_text, run.field_limit)
        _append(run, rec)
        if _state["run"] is run:
            _state["run"] = None


def _at_exit() -> None:
    run = _state["run"]
    if run is not None and enabled():
        try:
            _finish(run, None, "server_exit")
        except Exception as failure:
            _failed(failure)


def _head(span: _Span) -> dict:
    head = {
        "seq": span.seq, "tool": span.tool, "name": span.name, "mutating": span.mutating,
        "parent_span_id": span.run.span_id, "start_time": _iso(span.start_ns),
        "start_ns": span.start_ns,
    }
    if span.command:
        head["command"] = span.command
    if span.called_as:
        head["called_as"] = span.called_as
    if span.intent is not None:
        head["intent"] = span.intent
        head["intent_span_id"] = span.intent["intent_id"]
    return head


def _open(span: _Span) -> None:
    with _LOCK:
        run = _active_run(span.start_ns)
        run.span_count += 1
        run.last_activity = time.monotonic()
        span.run, span.seq = run, run.span_count
        # An intent belongs to the next MUTATING call; reads in between (an
        # agent checking state before acting) do not take it.
        if span.mutating and run.pending_intent is not None:
            span.intent, run.pending_intent = run.pending_intent, None
        _append(run, {**_base("span_start", run, span.span_id, span.start_ns), **_head(span)})


def _close(span: _Span, result: Any, exc: Optional[BaseException]) -> None:
    if span.closed:
        return
    span.closed = True
    if exc is not None:
        span.outcome = "error"
        span.fields["error"] = _error(exc, span)
    duration_ns = int((time.perf_counter() - span.t0) * 1e9)
    end_ns = span.start_ns + duration_ns
    with _LOCK:
        if span.run is None:
            _open(span)
        run = span.run
        result_name = span.outcome or "ok"
        text = _result_text(result)
        status: dict[str, Any] = {"code": "OK"}
        if result_name != "ok":
            # The line the model was shown, except where that is only a class
            # name ("Error: RuntimeError"): then the exception's own words.
            run.error_count += 1
            error = span.fields.get("error")
            lines = (text or "").strip().splitlines()
            if error and (result_name == "error" or not lines):
                message = f"{error['type']}: {field_text(error['message'])}"
            else:
                message = _scrub(lines[0], span.secrets) if lines else result_name
            status = {"code": "ERROR", "message": _field(message, span.limit)}
        rec = {**_base("span", run, span.span_id, end_ns), **_head(span), **span.fields}
        rec.update({
            "args": span.args,
            "result": _result_field(span, text),
            "status": status,
            "outcome": result_name,
            "duration_ms": round(duration_ns / 1e6, 1),
            "end_time": _iso(end_ns),
            "end_ns": end_ns,
        })
        if span.subprocess:
            rec["subprocess"] = span.subprocess
        if span.logs:
            rec["logs"] = span.logs
        _append(run, rec)
        if span.name == "trace.end_run" and run.closing is not None:
            _finish(run, *run.closing)


def _result_text(result: Any) -> Optional[str]:
    if result is None:
        return None
    texts = [getattr(item, "text", None) for item in result]
    texts = [t for t in texts if isinstance(t, str)]
    return "\n\n".join(texts) if texts else None


def _result_field(span: _Span, text: Optional[str]) -> Any:
    if text is None:
        return None
    if span.name == "transcript.transcript_pack":  # the reply IS the transcript
        return _content(text, "transcript")
    if _is_fcpxml(text):
        return _content(text, "fcpxml")
    return _field(_scrub(text, span.secrets), span.limit)


def field_text(value: Any) -> str:
    """The text of a possibly truncated field."""
    if isinstance(value, dict) and "preview" in value:
        return value["preview"]
    return str(value)


def _error(exc: BaseException, span: _Span) -> dict:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    error = {
        "type": type(exc).__name__,
        "message": _field(_scrub(str(exc), span.secrets), span.limit),
        "traceback": _field(_scrub(tb, span.secrets), span.limit),
    }
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        last = frames[-1]
        error["frame"] = f"{_short(last.filename)}:{last.lineno} in {last.name}"
    return error


def _short(filename: str) -> str:
    base = Path(__file__).resolve().parent.parent
    try:
        return str(Path(filename).resolve().relative_to(base))
    except (ValueError, OSError):
        return filename


_GIT_SHA: dict[str, Optional[str]] = {}


def _git_sha() -> Optional[str]:
    """HEAD of the checkout this module runs from, read without running git."""
    if "sha" not in _GIT_SHA:
        _GIT_SHA["sha"] = None
        try:
            git = Path(__file__).resolve().parent.parent / ".git"
            if git.is_file():  # a worktree: ".git" names the real git dir
                git = (git.parent / git.read_text().split(":", 1)[1].strip()).resolve()
            common = git
            if (git / "commondir").is_file():
                common = (git / (git / "commondir").read_text().strip()).resolve()
            head = (git / "HEAD").read_text().strip()
            sha = head
            if head.startswith("ref:"):
                ref_name = head[4:].strip()
                loose = common / ref_name
                if loose.is_file():
                    sha = loose.read_text().strip()
                else:
                    packed = (common / "packed-refs").read_text().splitlines()
                    sha = next((ln.split()[0] for ln in packed if ln.endswith(" " + ref_name)), "")
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                _GIT_SHA["sha"] = sha
        except (OSError, IndexError):
            pass
    return _GIT_SHA["sha"]


# -- the trace tool ------------------------------------------------------------------

def _describe(run: _Run) -> dict:
    return {
        "run_id": run.run_id, "trace_id": run.trace_id, "label": run.label, "goal": run.goal,
        "project_path": run.project_path, "file": str(run.file()), "spans": run.span_count,
        "errors": run.error_count, "pending_intent": run.pending_intent,
    }


def begin_run(goal: str, project_path: Optional[str] = None) -> tuple[dict, Optional[dict]]:
    """End the open run (superseded) and start a labelled one."""
    with _LOCK:
        previous = _state["run"]
        replaced = None
        if previous is not None:
            replaced = {"run_id": previous.run_id, "label": previous.label}
            _finish(previous, None, "superseded")
        span = _SPAN.get()
        started = span.start_ns if span is not None and span.run is None else None
        return _describe(_start("explicit", goal, project_path, start_ns=started)), replaced


def log_intent(step: str, expected_effect: str) -> dict:
    """Record an intent for the next mutating call in the open run."""
    span = _SPAN.get()
    with _LOCK:
        run = span.run if span is not None and span.run is not None else _active_run()
        ns = time.time_ns()
        intent = {
            "intent_id": secrets.token_hex(8),
            "step": _field(step, run.field_limit),
            "expected_effect": _field(expected_effect, run.field_limit),
            "ts": _iso(ns),
        }
        replaced = run.pending_intent
        if replaced is not None:
            _append(run, {**_base("event", run, run.span_id, ns), "name": "intent_superseded",
                          "intent": replaced})
        run.pending_intent = intent
        _append(run, {**_base("intent", run, intent["intent_id"], ns), "intent": intent})
        return {"intent_id": intent["intent_id"], "run_id": run.run_id, "replaced": replaced is not None}


def end_run(outcome_text: Optional[str] = None) -> Optional[dict]:
    """Close the open run. Through call_tool, after the end_run span is written."""
    span = _SPAN.get()
    with _LOCK:
        run = _state["run"]
        if run is None:
            return None
        run.closing = (outcome_text, "end_run")
        info = _describe(run)
        info["file"] = str(run.file(1))
        if span is None or span.run is not run:
            _finish(run, outcome_text, "end_run")
        return info


def status() -> dict:
    run = _state["run"]
    return {
        "enabled": enabled(), "root": str(trace_root()),
        "write_failures": _state["failures"], "last_failure": _state["last_failure"],
        "run": _describe(run) if run is not None and enabled() else None,
    }


def current_span_id() -> Optional[str]:
    span = _SPAN.get()
    return span.span_id if span is not None else None


_RUN_FILE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}\.jsonl")


def _newest_first(path: Path) -> tuple[str, float]:
    # The run id's second, then the file's last write: begin_run supersedes an
    # implicit run within the same second, and the hex suffix orders nothing.
    try:
        return path.name[:16], path.stat().st_mtime
    except OSError:
        return path.name[:16], 0.0


def run_files(root: Optional[Path] = None) -> list[Path]:
    """Every run's first file under *root*, newest first."""
    root = root or trace_root()
    if not root.is_dir():
        return []
    files = [p for day in root.iterdir() if day.is_dir()
             for p in day.iterdir() if _RUN_FILE.fullmatch(p.name)]
    return sorted(files, key=_newest_first, reverse=True)


def find_run(query: str, root: Optional[Path] = None) -> Path:
    """A run id, a unique prefix of one, or ``last`` (the open run, if any)."""
    files = run_files(root)
    if not files:
        raise FileNotFoundError(f"no traces under {root or trace_root()}")
    if query in ("last", "latest"):
        run = _state["run"]
        if run is not None and root is None and run.file(1).is_file():
            return run.file(1)
        return files[0]
    hits = [p for p in files if p.name[:-len(".jsonl")].startswith(query)]
    exact = [p for p in hits if p.name == f"{query}.jsonl"]
    if exact or len(hits) == 1:
        return (exact or hits)[0]
    if not hits:
        raise FileNotFoundError(f"no run {query} under {root or trace_root()}")
    names = ", ".join(p.name[:-len(".jsonl")] for p in hits[:5])
    raise ValueError(f"{len(hits)} runs match {query}: {names}")


def read_run(first: Path) -> list[dict]:
    """Every record of a run, continuation files included, in order."""
    records = []
    part, path = 1, first
    while path.is_file():
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue  # a torn line from a crash mid-write; the rest is intact
        part += 1
        path = first.with_name(f"{first.name[:-len('.jsonl')]}.{part}.jsonl")
    return records


# -- records on disk -------------------------------------------------------------------

def _encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str).encode("utf-8")


def _append(run: _Run, record: dict) -> None:
    try:
        line = _encode(record) + b"\n"
        with _LOCK:
            if run.bytes and run.bytes + len(line) > MAX_FILE_BYTES:
                _roll(run)
            _write_line(run, run.file(), line)
            run.bytes += len(line)
    except Exception as exc:
        _failed(exc)


def _roll(run: _Run) -> None:
    """Close a full file with a pointer to the next and start that one."""
    old, new = run.file(), run.file(run.part + 1)
    _write_line(run, old, _encode({**_base("event", run, run.span_id), "name": "continued",
                                    "next_file": new.name}) + b"\n")
    run.part += 1
    first = _encode({**_base("event", run, run.span_id), "name": "continuation",
                     "part": run.part, "previous_file": old.name}) + b"\n"
    _write_line(run, new, first)
    run.bytes = len(first)


def _write_line(run: _Run, path: Path, data: bytes) -> None:
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
        private = [run.root, path.parent]
        if not os.environ.get("FCP_MCP_TRACE_DIR", "").strip():
            private.insert(0, run.root.parent)  # ~/.fcp-mcp, as the journal does
        for p in private:
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "ab") as fh:
        fh.write(data)


# -- size and privacy ----------------------------------------------------------------------

_SECRET_WORD = re.compile(r"(?:api)?key|api|.*(?:token|secret|password|passwd|apikey)")


def _secret_name(name: str) -> bool:
    """Whether a key or variable name has a secret-ish WORD in it.

    Split on separators and camelCase rather than searched as a substring:
    "key" is inside ``keywords`` and ``keyframes``, two of the most common
    arguments this server takes.
    """
    words = re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower())
    return any(w and _SECRET_WORD.fullmatch(w) for w in words)


def _env_secrets() -> list[tuple[str, str]]:
    """(value, name) for environment values no trace line may contain.

    The value of every variable with a secret-ish name (FAL_KEY,
    ELEVENLABS_API_KEY, GEMINI_API_KEY, ...). Not every value: ids and names
    in the environment are also parts of paths (a session id is a directory
    name), and scrubbing them rewrote the paths a trace exists to show. Under
    8 characters nothing is replaced, or a variable set to "1" would erase
    every 1.
    """
    out = [(value, name) for name, value in os.environ.items()
           if len(value) >= 8 and _secret_name(name)]
    return sorted(out, key=lambda pair: -len(pair[0]))


def _scrub(text: str, secrets_: list[tuple[str, str]]) -> str:
    for value, name in secrets_:
        if value in text:
            text = text.replace(value, f"[redacted:{name}]")
    return text


def _is_fcpxml(text: str) -> bool:
    """A string that IS an FCPXML document, not one that mentions the element."""
    head = text[:4096].lstrip()
    return head.startswith(("<?xml", "<!DOCTYPE", "<fcpxml")) and "<fcpxml" in head


def _content(text: str, kind: str) -> dict:
    data = text.encode("utf-8")
    return {"_redacted": kind, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _redact(value: Any, key: Optional[str], secrets_: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        if key is not None and _secret_name(key):
            return {"_redacted": "secret"}
        if key == "transcript":
            return _content(value, "transcript")
        if _is_fcpxml(value):
            return _content(value, "fcpxml")
        return _scrub(value, secrets_)
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k), secrets_) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, None, secrets_) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _scrub(str(value), secrets_)


def _marker(length: int, digest: str, preview: bytes, limit: int) -> dict:
    """``{_truncated, length, sha256, preview}``, cut until it fits *limit* itself."""
    n = max(0, limit - 256)
    while True:
        marker = {"_truncated": True, "truncated": True, "length": length, "sha256": digest,
                  "preview": preview[:n].decode("utf-8", errors="ignore")}
        if n == 0 or len(_encode(marker)) <= limit:
            return marker
        n //= 2


def _cut(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        data = value.encode("utf-8")
        if len(data) <= limit:
            return value
        return _marker(len(data), hashlib.sha256(data).hexdigest(), data, limit)
    if isinstance(value, dict):
        return {k: _cut(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut(v, limit) for v in value]
    return value


def _field(value: Any, limit: int) -> Any:
    """*value* with no string over *limit* bytes, and the whole within it too.

    The Logic MCP's rule: long strings become markers first; a field whose JSON
    is still over the limit becomes one marker with a JSON-text preview.
    """
    cut = _cut(value, limit)
    encoded = _encode(cut)
    if len(encoded) <= limit:
        return cut
    full = _encode(value)
    return _marker(len(full), hashlib.sha256(full).hexdigest(), encoded, limit)
