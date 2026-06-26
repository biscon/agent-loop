# Plan Executor

A small Python harness for running Codex against a self-tracking Markdown plan, one phase or pass at a time.

## What It Does

Plan Executor is a plan-driven Codex runner. It reads a Markdown plan, selects the next unfinished work item from a `plan-state-json` block, and invokes `codex exec` for exactly that selected item.

The active Markdown plan is the source of truth. The runner updates logs and backups under `.agent-runs/`, but progress is tracked in the plan file itself.

It can optionally run a separate review, one bounded fix attempt, and a commit after a successful pass. It does not push, create branches, use `gh`, or drive the interactive Codex TUI.

## Mental Model

```text
Markdown plan
    ↓
tools/plan_executor.py
    ↓
fresh codex exec per pass
    ↓
checks / optional review / optional fix / optional commit
    ↓
updated plan
```

Each implementation pass, review, and fix is a fresh `codex exec` invocation. Do not rely on conversational state carrying between passes; put durable instructions and progress in the plan.

## Quick Start

Inspect the next selected item without executing:

```bash
python3 tools/plan_executor.py docs/my_plan.md --status
```

Run the next selected item:

```bash
python3 tools/plan_executor.py docs/my_plan.md
```

Open the optional Textual TUI for status inspection and option setup:

```bash
python3 tools/plan_executor.py --tui
```

The TUI is read-only in V3.0: it loads plans, previews commands, and lets you
adjust options, but it does not execute Codex. Paste paths using your terminal
paste shortcut, usually Ctrl+Shift+V.

Textual is optional for normal CLI use. These commands work without installing
Textual:

```bash
python3 tools/plan_executor.py docs/my_plan.md --status
python3 tools/plan_executor.py docs/my_plan.md
```

Only TUI mode requires Textual:

```bash
pip install -r requirements.txt
```

or:

```bash
pip install textual
```

Run one pass, then review, fix once if needed, and commit if the final state passes:

```bash
python3 tools/plan_executor.py docs/my_plan.md \
  --review-after-pass \
  --fix-after-review \
  --commit-after-pass
```

Run multiple items with a safety cap:

```bash
python3 tools/plan_executor.py docs/my_plan.md \
  --run-all \
  --max-passes 3 \
  --review-after-pass \
  --fix-after-review \
  --commit-after-pass
```

## Real Project Mode

Use normal in-place mode for actual project work:

```bash
python3 tools/plan_executor.py docs/real_project_plan.md
```

This mutates `docs/real_project_plan.md` in place and lets Codex edit real repository files according to the selected plan item. Logs and backups still go under `.agent-runs/`.

## Copy Mode For Testing

Copy mode is useful for disposable toy plans and tests:

```bash
python3 tools/plan_executor.py docs/test_plan.md --copy-to-run-dir
```

This creates a copied plan under `.agent-runs/<run>/plan.md`, patches its sandbox path into that run directory, and leaves the original plan alone.

After creating the copy, point subsequent commands at the copied plan:

```bash
python3 tools/plan_executor.py .agent-runs/<run>/plan.md
```

Copy mode is mainly for testing runner behavior or experimenting with small plans. For normal real-repository work, use real project mode.

## Runner-Compatible Plans

See [docs/runner_compatible_plans.md](docs/runner_compatible_plans.md) for the detailed plan format.

At a minimum, a runner-compatible plan contains exactly one fenced `plan-state-json` block with:

| Field | Purpose |
| ----- | ------- |
| `plan_id` | Stable identifier used in run/log naming. |
| `items` | Ordered list of executable phases and passes. |

Each item has:

| Field | Purpose |
| ----- | ------- |
| `id` | Stable unique item id. |
| `title` | Human-readable item title. |
| `type` | Either `phase` or `pass`. |
| `status` | Current progress status. |
| `parent` | Optional parent phase id, commonly used by pass items. |

`Completed` and `Deferred` are treated as finished. Other statuses are unfinished. Markdown prose around the JSON should explain what each phase/pass means, how to verify it, and how the plan should be updated.

By default, unfinished parent phases with child passes are not selected before their unfinished children. Use `--include-parents` when you intentionally want the runner to select unfinished parent phases directly.

## Common Commands

