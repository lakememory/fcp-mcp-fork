"""Run tracing (fork): every call is a span, and tracing can never change a call.

Driven through call_tool the way the dispatch tests are. The outcome table,
the off switch, a tracer that cannot write, redaction, truncation and the
continuation file are each checked against the records on disk, and the
viewer reads what these tests wrote. docs/TRACE.md is the schema.
"""

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import server
from fcpxml import journal, live, trace

REPO = Path(__file__).resolve().parent.parent
SAMPLE = REPO / "examples" / "sample.fcpxml"
VIEWER = REPO / "scripts" / "fcp_trace.py"
WRONG = "0" * 64
EMPTY = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
         '<fcpxml version="1.13"><resources/></fcpxml>\n')


@pytest.fixture(autouse=True)
def traces(tmp_path, monkeypatch):
    """Tracing on (whatever the pass), a private root, and no run carried in."""
    monkeypatch.delenv("FCP_MCP_TRACE", raising=False)
    monkeypatch.delenv("FCP_MCP_TRACE_FIELD_LIMIT", raising=False)
    monkeypatch.delenv("FCP_MCP_AUTOPUSH", raising=False)
    root = tmp_path / "traces"
    monkeypatch.setenv("FCP_MCP_TRACE_DIR", str(root))
    trace._state.update(run=None, failures=0, last_failure=None)
    yield root
    trace._state["run"] = None


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    p = folder / "sample.fcpxml"
    shutil.copy(SAMPLE, p)
    return p


def _call(name, arguments):
    return asyncio.run(server.call_tool(name, arguments))[0].text


def _untraced(name, arguments):
    return asyncio.run(server._call_tool(name, arguments))[0].text


def _records():
    return trace.read_run(trace._state["run"].file(1))


def _spans():
    return [r for r in _records() if r["type"] == "span"]


def _last_span():
    return _spans()[-1]


def _list_clips(path):
    return ("inspect", {"action": "list_clips", "args": {"filepath": str(path)}})


def _marker(path, name="m", **extra):
    args = {"filepath": str(path), "timecode": "00:00:01:00", "name": name, **extra}
    return ("mark", {"action": "add_marker", "args": args})


def _boom_on(monkeypatch, action="list_clips", exc=None):
    async def boom(args):
        raise exc or RuntimeError("disk on fire")
    monkeypatch.setitem(server.TOOL_HANDLERS, action, boom)


