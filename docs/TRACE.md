# Run tracing

A structured, local record of every tool call the server handles, so the
sequence of actions an agent took can be read back afterwards and a failure
read in place instead of reconstructed. The format is the Logic Pro MCP's
(schema 1), so one viewer reads both servers' traces.

This is a fork addition (see `FORK.md`). Base: upstream `6733d29`, v0.27.2.

## What was missing

Confirmed in the code at the base commit:

- **The journal records writes only.** `fcpxml/journal.py` appends a row per
  output a call actually wrote (tool, action, args, input path and hash,
  output path and hash). Reads, timing and failures leave nothing.
- **Refusals leave no record.** `_journaled` returns the `_project_guard`
  (`expected_sha256`) and `_review_gate` refusals before `_journal.begin`.
- **Unexpected errors lose everything but the class name.** The last
  `except Exception as e:` in `call_tool` returns `Error: <ExceptionType>`.
  The message and traceback are gone.
- **Nothing configures logging.** Several `fcpxml.*` modules call
  `logging.getLogger(__name__)`. With no handler anywhere, WARNING and above
  reach stderr through `logging.lastResort`; INFO and DEBUG are dropped
  before a record exists.
- **Subprocess stderr mostly vanishes.** It survives only where a caller folds
  it into a message: `render_proxy` keeps the last three lines, `dtd` returns
  it, `live` puts it in a `RuntimeError`, `writer` in a `RuntimeError`.
  `scenes`, `media_intel`, `visual` and `gen.probe_media` discard it.

Also found, and worth knowing:

- The server sends **no MCP instructions** at all (`Server(...)` has no
  `instructions=`), so the trace line below is the first.
- The journal makes its **directories** mode 700 but creates its files with
  the process umask (normally 644). Trace files are created 600 explicitly.
- `_journal.begin` hashes the input file on every call, reads included. The
  trace takes that hash from the open ledger instead of hashing again.

## Behaviour

- **On by default, local only.** `FCP_MCP_TRACE=off` (also `0`, `false`,
  `no`) disables it: no file, no directory, and every result byte-identical
  to an untraced call.
- `FCP_MCP_TRACE_DIR` moves the root (default `~/.fcp-mcp/traces/`).
  `FCP_MCP_TRACE_FIELD_LIMIT` changes the per-field byte limit (default 8192,
  minimum 256), as `LOGIC_MCP_TRACE_FIELD_LIMIT` does.
- Directories are mode 700 (and `~/.fcp-mcp` itself when the default root is
  used, as the journal does). Files are created 600.
- **Tracing never breaks a call.** Every entry point catches its own
  exceptions; a failed write is counted and shown by `trace status`. The text
  returned to the model is unchanged, with one exception: when a call ends in
  an unexpected error (the last `except` in dispatch), ` (trace <run_id>:<seq>)`
  is appended, so `Error: RuntimeError` becomes
  `Error: RuntimeError (trace 20261025T201530Z-a1b2c3:7)`.
- **Nothing leaves the machine.** There is no network code in the module.
- The server's MCP instructions gain one line while tracing is on (none is
  sent when it is off): *Calls are traced locally for debugging; optionally
  call trace begin_run {goal} at the start of a task and trace log_intent
  {step, expected_effect} before a mutating call (never required).*

## Where it hooks in

All the logic is in `fcpxml/trace.py`. The hooks in existing code:

| Where | What |
|---|---|
| `server.call_tool` | Now a short wrapper: `start_span`, then upstream's dispatch (renamed `_call_tool`, body unchanged apart from the hooks below), then `end_span`; an exception `_call_tool` does not catch (cancellation) is recorded and re-raised. |
| `server.Server(...)` | `instructions=` the trace line while tracing is on. |
| `_trace.bind(...)` | One line before `call_tool`: the version and the live `TOOL_GROUPS`. |
| `_call_tool` | One `_trace.outcome(...)` per early return and `except` branch, given the exception. The last branch appends `_trace.ref()`. |
| `handle_group` | One `_trace.outcome(...)` before each early return. |
| `_journaled` | Records the gate facts, splits `guard or gate` into two checks so the refusal is attributed, takes the input hash from the open ledger, the written paths in the `finally`, their hashes from `_written_hashes`, and whether autopush ran. |
| 11 subprocess call sites | `subprocess.run(` becomes `traced_run(`: same arguments, same return value, same exceptions. |
| `logging.getLogger("fcpxml")` | One handler, added on the first traced call. |

