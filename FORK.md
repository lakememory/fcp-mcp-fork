# This fork

A fork of [DareDev256/fcp-mcp-server](https://github.com/DareDev256/fcp-mcp-server)
that adds one thing: **local run tracing**, a structured record of every tool
call the server handles, in the Logic Pro MCP's trace format. Nothing else in
the server is fixed, refactored or restyled. `docs/TRACE.md` is the design and
the schema.

## Branch `perception` (2026-11-05)

Everything below this section describes the `trace` branch, which is unchanged.
`perception` is a second branch: upstream `v0.30.0` (`dec1ff2`), the two trace commits
cherry-picked onto it, then five commits that let a model judge a cut from what
plays rather than from clip counts. Local only, nothing pushed.

| Commit | What | Upstreamable |
|---|---|---|
| `fix(parser): read connected storylines, disabled clips and audition picks` | Final Cut writes a connected storyline as `<spine lane="N">`. The parser matched only `<storyline>`, so a film with its narration, music and effects in storylines read as 0 connected clips. Also reads `enabled="0"` and plays an `<audition>`'s first child. | Yes, as it stands. It touches no tracing code. |
| `feat(see): ...` | `fcpxml/coverage.py`, `checks.py`, `shadow.py`, `sheet.py`, `tools/see.py`: the `see` group (`shadow`, `contact_sheet`). Also `Timeline.gaps`, `time_map` on clips, and a sound file with no `hasVideo` attribute is no longer picture. | Yes, minus the `fcpxml/trace.py` and count lines. |
| `fix(checks): ...` | Inheritance of a hole from the base build. | With the one above. |
| `fix(see): a clip whose file is black is black` | `fcpxml/blackness.py` looks at three frames of each enabled full-frame clip's file (one for a shot that is not black; cached). A black movie laid over the cut used to count as picture, so V18a measured 15.2 s of black with 166.8 s on screen. Also: more than three narration lines over one black are one finding. Found by the first live test. | With the `see` commit. |
| `fix(see): a file instant sits on the file's frame grid` | `MediaRef.frame` (the asset format's `frameDuration`) and `Item.file_time` floors to it. A freeze written at 8.04 s of a 193-frame 24 fps file is inside its last frame, and a seek there found nothing: the contact sheet read "NO FRAME" on V19's Hickey freeze and the agent had to guess. Found by the second live test. | Yes, with the parser change. |

Upstream-owned files touched: `fcpxml/parser.py`, `fcpxml/models.py`,
`fcpxml/filtergraph.py`, `fcpxml/timeline_app.py`, `tools/__init__.py` (two lines),
`pyproject.toml` (the optional `see` extra: Pillow), and the counts in `server.json`,
`.claude-plugin/plugin.json`, `skill/SKILL.md` and `tests/test_tool_groups.py`
(17 groups, 109 operations). Everything else is new files.

Conflicts met when cherry-picking the trace commits onto `v0.30.0`: the version and
counts in `plugin.json` and `server.json`, and `fcpxml/render.py` (upstream rewrote
`render_proxy`; resolved by taking upstream's and routing its four new
`subprocess.run` sites through `traced_run`).

Known limits, stated in the tools' own output: picture is the top enabled full-frame
clip read from its source file. Opacity, crops, keyframed transforms and transitions
are not read. A clip is called black only when its file is black at three instants
inside the range it plays, so one that is black for part of its length is not caught;
a generator with no file is never looked at. `preview_render` does not read `<timeMap>` either, so a retimed clip in
a proxy render plays at source speed; the contact sheets do read it.

## Base

| | |
|---|---|
| Upstream commit | `6733d2939540db868510d11d8dd510ef9859ced1` (2026-10-24, "docs(README): current test count in the tree, drop the stale 62-handler figure") |
| Upstream version | `0.27.2` (`pyproject.toml`; the fork does not bump it) |
| Branch | `trace`, on top of upstream `main` |
| Remotes | `upstream` only. No `origin`, nothing pushed. |

## What the fork adds

- `fcpxml/trace.py`: the tracer. Spans, runs, redaction and truncation, the
  file layout, the `mutating` table, the subprocess helper `traced_run` and
  the `fcpxml.*` log handler.
- `tools/trace.py`: the `trace` tool group (`begin_run`, `log_intent`,
  `end_run`, `status`, `show`), registered through `tools/`.
- `scripts/fcp_trace.py`: the viewer (`list`, `show`, `export --md`),
  standard library only. It reads Logic MCP traces too.
- `tests/test_trace.py`: 57 tests.
- `docs/TRACE.md`, this file, and an `Unreleased (fork)` CHANGELOG entry.

Behaviour for everything else is unchanged: every reply is byte-identical
with tracing on or off, except that an unexpected error's reply gains
` (trace <run_id>:<seq>)`. `FCP_MCP_TRACE=off` turns the whole thing off.

## Existing files touched

Line counts are `git diff --numstat 6733d29` (added / removed).

| File | + | - | What |
|---|---|---|---|
| `server.py` | 46 | 4 | Import; `instructions=` on `Server(...)`; `_trace.bind(...)`; `call_tool` becomes a wrapper around the renamed `_call_tool`; one `_trace.outcome(...)` per early return and `except` branch in `_call_tool` and `handle_group`; in `_journaled` the gate facts, the guard and gate checks split in two (same order, same short circuit), the ledger input, the written outputs, their hashes, and `autopushed`. |
| `tools/__init__.py` | 2 | 0 | Import and `register_group("trace", ...)`. |
| `fcpxml/render.py` | 4 | 3 | Import; three call sites. |
| `fcpxml/writer.py` | 3 | 2 | Import; two call sites. |
| `fcpxml/dtd.py` | 3 | 2 | Import; one call site; `import subprocess` removed because nothing else in the file used it (ruff F401). |
| `fcpxml/live.py` | 3 | 1 | Import; `_run_osascript`. `fcp_is_running` (pgrep) is untouched. |
| `fcpxml/gen.py` | 2 | 1 | Import; `probe_media`. |
| `fcpxml/media_intel.py` | 3 | 1 | Import; one call site. |
| `fcpxml/scenes.py` | 3 | 1 | Import; one call site. |
| `fcpxml/visual.py` | 3 | 1 | Import; one call site. |
| `tests/conftest.py` | 6 | 0 | `FCP_MCP_TRACE_DIR` per test, beside `_isolated_journal`, so the suite never writes `~/.fcp-mcp/traces`. |
| `tests/test_tool_groups.py` | 2 | 1 | Group cap 15 -> 16. |
| `server.json` | 1 | 1 | Description counts: 16 grouped tools, 107 operations. |
| `.claude-plugin/plugin.json` | 1 | 1 | The same counts. |
| `skill/SKILL.md` | 1 | 0 | A row for `trace` in the group table. |
| `CHANGELOG.md` | 21 | 0 | The `Unreleased (fork)` entry. |

Every subprocess call site change is `subprocess.run(` -> `traced_run(` plus
one import; arguments, return values and exceptions are untouched.

### Outside the hook list, and why

The prompt limited changes to the dispatch hooks, the subprocess call sites,
the instructions line and the group registration. Five more files changed,
because a new group registered through `tools/` changes counts that upstream
tests measure, and the full upstream suite has to pass:

- `tests/test_version.py` reads the group and operation counts out of
  `server.json` and `.claude-plugin/plugin.json`, so both descriptions say
  16 and 107.
- `tests/test_skill.py` requires every group to be named in `skill/SKILL.md`.
- `tests/test_tool_groups.py::test_group_count_is_a_real_reduction` caps the
  groups at 15. It is upstream's deliberate guard against un-grouping, and
  this is the one upstream test whose assertion the fork changes.
- `tests/conftest.py` keeps the suite's traces in `tmp_path`, as upstream
  does for the journal and the index.

With these, the 4 upstream tests that failed once the group existed pass,
and no other upstream test needed anything. `README.md`, `CLAUDE.md` and the
`pyproject.toml` description still say 15 groups and 102 operations: no test
reads them, and every one of them changes on most upstream releases.

## Test counts

Python 3.12.13, macOS, Final Cut Pro installed, ffmpeg NOT installed (that
is all 35 skips: ffmpeg, ffprobe, librosa, scenedetect).

| | mcp 2.3.0 | mcp 1.21.1 (declared floor) |
|---|---|---|
| Upstream baseline, before any change | 2344 passed, 35 skipped | 2341 passed, 38 skipped |
| Fork, tracing on (default) | 2401 passed, 35 skipped | 2398 passed, 38 skipped |
| Fork, `FCP_MCP_TRACE=off` | 2401 passed, 35 skipped | 2398 passed, 38 skipped |
| Fork, upstream tests only (`--ignore tests/test_trace.py`), on and off | 2344 passed, 35 skipped | 2341 passed, 38 skipped |

The extra 57 are `tests/test_trace.py`. Python 3.10.20 on mcp 2.3.0 gives
the same 2401 / 35, on and off. The 2 warnings on 1.21.1 are upstream's own
(`read_resource` returning a string), identical on the base commit.
`ruff check . --exclude docs/` passes. Seven deliberate breakages of the
tracer (the stderr passthrough, word-based key matching, one outcome hook,
the error suffix, the mutating-only intent rule, a missing `MUTATING` entry,
a call site bypassing `traced_run`) each fail a test in `test_trace.py`.

## Rebasing on upstream

```bash
git fetch upstream
git rebase upstream/main trace
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/ruff check . --exclude docs/
.venv/bin/python -m pytest tests/ -q                       # tracing on
FCP_MCP_TRACE=off .venv/bin/python -m pytest tests/ -q     # tracing off
```

Expected conflicts, and how to resolve them:

- **`server.json`, `.claude-plugin/plugin.json`.** Every release bumps the
  `version` line next to the description line the fork edits, so git sees
  one hunk. Take upstream's file, then set the description counts to
  upstream's groups + 1 and operations + 5 (`test_version.py` checks them
  against the live registry).
- **`CHANGELOG.md`.** The fork's entry sits between the title and upstream's
  `## [Unreleased]`; a release rewrites that area. Keep both.
- **`server.py`.** Only where upstream edits `call_tool`, `handle_group` or
  `_journaled` themselves. Re-apply one `_trace.outcome(...)` per early
  return and `except` branch.

Failures after a clean rebase that mean the fork has work to do, by design:

| Failing test | Do this |
|---|---|
| `test_trace.py::test_mutating_table_covers_every_action` | Upstream added, renamed or removed an action: classify it in `fcpxml.trace.MUTATING`. |
| `test_trace.py::test_every_tool_subprocess_goes_through_the_helper` | A new `subprocess.run(` call site: route it through `traced_run`, or add it to the allowed list with a reason. |
| `test_tool_groups.py::test_group_count_is_a_real_reduction` | Upstream raised its own cap: make the fork's cap upstream's + 1. |
| `test_trace.py::test_each_dispatch_branch_has_one_outcome` | A new branch in dispatch: give it an outcome (and a row in `docs/TRACE.md`). |

To re-run the suite at the `mcp` floor, as upstream's CI does:

```bash
uv venv .venv-mcp-floor && uv pip install --python .venv-mcp-floor/bin/python -e ".[dev]" "mcp==1.21.1"
.venv-mcp-floor/bin/python -m pytest tests/ -q
```

## What would make a sensible upstream pull request

In order of value for the size of the change:

1. **Keep the cause of an unexpected error.** `call_tool` returns
   `Error: <ExceptionType>` and drops the message and traceback. Even
   without tracing, recording them somewhere local (and giving the model a
   reference to it, as the trace suffix does) is a small change that makes
   every crash debuggable. Upstream may prefer that the message itself stays
   out of the reply, which this keeps.
2. **One subprocess seam.** `traced_run` puts all ffmpeg, ffprobe,
   osascript and xmllint calls through one function. SECURITY.md's invariant
   ("list-form arguments, bounded parameters, and timeouts") could then be
   enforced in one place, and `test_every_tool_subprocess_goes_through_the_helper`
   keeps new call sites honest.
3. **Attribute refusals.** Splitting `_project_guard(...) or _review_gate(...)`
   into two checks changes nothing and lets anything downstream (trace,
   journal, metrics) say which one refused.
4. **The tracer itself, opt-in.** Upstream might want it default-off.
   Everything is in two new modules plus the hooks listed above.
5. **A note for any future key-based redaction:** substring matching on
   `key` erases `keyword`, `keywords` and `keyframes`, which are among this
   server's most common arguments. `_secret_name` matches whole words.

Not for upstream as is: the count and cap edits (upstream would make its own),
the Logic-compatibility choices in `docs/TRACE.md`, and the `trace` group's
place in the advertised tool list (it costs schema tokens in every
conversation).
