#!/usr/bin/env python3
"""Select the next unfinished item from a markdown plan-state-json block."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


DONE_STATUSES = {"Completed", "Deferred"}
VALID_TYPES = {"phase", "pass"}
DEFAULT_RUNS_DIR = Path(".agent-runs")
PLAN_COPY_NAME = "plan.md"
WORKSPACE_DIR_NAME = "workspace"
SANDBOX_DIR_NAME = "agent_loop_sandbox"
LOGS_DIR_NAME = "logs"


class PlanError(Exception):
    """Raised when the plan file or plan state is malformed."""


@dataclass(frozen=True)
class PlanItem:
    id: str
    title: str
    type: str
    status: str
    parent: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "PlanItem":
        parent = raw.get("parent")
        return cls(
            id=raw["id"],
            title=raw["title"],
            type=raw["type"],
            status=raw["status"],
            parent=parent if isinstance(parent, str) and parent else None,
        )

    def to_json_obj(self) -> dict[str, str]:
        data = {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "status": self.status,
        }
        if self.parent is not None:
            data["parent"] = self.parent
        return data


@dataclass(frozen=True)
class Selection:
    item: PlanItem | None
    warning: str | None = None


@dataclass(frozen=True)
class PlanState:
    plan_id: str
    items: list[PlanItem]
    items_by_id: dict[str, PlanItem]
    children_by_parent: dict[str, list[PlanItem]]
    validation_details: list[str]


@dataclass(frozen=True)
class PlanFiles:
    original_plan_file: Path
    plan_file: Path
    mode: str
    run_dir: Path | None = None
    workspace_dir: Path | None = None
    sandbox_dir: Path | None = None
    mutates_active_plan: bool = True

    @property
    def copied(self) -> bool:
        return self.mode == "copy"


@dataclass(frozen=True)
class PlanStateBlock:
    opener_line: int
    start_index: int
    end_index: int
    content: str


@dataclass(frozen=True)
class HarnessCheck:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def to_json_obj(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True)
class ExecutionResult:
    selected_before: Selection
    selected_after: Selection
    codex_returncode: int
    logs_dir: Path
    plan_backup_before: Path
    plan_backup_after: Path
    harness_checks: list[HarnessCheck]
    prompt: str
    same_selection_warning: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select the first unfinished item from a markdown plan-state-json block."
    )
    parser.add_argument("plan_file", help="Path to the markdown plan file.")
    parser.add_argument(
        "--copy-to-run-dir",
        nargs="?",
        const="",
        metavar="RUN_DIR",
        help=(
            "Copy the plan into a run directory before selecting. "
            "If RUN_DIR is omitted, use .agent-runs/<timestamp>/."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Only parse, validate, and print the next item without executing Codex.",
    )
    parser.add_argument(
        "--execute-next",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--run-all",
        action="store_true",
        help="Reserved for future multi-pass execution.",
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex executable to use for execution. Defaults to codex.",
    )
    parser.add_argument(
        "--dry-run-prompt",
        action="store_true",
        help="Print the exact Codex prompt that would be sent without running Codex.",
    )
    parser.add_argument(
        "--include-parents",
        action="store_true",
        help="Allow unfinished parent phases with child passes to be selected.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print extra validation details.",
    )
    return parser.parse_args()


def is_plan_state_opener(line: str) -> bool:
    trimmed = line.strip()
    if not trimmed.startswith("```"):
        return False
    info = trimmed[3:].strip()
    if not info:
        return False
    first_token = info.split(maxsplit=1)[0]
    return first_token == "plan-state-json"


def find_plan_state_blocks(lines: list[str], plan_file: Path) -> list[PlanStateBlock]:
    blocks: list[PlanStateBlock] = []
    index = 0
    while index < len(lines):
        if not is_plan_state_opener(lines[index]):
            index += 1
            continue

        opener_line = index + 1
        start_index = index
        content: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip() != "```":
            content.append(lines[index])
            index += 1

        if index >= len(lines):
            raise PlanError(
                f"{plan_file}: plan-state-json block starting on line {opener_line} "
                "has no closing ``` fence"
            )

        blocks.append(
            PlanStateBlock(
                opener_line=opener_line,
                start_index=start_index,
                end_index=index,
                content="\n".join(content),
            )
        )
        index += 1

    return blocks


def get_single_plan_state_block(lines: list[str], plan_file: Path) -> PlanStateBlock:
    blocks = find_plan_state_blocks(lines, plan_file)
    if not blocks:
        raise PlanError(f"{plan_file}: missing plan-state-json fenced block")
    if len(blocks) > 1:
        lines_text = ", ".join(str(block.opener_line) for block in blocks)
        raise PlanError(
            f"{plan_file}: expected exactly one plan-state-json block, "
            f"found {len(blocks)} on lines {lines_text}"
        )
    return blocks[0]


def read_plan_lines(plan_file: Path) -> list[str]:
    try:
        return plan_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PlanError(f"{plan_file}: failed to read plan file: {exc}") from exc


def extract_plan_state_json(plan_file: Path) -> str:
    block = get_single_plan_state_block(read_plan_lines(plan_file), plan_file)
    return block.content


def patch_sandbox_refs(text: str, sandbox_dir: Path) -> str:
    sandbox_text = str(sandbox_dir)
    placeholder = "__PLAN_EXECUTOR_SANDBOX_DIR__"
    return (
        text.replace("agent_loop_sandbox/", f"{placeholder}/")
        .replace("agent_loop_sandbox", placeholder)
        .replace(placeholder, sandbox_text)
    )


def load_json_state(plan_file: Path) -> dict[str, Any]:
    raw_json = extract_plan_state_json(plan_file)
    try:
        loaded = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise PlanError(
            f"{plan_file}: invalid JSON in plan-state-json block at "
            f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(loaded, dict):
        raise PlanError(f"{plan_file}: plan-state-json top level must be an object")
    return loaded


def load_plan_state_from_file(plan_file: Path) -> PlanState:
    return validate_plan_state(load_json_state(plan_file))


def selection_to_json_obj(selection: Selection) -> dict[str, Any]:
    output: dict[str, Any] = {
        "selected": selection.item.to_json_obj() if selection.item is not None else None,
    }
    if selection.warning is not None:
        output["warning"] = selection.warning
    return output


def generated_run_dir() -> Path:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    base = DEFAULT_RUNS_DIR / timestamp
    if not base.exists():
        return base

    for suffix in range(1, 1000):
        candidate = DEFAULT_RUNS_DIR / f"{timestamp}-{suffix:03d}"
        if not candidate.exists():
            return candidate

    raise PlanError("failed to find an unused timestamped run directory")


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")


def safe_path_part(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ("-", "_") else "-" for char in value)
    return safe.strip("-") or "plan"


def is_existing_run_plan(plan_file: Path) -> bool:
    return plan_file.name == PLAN_COPY_NAME and plan_file.parent.parent.name == ".agent-runs"


def generated_in_place_run_dir(plan_id: str) -> Path:
    base_name = f"in-place-{safe_path_part(plan_id)}-{timestamp()}"
    base = DEFAULT_RUNS_DIR / base_name
    if not base.exists():
        return base

    for suffix in range(2, 1000):
        candidate = DEFAULT_RUNS_DIR / f"{base_name}-{suffix}"
        if not candidate.exists():
            return candidate

    raise PlanError("failed to find an unused in-place run directory")


def per_pass_logs_dir(run_dir: Path, selected_id: str) -> Path:
    logs_root = run_dir / LOGS_DIR_NAME
    base_name = f"{timestamp()}-{safe_path_part(selected_id)}"
    base = logs_root / base_name
    if not base.exists():
        return base

    for suffix in range(2, 1000):
        candidate = logs_root / f"{base_name}-{suffix}"
        if not candidate.exists():
            return candidate

    raise PlanError("failed to find an unused per-pass logs directory")


def ensure_run_dir(run_dir: Path, explicit: bool) -> None:
    if run_dir.exists():
        if not run_dir.is_dir():
            raise PlanError(f"{run_dir}: run directory path exists but is not a directory")
        if any(run_dir.iterdir()):
            if explicit:
                raise PlanError(f"{run_dir}: run directory already exists and is not empty")
            raise PlanError(f"{run_dir}: generated run directory already exists")
    run_dir.mkdir(parents=True, exist_ok=True)


def patch_copied_plan(plan_file: Path, sandbox_dir: Path) -> None:
    try:
        original_text = plan_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"{plan_file}: failed to read copied plan: {exc}") from exc

    had_trailing_newline = original_text.endswith("\n")
    lines = original_text.splitlines()
    block = get_single_plan_state_block(lines, plan_file)

    try:
        state = json.loads(block.content)
    except json.JSONDecodeError as exc:
        raise PlanError(
            f"{plan_file}: invalid JSON in plan-state-json block at "
            f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(state, dict):
        raise PlanError(f"{plan_file}: plan-state-json top level must be an object")

    state["sandbox_dir"] = str(sandbox_dir)
    patched_json = json.dumps(state, indent=2)

    before_block = [patch_sandbox_refs(line, sandbox_dir) for line in lines[: block.start_index]]
    after_block = [patch_sandbox_refs(line, sandbox_dir) for line in lines[block.end_index + 1 :]]
    patched_lines = (
        before_block
        + [lines[block.start_index]]
        + patched_json.splitlines()
        + [lines[block.end_index]]
        + after_block
    )
    patched_text = "\n".join(patched_lines)
    if had_trailing_newline:
        patched_text += "\n"

    try:
        plan_file.write_text(patched_text, encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"{plan_file}: failed to write patched plan: {exc}") from exc


def prepare_plan_file(
    original_plan_file: Path, copy_to_run_dir: str | None = None
) -> PlanFiles:
    if copy_to_run_dir is None:
        if is_existing_run_plan(original_plan_file):
            return PlanFiles(
                original_plan_file=original_plan_file,
                plan_file=original_plan_file,
                mode="existing-run",
                run_dir=original_plan_file.parent,
                workspace_dir=original_plan_file.parent / WORKSPACE_DIR_NAME,
            )
        return PlanFiles(
            original_plan_file=original_plan_file,
            plan_file=original_plan_file,
            mode="in-place",
        )

    explicit = copy_to_run_dir != ""
    run_dir = Path(copy_to_run_dir) if explicit else generated_run_dir()
    plan_copy = run_dir / PLAN_COPY_NAME
    workspace_dir = run_dir / WORKSPACE_DIR_NAME
    sandbox_dir = workspace_dir / SANDBOX_DIR_NAME

    ensure_run_dir(run_dir, explicit=explicit)
    if plan_copy.exists():
        raise PlanError(f"{plan_copy}: copied plan already exists")
    if workspace_dir.exists():
        raise PlanError(f"{workspace_dir}: workspace directory already exists")

    try:
        shutil.copy2(original_plan_file, plan_copy)
        workspace_dir.mkdir()
    except OSError as exc:
        raise PlanError(f"{run_dir}: failed to prepare run directory: {exc}") from exc

    patch_copied_plan(plan_copy, sandbox_dir)
    return PlanFiles(
        original_plan_file=original_plan_file,
        plan_file=plan_copy,
        mode="copy",
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        sandbox_dir=sandbox_dir,
    )


def with_plan_state_context(plan_files: PlanFiles, raw_state: dict[str, Any]) -> PlanFiles:
    sandbox_dir = plan_files.sandbox_dir
    raw_sandbox_dir = raw_state.get("sandbox_dir")
    if sandbox_dir is None and isinstance(raw_sandbox_dir, str) and raw_sandbox_dir:
        sandbox_dir = Path(raw_sandbox_dir)
    return replace(plan_files, sandbox_dir=sandbox_dir)


def with_execution_run_dir(plan_files: PlanFiles, plan_id: str) -> PlanFiles:
    if plan_files.run_dir is not None:
        return plan_files
    return replace(plan_files, run_dir=generated_in_place_run_dir(plan_id))


def require_non_empty_string(
    errors: list[str], value: Any, field: str, context: str
) -> None:
    if not isinstance(value, str) or not value:
        errors.append(f"{context}: {field} must be a non-empty string")


def validate_plan_state(raw_state: dict[str, Any]) -> PlanState:
    errors: list[str] = []
    details: list[str] = []

    plan_id = raw_state.get("plan_id")
    require_non_empty_string(errors, plan_id, "plan_id", "top level")

    raw_items = raw_state.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        errors.append("top level: items must be a non-empty list")
        raw_items = []

    status_values = raw_state.get("status_values")
    allowed_statuses: set[str] | None = None
    if status_values is not None:
        if not isinstance(status_values, list) or not all(
            isinstance(status, str) and status for status in status_values
        ):
            errors.append(
                "top level: status_values must be a list of non-empty strings when present"
            )
        else:
            allowed_statuses = set(status_values)

    ids_seen: set[str] = set()
    raw_items_by_id: dict[str, dict[str, Any]] = {}

    for item_index, raw_item in enumerate(raw_items):
        context = f"items[{item_index}]"
        if not isinstance(raw_item, dict):
            errors.append(f"{context}: item must be an object")
            continue

        for field in ("id", "title", "type", "status"):
            require_non_empty_string(errors, raw_item.get(field), field, context)

        item_id = raw_item.get("id")
        item_type = raw_item.get("type")
        item_status = raw_item.get("status")

        if isinstance(item_id, str) and item_id:
            if item_id in ids_seen:
                errors.append(f"{context}: duplicate item id {item_id!r}")
            else:
                ids_seen.add(item_id)
                raw_items_by_id[item_id] = raw_item

        if isinstance(item_type, str) and item_type and item_type not in VALID_TYPES:
            errors.append(
                f"{context}: type {item_type!r} must be one of "
                f"{', '.join(sorted(VALID_TYPES))}"
            )

        if (
            allowed_statuses is not None
            and isinstance(item_status, str)
            and item_status
            and item_status not in allowed_statuses
        ):
            errors.append(
                f"{context}: status {item_status!r} is not listed in status_values"
            )

        parent = raw_item.get("parent")
        if parent is not None and (not isinstance(parent, str) or not parent):
            errors.append(f"{context}: parent must be a non-empty string when present")

    for item_index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            continue
        parent = raw_item.get("parent")
        if isinstance(parent, str) and parent and parent not in raw_items_by_id:
            errors.append(
                f"items[{item_index}]: parent {parent!r} does not reference an existing item id"
            )

    if errors:
        raise PlanError("plan validation failed:\n" + "\n".join(f"- {e}" for e in errors))

    items = [PlanItem.from_raw(raw_item) for raw_item in raw_items]
    items_by_id = {item.id: item for item in items}
    children_by_parent: dict[str, list[PlanItem]] = {}
    for item in items:
        if item.type == "pass" and item.parent is not None:
            children_by_parent.setdefault(item.parent, []).append(item)

    details.append(f"validated plan_id: {plan_id}")
    details.append(f"validated item count: {len(items)}")
    details.append(f"validated unique item ids: {len(items_by_id)}")
    if allowed_statuses is not None:
        details.append(f"validated status_values count: {len(allowed_statuses)}")
    details.append(f"validated parent links: {sum(len(v) for v in children_by_parent.values())}")

    return PlanState(
        plan_id=plan_id,
        items=items,
        items_by_id=items_by_id,
        children_by_parent=children_by_parent,
        validation_details=details,
    )


def is_unfinished(item: PlanItem) -> bool:
    return item.status not in DONE_STATUSES


def select_next_item(plan_state: PlanState, include_parents: bool) -> Selection:
    for item in plan_state.items:
        if not is_unfinished(item):
            continue

        if include_parents or item.type != "phase":
            return Selection(item=item)

        children = plan_state.children_by_parent.get(item.id, [])
        if not children:
            return Selection(item=item)

        for child in children:
            if is_unfinished(child):
                return Selection(item=child)

        return Selection(
            item=item,
            warning=(
                f"parent phase {item.id!r} is unfinished but all child passes are "
                "Completed or Deferred; parent status may need updating"
            ),
        )

    return Selection(item=None)


def build_codex_prompt(plan_file: Path, selected_id: str) -> str:
    return f"""Read {plan_file}.

