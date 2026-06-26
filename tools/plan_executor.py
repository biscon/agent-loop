#!/usr/bin/env python3
"""Select the next unfinished item from a markdown plan-state-json block."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import threading
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
LOCAL_GIT_EXCLUDE_COMMENT = "# plan_executor local transient outputs"
LOCAL_GIT_EXCLUDE_PATTERNS = (
    ".agent-runs/",
    "agent_loop_sandbox/",
    "__pycache__/",
    "*.pyc",
)
REVIEW_VERDICTS = {"pass", "needs_fix", "needs_human"}
REVIEW_ISSUE_SEVERITIES = {"blocker", "major", "minor"}


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


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
    review: "ReviewResult | None" = None
    fix: "FixResult | None" = None
    review_after_fix: "ReviewResult | None" = None
    commit: "CommitResult | None" = None


@dataclass(frozen=True)
class ReviewResult:
    requested: bool
    attempted: bool = False
    returncode: int | None = None
    verdict: str | None = None
    summary: str | None = None
    result_json_path: Path | None = None
    result_md_path: Path | None = None
    stop_reason: str | None = None

    @classmethod
    def not_requested(cls) -> "ReviewResult":
        return cls(requested=False)

    @classmethod
    def not_attempted(cls, reason: str) -> "ReviewResult":
        return cls(requested=True, attempted=False, stop_reason=reason)

    def failed(self) -> bool:
        return self.requested and self.stop_reason is not None

    def to_json_obj(self, prefix: str = "review") -> dict[str, Any]:
        data: dict[str, Any] = {f"{prefix}_requested": self.requested}
        if not self.requested:
            return data
        data.update(
            {
                f"{prefix}_attempted": self.attempted,
                f"{prefix}_returncode": self.returncode,
                f"{prefix}_verdict": self.verdict,
                f"{prefix}_summary": self.summary,
                f"{prefix}_result_json": (
                    str(self.result_json_path) if self.result_json_path is not None else None
                ),
                f"{prefix}_result_md": (
                    str(self.result_md_path) if self.result_md_path is not None else None
                ),
                f"{prefix}_stop_reason": self.stop_reason,
            }
        )
        return data


@dataclass(frozen=True)
class FixResult:
    requested: bool
    attempted: bool = False
    returncode: int | None = None
    result_md_path: Path | None = None
    harness_checks: list[HarnessCheck] | None = None
    harness_checks_passed: bool | None = None
    stop_reason: str | None = None

    @classmethod
    def not_requested(cls) -> "FixResult":
        return cls(requested=False)

    @classmethod
    def not_attempted(cls, reason: str | None = None) -> "FixResult":
        return cls(requested=True, attempted=False, stop_reason=reason)

    def failed(self) -> bool:
        return self.requested and self.stop_reason is not None

    def to_json_obj(self) -> dict[str, Any]:
        data: dict[str, Any] = {"fix_requested": self.requested}
        if not self.requested:
            return data
        data.update(
            {
                "fix_attempted": self.attempted,
                "fix_returncode": self.returncode,
                "fix_result_md": (
                    str(self.result_md_path) if self.result_md_path is not None else None
                ),
                "fix_harness_checks_passed": self.harness_checks_passed,
                "fix_stop_reason": self.stop_reason,
            }
        )
        if self.harness_checks is not None:
            data["fix_harness_checks"] = [
                check.to_json_obj() for check in self.harness_checks
            ]
        return data


@dataclass(frozen=True)
class CommitResult:
    requested: bool
    attempted: bool = False
    created: bool = False
    hash: str | None = None
    subject: str | None = None
    skipped_reason: str | None = None
    returncode: int | None = None
    cached_diff_stat_path: Path | None = None
    cached_name_status_path: Path | None = None

    @classmethod
    def not_requested(cls) -> "CommitResult":
        return cls(requested=False)

    def to_json_obj(self) -> dict[str, Any]:
        data: dict[str, Any] = {"commit_requested": self.requested}
        if not self.requested:
            return data
        data.update(
            {
                "commit_attempted": self.attempted,
                "commit_created": self.created,
                "commit_hash": self.hash,
                "commit_subject": self.subject,
                "commit_skipped_reason": self.skipped_reason,
            }
        )
        if self.returncode is not None:
            data["commit_returncode"] = self.returncode
        if self.cached_diff_stat_path is not None:
            data["commit_cached_diff_stat"] = str(self.cached_diff_stat_path)
        if self.cached_name_status_path is not None:
            data["commit_cached_name_status"] = str(self.cached_name_status_path)
        return data


@dataclass
class RunAllRecord:
    selected_id: str
    selected_title: str
    status: str
    logs_dir: str | None = None
    codex_returncode: int | None = None
    selected_after_id: str | None = None
    selected_after_title: str | None = None
    selected_item_post_status: str | None = None
    review: ReviewResult | None = None
    fix: FixResult | None = None
    review_after_fix: ReviewResult | None = None
    commit: CommitResult | None = None

    def to_json_obj(self) -> dict[str, Any]:
        data = {
            "selected_id": self.selected_id,
            "selected_title": self.selected_title,
            "status": self.status,
            "logs_dir": self.logs_dir,
            "codex_returncode": self.codex_returncode,
            "selected_after_id": self.selected_after_id,
            "selected_after_title": self.selected_after_title,
            "selected_item_post_status": self.selected_item_post_status,
        }
        data.update((self.review or ReviewResult.not_requested()).to_json_obj())
        data.update((self.fix or FixResult.not_requested()).to_json_obj())
        data.update(
            (self.review_after_fix or ReviewResult.not_requested()).to_json_obj(
                "review_after_fix"
            )
        )
        data.update((self.commit or CommitResult.not_requested()).to_json_obj())
        return data


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
        help="Run one Codex process per unfinished item until complete or stopped.",
    )
    parser.add_argument(
        "--max-passes",
        type=positive_int,
        default=10,
        help="Maximum number of passes for --run-all. Defaults to 10.",
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex executable to use for execution. Defaults to codex.",
    )
    parser.add_argument(
        "--commit-after-pass",
        action="store_true",
        help=(
            "Commit source changes and active plan progress after each successful "
            "executed pass; local .agent-runs/ logs are transient and not committed."
        ),
    )
    parser.add_argument(
        "--review-after-pass",
        action="store_true",
        help="Run a read-only agent review after each successful executed pass.",
    )
    parser.add_argument(
        "--fix-after-review",
        action="store_true",
        help="After a needs_fix review verdict, run one bounded fix pass and rereview.",
    )
    parser.add_argument(
        "--max-fix-attempts",
        type=positive_int,
        default=1,
        help="Maximum fix attempts for --fix-after-review. V2.8 supports only 1.",
    )
    parser.add_argument(
        "--commit-prefix",
        default="plan",
        help="Prefix for automated commit subjects. Defaults to plan.",
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


def build_review_prompt(
    plan_file: Path,
    selected_item: PlanItem,
    status_after: str,
    logs_dir: Path,
    *,
    after_fix: bool = False,
) -> str:
    result_json = logs_dir / (
        "review_after_fix_result.json" if after_fix else "review_result.json"
    )
    result_md = logs_dir / (
        "review_after_fix_result.md" if after_fix else "review_result.md"
    )
    heading = (
        "Rereview the selected plan item after one fix attempt."
        if after_fix
        else "Review the implementation diff for the selected plan item only."
    )
    fix_artifacts = ""
    extra_instructions = ""
    if after_fix:
        fix_artifacts = f"""
- {logs_dir / "fix_prompt.txt"}
- {logs_dir / "fix_stdout.txt"}
- {logs_dir / "fix_stderr.txt"}
- {logs_dir / "fix_returncode.txt"}
- {logs_dir / "fix_result.md"}
- {logs_dir / "fix_harness_checks.json"}
- {logs_dir / "plan_before_fix.md"}
- {logs_dir / "plan_after_fix.md"}
- {logs_dir / "fix_git_status_before.txt"}
- {logs_dir / "fix_git_status_after.txt"}
- {logs_dir / "fix_git_diff_stat_before.txt"}
- {logs_dir / "fix_git_diff_stat_after.txt"}
- {logs_dir / "fix_git_name_status_after.txt"}"""
        extra_instructions = """