The helper calls `subprocess.run` through the module attribute at call time,
so every test that patches `subprocess.run` (on `render.subprocess`,
`media_intel.subprocess` or `fcpxml.writer.subprocess.run`) still patches the
call. The ffmpeg, ffprobe, osascript and xmllint call sites go through it;
`live.fcp_is_running` (pgrep) and `tools/preview._open_beside_terminal` (node)
do not, and `tests/test_trace.py` fails if any other `subprocess.run(` appears.

The log handler does not touch the root logger or any level. Because a handler
on `fcpxml` would otherwise stop `logging.lastResort` from printing
`fcpxml.*` warnings, the handler passes a record to `lastResort` itself in
exactly the case Python would have: no other handler on the record's path and
a level at or above `lastResort`'s. stderr is unchanged; a test runs both
configurations in a subprocess and compares stderr byte for byte. The cost of
not touching levels: only what the logger already emits is captured, which is
WARNING and up unless the host configures logging.

## Files

```
<root>/<YYYY-MM-DD>/<run_id>.jsonl      one JSON object per line
<root>/<YYYY-MM-DD>/<run_id>.2.jsonl    continuation, past 50 MB
<root>/<YYYY-MM-DD>/<run_id>.md         optional report (scripts/fcp_trace.py export)
<root>/<YYYY-MM-DD>/<run_id>/           optional artifacts folder (this server writes none)
```

`run_id` is `YYYYMMDDTHHMMSSZ-<6 hex>` (UTC). The date folder is the LOCAL
calendar day of the run's start, as in the Logic MCP. Lines are compact JSON
with sorted keys.

## Records

Every line carries `schema: 1`, `run_id`, `trace_id` (32 hex), `span_id`
(16 hex), `ts` (ISO 8601 UTC with milliseconds, `2026-10-25T20:15:30.123Z`)
and `type`.

### `run_start`

