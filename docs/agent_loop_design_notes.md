# Agent Loop Design Notes

These notes are for future maintainers, future Codex sessions, and anyone trying to change the runner or TUI without rediscovering the same traps. Keep this document practical and update it when the architecture changes.

## Mental Model

```text
tools/plan_executor.py
  Core CLI runner / worker / subprocess contract

tools/plan_executor_tui.py
  Textual UI shell / cockpit

docs/*.md
  Runner-compatible plans and project documentation

.agent-runs/
  transient run/log/artifact output
```

The runner owns execution. That includes plan parsing, pass selection, Codex subprocess execution, review/fix/commit behavior, log directory creation, result artifacts, and run-all logic.

The TUI owns presentation and control. That includes plan path input, progress display, command preview, option widgets, subprocess launching, raw output display, latest metadata reading, review artifact viewing, and safe quit behavior.

The TUI should not duplicate runner logic. When it needs to execute work, it shells out to `tools/plan_executor.py` using the same CLI boundary a human would use.

## Boundary Contract

The contract is intentionally Unix-ish.

Inputs to the runner:

- `argv` and options
- plan path
- optional metadata output path via `--result-json`
- Codex binary path via `--codex-bin`
- run/review/fix/commit flags

Outputs from the runner:

- process exit code
- stdout/stderr stream
- mutated active plan file
- `.agent-runs/` logs and backups
- single-pass result metadata via `--result-json`
- review/fix/commit artifacts
- run-all summaries when applicable

Important rule:

```text
The TUI must not parse human stdout to discover artifact paths.
Use machine-readable metadata instead.
```

Stdout is for humans and live raw output. Metadata is for programmatic discovery.

## Current TUI Views

The current Textual TUI has three main views.

### F2 Dashboard

The dashboard is the default cockpit. It supports plan path entry, docs browsing, plan loading, progress display, current selection/current run status, option controls, command preview, a small high-level log, a run button, and a safe quit modal while a run is active.

The dashboard log is deliberately small and high level. It should show lifecycle events such as loading, starting, finishing, and reload status. Raw stdout/stderr does not belong here.

### F3 Output

The output view is the raw stdout/stderr viewer for the runner subprocess. It uses a Textual `Log` widget with incremental writes instead of rebuilding one large text blob.

The TUI drains stdout and stderr into retained state, marks the output view dirty, and renders only when the output view is active. Rendering is bounded and incremental so real Codex output does not make the interface sluggish.

### F4 Review

The review view reads latest single-pass metadata from `.agent-runs/tui-latest-run-result.json`, which the runner writes through `--result-json`.

Artifact selection is metadata-only:

```text
review_after_fix_result_md
review_result_md
empty/error state
```

The view prefers `review_after_fix_result_md`, falls back to `review_result_md`, and displays an empty/error state if neither is available. It does not parse stdout, scan `.agent-runs/`, or infer the newest run directory. JSON verdict/summary data is best-effort if available; the markdown artifact is the primary viewer content.

## Textual Layout Contract

Textual is closer to a retained widget tree plus CSS layout engine than raw curses. Treat layout as ownership, not as line printing.

Rules that matter:

- Define container ownership clearly.
- Hidden views should use `display` toggling so they consume no layout space.
- Avoid nested scroll owners unless there is a specific reason.
- Only intended containers should consume flexible space.
- Be explicit about which widget owns borders, height, focus, and scrolling.

The current dashboard is broadly:

```text
top/path area: fixed
main progress/current selection: flexible
options: fixed
command preview: fixed
log: fixed small
footer: fixed
```

Within the dashboard, the Progress panel is scrollable and is part of the flexible main area. The raw output view owns its `Log` scrolling. The review view owns a `ScrollableContainer` for markdown content.

## Raw Output Performance Lessons

The first raw output viewer used a large `Static` inside a `ScrollableContainer`. It rebuilt and updated a growing text blob. Real Codex output made the TUI sluggish, freeze-prone, and CPU-hungry. That path summons old goblins; do not reopen it.

The current fix is incremental rendering with Textual `Log` plus bounded retained state.

Current rules:

- Subprocess draining must stay cheap.
- stdout/stderr drain loops append to retained state only.
- UI rendering must be throttled and bounded.
- Hidden Output view must not render repeatedly.
- F3 should render a bounded newest tail when opened.
- The dashboard log must stay high-level.
- Raw output must never be copied into the dashboard log.