This is a rereview after one bounded fix attempt.
Confirm whether the original review issues were addressed.
Look for regressions introduced by the fix.
"""
    return f"""{heading}

Active plan file: {plan_file}
Selected item id: {selected_item.id}
Selected item title: {selected_item.title}
Selected item status after implementation: {status_after}
Logs dir: {logs_dir}

Read the active plan file and these per-pass artifacts:
- {logs_dir / "selection_before.json"}
- {logs_dir / "selection_after.json"}
- {logs_dir / "plan_before.md"}
- {logs_dir / "plan_after.md"}
- {logs_dir / "codex_stdout.txt"}
- {logs_dir / "codex_stderr.txt"}
- {logs_dir / "harness_checks.json"}
- {logs_dir / "implementation_git_status_before.txt"}
- {logs_dir / "implementation_git_diff_before.patch"}
- {logs_dir / "implementation_git_diff_stat_before.txt"}
- {logs_dir / "implementation_git_status_after.txt"}
- {logs_dir / "implementation_git_diff_after.patch"}
- {logs_dir / "implementation_git_diff_stat_after.txt"}
- {logs_dir / "implementation_git_name_status_after.txt"}
{fix_artifacts}

Inspect the current git diff and changed files as needed.

The current git diff may include changes from earlier uncommitted passes.
Use the before/after implementation snapshots and plan_before/plan_after to focus on the selected item for this pass.
{extra_instructions}

Verify scope discipline:
- Did the changes match the selected plan item?
- Did the implementation accidentally broaden the task?
- Did the plan update match the source changes?
- Did it change behavior that the plan said should remain unchanged?

Look for likely code issues:
- compile errors visible from changed code
- stale names/includes
- dependency direction regressions
- missing call-site updates
- suspicious test updates
- accidental source/hash/serialization/collision/camera behavior changes

Do not edit source files.
Do not update the active plan.
Do not commit.
Do not run gh.
Do not push.
Only write review artifacts under {logs_dir}.

Write valid JSON to {result_json} with this shape:
{{
  "verdict": "pass",
  "summary": "Short review summary.",
  "issues": [],
  "scope_notes": "Notes about whether the diff stayed within scope.",
  "checks_considered": [
    "git diff",
    "harness checks",
    "plan update"
  ]
}}

Allowed verdicts: pass, needs_fix, needs_human.
Issue objects must use this shape:
{{
  "severity": "blocker",
  "file": "path/to/file.cpp",
  "reason": "What looks wrong.",
  "suggested_fix": "Concrete suggested fix or null."
}}
Allowed issue severities: blocker, major, minor.

Also write a human-readable review report to {result_md}.
"""


def build_fix_prompt(
    plan_file: Path,
    selected_item: PlanItem,
    logs_dir: Path,
) -> str:
    diff = run_git_command(["git", "diff", "--binary"])
    diff_stat = run_git_command(["git", "diff", "--stat"])
    name_status = run_git_command(["git", "diff", "--name-status"])
    result_md = logs_dir / "fix_result.md"
    return f"""Fix only issues listed in the review result.

Active plan file: {plan_file}
Selected item id: {selected_item.id}
Selected item title: {selected_item.title}
Selected item type: {selected_item.type}
Logs dir: {logs_dir}

Review result paths:
- {logs_dir / "review_result.json"}
- {logs_dir / "review_result.md"}

Implementation artifacts:
- {logs_dir / "codex_prompt.txt"}
- {logs_dir / "codex_stdout.txt"}
- {logs_dir / "codex_stderr.txt"}
- {logs_dir / "harness_checks.json"}
- {logs_dir / "selection_before.json"}
- {logs_dir / "selection_after.json"}
- {logs_dir / "plan_before.md"}
- {logs_dir / "plan_after.md"}
- {logs_dir / "implementation_git_status_before.txt"}
- {logs_dir / "implementation_git_diff_before.patch"}
- {logs_dir / "implementation_git_diff_stat_before.txt"}
- {logs_dir / "implementation_git_status_after.txt"}
- {logs_dir / "implementation_git_diff_after.patch"}
- {logs_dir / "implementation_git_diff_stat_after.txt"}
- {logs_dir / "implementation_git_name_status_after.txt"}

Rules:
- Fix only issues listed in the review result.
- Do not broaden the selected plan item.
- Do not start the next phase/pass.
- Do not refactor unrelated code.
- Do not update unrelated plan sections.
- Do not commit.
- Do not push.
- Do not use gh.
- Do not create branches.
- Do not run interactive Codex/TUI.
- If the review issue is not safely fixable, write a short explanation to {result_md} and exit without making risky changes.

The active plan may be updated only if the review issue specifically concerns the selected item's plan update, status note, verification note, or behavior note.
Do not mark a new phase/pass complete.

Current git diff stat:
```text
{diff_stat.stdout}
```

Current git name-status:
```text
{name_status.stdout}
```

Current git diff:
```diff
{diff.stdout}
```

Write a human-readable fix report to {result_md} with:
- summary of what was fixed
- files changed
- any issues intentionally left unresolved
- verification commands run, if any
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


def run_git_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