def _viewer():
    spec = importlib.util.spec_from_file_location("fcp_trace_viewer", VIEWER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- one test per outcome -------------------------------------------------------

def _case_ok(project, monkeypatch):
    return _list_clips(project)


def _case_refused_guard(project, monkeypatch):
    return _marker(project, expected_sha256=WRONG)


def _case_refused_gate(project, monkeypatch):
    return ("deliver", {"action": "export_csv", "args": {"filepath": str(project)}})


def _case_unknown_action(project, monkeypatch):
    return ("edit", {"action": "no_such_action"})


def _case_unknown_tool(project, monkeypatch):
    return ("no_such_tool", {})


def _case_missing_argument(project, monkeypatch):
    return ("inspect", {"action": "list_clips", "args": {}})


def _case_validation_error(project, monkeypatch):
    bad = project.with_suffix(".txt")
    bad.write_text("not a project")
    return _list_clips(bad)


def _case_args_not_an_object(project, monkeypatch):
    return ("inspect", {"action": "list_clips", "args": "filepath=x"})


def _case_not_found(project, monkeypatch):
    return _list_clips(project.with_name("gone.fcpxml"))


def _case_no_timeline(project, monkeypatch):
    empty = project.with_name("empty.fcpxml")
    empty.write_text(EMPTY)
    return _list_clips(empty)


def _case_error(project, monkeypatch):
    _boom_on(monkeypatch)
    return _list_clips(project)


@pytest.mark.parametrize("outcome, case", [
    ("ok", _case_ok),
    ("refused_guard", _case_refused_guard),
    ("refused_gate", _case_refused_gate),
    ("unknown_action", _case_unknown_action),
    ("unknown_action", _case_unknown_tool),
    ("missing_argument", _case_missing_argument),
    ("validation_error", _case_validation_error),
    ("validation_error", _case_args_not_an_object),
    ("not_found", _case_not_found),
    ("no_timeline", _case_no_timeline),
    ("error", _case_error),
], ids=lambda v: v if isinstance(v, str) else v.__name__[len("_case_"):])
def test_each_dispatch_branch_has_one_outcome(outcome, case, project, monkeypatch):
    name, arguments = case(project, monkeypatch)
    text = _call(name, arguments)
    span = _last_span()
    assert span["outcome"] == outcome
    assert span["status"]["code"] == ("OK" if outcome == "ok" else "ERROR")
    if outcome == "ok":
        assert span["result"] == text
    elif outcome != "error":
        # The status names what the model was shown.
        assert span["status"]["message"] == text.strip().splitlines()[0]
    starts = [r for r in _records() if r["type"] == "span_start"]
    assert [s["span_id"] for s in starts] == [span["span_id"]]


def test_exceptions_are_kept_for_every_except_branch(project):
    _call("inspect", {"action": "list_clips", "args": {}})
    error = _last_span()["error"]
    assert error["type"] == "KeyError" and error["message"] == "'filepath'"
    assert error["frame"].startswith("server.py:") and error["frame"].endswith("in handle_list_clips")


def test_an_unexpected_error_keeps_type_message_and_traceback(project, monkeypatch):
    _boom_on(monkeypatch)
    text = _call(*_list_clips(project))
    span = _last_span()
    run = trace._state["run"]
    assert text == f"Error: RuntimeError (trace {run.run_id}:{span['seq']})"
    assert span["error"]["type"] == "RuntimeError"
    assert span["error"]["message"] == "disk on fire"
    assert span["error"]["traceback"].startswith("Traceback (most recent call last)")
    assert "RuntimeError: disk on fire" in span["error"]["traceback"]
    assert span["error"]["frame"].endswith("in boom")
    assert span["status"] == {"code": "ERROR", "message": "RuntimeError: disk on fire"}


def test_the_suffix_is_the_only_change_to_an_error_reply(project, monkeypatch):
    _boom_on(monkeypatch)
    traced = _call(*_list_clips(project))
    upstream = _untraced(*_list_clips(project))
    assert upstream == "Error: RuntimeError"
    assert traced.startswith(upstream + " (trace ")
    monkeypatch.setenv("FCP_MCP_TRACE", "off")
    assert _call(*_list_clips(project)) == upstream


def test_a_cancelled_call_is_recorded_and_still_cancelled(project, monkeypatch):
    _boom_on(monkeypatch, exc=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(server.call_tool(*_list_clips(project)))
    span = _last_span()
    assert span["outcome"] == "error" and span["error"]["type"] == "CancelledError"


# -- off, and a tracer that fails --------------------------------------------------

def _plain_calls(project):
    return [
        _list_clips(project),
        _marker(project, expected_sha256=WRONG),
        ("deliver", {"action": "export_csv", "args": {"filepath": str(project)}}),
        ("inspect", {"action": "list_clips", "args": {}}),
        ("edit", {"action": "no_such_action"}),
        ("no_such_tool", {}),
    ]


@pytest.mark.parametrize("value", ["off", "0", "false", "NO"])
def test_off_writes_nothing_and_changes_no_result(value, project, monkeypatch, traces):
    monkeypatch.setenv("FCP_MCP_TRACE", value)
    calls = _plain_calls(project)
    results = [asyncio.run(server.call_tool(n, a)) for n, a in calls]
    untraced = [asyncio.run(server._call_tool(n, a)) for n, a in calls]
    assert [[c.model_dump() for c in r] for r in results] == [[c.model_dump() for c in r] for r in untraced]
    _call(*_marker(project))
    _boom_on(monkeypatch)
    assert _call(*_list_clips(project)) == "Error: RuntimeError"
    assert not traces.exists()
    assert trace._state["run"] is None
    assert "nothing is recorded" in _call("trace", {"action": "begin_run", "args": {"goal": "x"}})


def test_on_changes_no_result_either(project):
    for name, arguments in _plain_calls(project):
        assert _call(name, arguments) == _untraced(name, arguments)


def test_a_tracer_that_cannot_write_changes_no_result(project, monkeypatch):
    expected = [_untraced(n, a) for n, a in _plain_calls(project)]

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(trace, "_write_line", full_disk)
    assert [_call(n, a) for n, a in _plain_calls(project)] == expected
    assert trace._state["failures"] > 0
    assert "No space left on device" in trace.status()["last_failure"]


@pytest.mark.parametrize("hook", ["_open", "_close", "_redact", "_error", "_hook_logging"])
def test_a_tracer_that_raises_anywhere_changes_no_result(hook, project, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError(f"{hook} is broken")

    calls = _plain_calls(project)
    expected = [_untraced(n, a) for n, a in calls]
    monkeypatch.setattr(trace, hook, broken)
    assert [_call(n, a) for n, a in calls] == expected
    _boom_on(monkeypatch)
    assert _call(*_list_clips(project)).startswith("Error: RuntimeError")
    assert trace._state["failures"] > 0


# -- what a span carries -------------------------------------------------------------

def test_a_write_records_its_input_and_output_hashes_once(project):
    text = _call(*_marker(project))
    span = _last_span()
    assert span["mutating"] is True and span["name"] == "mark.add_marker"
    assert span["input"] == {"path": str(project), "sha256": journal.file_hash(str(project))}
    [out] = span["outputs"]
    assert out["sha256"] == re.search(r"sha256 \([^)]+\): ([0-9a-f]{64})", text).group(1)
    assert out["sha256"] == journal.file_hash(out["path"])
    row = journal.records(str(project))[-1]
    assert row["input"] == span["input"]
    assert row["output"] == {"path": out["path"], "sha256": out["sha256"]}


def test_outputs_are_recorded_even_when_the_handler_then_raises(project, monkeypatch):
    out = project.with_name("half_written.fcpxml")

    async def writes_then_fails(args):
        server._validate_output_path(str(out), anchor_dir=str(project.parent))
        out.write_text(EMPTY)
        raise RuntimeError("after the write")

    monkeypatch.setitem(server.TOOL_HANDLERS, "add_marker", writes_then_fails)
    _call(*_marker(project))
    span = _last_span()
    assert span["outcome"] == "error"
    assert [o["path"] for o in span["outputs"]] == [str(out.resolve())]


def test_gate_and_autopush_facts(project, monkeypatch):
    _call("deliver", {"action": "export_csv", "args": {"filepath": str(project),
                                                       "confirm_unreviewed": True}})
    span = _last_span()
    assert (span["gated"], span["confirm_unreviewed"], span["autopushed"]) == (True, True, False)
    assert span["outcome"] == "ok"

    pushed = []
    monkeypatch.setenv("FCP_MCP_AUTOPUSH", "1")
    monkeypatch.setattr(live, "push_to_fcp", lambda path, **kw: pushed.append(path) or {"sent": path})
    _call(*_marker(project))
    span = _last_span()
    assert (span["gated"], span["confirm_unreviewed"], span["autopushed"]) == (False, False, True)
    assert pushed


def test_a_flat_legacy_name_is_recorded_under_its_group(project):
    _call("add_marker", {"filepath": str(project), "timecode": "00:00:01:00", "name": "flat"})
    span = _last_span()
    assert (span["tool"], span["command"], span["name"]) == ("mark", "add_marker", "mark.add_marker")
    assert span["called_as"] == "add_marker" and span["mutating"] is True


def test_every_line_carries_the_common_fields(project, monkeypatch):
    _call(*_list_clips(project))
    _call("trace", {"action": "log_intent", "args": {"step": "s", "expected_effect": "e"}})
    _call("trace", {"action": "end_run", "args": {"outcome": "done"}})
    lines = (trace.run_files()[0]).read_text().splitlines()
    for line in lines:
        rec = json.loads(line)
        assert rec["schema"] == 1 and rec["type"]
        assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", rec["run_id"])
        assert re.fullmatch(r"[0-9a-f]{32}", rec["trace_id"])
        assert re.fullmatch(r"[0-9a-f]{16}", rec["span_id"])
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", rec["ts"])
        assert line == json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    start = json.loads(lines[0])
    assert start["type"] == "run_start" and start["label"] == "implicit" and start["goal"] == ""
    assert start["server"]["name"] == "fcp-mcp-server"
    assert start["server"]["version"] == server.__version__
    assert start["server"]["pid"] == os.getpid()
    assert start["config"]["field_limit"] == trace.FIELD_LIMIT
    end = json.loads(lines[-1])
    assert end["type"] == "run_end" and end["reason"] == "end_run" and end["outcome"] == "done"
    assert end["span_count"] == 3 and end["error_count"] == 0 and end["status"] == {"code": "OK"}


def test_git_sha_is_the_checkout_head():
    if not (REPO / ".git").exists() or shutil.which("git") is None:
        pytest.skip("not a git checkout")
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                          text=True, timeout=30).stdout.strip()
    trace._GIT_SHA.clear()
    assert trace._git_sha() == head


def test_files_are_private(project, monkeypatch, tmp_path):
    _call(*_list_clips(project))
    run = trace._state["run"]
    assert run.file().stat().st_mode & 0o777 == 0o600
    assert run.day_dir.stat().st_mode & 0o777 == 0o700
    assert run.root.stat().st_mode & 0o777 == 0o700

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FCP_MCP_TRACE_DIR")
    _call(*_list_clips(project))
    assert trace._state["run"].root == home / ".fcp-mcp" / "traces"
    for p in (home / ".fcp-mcp", home / ".fcp-mcp" / "traces"):
        assert p.stat().st_mode & 0o777 == 0o700


# -- subprocesses and logs ---------------------------------------------------------------

def test_subprocesses_are_recorded_on_the_span(project, monkeypatch):
    def fake_run(args, **kwargs):
        if args[0] == "ffmpeg":
            return subprocess.CompletedProcess(args, 1, "", "x" * 5000 + "Invalid data found")
        raise subprocess.TimeoutExpired(args, 60, stderr=b"partial output")

    async def handler(args):
        trace.traced_run(["ffmpeg", "-i", "in.mov"], capture_output=True, text=True, timeout=60)
        with pytest.raises(subprocess.TimeoutExpired):
            trace.traced_run(["/opt/homebrew/bin/ffprobe", "in.mov"], capture_output=True, timeout=60)
        live._run_osascript('return "hi"')
        return server._text_result("done")

    monkeypatch.setattr(subprocess, "run", lambda args, **kw: (
        subprocess.CompletedProcess(args, 0, "hi", "") if args[0] == "osascript" else fake_run(args, **kw)))
    monkeypatch.setitem(server.TOOL_HANDLERS, "list_clips", handler)
    assert _call(*_list_clips(project)) == "done"
    ffmpeg, ffprobe, osascript = _last_span()["subprocess"]
    assert ffmpeg["argv0"] == "ffmpeg" and ffmpeg["returncode"] == 1
    assert len(ffmpeg["stderr_tail"]) == trace.STDERR_TAIL
    assert ffmpeg["stderr_tail"].endswith("Invalid data found")
    assert ffmpeg["duration_ms"] >= 0
    assert (ffprobe["argv0"], ffprobe["returncode"], ffprobe["error"]) == ("ffprobe", None, "TimeoutExpired")
    assert ffprobe["stderr_tail"] == "partial output"
    assert (osascript["argv0"], osascript["returncode"], osascript["stderr_tail"]) == ("osascript", 0, "")


def test_outside_a_call_the_helper_is_subprocess_run(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: seen.append((args, kw)) or "proc")
    assert trace.traced_run(["xmllint", "--noout", "x"], capture_output=True) == "proc"
    assert seen == [(["xmllint", "--noout", "x"], {"capture_output": True})]


def test_inside_a_call_a_fake_process_object_is_passed_through(project, monkeypatch):
    """Tests patch subprocess.run with all sorts of fakes; none may break a call."""
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: "not a CompletedProcess")

    async def handler(args):
        return server._text_result(str(trace.traced_run(["ffprobe", "x"])))

    monkeypatch.setitem(server.TOOL_HANDLERS, "list_clips", handler)
    assert _call(*_list_clips(project)) == "not a CompletedProcess"
    assert _last_span()["subprocess"][0]["returncode"] is None


def test_every_tool_subprocess_goes_through_the_helper():
    """A new subprocess.run call site upstream adds fails here, not silently."""
    allowed = {
        ("fcpxml/trace.py", 2),    # the helper itself
        ("fcpxml/live.py", 1),     # fcp_is_running: pgrep, no tool output worth keeping
        ("tools/preview.py", 1),   # the cmux image pane: node, cosmetic
    }
    found = set()
    for path in sorted([*REPO.glob("fcpxml/*.py"), *REPO.glob("tools/*.py"), REPO / "server.py"]):
        n = len(re.findall(r"\bsubprocess\.run\(", path.read_text()))
        if n:
            found.add((str(path.relative_to(REPO)), n))
    assert found == allowed


def test_fcpxml_warnings_land_on_the_span(project, monkeypatch):
    async def handler(args):
        logging.getLogger("fcpxml.render").warning("ffmpeg proxy render failed for %s", "x.mp4")
        logging.getLogger("elsewhere").warning("not an fcpxml logger")
        return server._text_result("done")

    monkeypatch.setitem(server.TOOL_HANDLERS, "list_clips", handler)
    _call(*_list_clips(project))
    assert _last_span()["logs"] == [
        {"level": "WARNING", "logger": "fcpxml.render", "message": "ffmpeg proxy render failed for x.mp4"},
    ]


_LOG_PROBE = """
import logging, sys
sys.path.insert(0, {repo!r})
from fcpxml import trace
if {hooked}:
    trace._hook_logging()
logging.getLogger("fcpxml.render").warning("ffmpeg proxy render failed for %s", "x.mp4")
logging.getLogger("fcpxml.index").info("below the default level")
logging.getLogger("other").warning("not ours")
"""


def test_the_log_handler_leaves_stderr_exactly_as_it_was():
    runs = [
        subprocess.run([sys.executable, "-c", _LOG_PROBE.format(repo=str(REPO), hooked=hooked)],
                       capture_output=True, text=True, timeout=120, cwd=str(REPO))
        for hooked in (False, True)
    ]
    assert runs[0].returncode == runs[1].returncode == 0
    assert runs[0].stderr == runs[1].stderr
    assert runs[1].stderr.endswith("ffmpeg proxy render failed for x.mp4\nnot ours\n")


# -- size and privacy ---------------------------------------------------------------------

def test_secret_keys_env_values_and_content_are_redacted(project, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-0123456789abcdef")
    monkeypatch.setenv("FCP_TEST_OPAQUE", "opaque-value-without-a-slash")  # not secret-named
    monkeypatch.setenv("FCP_TEST_API_FLAG", "1")
    transcript = "So we start with the interview, then cut to the drone shot."
    document = SAMPLE.read_text()
    args = {
        "filepath": str(project), "api_key": "sk-live-abcdef123456", "xi-api-key": "xi-0011223344",
        "accessToken": "tok-99887766", "password": "hunter2-hunter2",
        "keyword": "interview", "keywords": ["interview"],
        "keyframes": [{"time": "1s", "value": "1"}],
        "note": "key is fal-0123456789abcdef and opaque-value-without-a-slash, flag 1",
        "transcript": transcript, "xml": document,
    }

    async def echo(a):
        return server._text_result(f"echo {a['note']}")

    monkeypatch.setitem(server.TOOL_HANDLERS, "list_clips", echo)
    reply = _call("inspect", {"action": "list_clips", "args": args})
    assert "fal-0123456789abcdef" in reply  # the reply itself is untouched
    span = _last_span()
    recorded = span["args"]["args"]
    for key in ("api_key", "xi-api-key", "accessToken", "password"):
        assert recorded[key] == {"_redacted": "secret"}, key
    assert recorded["keyword"] == "interview"  # organize selects clips by it; "key" is not a word in it
    assert recorded["keywords"] == ["interview"]
    assert recorded["keyframes"] == [{"time": "1s", "value": "1"}]
    assert recorded["note"] == "key is [redacted:FAL_KEY] and opaque-value-without-a-slash, flag 1"
    assert recorded["transcript"] == {
        "_redacted": "transcript", "length": len(transcript.encode()),
        "sha256": hashlib.sha256(transcript.encode()).hexdigest(),
    }
    assert recorded["xml"]["_redacted"] == "fcpxml"
    assert span["result"] == "echo key is [redacted:FAL_KEY] and opaque-value-without-a-slash, flag 1"
    raw = trace._state["run"].file().read_text()
    for secret in ("fal-0123456789abcdef", "sk-live", "xi-0011", "tok-9988", "hunter2", "drone shot"):
        assert secret not in raw, secret


def test_paths_survive_redaction(tmp_path, monkeypatch):
    """An id in the environment is often a directory name too.

    Scrubbing every long environment value rewrote paths like this one (a
    session id that is part of the scratch directory) before the rule was
    narrowed to secret-named variables.
    """
    session = "f77d87d0-2744-461e-bfcb-88291f494d99"
    folder = tmp_path / session / "proj"
    folder.mkdir(parents=True)
    project = folder / "sample.fcpxml"
    shutil.copy(SAMPLE, project)
    monkeypatch.setenv("FCP_TEST_SESSION_ID", session)
    monkeypatch.setenv("FCP_TEST_PROJECTS", str(folder))
    text = _call(*_marker(project, expected_sha256=WRONG))
    span = _last_span()
    assert span["args"]["args"]["filepath"] == str(project)
    assert span["status"]["message"] == text.splitlines()[0] and session in text


def test_transcript_pack_reply_is_recorded_as_length_and_hash(project, monkeypatch):
    async def pack(args):
        return server._text_result("# Transcript Pack\n\nevery word the shoot said")

    monkeypatch.setitem(server.TOOL_HANDLERS, "transcript_pack", pack)
    reply = _call("transcript", {"action": "transcript_pack", "args": {"filepath": str(project)}})
    assert _last_span()["result"] == {
        "_redacted": "transcript", "length": len(reply.encode()),
        "sha256": hashlib.sha256(reply.encode()).hexdigest(),
    }


def test_a_reply_that_mentions_fcpxml_is_not_mistaken_for_one(project, monkeypatch):
    async def mention(args):
        return server._text_result("Root element is <fcpxml version='1.13'>, as expected.")

    monkeypatch.setitem(server.TOOL_HANDLERS, "list_clips", mention)
    text = _call(*_list_clips(project))
    assert _last_span()["result"] == text


def test_long_fields_are_cut_with_length_and_hash(project, monkeypatch):
    monkeypatch.setenv("FCP_MCP_TRACE_FIELD_LIMIT", "300")
    long_reply = "é" * 3000

    async def verbose(args):
        return server._text_result(long_reply)

    monkeypatch.setitem(server.TOOL_HANDLERS, "list_clips", verbose)
    assert _call("inspect", {"action": "list_clips", "args": {
        "filepath": str(project), "note": "n" * 1000}}) == long_reply
    span = _last_span()
    data = long_reply.encode()
    result = span["result"]
    assert result["_truncated"] is True and result["truncated"] is True
    assert result["length"] == len(data) and result["sha256"] == hashlib.sha256(data).hexdigest()
    assert long_reply.startswith(result["preview"]) and result["preview"]
    assert len(trace._encode(result)) <= 300
    # A field still over the limit once its long strings are cut becomes one
    # marker whose preview is the JSON text: the Logic MCP's rule.
    args = span["args"]
    assert args["_truncated"] is True and args["preview"].startswith('{"action":"list_clips"')
    assert len(trace._encode(args)) <= 300
    assert all(len(line.encode()) < 2000 for line in trace._state["run"].file().read_text().splitlines())


def test_a_full_file_continues_in_the_next(project, monkeypatch):
    async def reply(args):
        return server._text_result("x" * 1000)

    monkeypatch.setitem(server.TOOL_HANDLERS, "list_clips", reply)
    monkeypatch.setattr(trace, "MAX_FILE_BYTES", 6000)
    for _ in range(8):
        _call(*_list_clips(project))
    run = trace._state["run"]
    parts = [run.file(n) for n in range(1, run.part + 1)]
    assert len(parts) >= 3 and all(p.is_file() for p in parts)
    for older, newer in zip(parts, parts[1:]):
        last = json.loads(older.read_text().splitlines()[-1])
        first = json.loads(newer.read_text().splitlines()[0])
        assert (last["type"], last["name"], last["next_file"]) == ("event", "continued", newer.name)
        assert (first["name"], first["previous_file"]) == ("continuation", older.name)
        assert older.stat().st_size <= 6000 + len(older.read_text().splitlines()[-1]) + 1
    assert [s["seq"] for s in trace.read_run(parts[0]) if s["type"] == "span"] == list(range(1, 9))
    assert len(_viewer().Run(parts[0]).spans) == 8


# -- runs, intents and the trace group ---------------------------------------------------

def test_mutating_table_covers_every_action():
    actions = {f"{group}.{action}" for group, spec in server.TOOL_GROUPS.items()
               for action in spec["actions"]}
    missing = sorted(actions - set(trace.MUTATING))
    assert not missing, f"classify these in fcpxml.trace.MUTATING: {missing}"
    stale = sorted(set(trace.MUTATING) - actions)
    assert not stale, f"no longer in TOOL_GROUPS: {stale}"
    assert all(trace.MUTATING[f"deliver.{a}"] for a in ("push_to_fcp", "export_fcp7_xml"))
    assert not any(v for k, v in trace.MUTATING.items() if k.startswith(("inspect.", "trace.")))


def test_begin_run_supersedes_the_implicit_run_and_owns_its_span(project):
    _call(*_list_clips(project))
    implicit = trace._state["run"]
    text = _call("trace", {"action": "begin_run", "args": {
        "goal": "tighten the intro", "project_path": str(project)}})
    explicit = trace._state["run"]
    assert explicit is not implicit and explicit.label == "explicit"
    assert f"Closed the previous implicit run {implicit.run_id} (superseded)" in text
    old = trace.read_run(implicit.file(1))
    assert (old[0]["type"], old[0]["label"]) == ("run_start", "implicit")
    assert (old[-1]["type"], old[-1]["reason"], old[-1]["span_count"]) == ("run_end", "superseded", 1)
    new = trace.read_run(explicit.file(1))
    assert (new[0]["label"], new[0]["goal"], new[0]["project_path"]) == (
        "explicit", "tighten the intro", str(project))
    assert [(r["type"], r["seq"], r["name"]) for r in new[1:3]] == [
        ("span_start", 1, "trace.begin_run"), ("span", 1, "trace.begin_run")]
    assert new[2]["mutating"] is False and new[2]["parent_span_id"] == new[0]["span_id"]
    assert "run_end" not in [r["type"] for r in new]
    # A run starts with the call that opened it: no span begins before its run.
    assert old[0]["start_ns"] == old[1]["start_ns"]
    assert new[0]["start_ns"] == new[1]["start_ns"]


def test_a_second_begin_run_supersedes_an_explicit_one_too(project):
    _call("trace", {"action": "begin_run", "args": {"goal": "first"}})
    first = trace._state["run"]
    _call("trace", {"action": "begin_run", "args": {"goal": "second"}})
    assert trace.read_run(first.file(1))[-1]["reason"] == "superseded"
    assert trace._state["run"].goal == "second"


def test_log_intent_attaches_to_the_next_mutating_call_not_the_read_before_it(project):
    _call("trace", {"action": "log_intent", "args": {
        "step": "1. mark the verse", "expected_effect": "one marker at 1s"}})
    _call("inspect", {"action": "list_markers", "args": {"filepath": str(project)}})
    _call(*_marker(project, name="verse"))
    _call(*_marker(project, name="chorus"))
    records = _records()
    [intent] = [r for r in records if r["type"] == "intent"]
    assert intent["span_id"] == intent["intent"]["intent_id"]
    spans = [r for r in records if r["type"] == "span"]
    read, verse, chorus = spans[1], spans[2], spans[3]
    assert read["name"] == "inspect.list_markers" and "intent" not in read
    assert verse["intent_span_id"] == intent["span_id"]
    assert verse["intent"] == intent["intent"]
    assert verse["intent"]["step"] == "1. mark the verse"
    [verse_start] = [r for r in records if r["type"] == "span_start" and r["span_id"] == verse["span_id"]]
    assert verse_start["intent_span_id"] == intent["span_id"]
    assert "intent_span_id" not in chorus


def test_an_intent_no_call_used_is_reported(project):
    _call("trace", {"action": "log_intent", "args": {"step": "a"}})
    text = _call("trace", {"action": "log_intent", "args": {"step": "b"}})
    assert "replaces an earlier intent" in text
    _call("trace", {"action": "end_run", "args": {}})
    events = [r["name"] for r in trace.read_run(trace.run_files()[0]) if r["type"] == "event"]
    assert events == ["intent_superseded", "intent_unused"]


def test_end_run_records_itself_then_closes_and_the_next_call_opens_a_new_run(project):
    _call(*_list_clips(project))
    first = trace._state["run"]
    text = _call("trace", {"action": "end_run", "args": {"outcome": "intro tightened"}})
    assert f"Run {first.run_id} ended: 2 calls, 0 errors." in text
    records = trace.read_run(first.file(1))
    assert [r["type"] for r in records[-3:]] == ["span_start", "span", "run_end"]
    assert records[-2]["name"] == "trace.end_run"
    assert records[-1]["outcome"] == "intro tightened"
    assert trace._state["run"] is None
    _call(*_list_clips(project))
    assert trace._state["run"].run_id != first.run_id


def test_an_idle_implicit_run_closes_and_a_moved_root_starts_over(project, monkeypatch, tmp_path):
    _call(*_list_clips(project))
    first = trace._state["run"]
    monkeypatch.setattr(trace, "IDLE_SECONDS", -1)
    _call(*_list_clips(project))
    second = trace._state["run"]
    assert trace.read_run(first.file(1))[-1]["reason"] == "idle_timeout"
    monkeypatch.setattr(trace, "IDLE_SECONDS", 1800)
    monkeypatch.setenv("FCP_MCP_TRACE_DIR", str(tmp_path / "elsewhere"))
    _call(*_list_clips(project))
    assert trace.read_run(second.file(1))[-1]["reason"] == "relocated"
    assert trace._state["run"].root == tmp_path / "elsewhere"


def test_status_and_show_read_the_open_run(project):
    _call("trace", {"action": "begin_run", "args": {"goal": "check the cut"}})
    _call(*_list_clips(project))
    _call("trace", {"action": "log_intent", "args": {"step": "add a marker"}})
    _call(*_marker(project))
    run = trace._state["run"]
    status = _call("trace", {"action": "status"})
    assert f"Run: {run.run_id} (explicit, goal: check the cut)" in status
    assert "Calls: 5 (0 errors)" in status and "Trace write failures: 0" in status
    shown = _call("trace", {"action": "show"})
    assert f"**Run {run.run_id}** (explicit): 6 calls, 0 errors, open" in shown
    assert "| 4 | `mark.add_marker` | ok |" in shown and "add a marker" in shown
    assert "| 6 | `trace.show` | (this call) |" in shown
    by_prefix = _call("trace", {"action": "show", "args": {"run_id": run.run_id[:20]}})
    assert run.run_id in by_prefix
    assert _call("trace", {"action": "show", "args": {"run_id": "1999"}}).startswith("File not found:")


def test_trace_actions_refuse_bad_arguments(project):
    assert _call("trace", {"action": "begin_run", "args": {}}).startswith(
        "Missing required argument: goal")
    assert _call("trace", {"action": "begin_run", "args": {"goal": "  "}}).startswith(
        "Validation error: goal must be a non-empty string")
    assert _call("trace", {"action": "log_intent", "args": {}}).startswith("Validation error:")


def test_instructions_mention_tracing_only_when_it_is_on():
    code = "import server; print(repr(server.server.create_initialization_options().instructions))"
    env = {k: v for k, v in os.environ.items() if k != "FCP_MCP_TRACE"}
    on = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                        cwd=str(REPO), env=env)
    off = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         cwd=str(REPO), env={**env, "FCP_MCP_TRACE": "off"})
    assert on.stdout.strip() == repr(trace.INSTRUCTIONS)
    assert off.stdout.strip() == "None"
    assert "trace begin_run {goal}" in trace.INSTRUCTIONS and "never required" in trace.INSTRUCTIONS