Current raw output constants in `tools/plan_executor_tui.py`:

```text
MAX_RAW_OUTPUT_LINES
MAX_RAW_OUTPUT_DISPLAY_BOOTSTRAP_LINES
MAX_RAW_OUTPUT_APPEND_PER_REFRESH
MAX_RAW_OUTPUT_PENDING_RESET_THRESHOLD
MAX_RAW_OUTPUT_RENDER_LINE_CHARS
RAW_OUTPUT_REFRESH_INTERVAL_SECONDS
RAW_OUTPUT_TRUNCATION_TEXT
```

Those constants are part of the performance shape. If they change, verify with high-volume output, not only tiny fake subprocess output.

## Review Artifact Discovery

F4 Review depends on latest single-pass metadata. The current metadata path is:

```text
.agent-runs/tui-latest-run-result.json
```

The runner writes this file when the TUI launches a single pass with `--result-json`. The runner also writes `execution_summary.json` under the pass logs directory.

Review discovery rules:

- Use only metadata-provided paths.
- Prefer `review_after_fix_result_md`.
- Fall back to `review_result_md`.
- Treat review JSON summary/verdict as optional supporting data.
- Do not parse stdout.
- Do not scan `.agent-runs/`.
- Do not infer the newest run directory.

This keeps the viewer deterministic even when multiple runs, copied plans, or stale artifacts exist.

## Run-All Notes

TUI run-all support exists in the current code. The TUI calls the CLI runner with `--run-all` and `--max-passes`; the runner owns pass transitions and stopping rules.

Current behavior and limits:

- The TUI command preview includes `--run-all` when the option is checked.
- The run button changes to `Run All`.
- The subprocess command does not include `--result-json` for run-all.
- F3 Output shows the live runner stdout/stderr stream.
- The dashboard live-reloads the currently loaded plan during run-all.
- F4 Review shows a run-all unavailable message; single-pass review artifact viewing is not implemented for run-all.
- Run-all plus copy mode is blocked in the TUI until the UI can switch to or follow the copied active plan path.
- The TUI does not parse stdout for pass transitions.

Runner-side run-all writes `run_all_summary.json` and `run_all_summary.txt` under the run directory. A dedicated TUI summary viewer is future work.

Stop-after-current-pass is not currently implemented. It needs runner support for a cooperative stop mechanism; it should not be hacked in by killing Codex from the TUI.

## Goblin Graveyard: Things Not To Reintroduce

- Do not parse stdout for artifact paths.
- Do not scan `.agent-runs/` for the newest run directory.
- Do not render raw output as one giant `Static` text blob.
- Do not update the Textual raw output widget once per stdout/stderr line.
- Do not use `--result-json` with `--run-all`.
- Do not support TUI run-all plus copy mode until the TUI can follow the copied plan path.
- Do not implement stop-after-current-pass only in the TUI; the runner needs a real cooperative stop mechanism.
- Do not let hidden views keep consuming layout space.
- Do not add historical artifact browsing as part of simple latest-result viewers.
- Do not duplicate runner execution logic inside the TUI.
- Do not make tests invoke real Codex; use fake executables and `--codex-bin`.

## Future Work Guidance

Run-all summary viewer: read runner-produced summary artifacts or future metadata, not stdout.

Stop after current pass: implement a cooperative runner-level stop signal checked between passes, not a TUI-only process kill.

Run/artifact browser: make this an explicit history browser with clear selection, not an implicit newest-directory scan.

Review history browser: browse known artifacts by run record or metadata, while keeping F4 as the latest single-pass viewer.

Better metadata for run-all: add a machine-readable runner contract for run-all if the TUI needs richer progress or artifact discovery.

Run-all plus copy mode in TUI: switch the loaded plan to the copied active plan or teach the runner to report it through metadata before enabling this path.

Optional GUI frontend: reuse the same runner subprocess boundary and metadata contracts instead of importing and reimplementing execution.

## How To Use These Notes In Future Codex Tasks

Before changing runner/TUI behavior, read this document and the relevant code. Respect the boundary contract, preserve the rejected approaches, and update this file when architecture changes.

Prefer small vertical slices: runner contract first, then TUI presentation, then tests. Do not run real Codex in tests.