def write_git_command_output(path: Path, command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = run_git_command(command)
    text = completed.stdout
    if completed.returncode != 0 and completed.stderr:
        text += completed.stderr
    write_command_output(path, text)
    return completed


def write_implementation_git_snapshots(logs_dir: Path, suffix: str) -> None:
    write_git_command_output(
        logs_dir / f"implementation_git_status_{suffix}.txt",
        ["git", "status", "--porcelain"],
    )
    write_git_command_output(
        logs_dir / f"implementation_git_diff_{suffix}.patch",
        ["git", "diff", "--binary"],
    )
    write_git_command_output(
        logs_dir / f"implementation_git_diff_stat_{suffix}.txt",
        ["git", "diff", "--stat"],
    )
    if suffix == "after":
        write_git_command_output(
            logs_dir / "implementation_git_name_status_after.txt",
            ["git", "diff", "--name-status"],
        )


def write_fix_git_snapshots(logs_dir: Path, suffix: str) -> None:
    write_git_command_output(
        logs_dir / f"fix_git_status_{suffix}.txt",
        ["git", "status", "--porcelain"],
    )
    write_git_command_output(
        logs_dir / f"fix_git_diff_stat_{suffix}.txt",
        ["git", "diff", "--stat"],
    )
    if suffix == "after":
        write_git_command_output(
            logs_dir / "fix_git_name_status_after.txt",
            ["git", "diff", "--name-status"],
        )


def git_worktree_fingerprint() -> tuple[str, str, str]:
    status = run_git_command(["git", "status", "--porcelain"])
    diff = run_git_command(["git", "diff", "--binary"])
    status_text = status.stdout
    diff_text = diff.stdout
    digest = hashlib.sha256()
    digest.update(status_text.encode("utf-8"))
    digest.update(b"\0")
    digest.update(diff_text.encode("utf-8"))
    return digest.hexdigest(), status_text, diff_text


def write_review_fingerprint(path: Path, fingerprint: str) -> None:
    write_command_output(path, fingerprint + "\n")


def resolve_git_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def local_git_exclude_path() -> Path | None:
    top_level = run_git_command(["git", "rev-parse", "--show-toplevel"])
    if top_level.returncode != 0 or not top_level.stdout.strip():
        return None

    exclude_path = run_git_command(["git", "rev-parse", "--git-path", "info/exclude"])
    if exclude_path.returncode != 0 or not exclude_path.stdout.strip():
        return None

    return resolve_git_path(exclude_path.stdout.strip())


def warn_if_transient_paths_tracked() -> None:
    tracked = run_git_command(["git", "ls-files", ".agent-runs", "agent_loop_sandbox"])
    if tracked.returncode == 0 and tracked.stdout.strip():
        print(
            "Warning: executor transient paths are already tracked by git. "
            "Ignoring will not untrack existing files.",
            file=sys.stderr,
        )


def ensure_local_git_exclude(verbose: bool) -> None:
    exclude_path = local_git_exclude_path()
    if exclude_path is None:
        return

    try:
        if exclude_path.exists():
            existing_text = exclude_path.read_text(encoding="utf-8")
        else:
            existing_text = ""
    except OSError as exc:
        raise PlanError(f"{exclude_path}: failed to read local git exclude: {exc}") from exc

    existing_patterns = {
        line.strip()
        for line in existing_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing_patterns = [
        pattern for pattern in LOCAL_GIT_EXCLUDE_PATTERNS if pattern not in existing_patterns
    ]

    if not missing_patterns:
        if verbose:
            print(
                "Local git exclude already contains plan executor transient patterns: "
                f"{exclude_path}",
                file=sys.stderr,
            )
        warn_if_transient_paths_tracked()
        return

    lines_to_append = [LOCAL_GIT_EXCLUDE_COMMENT, *missing_patterns]
    separator = "" if not existing_text or existing_text.endswith("\n") else "\n"
    appended_text = separator + "\n".join(lines_to_append) + "\n"

    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        with exclude_path.open("a", encoding="utf-8") as handle:
            handle.write(appended_text)
    except OSError as exc:
        raise PlanError(f"{exclude_path}: failed to update local git exclude: {exc}") from exc

    print(
        "Updated local git exclude with plan executor transient patterns: "
        f"{exclude_path}",
        file=sys.stderr,
    )
    warn_if_transient_paths_tracked()


def require_clean_worktree_for_commits() -> None:
    completed = run_git_command(["git", "status", "--porcelain"])
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PlanError(f"failed to inspect git worktree before commit: {detail}")
    if completed.stdout.strip():
        raise PlanError(
            "Cannot use --commit-after-pass with a dirty worktree.\n"
            "Commit, stash, or clean existing changes first."
        )
    print("Commit preflight: git worktree is clean.")


def selected_item_status_after(
    plan_state_after: PlanState,
    selected_before: Selection,
) -> str | None:
    if selected_before.item is None:
        return None
    item = plan_state_after.items_by_id.get(selected_before.item.id)
    return item.status if item is not None else None


def validate_post_fix_plan_scope(
    before_fix: PlanState,
    after_fix: PlanState,
    selected_before: Selection,
) -> str | None:
    if selected_before.item is None:
        return "fix_broadened_scope"
    selected_id = selected_before.item.id
    before_selected = before_fix.items_by_id.get(selected_id)
    after_selected = after_fix.items_by_id.get(selected_id)
    if before_selected is None or after_selected is None:
        return "fix_invalid_selected_status"
    if after_selected.status not in DONE_STATUSES:
        return "fix_invalid_selected_status"

    if (
        before_selected.id != after_selected.id
        or before_selected.title != after_selected.title
        or before_selected.type != after_selected.type
        or before_selected.parent != after_selected.parent
    ):
        return "fix_broadened_scope"

    before_children = {
        child.id for child in before_fix.children_by_parent.get(selected_id, [])
    }
    after_children = {
        child.id for child in after_fix.children_by_parent.get(selected_id, [])
    }
    if after_children - before_children:
        return "fix_broadened_scope"

    for item_id, before_item in before_fix.items_by_id.items():
        if item_id == selected_id:
            continue
        after_item = after_fix.items_by_id.get(item_id)
        if after_item is None:
            return "fix_broadened_scope"
        if before_item.status not in DONE_STATUSES and after_item.status in DONE_STATUSES:
            return "fix_broadened_scope"

    return None


def commit_subject(prefix: str, selected_item: PlanItem) -> str:
    return f"{prefix}: complete {selected_item.id} - {selected_item.title}"


def commit_body(
    plan_file: Path,
    selected_item: PlanItem,
    status_after: str,
    logs_dir: Path,
    review: ReviewResult | None,
    fix: FixResult | None,
    review_after_fix: ReviewResult | None,
) -> str:
    lines = [
        "Automated plan executor pass.",
        "",
        f"Plan: {plan_file}",
        f"Selected item: {selected_item.id} - {selected_item.title}",
        f"Status after: {status_after}",
        f"Logs: {logs_dir}",
    ]
    review = review or ReviewResult.not_requested()
    fix = fix or FixResult.not_requested()
    review_after_fix = review_after_fix or ReviewResult.not_requested()
    lines.extend(
        [
            f"Review requested: {review.requested}",
            f"Review verdict: {review.verdict}",
            f"Review summary: {review.summary}",
            f"Review result JSON: {review.result_json_path}",
            f"Review result MD: {review.result_md_path}",
            f"Fix requested: {fix.requested}",
            f"Fix attempted: {fix.attempted}",
            f"Fix return code: {fix.returncode}",
            f"Fix stop reason: {fix.stop_reason}",
            f"Fix result MD: {fix.result_md_path}",
            f"Review after fix requested: {review_after_fix.requested}",
            f"Review after fix verdict: {review_after_fix.verdict}",
            f"Review after fix summary: {review_after_fix.summary}",
            f"Review after fix result JSON: {review_after_fix.result_json_path}",
            f"Review after fix result MD: {review_after_fix.result_md_path}",
        ]
    )
    return "\n".join(lines)


def write_command_output(path: Path, text: str) -> None:
    write_text_file(path, text)


def attempt_git_commit(
    plan_files: PlanFiles,
    result: ExecutionResult,
    plan_state_after: PlanState,
    commit_prefix: str,
) -> CommitResult:
    if result.selected_before.item is None:
        return CommitResult(
            requested=True,
            skipped_reason="no selected item",
        )

    selected_item = result.selected_before.item
    status_after = selected_item_status_after(plan_state_after, result.selected_before)
    subject = commit_subject(commit_prefix, selected_item)
    logs_dir = result.logs_dir
    for log_name in (
        "git_commit_status_before.txt",
        "git_commit_add_stdout.txt",
        "git_commit_add_stderr.txt",
        "git_commit_status_after_add.txt",
        "git_commit_cached_diff_stat.txt",
        "git_commit_cached_name_status.txt",
        "git_commit_stdout.txt",
        "git_commit_stderr.txt",
        "git_commit_returncode.txt",
    ):
        write_command_output(logs_dir / log_name, "")

    status_before = run_git_command(["git", "status", "--porcelain"])
    write_command_output(logs_dir / "git_commit_status_before.txt", status_before.stdout)
    if status_before.returncode != 0:
        write_command_output(
            logs_dir / "git_commit_stderr.txt",
            status_before.stderr,
        )
        write_command_output(
            logs_dir / "git_commit_returncode.txt",
            f"{status_before.returncode}\n",
        )
        return CommitResult(
            requested=True,
            attempted=True,
            subject=subject,
            skipped_reason="git status failed before commit",
            returncode=status_before.returncode,
        )

    add = run_git_command(["git", "add", "-A"])
    write_command_output(logs_dir / "git_commit_add_stdout.txt", add.stdout)
    write_command_output(logs_dir / "git_commit_add_stderr.txt", add.stderr)
    if add.returncode != 0:
        write_command_output(logs_dir / "git_commit_returncode.txt", f"{add.returncode}\n")
        return CommitResult(
            requested=True,
            attempted=True,
            subject=subject,
            skipped_reason="git add failed",
            returncode=add.returncode,
        )

    status_after_add = run_git_command(["git", "status", "--porcelain"])
    write_command_output(
        logs_dir / "git_commit_status_after_add.txt",
        status_after_add.stdout,
    )
    if status_after_add.returncode != 0:
        write_command_output(
            logs_dir / "git_commit_stderr.txt",
            status_after_add.stderr,
        )
        write_command_output(
            logs_dir / "git_commit_returncode.txt",
            f"{status_after_add.returncode}\n",
        )
        return CommitResult(
            requested=True,
            attempted=True,
            subject=subject,
            skipped_reason="git status failed after add",
            returncode=status_after_add.returncode,
        )

    cached_quiet = run_git_command(["git", "diff", "--cached", "--quiet"])
    if cached_quiet.returncode == 0:
        write_command_output(logs_dir / "git_commit_returncode.txt", "0\n")
        return CommitResult(
            requested=True,
            attempted=True,
            subject=subject,
            skipped_reason=f"No git changes to commit for {selected_item.id}.",
            returncode=0,
        )
    if cached_quiet.returncode != 1:
        write_command_output(
            logs_dir / "git_commit_stderr.txt",
            cached_quiet.stderr,
        )
        write_command_output(
            logs_dir / "git_commit_returncode.txt",
            f"{cached_quiet.returncode}\n",
        )
        return CommitResult(
            requested=True,
            attempted=True,
            subject=subject,
            skipped_reason="git diff --cached --quiet failed",
            returncode=cached_quiet.returncode,
        )

    cached_stat = run_git_command(["git", "diff", "--cached", "--stat"])
    cached_stat_path = logs_dir / "git_commit_cached_diff_stat.txt"
    write_command_output(cached_stat_path, cached_stat.stdout)
    cached_name_status = run_git_command(["git", "diff", "--cached", "--name-status"])
    cached_name_status_path = logs_dir / "git_commit_cached_name_status.txt"
    write_command_output(cached_name_status_path, cached_name_status.stdout)

    if cached_stat.stdout:
        print("Staged diff stat:")
        print(cached_stat.stdout.rstrip())
    if cached_name_status.stdout:
        print("Staged name-status:")
        print(cached_name_status.stdout.rstrip())
    print(f"Staged name-status log: {cached_name_status_path}")

    if cached_stat.returncode != 0:
        write_command_output(logs_dir / "git_commit_returncode.txt", f"{cached_stat.returncode}\n")
        return CommitResult(
            requested=True,
            attempted=True,
            subject=subject,
            skipped_reason="git diff --cached --stat failed",
            returncode=cached_stat.returncode,
            cached_diff_stat_path=cached_stat_path,
            cached_name_status_path=cached_name_status_path,
        )
    if cached_name_status.returncode != 0:
        write_command_output(
            logs_dir / "git_commit_returncode.txt",
            f"{cached_name_status.returncode}\n",
        )
        return CommitResult(
            requested=True,
            attempted=True,
            subject=subject,
            skipped_reason="git diff --cached --name-status failed",
            returncode=cached_name_status.returncode,
            cached_diff_stat_path=cached_stat_path,
            cached_name_status_path=cached_name_status_path,
        )

    body = commit_body(
        plan_files.plan_file,
        selected_item,
        status_after or "",
        logs_dir,
        result.review,
        result.fix,
        result.review_after_fix,
    )
    commit = run_git_command(["git", "commit", "-m", subject, "-m", body])
    write_command_output(logs_dir / "git_commit_stdout.txt", commit.stdout)
    write_command_output(logs_dir / "git_commit_stderr.txt", commit.stderr)
    write_command_output(logs_dir / "git_commit_returncode.txt", f"{commit.returncode}\n")
    if commit.returncode != 0:
        return CommitResult(
            requested=True,
            attempted=True,
            subject=subject,
            skipped_reason="git commit failed",
            returncode=commit.returncode,
            cached_diff_stat_path=cached_stat_path,
            cached_name_status_path=cached_name_status_path,
        )

    rev_parse = run_git_command(["git", "rev-parse", "HEAD"])
    commit_hash = rev_parse.stdout.strip() if rev_parse.returncode == 0 else None
    return CommitResult(
        requested=True,
        attempted=True,
        created=True,
        hash=commit_hash,
        subject=subject,
        returncode=0,
        cached_diff_stat_path=cached_stat_path,
        cached_name_status_path=cached_name_status_path,
    )


def commit_result_failed(commit: CommitResult | None) -> bool:
    return commit is not None and commit.returncode not in (None, 0)


def commit_not_eligible(reason: str) -> CommitResult:
    return CommitResult(
        requested=True,
        attempted=False,
        skipped_reason=reason,
    )


def apply_commit_if_eligible(
    plan_files: PlanFiles,
    result: ExecutionResult,
    plan_state_after: PlanState,
    commit_requested: bool,
    commit_prefix: str,
) -> ExecutionResult:
    if not commit_requested:
        return replace(result, commit=CommitResult.not_requested())

    if result.codex_returncode != 0:
        return replace(result, commit=commit_not_eligible("Codex failed"))

    if not harness_checks_passed(result):
        return replace(result, commit=commit_not_eligible("harness failed"))

    if result.review is not None and result.review.requested:
        review_passed_directly = (
            result.review.verdict == "pass" and result.review.stop_reason is None
        )
        review_passed_after_fix = (
            result.review.verdict == "needs_fix"
            and result.fix is not None
            and result.fix.requested
            and result.fix.attempted
            and result.fix.stop_reason is None
            and result.fix.harness_checks_passed is True
            and result.review_after_fix is not None
            and result.review_after_fix.verdict == "pass"
            and result.review_after_fix.stop_reason is None
        )
        if not (review_passed_directly or review_passed_after_fix):
            return replace(result, commit=commit_not_eligible("review did not pass"))

    status_after = selected_item_status_after(plan_state_after, result.selected_before)
    if status_after not in DONE_STATUSES:
        return replace(result, commit=commit_not_eligible("selected item not completed"))

    commit = attempt_git_commit(plan_files, result, plan_state_after, commit_prefix)
    return replace(result, commit=commit)


def stream_pipe(pipe: Any, prefix: str, sink: Any, collected: list[str]) -> None:
    for line in pipe:
        collected.append(line)
        print(f"{prefix}{line}", end="", file=sink)


def run_codex_exec(
    codex_bin: str,
    prompt: str,
    stdout_prefix: str = "[codex] ",
    stderr_prefix: str = "[codex:stderr] ",
) -> tuple[int, str, str]:
    try:
        process = subprocess.Popen(
            [codex_bin, "exec", prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError:
        raise

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(
        target=stream_pipe,
        args=(process.stdout, stdout_prefix, sys.stdout, stdout_lines),
    )
    stderr_thread = threading.Thread(
        target=stream_pipe,
        args=(process.stderr, stderr_prefix, sys.stderr, stderr_lines),
    )
    stdout_thread.start()
    stderr_thread.start()
    returncode = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    return returncode, "".join(stdout_lines), "".join(stderr_lines)


def validate_review_result(raw: Any, path: Path) -> tuple[str, str]:
    if not isinstance(raw, dict):
        raise PlanError(f"{path}: review result must be a JSON object")
    verdict = raw.get("verdict")
    if verdict not in REVIEW_VERDICTS:
        raise PlanError(f"{path}: review verdict must be one of pass, needs_fix, needs_human")
    summary = raw.get("summary")
    if not isinstance(summary, str):
        raise PlanError(f"{path}: review summary must be a string")
    issues = raw.get("issues")
    if not isinstance(issues, list):
        raise PlanError(f"{path}: review issues must be a list")
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            raise PlanError(f"{path}: review issue {index} must be an object")
        if issue.get("severity") not in REVIEW_ISSUE_SEVERITIES:
            raise PlanError(
                f"{path}: review issue {index} severity must be blocker, major, or minor"
            )
        if not isinstance(issue.get("file"), str):
            raise PlanError(f"{path}: review issue {index} file must be a string")
        if not isinstance(issue.get("reason"), str):
            raise PlanError(f"{path}: review issue {index} reason must be a string")
        suggested_fix = issue.get("suggested_fix")
        if suggested_fix is not None and not isinstance(suggested_fix, str):
            raise PlanError(
                f"{path}: review issue {index} suggested_fix must be a string or null"
            )
    return verdict, summary


def parse_review_result_json(path: Path) -> tuple[str, str]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"{path}: missing review_result.json: {exc}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PlanError(
            f"{path}: invalid review JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    return validate_review_result(raw, path)


def run_review_after_pass(
    plan_files: PlanFiles,
    result: ExecutionResult,
    plan_state_after: PlanState,
    codex_bin: str,
    *,
    after_fix: bool = False,
) -> ExecutionResult:
    selected_item = result.selected_before.item
    if selected_item is None:
        review = ReviewResult.not_attempted("no selected item")
        return replace(result, review_after_fix=review) if after_fix else replace(result, review=review)

    status_after = selected_item_status_after(plan_state_after, result.selected_before)
    if status_after not in DONE_STATUSES:
        review = ReviewResult.not_attempted("selected item not completed")
        return replace(result, review_after_fix=review) if after_fix else replace(result, review=review)

    logs_dir = result.logs_dir
    prefix = "review_after_fix" if after_fix else "review"
    result_json_path = logs_dir / f"{prefix}_result.json"
    result_md_path = logs_dir / f"{prefix}_result.md"
    prompt = build_review_prompt(
        plan_files.plan_file,
        selected_item,
        status_after,
        logs_dir,
        after_fix=after_fix,
    )
    write_text_file(logs_dir / f"{prefix}_prompt.txt", prompt)

    fingerprint_before, status_before, _diff_before = git_worktree_fingerprint()
    write_command_output(logs_dir / f"{prefix}_git_status_before.txt", status_before)
    write_review_fingerprint(
        logs_dir / f"{prefix}_git_diff_fingerprint_before.txt",
        fingerprint_before,
    )

    try:
        returncode, stdout, stderr = run_codex_exec(
            codex_bin,
            prompt,
            stdout_prefix="[review] ",
            stderr_prefix="[review:stderr] ",
        )
    except OSError as exc:
        write_text_file(logs_dir / f"{prefix}_error.txt", str(exc) + "\n")
        fingerprint_after, status_after_text, _diff_after = git_worktree_fingerprint()
        write_command_output(logs_dir / f"{prefix}_git_status_after.txt", status_after_text)
        write_review_fingerprint(
            logs_dir / f"{prefix}_git_diff_fingerprint_after.txt",
            fingerprint_after,
        )
        review = ReviewResult(
            requested=True,
            attempted=True,
            result_json_path=result_json_path,
            result_md_path=result_md_path,
            stop_reason="review_launch_failed",
        )
        return replace(result, review_after_fix=review) if after_fix else replace(result, review=review)

    write_text_file(logs_dir / f"{prefix}_stdout.txt", stdout)
    write_text_file(logs_dir / f"{prefix}_stderr.txt", stderr)
    write_text_file(logs_dir / f"{prefix}_returncode.txt", f"{returncode}\n")

    fingerprint_after, status_after_text, _diff_after = git_worktree_fingerprint()
    write_command_output(logs_dir / f"{prefix}_git_status_after.txt", status_after_text)
    write_review_fingerprint(
        logs_dir / f"{prefix}_git_diff_fingerprint_after.txt",
        fingerprint_after,
    )

    if fingerprint_after != fingerprint_before:
        review = ReviewResult(
            requested=True,
            attempted=True,
            returncode=returncode,
            result_json_path=result_json_path,
            result_md_path=result_md_path,
            stop_reason="review_modified_worktree",
        )
        return replace(result, review_after_fix=review) if after_fix else replace(result, review=review)

    if returncode != 0:
        review = ReviewResult(
            requested=True,
            attempted=True,
            returncode=returncode,
            result_json_path=result_json_path,
            result_md_path=result_md_path,
            stop_reason="review_codex_failed",
        )
        return replace(result, review_after_fix=review) if after_fix else replace(result, review=review)

    try:
        verdict, summary = parse_review_result_json(result_json_path)
    except PlanError as exc:
        write_text_file(logs_dir / f"{prefix}_parse_error.txt", str(exc) + "\n")
        review = ReviewResult(
            requested=True,
            attempted=True,
            returncode=returncode,
            result_json_path=result_json_path,
            result_md_path=result_md_path,
            stop_reason="review_invalid_json",
        )
        return replace(result, review_after_fix=review) if after_fix else replace(result, review=review)

    if not result_md_path.exists():
        review = ReviewResult(
            requested=True,
            attempted=True,
            returncode=returncode,
            verdict=verdict,
            summary=summary,
            result_json_path=result_json_path,
            result_md_path=result_md_path,
            stop_reason="review_missing_markdown",
        )
        return replace(result, review_after_fix=review) if after_fix else replace(result, review=review)

    stop_reason = None
    if verdict != "pass":
        if after_fix and verdict == "needs_fix":
            stop_reason = "fix_incomplete"
        elif after_fix and verdict == "needs_human":
            stop_reason = "review_after_fix_needs_human"
        else:
            stop_reason = f"review_{verdict}"
    review = ReviewResult(
        requested=True,
        attempted=True,
        returncode=returncode,
        verdict=verdict,
        summary=summary,
        result_json_path=result_json_path,
        result_md_path=result_md_path,
        stop_reason=stop_reason,
    )
    return replace(result, review_after_fix=review) if after_fix else replace(result, review=review)


def run_fix_after_review(
    plan_files: PlanFiles,
    result: ExecutionResult,
    plan_state_after: PlanState,
    include_parents: bool,
    codex_bin: str,
) -> tuple[ExecutionResult, PlanState]:
    selected_item = result.selected_before.item
    if selected_item is None:
        return replace(result, fix=FixResult.not_attempted("no selected item")), plan_state_after

    logs_dir = result.logs_dir
    result_md_path = logs_dir / "fix_result.md"
    copy_plan_backup(plan_files.plan_file, logs_dir / "plan_before_fix.md")
    before_fix_state = load_plan_state_from_file(logs_dir / "plan_before_fix.md")
    write_fix_git_snapshots(logs_dir, "before")
    prompt = build_fix_prompt(plan_files.plan_file, selected_item, logs_dir)
    write_text_file(logs_dir / "fix_prompt.txt", prompt)

    try:
        fix_returncode, stdout, stderr = run_codex_exec(
            codex_bin,
            prompt,
            stdout_prefix="[fix] ",
            stderr_prefix="[fix:stderr] ",
        )
    except OSError as exc:
        write_text_file(logs_dir / "fix_error.txt", str(exc) + "\n")
        write_fix_git_snapshots(logs_dir, "after")
        fix = FixResult(
            requested=True,
            attempted=True,
            result_md_path=result_md_path,
            stop_reason="fix_failed",
        )
        return replace(result, fix=fix), plan_state_after

    write_text_file(logs_dir / "fix_stdout.txt", stdout)
    write_text_file(logs_dir / "fix_stderr.txt", stderr)
    write_text_file(logs_dir / "fix_returncode.txt", f"{fix_returncode}\n")
    write_fix_git_snapshots(logs_dir, "after")

    if fix_returncode != 0:
        fix = FixResult(
            requested=True,
            attempted=True,
            returncode=fix_returncode,
            result_md_path=result_md_path,
            stop_reason="fix_failed",
        )
        return replace(result, fix=fix), plan_state_after

    copy_plan_backup(plan_files.plan_file, logs_dir / "plan_after_fix.md")
    try:
        after_fix_state = load_plan_state_from_file(plan_files.plan_file)
    except PlanError as exc:
        write_text_file(logs_dir / "fix_plan_parse_error.txt", str(exc) + "\n")
        fix = FixResult(
            requested=True,
            attempted=True,
            returncode=fix_returncode,
            result_md_path=result_md_path,
            stop_reason="fix_plan_parse_failed",
        )
        return replace(result, fix=fix), plan_state_after

    scope_stop_reason = validate_post_fix_plan_scope(
        before_fix_state,
        after_fix_state,
        result.selected_before,
    )
    if scope_stop_reason is not None:
        fix = FixResult(
            requested=True,
            attempted=True,
            returncode=fix_returncode,
            result_md_path=result_md_path,
            stop_reason=scope_stop_reason,
        )
        return replace(result, fix=fix), after_fix_state

    selected_after = select_next_item(after_fix_state, include_parents=include_parents)
    harness_checks = run_harness_checks()
    write_json_file(
        logs_dir / "fix_harness_checks.json",
        [check.to_json_obj() for check in harness_checks],
    )
    checks_passed = all(check.returncode == 0 for check in harness_checks)
    fix = FixResult(
        requested=True,
        attempted=True,
        returncode=fix_returncode,
        result_md_path=result_md_path,
        harness_checks=harness_checks,
        harness_checks_passed=checks_passed,
        stop_reason=None if checks_passed else "fix_checks_failed",
    )
    return replace(result, fix=fix, selected_after=selected_after), after_fix_state


def maybe_fix_and_rereview(
    plan_files: PlanFiles,
    result: ExecutionResult,
    plan_state_after: PlanState,
    include_parents: bool,
    codex_bin: str,
    fix_after_review: bool,
) -> tuple[ExecutionResult, PlanState]:
    review = result.review
    if not fix_after_review:
        return replace(result, fix=FixResult.not_requested()), plan_state_after
    if review is None or not review.requested:
        return replace(result, fix=FixResult.not_attempted()), plan_state_after
    if review.verdict != "needs_fix" or review.stop_reason != "review_needs_fix":
        return replace(result, fix=FixResult.not_attempted()), plan_state_after

    result, plan_state_after = run_fix_after_review(
        plan_files,
        result,
        plan_state_after,
        include_parents=include_parents,
        codex_bin=codex_bin,
    )
    if result.fix is not None and result.fix.failed():
        return result, plan_state_after

    result = run_review_after_pass(
        plan_files,
        result,
        plan_state_after,
        codex_bin=codex_bin,
        after_fix=True,
    )
    return result, plan_state_after


def execute_next(
    plan_files: PlanFiles,
    selected_before: Selection,
    include_parents: bool,
    codex_bin: str,
    logs_dir: Path | None = None,
) -> ExecutionResult:
    if selected_before.item is None:
        raise PlanError("cannot execute next item because the plan is complete")
    if plan_files.run_dir is None:
        raise PlanError("execution requires a run directory")

    if logs_dir is None:
        logs_dir = per_pass_logs_dir(plan_files.run_dir, selected_before.item.id)
    try:
        logs_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise PlanError(f"{logs_dir}: failed to create logs directory: {exc}") from exc

    plan_backup_before = logs_dir / "plan_before.md"
    plan_backup_after = logs_dir / "plan_after.md"
    copy_plan_backup(plan_files.plan_file, plan_backup_before)
    write_implementation_git_snapshots(logs_dir, "before")

    prompt = build_codex_prompt(plan_files.plan_file, selected_before.item.id)
    write_text_file(logs_dir / "codex_prompt.txt", prompt)
    write_json_file(logs_dir / "selection_before.json", selection_to_json_obj(selected_before))

    try:
        codex_returncode, codex_stdout, codex_stderr = run_codex_exec(codex_bin, prompt)
    except OSError as exc:
        write_text_file(logs_dir / "codex_error.txt", str(exc) + "\n")
        copy_plan_backup(plan_files.plan_file, plan_backup_after)
        raise PlanError(f"failed to run {codex_bin!r}: {exc}") from exc

    write_text_file(logs_dir / "codex_stdout.txt", codex_stdout)
    write_text_file(logs_dir / "codex_stderr.txt", codex_stderr)
    write_text_file(logs_dir / "codex_returncode.txt", f"{codex_returncode}\n")
    copy_plan_backup(plan_files.plan_file, plan_backup_after)
    write_implementation_git_snapshots(logs_dir, "after")

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
        codex_returncode=codex_returncode,
        logs_dir=logs_dir,
        plan_backup_before=plan_backup_before,
        plan_backup_after=plan_backup_after,
        harness_checks=harness_checks,
        prompt=prompt,
        same_selection_warning=same_selection_warning,
        review=ReviewResult.not_requested(),
        fix=FixResult.not_requested(),
        review_after_fix=ReviewResult.not_requested(),
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


def print_commit_result(commit: CommitResult | None) -> None:
    commit = commit or CommitResult.not_requested()
    print("Git commit:")
    print(f"Requested: {commit.requested}")
    if not commit.requested:
        return
    print(f"Attempted: {commit.attempted}")
    if commit.subject is not None:
        print(f"Subject: {commit.subject}")
    if commit.created:
        print("Created: True")
        print(f"Commit hash: {commit.hash}")
    else:
        print("Created: False")
    if commit.skipped_reason is not None:
        print(f"Skipped reason: {commit.skipped_reason}")
    if commit.cached_name_status_path is not None:
        print(f"Staged name-status log: {commit.cached_name_status_path}")


def print_review_result(review: ReviewResult | None) -> None:
    review = review or ReviewResult.not_requested()
    print("Review:")
    print(f"Requested: {review.requested}")
    if not review.requested:
        return
    print(f"Attempted: {review.attempted}")
    if review.returncode is not None:
        print(f"Return code: {review.returncode}")
    if review.verdict is not None:
        print(f"Verdict: {review.verdict}")
    if review.summary is not None:
        print(f"Summary: {review.summary}")
    if review.result_md_path is not None:
        print(f"Review result: {review.result_md_path}")
    if review.stop_reason is not None:
        print(f"Stop reason: {review.stop_reason}")


def print_fix_result(fix: FixResult | None) -> None:
    fix = fix or FixResult.not_requested()
    print("Fix:")
    print(f"Requested: {fix.requested}")
    if not fix.requested:
        return
    print(f"Attempted: {fix.attempted}")
    if fix.returncode is not None:
        print(f"Return code: {fix.returncode}")
    if fix.result_md_path is not None:
        print(f"Result: {fix.result_md_path}")
    if fix.harness_checks_passed is not None:
        print(f"Harness checks passed: {fix.harness_checks_passed}")
    print(f"Stop reason: {fix.stop_reason}")


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
    print()
    print_review_result(result.review)
    print()
    print_fix_result(result.fix)
    print()
    if result.review_after_fix is not None and result.review_after_fix.requested:
        print("Review after fix:")
        print_review_result(result.review_after_fix)
        print()
    print_commit_result(result.commit)


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
    output.update((result.review or ReviewResult.not_requested()).to_json_obj())
    output.update((result.fix or FixResult.not_requested()).to_json_obj())
    output.update(
        (result.review_after_fix or ReviewResult.not_requested()).to_json_obj(
            "review_after_fix"
        )
    )
    output.update((result.commit or CommitResult.not_requested()).to_json_obj())
    if result.same_selection_warning is not None:
        output["warning"] = result.same_selection_warning
    return output


def command_exit_code(result: ExecutionResult) -> int:
    if result.review is not None and result.review.failed():
        recoverable_needs_fix = (
            result.review.verdict == "needs_fix"
            and result.fix is not None
            and result.fix.requested
        )
        if not recoverable_needs_fix:
            if result.review.returncode not in (None, 0):
                return result.review.returncode
            return 1
    if result.fix is not None and result.fix.failed():
        if result.fix.returncode not in (None, 0):
            return result.fix.returncode
        return 1
    if result.review_after_fix is not None and result.review_after_fix.failed():
        if result.review_after_fix.returncode not in (None, 0):
            return result.review_after_fix.returncode
        return 1
    if commit_result_failed(result.commit):
        return result.commit.returncode or 1
    for check in result.harness_checks:
        if check.returncode != 0:
            return check.returncode
    return result.codex_returncode


def harness_checks_passed(result: ExecutionResult) -> bool:
    return all(check.returncode == 0 for check in result.harness_checks)


def item_json(selection: Selection) -> dict[str, str] | None:
    return selection.item.to_json_obj() if selection.item is not None else None


def write_run_all_summary(run_dir: Path, records: list[RunAllRecord], stop_reason: str) -> None:
    summary = {
        "stop_reason": stop_reason,
        "passes": [record.to_json_obj() for record in records],
    }
    write_json_file(run_dir / "run_all_summary.json", summary)
    lines = ["Run-all summary:"]
    for record in records:
        commit = record.commit or CommitResult.not_requested()
        review = record.review or ReviewResult.not_requested()
        fix = record.fix or FixResult.not_requested()
        review_after_fix = record.review_after_fix or ReviewResult.not_requested()
        line = f"- {record.selected_id}: {record.status}"
        if review.requested:
            if review.verdict:
                line += f" review={review.verdict}"
            elif review.stop_reason:
                line += f" review_stop={review.stop_reason}"
        if fix.requested:
            if fix.attempted and fix.stop_reason is None:
                line += " fix=passed"
            elif fix.stop_reason:
                line += f" fix_stop={fix.stop_reason}"
        if review_after_fix.requested:
            if review_after_fix.verdict:
                line += f" rereview={review_after_fix.verdict}"
            elif review_after_fix.stop_reason:
                line += f" rereview_stop={review_after_fix.stop_reason}"
        if commit.requested:
            if commit.created:
                line += f" commit={commit.hash}"
            elif commit.skipped_reason:
                line += f" commit_skipped={commit.skipped_reason}"
        lines.append(line)
    lines.append(f"Stopped: {stop_reason}")
    write_text_file(run_dir / "run_all_summary.txt", "\n".join(lines) + "\n")


def print_run_all_summary(records: list[RunAllRecord], stop_reason: str) -> None:
    print("Run-all summary:")
    for record in records:
        commit = record.commit or CommitResult.not_requested()
        review = record.review or ReviewResult.not_requested()
        fix = record.fix or FixResult.not_requested()
        review_after_fix = record.review_after_fix or ReviewResult.not_requested()
        line = f"- {record.selected_id}: {record.status}"
        if review.requested:
            if review.verdict:
                line += f" review={review.verdict}"
            elif review.stop_reason:
                line += f" review_stop={review.stop_reason}"
        if fix.requested:
            if fix.attempted and fix.stop_reason is None:
                line += " fix=passed"
            elif fix.stop_reason:
                line += f" fix_stop={fix.stop_reason}"
        if review_after_fix.requested:
            if review_after_fix.verdict:
                line += f" rereview={review_after_fix.verdict}"
            elif review_after_fix.stop_reason:
                line += f" rereview_stop={review_after_fix.stop_reason}"
        if commit.requested:
            if commit.created:
                line += f" commit={commit.hash}"
            elif commit.skipped_reason:
                line += f" commit_skipped={commit.skipped_reason}"
        print(line)
    print(f"Stopped: {stop_reason}")


def print_run_all_banner(
    pass_number: int,
    max_passes: int,
    selection: Selection,
    plan_file: Path,
    logs_dir: Path,
) -> None:
    print("=" * 80)
    print(f"Run-all pass {pass_number}/{max_passes}")
    if selection.item is not None:
        print(f"Selected: {selection.item.id} - {selection.item.title}")
    print(f"Active plan: {plan_file}")
    print(f"Logs: {logs_dir}")
    print("=" * 80)


def children_count(plan_state: PlanState, item_id: str) -> int:
    return len(plan_state.children_by_parent.get(item_id, []))


def classify_run_all_result(
    selected_before: Selection,
    plan_state_before: PlanState,
    selected_after: Selection,
    plan_state_after: PlanState,
) -> tuple[str, str]:
    if selected_before.item is None:
        return "parse_failed", "parse failed"

    selected_after_item = plan_state_after.items_by_id.get(selected_before.item.id)
    if selected_after_item is None:
        return "selected_item_not_completed", "selected item not completed"

    if selected_after_item.status in DONE_STATUSES:
        return "passed", ""

    before_children = children_count(plan_state_before, selected_before.item.id)
    after_children = children_count(plan_state_after, selected_before.item.id)
    if after_children > before_children:
        return "plan_expanded_needs_review", "plan expanded needs review"

    if selected_after_item.status in {"Blocked", "Partial", "Planned", "In Progress"}:
        return "selected_item_not_completed", "selected item not completed"

    if (
        selected_after.item is not None
        and selected_after.item.id == selected_before.item.id
    ):
        return "no_progress", "no progress"

    return "selected_item_not_completed", "selected item not completed"


def run_all(
    plan_files: PlanFiles,
    max_passes: int,
    include_parents: bool,
    codex_bin: str,
    verbose: bool,
    review_after_pass: bool,
    fix_after_review: bool,
    commit_after_pass: bool,
    commit_prefix: str,
) -> int:
    records: list[RunAllRecord] = []

    for pass_number in range(1, max_passes + 1):
        try:
            plan_state_before = load_plan_state_from_file(plan_files.plan_file)
        except PlanError:
            stop_reason = "parse failed"
            write_run_all_summary(plan_files.run_dir, records, stop_reason)
            print_run_all_summary(records, stop_reason)
            raise

        selection_before = select_next_item(plan_state_before, include_parents=include_parents)
        if selection_before.item is None:
            stop_reason = "plan complete"
            print("Plan complete: no unfinished items remain.")
            write_run_all_summary(plan_files.run_dir, records, stop_reason)
            print_run_all_summary(records, stop_reason)
            return 0

        logs_dir = per_pass_logs_dir(plan_files.run_dir, selection_before.item.id)
        print_run_all_banner(
            pass_number,
            max_passes,
            selection_before,
            plan_files.plan_file,
            logs_dir,
        )

        try:
            result = execute_next(
                plan_files,
                selection_before,
                include_parents=include_parents,
                codex_bin=codex_bin,
                logs_dir=logs_dir,
            )
            plan_state_after = load_plan_state_from_file(plan_files.plan_file)
        except PlanError:
            record = RunAllRecord(
                selected_id=selection_before.item.id,
                selected_title=selection_before.item.title,
                status="parse_failed",
                logs_dir=str(logs_dir),
                review=ReviewResult.not_attempted("parse failed") if review_after_pass else None,
                fix=FixResult.not_attempted() if fix_after_review else None,
                commit=commit_not_eligible("parse failed") if commit_after_pass else None,
            )
            records.append(record)
            stop_reason = "parse failed"
            write_run_all_summary(plan_files.run_dir, records, stop_reason)
            print_run_all_summary(records, stop_reason)
            raise

        selected_after_item = item_json(result.selected_after)
        record = RunAllRecord(
            selected_id=selection_before.item.id,
            selected_title=selection_before.item.title,
            status="passed",
            logs_dir=str(result.logs_dir),
            codex_returncode=result.codex_returncode,
            selected_after_id=selected_after_item["id"] if selected_after_item else None,
            selected_after_title=selected_after_item["title"] if selected_after_item else None,
            selected_item_post_status=plan_state_after.items_by_id.get(
                selection_before.item.id,
                selection_before.item,
            ).status,
            review=ReviewResult.not_requested(),
            fix=FixResult.not_requested(),
            review_after_fix=ReviewResult.not_requested(),
            commit=CommitResult.not_requested(),
        )
        if review_after_pass:
            record.review = ReviewResult.not_attempted("pass not review-eligible")
        if fix_after_review:
            record.fix = FixResult.not_attempted()
        if commit_after_pass:
            record.commit = commit_not_eligible("pass not commit-eligible")

        if result.codex_returncode != 0:
            record.status = "codex_failed"
            if review_after_pass:
                record.review = ReviewResult.not_attempted("Codex failed")
            if commit_after_pass:
                record.commit = commit_not_eligible("Codex failed")
            records.append(record)
            stop_reason = "Codex failed"
            write_run_all_summary(plan_files.run_dir, records, stop_reason)
            print(f"Codex failed with return code {result.codex_returncode}.")
            print_run_all_summary(records, stop_reason)
            return result.codex_returncode or 1

        if not harness_checks_passed(result):
            record.status = "harness_failed"
            if review_after_pass:
                record.review = ReviewResult.not_attempted("harness failed")
            if commit_after_pass:
                record.commit = commit_not_eligible("harness failed")
            records.append(record)
            stop_reason = "harness failed"
            write_run_all_summary(plan_files.run_dir, records, stop_reason)
            print("Harness checks failed.")
            print_run_all_summary(records, stop_reason)
            return 1

        record.status, stop_reason = classify_run_all_result(
            selection_before,
            plan_state_before,
            result.selected_after,
            plan_state_after,
        )

        if record.status != "passed":
            if review_after_pass:
                record.review = ReviewResult.not_attempted(stop_reason)
            if commit_after_pass:
                record.commit = commit_not_eligible(stop_reason)
            records.append(record)
            write_run_all_summary(plan_files.run_dir, records, stop_reason)
            print(f"Run-all stopped: {stop_reason}.")
            print_run_all_summary(records, stop_reason)
            return 1

        if review_after_pass:
            result = run_review_after_pass(
                plan_files,
                result,
                plan_state_after,
                codex_bin=codex_bin,
            )
            record.review = result.review
            if result.review is not None and result.review.failed():
                if (
                    fix_after_review
                    and result.review.verdict == "needs_fix"
                    and result.review.stop_reason == "review_needs_fix"
                ):
                    result, plan_state_after = maybe_fix_and_rereview(
                        plan_files,
                        result,
                        plan_state_after,
                        include_parents=include_parents,
                        codex_bin=codex_bin,
                        fix_after_review=fix_after_review,
                    )
                    record.fix = result.fix
                    record.review_after_fix = result.review_after_fix
                    if result.fix is not None and result.fix.failed():
                        record.status = result.fix.stop_reason or "fix_failed"
                        if commit_after_pass:
                            record.commit = commit_not_eligible("fix did not pass")
                        records.append(record)
                        stop_reason = result.fix.stop_reason or "fix failed"
                        write_run_all_summary(plan_files.run_dir, records, stop_reason)
                        print_fix_result(result.fix)
                        print_run_all_summary(records, stop_reason)
                        return command_exit_code(result)
                    if (
                        result.review_after_fix is not None
                        and result.review_after_fix.failed()
                    ):
                        record.status = (
                            result.review_after_fix.stop_reason or "review_after_fix_failed"
                        )
                        if commit_after_pass:
                            record.commit = commit_not_eligible("review did not pass")
                        records.append(record)
                        stop_reason = (
                            result.review_after_fix.stop_reason or "review after fix failed"
                        )
                        write_run_all_summary(plan_files.run_dir, records, stop_reason)
                        print_fix_result(result.fix)
                        print_review_result(result.review_after_fix)
                        print_run_all_summary(records, stop_reason)
                        return command_exit_code(result)
                else:
                    record.status = result.review.stop_reason or "review_failed"
                    if commit_after_pass:
                        record.commit = commit_not_eligible("review did not pass")
                    records.append(record)
                    stop_reason = result.review.stop_reason or "review failed"
                    write_run_all_summary(plan_files.run_dir, records, stop_reason)
                    print_review_result(result.review)
                    print_run_all_summary(records, stop_reason)
                    return command_exit_code(result)

            if fix_after_review and (record.fix is None or not record.fix.requested):
                record.fix = result.fix or FixResult.not_attempted()

            if (
                result.review_after_fix is not None
                and result.review_after_fix.requested
                and result.review_after_fix.failed()
            ):
                record.status = result.review.stop_reason or "review_failed"
                if commit_after_pass:
                    record.commit = commit_not_eligible("review did not pass")
                records.append(record)
                stop_reason = result.review_after_fix.stop_reason or "review after fix failed"
                write_run_all_summary(plan_files.run_dir, records, stop_reason)
                print_review_result(result.review_after_fix)
                print_run_all_summary(records, stop_reason)
                return command_exit_code(result)

        result = apply_commit_if_eligible(
            plan_files,
            result,
            plan_state_after,
            commit_requested=commit_after_pass,
            commit_prefix=commit_prefix,
        )
        record.commit = result.commit
        if commit_result_failed(result.commit):
            record.status = "commit_failed"
            records.append(record)
            stop_reason = "commit failed"
            write_run_all_summary(plan_files.run_dir, records, stop_reason)
            print_commit_result(result.commit)
            print_run_all_summary(records, stop_reason)
            return result.commit.returncode or 1
        records.append(record)

        next_text = "plan complete"
        if result.selected_after.item is not None:
            next_text = f"{result.selected_after.item.id} - {result.selected_after.item.title}"
        print(f"Completed pass: {selection_before.item.id}")
        print(f"Codex return code: {result.codex_returncode}")
        print(f"Next selected: {next_text}")
        print("Harness checks: passed")
        print_review_result(result.review)
        print_fix_result(result.fix)
        if result.review_after_fix is not None and result.review_after_fix.requested:
            print_review_result(result.review_after_fix)
        print_commit_result(result.commit)

        if result.selected_after.item is None:
            stop_reason = "plan complete"
            write_run_all_summary(plan_files.run_dir, records, stop_reason)
            print_run_all_summary(records, stop_reason)
            return 0

    stop_reason = "max passes"
    write_run_all_summary(plan_files.run_dir, records, stop_reason)
    print("Max-pass limit reached before plan completion.")
    print_run_all_summary(records, stop_reason)
    return 1


def main() -> int:
    args = parse_args()
    original_plan_file = Path(args.plan_file)

    try:
        if args.run_all and args.status:
            raise PlanError("--run-all cannot be used with --status")
        if args.run_all and args.dry_run_prompt:
            raise PlanError("--run-all cannot be used with --dry-run-prompt")
        if args.run_all and args.json:
            raise PlanError("--run-all --json is not implemented yet.")
        if args.fix_after_review and not args.review_after_pass:
            raise PlanError("--fix-after-review requires --review-after-pass")
        if args.fix_after_review and args.max_fix_attempts != 1:
            raise PlanError("V2.8 supports only --max-fix-attempts 1")
        if args.execute_next:
            print(
                "--execute-next is deprecated; execution is now the default.",
                file=sys.stderr,
            )
        ensure_local_git_exclude(verbose=args.verbose)
        plan_files = prepare_plan_file(original_plan_file, args.copy_to_run_dir)
        raw_state = load_json_state(plan_files.plan_file)
        plan_files = with_plan_state_context(plan_files, raw_state)
        plan_state = validate_plan_state(raw_state)
        selection = select_next_item(plan_state, include_parents=args.include_parents)
    except PlanError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.run_all:
        try:
            plan_files = with_execution_run_dir(plan_files, plan_state.plan_id)
            if args.review_after_pass and not args.commit_after_pass:
                print(
                    "Warning: review diff is cumulative because commits are disabled. "
                    "The reviewer will use per-pass diff snapshots, but pass isolation "
                    "is stronger with --commit-after-pass.",
                    file=sys.stderr,
                )
            if args.commit_after_pass:
                require_clean_worktree_for_commits()
            return run_all(
                plan_files,
                max_passes=args.max_passes,
                include_parents=args.include_parents,
                codex_bin=args.codex_bin,
                verbose=args.verbose,
                review_after_pass=args.review_after_pass,
                fix_after_review=args.fix_after_review,
                commit_after_pass=args.commit_after_pass,
                commit_prefix=args.commit_prefix,
            )
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
        if args.commit_after_pass:
            require_clean_worktree_for_commits()
        result = execute_next(
            plan_files,
            selection,
            include_parents=args.include_parents,
            codex_bin=args.codex_bin,
        )
        plan_state_after = load_plan_state_from_file(plan_files.plan_file)
        if args.review_after_pass:
            if (
                result.codex_returncode == 0
                and harness_checks_passed(result)
                and selected_item_status_after(plan_state_after, result.selected_before)
                in DONE_STATUSES
            ):
                result = run_review_after_pass(
                    plan_files,
                    result,
                    plan_state_after,
                    codex_bin=args.codex_bin,
                )
                result, plan_state_after = maybe_fix_and_rereview(
                    plan_files,
                    result,
                    plan_state_after,
                    include_parents=args.include_parents,
                    codex_bin=args.codex_bin,
                    fix_after_review=args.fix_after_review,
                )
            else:
                reason = "pass not review-eligible"
                if result.codex_returncode != 0:
                    reason = "Codex failed"
                elif not harness_checks_passed(result):
                    reason = "harness failed"
                result = replace(result, review=ReviewResult.not_attempted(reason))
                if args.fix_after_review:
                    result = replace(result, fix=FixResult.not_attempted())
        elif args.fix_after_review:
            result = replace(result, fix=FixResult.not_attempted())
        result = apply_commit_if_eligible(
            plan_files,
            result,
            plan_state_after,
            commit_requested=args.commit_after_pass,
            commit_prefix=args.commit_prefix,
        )
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