| Switch | Purpose |
| ------ | ------- |
| `--status` | Inspect selected item without executing. |
| `--json` | Machine-readable output where supported. |
| `--dry-run-prompt` | Print generated Codex prompt without executing. |
| `--copy-to-run-dir [RUN_DIR]` | Copy/patch plan into a disposable run dir. |
| `--run-all` | Execute multiple items until complete/failure/max. |
| `--max-passes N` | Safety cap for `--run-all`; defaults to `10`. |
| `--review-after-pass` | Run a separate read-only review after implementation. |
| `--fix-after-review` | Run one bounded fix attempt when review says `needs_fix`. |
| `--max-fix-attempts N` | Currently only `1` is supported. |
| `--commit-after-pass` | Commit after successful pass/review/fix. |
| `--commit-prefix PREFIX` | Prefix commit subjects; defaults to `plan`. |
| `--include-parents` | Allow unfinished parent phases with child passes to be selected. |
| `--inhibit-sleep` | Re-exec through `systemd-inhibit` on Linux. |
| `--codex-bin PATH` | Use an alternate/fake Codex executable. |
| `--verbose` | More diagnostic output. |

`--run-all --json` is not implemented. `--fix-after-review` requires `--review-after-pass`.

## Review, Fix, And Commit Flow

```text
implementation -> harness checks -> review -> optional one fix -> fix checks -> rereview -> optional commit
```

Review verdicts are:

| Verdict | Meaning |
| ------- | ------- |
| `pass` | The pass is acceptable. |
| `needs_fix` | The pass has fixable issues. |
| `needs_human` | Human judgment is needed before continuing. |

Fix runs only for `needs_fix`. V2.8 supports only one fix attempt.

Commit mode requires a clean worktree before execution. A commit is attempted only after Codex succeeds, harness checks pass, the selected item is finished, and the final review state is `pass` when review is enabled.

Commit subjects are deterministic:

```text
<prefix>: complete <item-id> - <item-title>
```

The default prefix is `plan`. The runner stages and commits local changes, but it does not push.

## Linux Sleep Inhibition

On Linux systems with `systemd-inhibit`, use:

```bash
python3 tools/plan_executor.py docs/my_plan.md \
  --run-all \
  --inhibit-sleep
```

This re-execs the runner through `systemd-inhibit` with `--what=idle:sleep`. It is intended to prevent idle sleep while a run is active. It does not inhibit shutdown or reboot, and normal systemd desktop use should not require `sudo`.

Sleep-inhibition diagnostics are printed to stderr.

## Logs And Local Files

`.agent-runs/` stores transient local audit/debug data, including prompts, stdout/stderr, return codes, plan backups, review/fix artifacts, harness check output, git snapshots, and run summaries.

The active plan file remains the progress source of truth:

| Mode | Active plan |
| ---- | ----------- |
| Real project mode | The plan path you passed on the command line. |
| Copy mode | `.agent-runs/<run>/plan.md`. |

The runner may bootstrap `.git/info/exclude` locally so these transient outputs stay ignored without editing tracked `.gitignore`:

```text
.agent-runs/
agent_loop_sandbox/
__pycache__/
*.pyc
```

## Safety Boundaries

The runner does not:

| Boundary | Notes |
| -------- | ----- |
| Push | No `git push` is performed. |
| Create branches | It works in the current checkout. |
| Use `gh` | Prompts explicitly tell Codex not to use `gh`. |
| Auto-stash | Commit mode requires a clean worktree before execution. |
| Auto-retry elevated | Failed permission/network actions are not retried with elevated privileges by the runner. |
| Drive the Codex TUI | It invokes `codex exec`, not the interactive interface. |
| Run real Codex from default unit tests | Tests use fake Codex executables. |

The implementation prompt also tells Codex to execute exactly one selected item, avoid skipping ahead, avoid committing/pushing, and avoid modifying files outside the active plan and its configured sandbox/workspace unless the selected plan item explicitly requires it.

## Development And Tests

Useful checks before committing runner changes:

```bash
python3 -m unittest discover -s tests
git diff --check
git diff --stat
git status --short
```

The unit tests should not call real `codex`. Use `--codex-bin` with a fake executable when testing execution paths manually.