Execute {selected_id} only.

Important:
- Follow the "How To Use This Plan" section in {plan_file}.
- Execute exactly one phase/pass.
- Do not skip ahead.
- Do not execute multiple phases/passes.
- If {selected_id} is too broad, update {plan_file} with smaller passes under that phase and stop.
- If implementing, keep all generated artifacts under the sandbox_dir specified in the plan-state-json block.
- Update {plan_file} after the pass according to its execution tracking rules.
- Run the checks listed for the selected phase/pass.
- Do not commit.
- Do not push.
- Do not use gh.
- Do not modify files outside the active plan and its specified sandbox/workspace unless the selected phase/pass explicitly requires it.
"""


def write_text_file(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"{path}: failed to write file: {exc}") from exc


def write_json_file(path: Path, data: Any) -> None:
    write_text_file(path, json.dumps(data, indent=2) + "\n")


def copy_plan_backup(source: Path, destination: Path) -> None:
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        raise PlanError(f"failed to copy plan backup from {source} to {destination}: {exc}") from exc


def run_harness_checks() -> list[HarnessCheck]:
    checks: list[HarnessCheck] = []
    for command in (
        ["git", "diff", "--check"],
        ["git", "diff", "--stat"],
        ["git", "status", "--short"],
    ):
        completed = subprocess.run(command, capture_output=True, text=True)
        checks.append(
            HarnessCheck(
                command=command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        )
    return checks


def execute_next(
    plan_files: PlanFiles,
    selected_before: Selection,
    include_parents: bool,
    codex_bin: str,
) -> ExecutionResult:
    if selected_before.item is None:
        raise PlanError("cannot execute next item because the plan is complete")
    if plan_files.run_dir is None:
        raise PlanError("execution requires a run directory")

    logs_dir = per_pass_logs_dir(plan_files.run_dir, selected_before.item.id)
    try:
        logs_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise PlanError(f"{logs_dir}: failed to create logs directory: {exc}") from exc

    plan_backup_before = logs_dir / "plan_before.md"
    plan_backup_after = logs_dir / "plan_after.md"
    copy_plan_backup(plan_files.plan_file, plan_backup_before)

    prompt = build_codex_prompt(plan_files.plan_file, selected_before.item.id)
    write_text_file(logs_dir / "codex_prompt.txt", prompt)
    write_json_file(logs_dir / "selection_before.json", selection_to_json_obj(selected_before))

    try:
        completed = subprocess.run(
            [codex_bin, "exec", prompt],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        write_text_file(logs_dir / "codex_error.txt", str(exc) + "\n")
        copy_plan_backup(plan_files.plan_file, plan_backup_after)
        raise PlanError(f"failed to run {codex_bin!r}: {exc}") from exc

    write_text_file(logs_dir / "codex_stdout.txt", completed.stdout)
    write_text_file(logs_dir / "codex_stderr.txt", completed.stderr)
    write_text_file(logs_dir / "codex_returncode.txt", f"{completed.returncode}\n")
    copy_plan_backup(plan_files.plan_file, plan_backup_after)

    plan_state_after = load_plan_state_from_file(plan_files.plan_file)
    selected_after = select_next_item(plan_state_after, include_parents=include_parents)
    write_json_file(logs_dir / "selection_after.json", selection_to_json_obj(selected_after))

    harness_checks = run_harness_checks()
    write_json_file(
        logs_dir / "harness_checks.json",
        [check.to_json_obj() for check in harness_checks],
    )
    same_selection_warning = None
    if (
        selected_before.item is not None
        and selected_after.item is not None
        and selected_before.item.id == selected_after.item.id
    ):
        same_selection_warning = (
            "selected item did not advance. Codex may have failed, split the phase, "
            "or left the plan incomplete."
        )

    return ExecutionResult(
        selected_before=selected_before,
        selected_after=selected_after,
        codex_returncode=completed.returncode,
        logs_dir=logs_dir,
        plan_backup_before=plan_backup_before,
        plan_backup_after=plan_backup_after,
        harness_checks=harness_checks,
        prompt=prompt,
        same_selection_warning=same_selection_warning,
    )


def print_human_output(
    plan_files: PlanFiles,
    plan_state: PlanState,
    selection: Selection,
    verbose: bool,
) -> None:
    if verbose:
        print("Validation:")
        for detail in plan_state.validation_details:
            print(f"- {detail}")
        print()

    if plan_files.copied:
        print(f"Original plan file: {plan_files.original_plan_file}")
        print(f"Active plan file: {plan_files.plan_file}")
    else:
        print(f"Plan file: {plan_files.plan_file}")
    print(f"Mode: {plan_files.mode}")
    print(f"Run dir: {plan_files.run_dir if plan_files.run_dir is not None else '(generated on execution)'}")
    if plan_files.workspace_dir is not None:
        print(f"Workspace dir: {plan_files.workspace_dir}")
    if plan_files.sandbox_dir is not None:
        print(f"Sandbox dir: {plan_files.sandbox_dir}")
    print("Plan mutation: active plan will be updated in place by Codex execution.")
    print(f"Plan ID: {plan_state.plan_id}")

    if selection.item is None:
        print("Plan complete: no unfinished items remain.")
        return

    item = selection.item
    print(f"Selected ID: {item.id}")
    print(f"Selected title: {item.title}")
    print(f"Selected type: {item.type}")
    print(f"Selected status: {item.status}")

    if item.parent is not None:
        parent = plan_state.items_by_id[item.parent]
        print(f"Parent: {parent.id} - {parent.title}")

    if selection.warning is not None:
        print(f"Warning: {selection.warning}")

    print(
        f"Suggested next Codex prompt: Read {plan_files.plan_file} "
        f"and execute {item.id} only."
    )


def print_selection_details(
    plan_state: PlanState,
    selection: Selection,
    label: str,
) -> None:
    print(label)
    if selection.item is None:
        print("Plan complete: no unfinished items remain.")
        return

    item = selection.item
    print(f"Selected ID: {item.id}")
    print(f"Selected title: {item.title}")
    print(f"Selected type: {item.type}")
    print(f"Selected status: {item.status}")
    if item.parent is not None:
        parent = plan_state.items_by_id[item.parent]
        print(f"Parent: {parent.id} - {parent.title}")
    if selection.warning is not None:
        print(f"Warning: {selection.warning}")


def print_harness_checks(harness_checks: list[HarnessCheck]) -> None:
    print("Harness checks:")
    for check in harness_checks:
        print(f"$ {' '.join(check.command)}")
        print(f"Return code: {check.returncode}")
        if check.stdout:
            print(check.stdout.rstrip())
        if check.stderr:
            print(check.stderr.rstrip(), file=sys.stderr)


def print_execution_output(
    plan_files: PlanFiles,
    plan_state_before: PlanState,
    plan_state_after: PlanState,
    result: ExecutionResult,
    codex_bin: str,
    verbose: bool,
) -> None:
    print_human_output(
        plan_files,
        plan_state_before,
        result.selected_before,
        verbose,
    )
    print()
    print(f"Codex command: {codex_bin} exec <prompt>")
    print(f"Logs dir: {result.logs_dir}")
    print(f"Plan backup before: {result.plan_backup_before}")
    print(f"Plan backup after: {result.plan_backup_after}")
    print(f"Codex return code: {result.codex_returncode}")
    print()
    print_selection_details(plan_state_after, result.selected_after, "Selected after execution:")
    if result.same_selection_warning is not None:
        print(f"Warning: {result.same_selection_warning}")
    print()
    print_harness_checks(result.harness_checks)


def build_json_output(
    plan_files: PlanFiles,
    plan_state: PlanState,
    selection: Selection,
    verbose: bool,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "plan_file": str(plan_files.plan_file),
        "mode": plan_files.mode,
        "run_dir": str(plan_files.run_dir) if plan_files.run_dir is not None else None,
        "mutates_active_plan": plan_files.mutates_active_plan,
        "plan_id": plan_state.plan_id,
        "complete": selection.item is None,
        "selected": selection.item.to_json_obj() if selection.item is not None else None,
    }
    output["original_plan_file"] = str(plan_files.original_plan_file)
    output["workspace_dir"] = (
        str(plan_files.workspace_dir) if plan_files.workspace_dir is not None else None
    )
    output["sandbox_dir"] = (
        str(plan_files.sandbox_dir) if plan_files.sandbox_dir is not None else None
    )
    if selection.warning is not None:
        output["warning"] = selection.warning
    if verbose:
        output["validation"] = plan_state.validation_details
    return output


def build_execution_json_output(
    plan_files: PlanFiles,
    plan_state: PlanState,
    result: ExecutionResult,
    verbose: bool,
) -> dict[str, Any]:
    output = build_json_output(
        plan_files,
        plan_state,
        result.selected_before,
        verbose,
    )
    output["selected_before"] = (
        result.selected_before.item.to_json_obj()
        if result.selected_before.item is not None
        else None
    )
    output["selected_after"] = (
        result.selected_after.item.to_json_obj()
        if result.selected_after.item is not None
        else None
    )
    output["codex_returncode"] = result.codex_returncode
    output["logs_dir"] = str(result.logs_dir)
    output["plan_backup_before"] = str(result.plan_backup_before)
    output["plan_backup_after"] = str(result.plan_backup_after)
    output["harness_checks"] = [check.to_json_obj() for check in result.harness_checks]
    if result.same_selection_warning is not None:
        output["warning"] = result.same_selection_warning
    return output


def command_exit_code(result: ExecutionResult) -> int:
    for check in result.harness_checks:
        if check.returncode != 0:
            return check.returncode
    return result.codex_returncode


def main() -> int:
    args = parse_args()
    original_plan_file = Path(args.plan_file)

    try:
        if args.run_all:
            raise PlanError("--run-all is not implemented yet.")
        if args.execute_next:
            print(
                "--execute-next is deprecated; execution is now the default.",
                file=sys.stderr,
            )
        plan_files = prepare_plan_file(original_plan_file, args.copy_to_run_dir)
        raw_state = load_json_state(plan_files.plan_file)
        plan_files = with_plan_state_context(plan_files, raw_state)
        plan_state = validate_plan_state(raw_state)
        selection = select_next_item(plan_state, include_parents=args.include_parents)
    except PlanError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.status or selection.item is None:
        if args.json:
            print(
                json.dumps(
                    build_json_output(plan_files, plan_state, selection, args.verbose),
                    indent=2,
                )
            )
        else:
            print_human_output(plan_files, plan_state, selection, args.verbose)
        return 0

    if args.dry_run_prompt:
        if args.json:
            output = build_json_output(plan_files, plan_state, selection, args.verbose)
            output["dry_run_prompt"] = (
                build_codex_prompt(plan_files.plan_file, selection.item.id)
                if selection.item is not None
                else None
            )
            print(json.dumps(output, indent=2))
        else:
            print_human_output(plan_files, plan_state, selection, args.verbose)
            if selection.item is not None:
                print()
                print("Codex prompt:")
                print(build_codex_prompt(plan_files.plan_file, selection.item.id), end="")
        return 0

    try:
        plan_files = with_execution_run_dir(plan_files, plan_state.plan_id)
        result = execute_next(
            plan_files,
            selection,
            include_parents=args.include_parents,
            codex_bin=args.codex_bin,
        )
        plan_state_after = load_plan_state_from_file(plan_files.plan_file)
    except PlanError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                build_execution_json_output(plan_files, plan_state, result, args.verbose),
                indent=2,
            )
        )
    else:
        print_execution_output(
            plan_files,
            plan_state,
            plan_state_after,
            result,
            args.codex_bin,
            args.verbose,
        )
    return command_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