`label` (`"implicit"` for a run the server opened itself, `"explicit"` after
`trace begin_run`), `goal` (`""` when implicit), `project_path` (when given),
`server {name: "fcp-mcp-server", version, pid, git_sha}`, `config`,
`start_time`, `start_ns`. `span_id` is the run's own span, the parent of every
call. `git_sha` is read from the checkout's `.git` without running git, and
is `null` for an installed wheel. `config` holds the options in effect:
`field_limit`, `max_file_bytes`, `idle_timeout_s`, `stderr_tail`,
`span_list_cap`, `log_level` (the `fcpxml` logger's effective level), and
`redact_keys`.

### `span_start`

Written when a call starts: `seq`, `tool` (the group, e.g. `edit`), `command`
(the action), `name` (`edit.set_keyframes`), `mutating`, `parent_span_id`,
`start_time`, `start_ns`, `intent` and `intent_span_id` when an intent
attached, and `called_as` when the call used a legacy flat tool name
(`add_marker` is recorded as `mark.add_marker`, `called_as: "add_marker"`).
An unknown tool keeps the name it was called by as `tool`, with no
`command`.

### `span`

The completed call: everything in `span_start`, plus

| Field | |
|---|---|
| `args` | The arguments as the client sent them, after redaction. |
| `result` | The text returned, kept up to the field limit. |
| `status` | `{code: "OK"}` when `outcome` is `ok`, else `{code: "ERROR", message}`. The message is the exception (`Type: message`) or the first line of the reply. |
| `outcome` | How dispatch ended; see the table below. |
| `duration_ms`, `end_time`, `end_ns` | Duration from a monotonic clock; `end_ns = start_ns + duration`. |
| `error` | For any exception: `type`, `message`, `traceback`, and `frame` (`fcpxml/writer.py:123 in split_clip`, the innermost frame). Captured in the `except` branch, before dispatch collapses it. |
| `input` | `{path, sha256}`: the `filepath` argument and the hash the journal ledger computed for it. |
| `outputs` | `[{path, sha256?}]`: every path the request wrote (what `_journal.finish` returns, recorded even when the handler then raised). `sha256` is present for `.fcpxml`/`.fcpxmld` outputs, taken from `_written_hashes`, the hash the reply already reports. Other outputs (renders, CSV, SRT) carry the path only: the journal row for the same path holds their hash, and hashing them again here is the cost the design avoids. The journal has no row identifier, so there is no `journal_entry`; input path plus output path correlate a span with its journal rows. |
| `subprocess` | `[{argv0, returncode, stderr_tail, duration_ms, error?}]` for each ffmpeg, ffprobe, osascript and xmllint run during the call. `stderr_tail` is the last 2000 characters. `returncode` is `null` and `error` names the exception when the process did not finish (`TimeoutExpired`, `FileNotFoundError`). At most 100 per span; past that `subprocess_dropped` counts the rest. |
| `logs` | `[{level, logger, message}]` from `fcpxml.*` loggers during the call. Same cap, `logs_dropped`. |
| `gated` | The action is behind the review gate (`GATED_ACTIONS`). |
| `confirm_unreviewed` | The call passed `confirm_unreviewed=true`. |
| `autopushed` | Autopush ran for something this call wrote (`FCP_MCP_AUTOPUSH`). Whether the push itself worked is in the reply text and the osascript entry. |

`gated`, `confirm_unreviewed` and `autopushed` appear once the call reaches
`_journaled`; a call refused earlier (unknown action) has none of them.

#### `outcome`

Each early return and `except` branch in dispatch maps to exactly one value:

| Branch | `outcome` |
|---|---|
| handler returned | `ok` |
| `_journaled`: `_project_guard` refused | `refused_guard` |
| `_journaled`: `_review_gate` refused | `refused_gate` |
| `call_tool`: unknown tool name | `unknown_action` |
| `handle_group`: unknown group, missing or unknown action, action with no handler | `unknown_action` |
| `handle_group`: `args` is not an object | `validation_error` |
| `except _NoTimelineError` | `no_timeline` |
| `except FileNotFoundError` | `not_found` |
| `except ValueError` | `validation_error` |
| `except KeyError` | `missing_argument` |
| `except Exception` | `error` (and the trace suffix) |
| cancelled (an exception `call_tool` does not catch) | `error`, then re-raised |

The mapping is by branch, not by reply text. A handler that answers
`No timelines found` itself (two in `server.py`, and `tools/preview`,
`tools/view`, `tools/gen`) is recorded as `ok`; only `_require_timeline`
raises.

### `intent`

Written by `trace log_intent`: `intent {intent_id, step, expected_effect,
ts}`. Its `span_id` is the `intent_id`. The intent attaches to the next
**mutating** call in the run (reads in between, such as checking state before
acting, do not take it), which carries the same object as `intent` and the id
as `intent_span_id`. This is the Logic MCP's current rule; its traces from
before that rule show intents landing on the read just before the change.

### `event`

`name` is one of `intent_superseded` (a second `log_intent` replaced one no
call used), `intent_unused` (the run ended with an intent pending),
`continued` (last line of a full file, with `next_file`) and `continuation`
(first line of the next file, with `part` and `previous_file`).

### `run_end`

`reason`, `outcome` (the agent's `end_run {outcome}` text, when given),
`span_count`, `error_count` (spans with status `ERROR`), `status` (`ERROR`
when `error_count > 0`), `duration_ms`, `end_time`, `end_ns`. Reasons:

| `reason` | When |
|---|---|
| `end_run` | `trace end_run` was called. |
| `superseded` | `trace begin_run` replaced the open run (implicit or not). |
| `idle_timeout` | An implicit run had no call for 30 minutes; the next call opens a new one. |
| `server_exit` | The interpreter exited normally (`atexit`). A killed server leaves no `run_end`. |
| `relocated` | `FCP_MCP_TRACE_DIR` changed under a running process (the test suite does this per test). |

## Size and privacy

- **Field limit.** A string over `field_limit` bytes (UTF-8), or a whole field
  whose JSON is over it, becomes `{_truncated: true, truncated: true, length,
  sha256, preview}` where `length` and `sha256` describe the full value and
  `preview` is its start, cut so the marker itself fits the limit. `result`,
  `args`, `goal`, intents, `error.message` and `error.traceback`,
  `status.message` and log messages are each limited.
- **Secret keys.** An argument whose key contains the word `key`, `token`,
  `secret`, `password` or `api` (split on `_`, `-` and camelCase, so
  `api_key`, `xi-api-key` and `accessToken` match and `keywords` and
  `keyframes` do not) and whose value is a string is recorded as
  `{_redacted: "secret"}`. Plain substring matching would have erased
  `keywords` and `keyframes`, which are among the most common arguments.
- **Environment values.** No environment block is recorded. Any string the
  trace writes (arguments, result, error text, traceback, stderr tail, log
  message) has the value of every environment variable whose NAME has one of
  the words above (`FAL_KEY`, `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`, ...)
  replaced by `[redacted:<NAME>]`. Values under 8 characters are never
  replaced, so a variable set to `1` does not erase every `1`. Other
  variables are not scrubbed: a first version also replaced every long
  non-path value, and it rewrote real paths, because ids in the environment
  are directory names too (a session id inside a scratch path).
  `test_paths_survive_redaction` keeps that case.
- **Content.** A `transcript` argument, any string that is an FCPXML document
  and the reply of `transcript_pack` (the transcript itself) are recorded as
  `{_redacted: "transcript" | "fcpxml", length, sha256}`. Other replies can
  quote a few words (`transcribe_media` previews 160 characters, `view_clip`
  prints a clip's words) and are cut at the field limit like any reply.
- **50 MB per file.** Past that the run continues in `<run_id>.2.jsonl`; the
  old file's last line and the new file's first say so.

## The `trace` group

Registered through `tools/` like every other group (`tools/trace.py`).

| Action | Args | |
|---|---|---|
| `begin_run` | `goal`, `project_path?` | Ends the open run (`superseded`) and starts an explicit one. The `begin_run` call is the new run's first span. |
| `log_intent` | `step`, `expected_effect` (one is enough) | Attaches to the next mutating call. Never required. |
| `end_run` | `outcome?` | Ends the run after recording the `end_run` call itself. The next call opens a new implicit run. |
| `status` | | Root, open run, its file, call and error counts, pending intent, trace write failures. |
| `show` | `run_id?` | A run id, a unique prefix, or `last` (the default): a compact table of seq, call, outcome, duration and intent. |

Calls to the group are spans like any other, with `mutating: false`. With
tracing off they answer that nothing is recorded.

### `mutating`

From one table, `fcpxml.trace.MUTATING`, keyed by `group.action`. A call is
mutating when the action exists to change something: an edited, generated or
exported file, files moved by `undo`, Final Cut Pro itself, or money spent.
Analysis that leaves a cache or sidecar behind (transcripts, beats JSON,
previews, index rows) is a read; `outputs` lists those files whatever the
table says. `tests/test_trace.py` fails when an action in `TOOL_GROUPS` is
missing from the table or the table names one that no longer exists, so an
action upstream adds is a visible failure, not a silent `false`.

## Viewer

`scripts/fcp_trace.py`, standard library only. `--dir` defaults to
`FCP_MCP_TRACE_DIR`, else `~/.fcp-mcp/traces`. It also reads Logic MCP traces
(`--dir ~/.logic-mcp/traces`).

```
python scripts/fcp_trace.py list [--date YYYY-MM-DD]
python scripts/fcp_trace.py show <run_id|prefix|last> [-v]
python scripts/fcp_trace.py export <run_id|prefix|last> --md [--out FILE|-]
```

`show` prints the span tree: intent, outcome, duration, input, outputs, and
for an error its message and innermost frame. `-v` adds arguments, the
reply, subprocess stderr tails, log lines and the full traceback. `export
--md` writes `<run_id>.md` next to the trace (or `--out`, `-` for stdout).

## Compatibility with the Logic MCP

Where the fork prompt and the Logic MCP's real traces
(`~/.logic-mcp/traces/`, `Sources/LogicProMCP/Trace/Tracer.swift`) disagree,
the records follow the real ones, because one viewer reading both is the
point:

| Prompt said | Logic writes | This writes |
|---|---|---|
| `reason` `"ended"`, `"shutdown"` | `"end_run"`, `"server_exit"` (and `"idle_timeout"`) | Logic's values, plus `relocated` |
| `"truncated": true` | `"_truncated": true` with `length`, `sha256`, `preview` | both keys |
| intent attaches through `intent_span_id` | the span embeds `intent {intent_id, ...}` | both |
| every line has `span_id` | `intent` and `event` lines have none | every line has one |
| intent attaches to the next span | next mutating span | next mutating span |
| trace calls are spans | `begin_run`, `log_intent`, `end_run`, `status` are not | they are (the prompt's rule) |

Fields the Logic MCP writes and this does not: `pathway`, `via`, `verified`,
`call_ms`, `overhead_ms`, `state`, `screenshots`, `result_images`. Fields
this adds: `outcome`, `error.frame`, `input`, `outputs`, `subprocess`,
`logs`, `gated`, `confirm_unreviewed`, `autopushed`, `called_as`,
`server.git_sha`.

## Example

A trace written by `tests/test_trace.py::test_example_trace`: an implicit
read, `begin_run` superseding it, an intent, a write, and an unexpected
error. Two files: the implicit run the first read opened, closed as
`superseded`, and the explicit run. Paths under pytest's temp directory are
written `<tmp>`, values over 90 characters are cut with `...`, and the
traceback keeps its first and last line; everything else is as written.

```jsonl
# 2026-10-25/20261026T013650Z-584502.jsonl
{"config":{"field_limit":8192,"idle_timeout_s":1800,"log_level":"WARNING","max_file_bytes":52428800,"redact_keys":"key|token|secret|password|api","span_list_cap":100,"stderr_tail":2000},"goal":"","label":"implicit","run_id":"20261026T013650Z-584502","schema":1,"server":{"git_sha":"6733d2939540db868510d11d8dd510ef9859ced1","name":"fcp-mcp-server","pid":85746,"version":"0.27.2"},"span_id":"082e38ea4cf56eb2","start_ns":1792978610779972000,"start_time":"2026-10-26T01:36:50.779Z","trace_id":"735aa4d0aed420a7462ea396e32f6333","ts":"2026-10-26T01:36:50.779Z","type":"run_start"}
{"command":"list_clips","mutating":false,"name":"inspect.list_clips","parent_span_id":"082e38ea4cf56eb2","run_id":"20261026T013650Z-584502","schema":1,"seq":1,"span_id":"d5cde81965f89117","start_ns":1792978610779972000,"start_time":"2026-10-26T01:36:50.779Z","tool":"inspect","trace_id":"735aa4d0aed420a7462ea396e32f6333","ts":"2026-10-26T01:36:50.779Z","type":"span_start"}
{"args":{"action":"list_clips","args":{"filepath":"<tmp>/proj/sample.fcpxml"}},"autopushed":false,"command":"list_clips","confirm_unreviewed":false,"duration_ms":1.2,"end_ns":1792978610781189541,"end_time":"2026-10-26T01:36:50.781Z","gated":false,"input":{"path":"<tmp>/proj/sample.fcpxml","sha256":"0977db7367112c0a779561f0f275731ff003559d42ae2e18aed59f0aab1e1303"},"mutating":false,"name":"inspect.list_clips","outcome":"ok","outputs":[],"parent_span_id":"082e38ea4cf56eb2","result":"# Clips in Music Video Edit\n\n| # | Name | Start | Duration | Keywords |\n|---|---...","run_id":"20261026T013650Z-584502","schema":1,"seq":1,"span_id":"d5cde81965f89117","start_ns":1792978610779972000,"start_time":"2026-10-26T01:36:50.779Z","status":{"code":"OK"},"tool":"inspect","trace_id":"735aa4d0aed420a7462ea396e32f6333","ts":"2026-10-26T01:36:50.781Z","type":"span"}
{"duration_ms":1.6,"end_ns":1792978610781528000,"end_time":"2026-10-26T01:36:50.781Z","error_count":0,"reason":"superseded","run_id":"20261026T013650Z-584502","schema":1,"span_count":1,"span_id":"082e38ea4cf56eb2","status":{"code":"OK"},"trace_id":"735aa4d0aed420a7462ea396e32f6333","ts":"2026-10-26T01:36:50.781Z","type":"run_end"}

# 2026-10-25/20261026T013650Z-b5c8ae.jsonl
{"config":{"field_limit":8192,"idle_timeout_s":1800,"log_level":"WARNING","max_file_bytes":52428800,"redact_keys":"key|token|secret|password|api","span_list_cap":100,"stderr_tail":2000},"goal":"Mark the verse, then split clip 2","label":"explicit","run_id":"20261026T013650Z-b5c8ae","schema":1,"server":{"git_sha":"6733d2939540db868510d11d8dd510ef9859ced1","name":"fcp-mcp-server","pid":85746,"version":"0.27.2"},"span_id":"3ce0542836d7c071","start_ns":1792978610781436000,"start_time":"2026-10-26T01:36:50.781Z","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.781Z","type":"run_start"}
{"command":"begin_run","mutating":false,"name":"trace.begin_run","parent_span_id":"3ce0542836d7c071","run_id":"20261026T013650Z-b5c8ae","schema":1,"seq":1,"span_id":"9794f4d7ed9a177e","start_ns":1792978610781436000,"start_time":"2026-10-26T01:36:50.781Z","tool":"trace","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.781Z","type":"span_start"}
{"args":{"action":"begin_run","args":{"goal":"Mark the verse, then split clip 2"}},"autopushed":false,"command":"begin_run","confirm_unreviewed":false,"duration_ms":0.3,"end_ns":1792978610781691499,"end_time":"2026-10-26T01:36:50.781Z","gated":false,"mutating":false,"name":"trace.begin_run","outcome":"ok","outputs":[],"parent_span_id":"3ce0542836d7c071","result":"Run 20261026T013650Z-b5c8ae started (explicit). Goal: Mark the verse, then split...","run_id":"20261026T013650Z-b5c8ae","schema":1,"seq":1,"span_id":"9794f4d7ed9a177e","start_ns":1792978610781436000,"start_time":"2026-10-26T01:36:50.781Z","status":{"code":"OK"},"tool":"trace","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.781Z","type":"span"}
{"command":"log_intent","mutating":false,"name":"trace.log_intent","parent_span_id":"3ce0542836d7c071","run_id":"20261026T013650Z-b5c8ae","schema":1,"seq":2,"span_id":"3982a09bd9302bab","start_ns":1792978610781965000,"start_time":"2026-10-26T01:36:50.781Z","tool":"trace","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.781Z","type":"span_start"}
{"intent":{"expected_effect":"one marker named Verse at 00:00:01:00","intent_id":"8ea94251c35741bc","step":"1. mark the verse","ts":"2026-10-26T01:36:50.782Z"},"run_id":"20261026T013650Z-b5c8ae","schema":1,"span_id":"8ea94251c35741bc","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.782Z","type":"intent"}
{"args":{"action":"log_intent","args":{"expected_effect":"one marker named Verse at 00:00:01:00","step":"1. mark the verse"}},"autopushed":false,"command":"log_intent","confirm_unreviewed":false,"duration_ms":0.2,"end_ns":1792978610782205708,"end_time":"2026-10-26T01:36:50.782Z","gated":false,"mutating":false,"name":"trace.log_intent","outcome":"ok","outputs":[],"parent_span_id":"3ce0542836d7c071","result":"Intent 8ea94251c35741bc recorded; it attaches to the next mutating call in run 2...","run_id":"20261026T013650Z-b5c8ae","schema":1,"seq":2,"span_id":"3982a09bd9302bab","start_ns":1792978610781965000,"start_time":"2026-10-26T01:36:50.781Z","status":{"code":"OK"},"tool":"trace","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.782Z","type":"span"}
{"command":"add_marker","intent":{"expected_effect":"one marker named Verse at 00:00:01:00","intent_id":"8ea94251c35741bc","step":"1. mark the verse","ts":"2026-10-26T01:36:50.782Z"},"intent_span_id":"8ea94251c35741bc","mutating":true,"name":"mark.add_marker","parent_span_id":"3ce0542836d7c071","run_id":"20261026T013650Z-b5c8ae","schema":1,"seq":3,"span_id":"f87174bf198486b4","start_ns":1792978610782398000,"start_time":"2026-10-26T01:36:50.782Z","tool":"mark","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.782Z","type":"span_start"}
{"args":{"action":"add_marker","args":{"filepath":"<tmp>/proj/sample.fcpxml","name":"Verse","timecode":"00:00:01:00"}},"autopushed":false,"command":"add_marker","confirm_unreviewed":false,"duration_ms":1.3,"end_ns":1792978610783747499,"end_time":"2026-10-26T01:36:50.783Z","gated":false,"input":{"path":"<tmp>/proj/sample.fcpxml","sha256":"0977db7367112c0a779561f0f275731ff003559d42ae2e18aed59f0aab1e1303"},"intent":{"expected_effect":"one marker named Verse at 00:00:01:00","intent_id":"8ea94251c35741bc","step":"1. mark the verse","ts":"2026-10-26T01:36:50.782Z"},"intent_span_id":"8ea94251c35741bc","mutating":true,"name":"mark.add_marker","outcome":"ok","outputs":[{"path":"<tmp>/proj/sample_modified.fcpxml","sha256":"c30cb4981d40499f5a8aa60cab5dc9d1ac1ef7b0f37bbc1b510a27561cb0d42c"}],"parent_span_id":"3ce0542836d7c071","result":"Added marker 'Verse' at 00:00:01:00\n\nSaved to: <tmp>/proj/sample_modified.fcpxml...","run_id":"20261026T013650Z-b5c8ae","schema":1,"seq":3,"span_id":"f87174bf198486b4","start_ns":1792978610782398000,"start_time":"2026-10-26T01:36:50.782Z","status":{"code":"OK"},"tool":"mark","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.783Z","type":"span"}
{"command":"split_clip","mutating":true,"name":"edit.split_clip","parent_span_id":"3ce0542836d7c071","run_id":"20261026T013650Z-b5c8ae","schema":1,"seq":4,"span_id":"2b2b54f4d8506e3d","start_ns":1792978610783945000,"start_time":"2026-10-26T01:36:50.783Z","tool":"edit","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.783Z","type":"span_start"}
{"args":{"action":"split_clip","args":{"clip_name":"Interview","filepath":"<tmp>/proj/sample.fcpxml","split_at":"9999"}},"autopushed":false,"command":"split_clip","confirm_unreviewed":false,"duration_ms":0.5,"end_ns":1792978610784407750,"end_time":"2026-10-26T01:36:50.784Z","error":{"frame":"tests/test_trace.py:87 in boom","message":"split point 9999s is past the clip","traceback":"Traceback (most recent call last):\\n...\\nRuntimeError: split point 9999s is past the clip","type":"RuntimeError"},"gated":false,"input":{"path":"<tmp>/proj/sample.fcpxml","sha256":"0977db7367112c0a779561f0f275731ff003559d42ae2e18aed59f0aab1e1303"},"mutating":true,"name":"edit.split_clip","outcome":"error","outputs":[],"parent_span_id":"3ce0542836d7c071","result":"Error: RuntimeError (trace 20261026T013650Z-b5c8ae:4)","run_id":"20261026T013650Z-b5c8ae","schema":1,"seq":4,"span_id":"2b2b54f4d8506e3d","start_ns":1792978610783945000,"start_time":"2026-10-26T01:36:50.783Z","status":{"code":"ERROR","message":"RuntimeError: split point 9999s is past the clip"},"tool":"edit","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.784Z","type":"span"}
{"command":"end_run","mutating":false,"name":"trace.end_run","parent_span_id":"3ce0542836d7c071","run_id":"20261026T013650Z-b5c8ae","schema":1,"seq":5,"span_id":"a3669fc2b0dee08c","start_ns":1792978610784658000,"start_time":"2026-10-26T01:36:50.784Z","tool":"trace","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.784Z","type":"span_start"}
{"args":{"action":"end_run","args":{"outcome":"marker added; split failed"}},"autopushed":false,"command":"end_run","confirm_unreviewed":false,"duration_ms":0.1,"end_ns":1792978610784803000,"end_time":"2026-10-26T01:36:50.784Z","gated":false,"mutating":false,"name":"trace.end_run","outcome":"ok","outputs":[],"parent_span_id":"3ce0542836d7c071","result":"Run 20261026T013650Z-b5c8ae ended: 5 calls, 1 error.\nFile: <tmp>/traces/2026-10-...","run_id":"20261026T013650Z-b5c8ae","schema":1,"seq":5,"span_id":"a3669fc2b0dee08c","start_ns":1792978610784658000,"start_time":"2026-10-26T01:36:50.784Z","status":{"code":"OK"},"tool":"trace","trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.784Z","type":"span"}
{"duration_ms":3.4,"end_ns":1792978610784854000,"end_time":"2026-10-26T01:36:50.784Z","error_count":1,"outcome":"marker added; split failed","reason":"end_run","run_id":"20261026T013650Z-b5c8ae","schema":1,"span_count":5,"span_id":"3ce0542836d7c071","status":{"code":"ERROR"},"trace_id":"c2294871fa3eeea93549279bf5711257","ts":"2026-10-26T01:36:50.784Z","type":"run_end"}
```

The same run through the viewer, `python scripts/fcp_trace.py show last`:

```
Run 20261026T013650Z-b5c8ae  (explicit)  trace_id c2294871fa3eeea93549279bf5711257
Goal: Mark the verse, then split clip 2
Server: fcp-mcp-server 0.27.2 (git 6733d29), pid 85746
Started 2026-10-25 21:36:50  ended 2026-10-25 21:36:50 (3 ms)  reason end_run
Outcome: marker added; split failed
Calls: 5 (1 error)
File: <tmp>/traces/2026-10-25/20261026T013650Z-b5c8ae.jsonl

run
├─ #1 trace.begin_run  [read]  0 ms  OK
├─ #2 trace.log_intent  [read]  0 ms  OK
├─ #3 mark.add_marker  [mutating]  1 ms  OK
│      intent: 1. mark the verse -> expect: one marker named Verse at 00:00:01:00
│      input: <tmp>/proj/sample.fcpxml (sha256 0977db736711)
│      output: <tmp>/proj/sample_modified.fcpxml (sha256 c30cb4981d40)
├─ #4 edit.split_clip  [mutating]  0 ms  ERROR error
│      input: <tmp>/proj/sample.fcpxml (sha256 0977db736711)
│      error: RuntimeError: split point 9999s is past the clip
│      at: tests/test_trace.py:87 in boom
└─ #5 trace.end_run  [read]  0 ms  OK
```