# -- the viewer ------------------------------------------------------------------------------

def _example_run(project, monkeypatch):
    """An implicit read, begin_run superseding it, an intent, a write, an unexpected error."""
    _call(*_list_clips(project))
    _call("trace", {"action": "begin_run", "args": {"goal": "Mark the verse, then split clip 2"}})
    _call("trace", {"action": "log_intent", "args": {
        "step": "1. mark the verse", "expected_effect": "one marker named Verse at 00:00:01:00"}})
    _call(*_marker(project, name="Verse"))
    _boom_on(monkeypatch, "split_clip", RuntimeError("split point 9999s is past the clip"))
    reply = _call("edit", {"action": "split_clip", "args": {
        "filepath": str(project), "clip_name": "Interview", "split_at": "9999"}})
    _call("trace", {"action": "end_run", "args": {"outcome": "marker added; split failed"}})
    return reply


def test_example_trace(project, monkeypatch, traces):
    """The run docs/TRACE.md shows."""
    reply = _example_run(project, monkeypatch)
    explicit, implicit = trace.run_files()
    records = trace.read_run(explicit)
    assert [r["type"] for r in records] == [
        "run_start", "span_start", "span", "span_start", "intent", "span", "span_start", "span",
        "span_start", "span", "span_start", "span", "run_end"]
    split = records[9]
    assert reply == f"Error: RuntimeError (trace {records[0]['run_id']}:{split['seq']})"
    assert records[-1]["error_count"] == 1 and records[-1]["status"] == {"code": "ERROR"}
    assert trace.read_run(implicit)[-1]["reason"] == "superseded"


