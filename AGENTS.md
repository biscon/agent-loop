# Repository Guidelines

## Project Structure & Module Organization

This repository contains a small Python harness for running Codex against Markdown plans. The main implementation lives in `tools/plan_executor.py`. Unit tests are in `tests/`, currently centered on `tests/test_plan_executor.py`. Project documentation and example runner-compatible plans live in `docs/`, including `docs/runner_compatible_plans.md` and `docs/agent_loop_test_plan.md`. Runtime artifacts are written under `.agent-runs/` and should stay untracked.

## Build, Test, and Development Commands

- `python3 tools/plan_executor.py docs/my_plan.md --status`: inspect the next selected plan item without executing Codex.
- `python3 tools/plan_executor.py docs/my_plan.md`: run one implementation pass for the selected unfinished item.
- `python3 tools/plan_executor.py docs/my_plan.md --run-all --max-passes 3`: execute multiple items with a safety cap.
- `python3 -m unittest discover -s tests`: run the test suite.
- `git diff --check`: catch whitespace errors before committing.

There is no separate build step; the project is a standard-library Python script plus tests.

## Coding Style & Naming Conventions

Use Python 3 with four-space indentation, type hints where useful, and clear dataclasses for structured state. Keep helper names in `snake_case`, classes in `PascalCase`, and constants in `UPPER_SNAKE_CASE`, matching `tools/plan_executor.py`. Prefer `pathlib.Path` for filesystem paths and `subprocess.run` with argument lists rather than shell strings. Keep comments focused on non-obvious behavior.

## Testing Guidelines

Tests use the standard `unittest` framework. Add new tests under `tests/` with filenames like `test_<feature>.py` and test methods beginning with `test_`. Existing tests create temporary plans and fake Codex executables; follow that pattern instead of invoking real Codex. Run `python3 -m unittest discover -s tests` before submitting changes.

## Commit & Pull Request Guidelines

Recent history uses short imperative commit subjects such as `Add isolated plan copy mode and tests` or `Document plan executor usage`; follow that style and avoid vague messages like `stuff`. Pull requests should include a concise description, the commands run for verification, and any plan-executor behavior changes. Link related issues when applicable, and include screenshots only when changes affect rendered documentation.

## Security & Configuration Tips

Do not commit `.agent-runs/`, `agent_loop_sandbox/`, `__pycache__/`, or `*.pyc` files. The runner intentionally does not push, create branches, use `gh`, or auto-retry elevated actions; preserve those safety boundaries unless a change explicitly revisits them.