def test_the_viewer_reads_a_trace_written_here(project, monkeypatch, traces):
    _example_run(project, monkeypatch)
    run_id = trace.run_files()[0].name[:-len(".jsonl")]

    def cli(*args):
        out = subprocess.run([sys.executable, "-I", str(VIEWER), *args, "--dir", str(traces)],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        return out.stdout

    listing = cli("list")
    assert run_id in listing and "end_run" in listing and "superseded" in listing
    assert run_id in cli("list", "--date", trace.run_files()[0].parent.name)
    assert "no traces" in cli("list", "--date", "1999-01-01")
    shown = cli("show", "last")
    assert f"Run {run_id}  (explicit)" in shown
    assert "#3 mark.add_marker  [mutating]" in shown
    assert "intent: 1. mark the verse -> expect: one marker named Verse at 00:00:01:00" in shown
    assert "#4 edit.split_clip  [mutating]" in shown and "ERROR error" in shown
    assert "error: RuntimeError: split point 9999s is past the clip" in shown
    assert re.search(r"at: tests/test_trace\.py:\d+ in boom", shown)
    assert "output: " in shown and "reason end_run" in shown
    verbose = cli("show", run_id[:22], "-v")
    assert "traceback:" in verbose and "args: " in verbose
    report = Path(cli("export", run_id, "--md").strip())
    assert report == trace.run_files()[0].with_name(f"{run_id}.md")
    assert report.stat().st_mode & 0o777 == 0o600
    md = report.read_text()
    assert md.startswith(f"# Trace {run_id}") and "| 4 | `edit.split_clip` |" in md
    assert "Error: `RuntimeError`" in md
    assert cli("export", run_id, "--md", "--out", "-").startswith(f"# Trace {run_id}")
