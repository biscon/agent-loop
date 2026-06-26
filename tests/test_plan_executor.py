import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import plan_executor


REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_PLAN = REPO_ROOT / "docs/agent_loop_test_plan.md"
CLI = [sys.executable, str(REPO_ROOT / "tools/plan_executor.py")]


def plan_markdown(state: dict, opener: str = "```plan-state-json") -> str:
    return (
        "# Test Plan\n\n"
        f"{opener}\n"
        f"{json.dumps(state, indent=2)}\n"
        "```\n"
    )


def base_state(items: list[dict]) -> dict:
    return {
        "plan_id": "test_plan",
        "sandbox_dir": "agent_loop_sandbox",
        "status_values": [
            "Not Started",
            "Planned",
            "In Progress",
            "Completed",
            "Deferred",
            "Blocked",
            "Partial",
        ],
        "items": items,
    }


def write_plan(path: Path, state: dict, opener: str = "```plan-state-json") -> None:
    path.write_text(plan_markdown(state, opener), encoding="utf-8")


def phase_items(count: int, first_status: str = "Not Started") -> list[dict]:
    items = []
    for index in range(1, count + 1):
        items.append(
            {
                "id": f"phase_{index:02d}",
                "title": f"Phase {index}",
                "type": "phase",
                "status": first_status if index == 1 else "Not Started",
            }
        )
    return items


def two_phase_state(phase_01_status: str = "Not Started") -> dict:
    return base_state(phase_items(2, first_status=phase_01_status))


def make_fake_git(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def env_with_fake_git(tmp: Path) -> dict[str, str]:
    fake_bin = tmp / "bin"
    fake_bin.mkdir()
    make_fake_git(fake_bin / "git")
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    return env


def make_fake_codex(
    path: Path,
    marker_path: Path,
    update_plan: bool = False,
    fail_on_call: int | None = None,
    fail_after_changes: bool = False,
    status_to_set: str = "Completed",
    expand_plan: bool = False,
    create_artifact: bool = False,
    review_verdict: str = "pass",
    review_verdicts: list[str] | None = None,
    review_invalid_json: bool = False,
    review_missing_json: bool = False,
    review_modify_tracked: str | None = None,
    fix_fail: bool = False,
    fix_break_plan_json: bool = False,
    fix_status_to_set: str | None = None,
    fix_advance_unrelated: bool = False,
    fix_whitespace_error: bool = False,
) -> None:
    review_verdicts = review_verdicts or []
    script = f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

marker_path = Path({str(marker_path)!r})
prompt = sys.argv[2] if len(sys.argv) > 2 else ""
is_review = prompt.startswith("Review the implementation diff")
is_rereview = prompt.startswith("Rereview the selected plan item")
is_fix = prompt.startswith("Fix only issues listed in the review result")
if marker_path.exists():
    marker_data = json.loads(marker_path.read_text(encoding="utf-8"))
else:
    marker_data = {{"calls": []}}
call_number = len(marker_data["calls"]) + 1
review_call_number = sum(1 for call in marker_data["calls"] if call.get("kind") in ("review", "rereview")) + 1
if is_rereview:
    kind = "rereview"
elif is_review:
    kind = "review"
elif is_fix:
    kind = "fix"
else:
    kind = "implementation"
marker_data["calls"].append({{"argv": sys.argv[1:], "prompt": prompt, "kind": kind}})
marker_data["argv"] = sys.argv[1:]
marker_data["prompt"] = prompt
marker_path.write_text(json.dumps(marker_data, indent=2), encoding="utf-8")
print("fake stdout")
print("fake stderr", file=sys.stderr)

fail_this_call = {fail_on_call!r} is not None and call_number == {fail_on_call!r}
if fail_this_call and not {fail_after_changes!r}:
    raise SystemExit(7)

if is_review or is_rereview:
    logs_dir = None
    for line in prompt.splitlines():
        if line.startswith("Logs dir: "):
            logs_dir = Path(line.removeprefix("Logs dir: "))
            break
    if logs_dir is None:
        raise SystemExit("could not parse logs dir from review prompt")
    verdicts = {review_verdicts!r}
    verdict = verdicts[review_call_number - 1] if review_call_number <= len(verdicts) else {review_verdict!r}
    json_name = "review_after_fix_result.json" if is_rereview else "review_result.json"
    md_name = "review_after_fix_result.md" if is_rereview else "review_result.md"
    if {review_modify_tracked!r} is not None:
        Path({review_modify_tracked!r}).write_text("modified by review\\n", encoding="utf-8")
    if {review_invalid_json!r}:
        (logs_dir / json_name).write_text("{{not json\\n", encoding="utf-8")
    elif not {review_missing_json!r}:
        (logs_dir / json_name).write_text(json.dumps({{
            "verdict": verdict,
            "summary": "fake review " + verdict,
            "issues": [] if verdict == "pass" else [{{
                "severity": "major",
                "file": "plan.md",
                "reason": "fake issue",
                "suggested_fix": None,
            }}],
            "scope_notes": "fake scope notes",
            "checks_considered": ["git diff", "harness checks", "plan update"],
        }}, indent=2) + "\\n", encoding="utf-8")
    (logs_dir / md_name).write_text("# Fake review\\n\\n" + verdict + "\\n", encoding="utf-8")
    raise SystemExit(0)

if is_fix:
    logs_dir = None
    plan_path = None
    selected_id = None
    for line in prompt.splitlines():
        if line.startswith("Logs dir: "):
            logs_dir = Path(line.removeprefix("Logs dir: "))
        elif line.startswith("Active plan file: "):
            plan_path = Path(line.removeprefix("Active plan file: "))
        elif line.startswith("Selected item id: "):
            selected_id = line.removeprefix("Selected item id: ")
    if logs_dir is None or plan_path is None or selected_id is None:
        raise SystemExit("could not parse fix prompt")
    (logs_dir / "fix_result.md").write_text("# Fake fix\\n\\nfixed\\n", encoding="utf-8")
    if {fix_fail!r}:
        raise SystemExit(8)
    text = plan_path.read_text(encoding="utf-8")
    if {fix_break_plan_json!r}:
        plan_path.write_text(text.replace('"items": [', '"items": [ INVALID', 1), encoding="utf-8")
        raise SystemExit(0)
    lines = text.splitlines()
    start = end = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            info = stripped[3:].strip()
            if info and info.split(maxsplit=1)[0] == "plan-state-json":
                start = index
                break
    if start is None:
        raise SystemExit("missing plan-state-json block")
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == "```":
            end = index
            break
    if end is None:
        raise SystemExit("missing closing fence")
    state = json.loads("\\n".join(lines[start + 1:end]))
    for item in state["items"]:
        if item["id"] == selected_id and {fix_status_to_set!r} is not None:
            item["status"] = {fix_status_to_set!r}
        elif item["id"] != selected_id and {fix_advance_unrelated!r} and item["status"] != "Completed":
            item["status"] = "Completed"
            break
    patched = lines[:start + 1] + json.dumps(state, indent=2).splitlines() + lines[end:]
    plan_path.write_text("\\n".join(patched) + "\\n", encoding="utf-8")
    if {fix_whitespace_error!r}:
        tracked = plan_path.parent / "tracked.txt"
        tracked.write_text("bad whitespace   \\n", encoding="utf-8")
    raise SystemExit(0)

if {update_plan!r} or {expand_plan!r} or {create_artifact!r} or fail_this_call:
    first_line = prompt.splitlines()[0]
    if not first_line.startswith("Read ") or not first_line.endswith("."):
        raise SystemExit("could not parse active plan path from prompt")
    selected_id = None
    for line in prompt.splitlines():
        if line.startswith("Execute ") and line.endswith(" only."):
            selected_id = line.removeprefix("Execute ").removesuffix(" only.")
            break
    if selected_id is None:
        raise SystemExit("could not parse selected id from prompt")
    plan_path = Path(first_line[5:-1])
    text = plan_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = end = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            info = stripped[3:].strip()
            if info and info.split(maxsplit=1)[0] == "plan-state-json":
                start = index
                break
    if start is None:
        raise SystemExit("missing plan-state-json block")
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == "```":
            end = index
            break
    if end is None:
        raise SystemExit("missing closing fence")
    state = json.loads("\\n".join(lines[start + 1:end]))
    selected_item = None
    for item in state["items"]:
        if item["id"] == selected_id:
            selected_item = item
            item["status"] = {status_to_set!r}
            break
    if selected_item is None:
        raise SystemExit("selected item not found")
    if {create_artifact!r} or fail_this_call:
        artifact_dir = plan_path.parent / "artifacts"
        artifact_dir.mkdir(exist_ok=True)
        (artifact_dir / (selected_id + ".txt")).write_text(
            "artifact for " + selected_id + "\\n",
            encoding="utf-8",
        )
    if {expand_plan!r}:
        selected_item["status"] = "Not Started"
        state["items"].append({{
            "id": selected_id + "a",
            "title": "Expanded child",
            "type": "pass",
            "parent": selected_id,
            "status": "Not Started",
        }})
    patched = lines[:start + 1] + json.dumps(state, indent=2).splitlines() + lines[end:]
    plan_path.write_text("\\n".join(patched) + "\\n", encoding="utf-8")
if fail_this_call:
    raise SystemExit(7)
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def make_fake_systemd_inhibit(path: Path, marker_path: Path) -> None:
    script = f"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

marker_path = Path({str(marker_path)!r})
marker_path.write_text(json.dumps({{
    "argv": sys.argv[1:],
    "sleep_inhibited": os.environ.get("PLAN_EXECUTOR_SLEEP_INHIBITED"),
}}, indent=2), encoding="utf-8")

command_start = None
for index, arg in enumerate(sys.argv[1:], start=1):
    if not arg.startswith("--"):
        command_start = index
        break
if command_start is None:
    raise SystemExit("missing command portion")
command = sys.argv[command_start:]
os.execvpe(command[0], command, os.environ)
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def require_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    completed = run_git(repo, *args)
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def init_temp_git_repo(repo: Path, create_gitignore: bool = True) -> None:
    repo.mkdir()
    require_git(repo, "init")
    require_git(repo, "config", "user.name", "Plan Executor Test")
    require_git(repo, "config", "user.email", "plan-executor-test@example.com")
    if create_gitignore:
        (repo / ".gitignore").write_text(
            ".agent-runs/\nagent_loop_sandbox/\n__pycache__/\n*.pyc\n",
            encoding="utf-8",
        )


def commit_all(repo: Path, message: str) -> None:
    require_git(repo, "add", "-A")
    require_git(repo, "commit", "-m", message)


def commit_count(repo: Path) -> int:
    completed = require_git(repo, "rev-list", "--count", "HEAD")
    return int(completed.stdout.strip())


def local_exclude_path(repo: Path) -> Path:
    path_text = require_git(repo, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    path = Path(path_text)
    return path if path.is_absolute() else repo / path


class PlanExecutorTests(unittest.TestCase):
    def test_selects_phase_01_from_real_plan(self) -> None:
        plan_state = plan_executor.load_plan_state_from_file(REAL_PLAN)
        selection = plan_executor.select_next_item(plan_state, include_parents=False)

        self.assertIsNotNone(selection.item)
        self.assertEqual(selection.item.id, "phase_01")

    def test_accepts_fence_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = Path(tmp) / "plan.md"
            write_plan(
                plan_file,
                base_state(
                    [
                        {
                            "id": "phase_01",
                            "title": "First phase",
                            "type": "phase",
                            "status": "Not Started",
                        }
                    ]
                ),
                opener='```plan-state-json id="abc123"',
            )

            plan_state = plan_executor.load_plan_state_from_file(plan_file)

        self.assertEqual(plan_state.plan_id, "test_plan")

    def test_missing_plan_state_block_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = Path(tmp) / "plan.md"
            plan_file.write_text("# Missing block\n", encoding="utf-8")

            with self.assertRaises(plan_executor.PlanError):
                plan_executor.load_plan_state_from_file(plan_file)

    def test_multiple_plan_state_blocks_fail(self) -> None:
        state = base_state(
            [
                {
                    "id": "phase_01",
                    "title": "First phase",
                    "type": "phase",
                    "status": "Not Started",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = Path(tmp) / "plan.md"
            plan_file.write_text(
                plan_markdown(state) + "\n" + plan_markdown(state),
                encoding="utf-8",
            )

            with self.assertRaises(plan_executor.PlanError):
                plan_executor.load_plan_state_from_file(plan_file)

    def test_complete_plan_returns_no_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = Path(tmp) / "plan.md"
            write_plan(
                plan_file,
                base_state(
                    [
                        {
                            "id": "phase_01",
                            "title": "First phase",
                            "type": "phase",
                            "status": "Completed",
                        }
                    ]
                ),
            )
            plan_state = plan_executor.load_plan_state_from_file(plan_file)
            selection = plan_executor.select_next_item(plan_state, include_parents=False)

        self.assertIsNone(selection.item)

    def test_parent_phase_selects_first_unfinished_child(self) -> None:
        plan_state = plan_executor.validate_plan_state(
            base_state(
                [
                    {
                        "id": "phase_01",
                        "title": "Parent phase",
                        "type": "phase",
                        "status": "Not Started",
                    },
                    {
                        "id": "phase_01a",
                        "title": "Done child",
                        "type": "pass",
                        "parent": "phase_01",
                        "status": "Completed",
                    },
                    {
                        "id": "phase_01b",
                        "title": "Next child",
                        "type": "pass",
                        "parent": "phase_01",
                        "status": "Not Started",
                    },
                ]
            )
        )

        selection = plan_executor.select_next_item(plan_state, include_parents=False)

        self.assertIsNotNone(selection.item)
        self.assertEqual(selection.item.id, "phase_01b")

    def test_parent_phase_warning_when_children_done_but_parent_unfinished(self) -> None:
        plan_state = plan_executor.validate_plan_state(
            base_state(
                [
                    {
                        "id": "phase_01",
                        "title": "Parent phase",
                        "type": "phase",
                        "status": "Not Started",
                    },
                    {
                        "id": "phase_01a",
                        "title": "Done child",
                        "type": "pass",
                        "parent": "phase_01",
                        "status": "Completed",
                    },
                ]
            )
        )

        selection = plan_executor.select_next_item(plan_state, include_parents=False)

        self.assertIsNotNone(selection.item)
        self.assertEqual(selection.item.id, "phase_01")
        self.assertIsNotNone(selection.warning)

    def test_include_parents_selects_parent(self) -> None:
        plan_state = plan_executor.validate_plan_state(
            base_state(
                [
                    {
                        "id": "phase_01",
                        "title": "Parent phase",
                        "type": "phase",
                        "status": "Not Started",
                    },
                    {
                        "id": "phase_01a",
                        "title": "Child",
                        "type": "pass",
                        "parent": "phase_01",
                        "status": "Not Started",
                    },
                ]
            )
        )

        selection = plan_executor.select_next_item(plan_state, include_parents=True)

        self.assertIsNotNone(selection.item)
        self.assertEqual(selection.item.id, "phase_01")

    def test_copy_to_run_dir_uses_patched_copy(self) -> None:
        original_text = REAL_PLAN.read_text(encoding="utf-8")
        repo_sandbox = Path("agent_loop_sandbox")
        repo_sandbox_existed = repo_sandbox.exists()

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            plan_files = plan_executor.prepare_plan_file(REAL_PLAN, str(run_dir))
            copied_text = plan_files.plan_file.read_text(encoding="utf-8")
            copied_state = plan_executor.load_json_state(plan_files.plan_file)
            plan_state = plan_executor.load_plan_state_from_file(plan_files.plan_file)
            selection = plan_executor.select_next_item(plan_state, include_parents=False)

            self.assertTrue(plan_files.plan_file.exists())
            self.assertEqual(plan_files.plan_file, run_dir / "plan.md")
            self.assertTrue(plan_files.workspace_dir.exists())
            self.assertTrue(plan_files.workspace_dir.is_dir())
            self.assertEqual(
                copied_state["sandbox_dir"],
                str(run_dir / "workspace" / "agent_loop_sandbox"),
            )
            for line in copied_text.splitlines():
                if "agent_loop_sandbox" in line:
                    self.assertIn(str(plan_files.sandbox_dir), line)
            self.assertIsNotNone(selection.item)
            self.assertEqual(selection.item.id, "phase_01")

        self.assertEqual(REAL_PLAN.read_text(encoding="utf-8"), original_text)
        if not repo_sandbox_existed:
            self.assertFalse(repo_sandbox.exists())

    def test_default_executes_with_fake_codex_on_existing_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, two_phase_state())
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(marker_data["argv"][0], "exec")
            self.assertEqual(
                plan_executor.load_json_state(plan_file)["items"][0]["status"],
                "Completed",
            )
            log_dirs = list((tmp_path / ".agent-runs").glob("in-place-test_plan-*/logs/*"))
            self.assertEqual(len(log_dirs), 1)
            logs_dir = log_dirs[0]
            self.assertTrue((logs_dir / "plan_before.md").exists())
            self.assertTrue((logs_dir / "plan_after.md").exists())
            self.assertFalse((tmp_path / "logs").exists())

    def test_status_does_not_call_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker)

            completed = subprocess.run(
                CLI
                + [
                    str(REAL_PLAN),
                    "--copy-to-run-dir",
                    str(run_dir),
                    "--status",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(marker.exists())
            self.assertFalse((run_dir / "logs").exists())
            self.assertEqual(list(run_dir.glob("**/plan_before.md")), [])
            self.assertEqual(list(run_dir.glob("**/plan_after.md")), [])

    def test_inhibit_sleep_missing_binary_fails_before_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, two_phase_state())
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)
            env = dict(os.environ)
            env.pop("PLAN_EXECUTOR_SLEEP_INHIBITED", None)
            env["PLAN_EXECUTOR_SYSTEMD_INHIBIT_BIN"] = str(tmp_path / "missing")

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--inhibit-sleep",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertIn(
                "--inhibit-sleep requested, but systemd-inhibit was not found.",
                completed.stderr,
            )

    def test_inhibit_sleep_non_executable_override_fails_before_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, two_phase_state())
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)
            fake_inhibitor = tmp_path / "systemd-inhibit"
            fake_inhibitor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            env = dict(os.environ)
            env.pop("PLAN_EXECUTOR_SLEEP_INHIBITED", None)
            env["PLAN_EXECUTOR_SYSTEMD_INHIBIT_BIN"] = str(fake_inhibitor)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--inhibit-sleep",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertIn(
                "--inhibit-sleep requested, but systemd-inhibit was not found.",
                completed.stderr,
            )

    def test_inhibit_sleep_reexecs_through_fake_systemd_inhibit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, two_phase_state())
            fake_inhibitor = tmp_path / "systemd-inhibit"
            inhibitor_marker = tmp_path / "inhibitor.json"
            make_fake_systemd_inhibit(fake_inhibitor, inhibitor_marker)
            env = dict(os.environ)
            env.pop("PLAN_EXECUTOR_SLEEP_INHIBITED", None)
            env["PLAN_EXECUTOR_SYSTEMD_INHIBIT_BIN"] = str(fake_inhibitor)

            completed = subprocess.run(
                CLI + [str(plan_file), "--status", "--inhibit-sleep"],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            marker_data = json.loads(inhibitor_marker.read_text(encoding="utf-8"))
            argv = marker_data["argv"]
            self.assertIn("--who=plan_executor", argv)
            self.assertIn("--what=idle:sleep", argv)
            self.assertIn("--mode=block", argv)
            self.assertIn(sys.executable, argv)
            self.assertIn(str(REPO_ROOT / "tools/plan_executor.py"), argv)
            self.assertIn(str(plan_file), argv)
            self.assertIn("--status", argv)
            self.assertIn("--inhibit-sleep", argv)
            self.assertEqual(marker_data["sleep_inhibited"], "1")
            self.assertIn(
                "Sleep inhibition requested: re-executing through systemd-inhibit.",
                completed.stderr,
            )

    def test_inhibit_sleep_does_not_recurse_when_env_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, two_phase_state())
            fake_inhibitor = tmp_path / "systemd-inhibit"
            inhibitor_marker = tmp_path / "inhibitor.json"
            make_fake_systemd_inhibit(fake_inhibitor, inhibitor_marker)
            env = dict(os.environ)
            env["PLAN_EXECUTOR_SLEEP_INHIBITED"] = "1"
            env["PLAN_EXECUTOR_SYSTEMD_INHIBIT_BIN"] = str(fake_inhibitor)

            completed = subprocess.run(
                CLI + [str(plan_file), "--status", "--inhibit-sleep"],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(inhibitor_marker.exists())

    def test_inhibit_sleep_keeps_json_stdout_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, two_phase_state())
            fake_inhibitor = tmp_path / "systemd-inhibit"
            inhibitor_marker = tmp_path / "inhibitor.json"
            make_fake_systemd_inhibit(fake_inhibitor, inhibitor_marker)
            env = dict(os.environ)
            env.pop("PLAN_EXECUTOR_SLEEP_INHIBITED", None)
            env["PLAN_EXECUTOR_SYSTEMD_INHIBIT_BIN"] = str(fake_inhibitor)

            completed = subprocess.run(
                CLI + [str(plan_file), "--status", "--json", "--inhibit-sleep"],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            self.assertEqual(output["plan_id"], "test_plan")
            self.assertEqual(output["selected"]["id"], "phase_01")
            self.assertIn(
                "Sleep inhibition requested: re-executing through systemd-inhibit.",
                completed.stderr,
            )
            self.assertNotIn("Sleep inhibition", completed.stdout)

    def test_inhibit_sleep_with_execution_uses_fake_codex_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, two_phase_state())
            fake_inhibitor = tmp_path / "systemd-inhibit"
            inhibitor_marker = tmp_path / "inhibitor.json"
            make_fake_systemd_inhibit(fake_inhibitor, inhibitor_marker)
            fake_codex = tmp_path / "fake_codex.py"
            codex_marker = tmp_path / "codex.json"
            make_fake_codex(fake_codex, codex_marker, update_plan=True)
            env = env_with_fake_git(tmp_path)
            env.pop("PLAN_EXECUTOR_SLEEP_INHIBITED", None)
            env["PLAN_EXECUTOR_SYSTEMD_INHIBIT_BIN"] = str(fake_inhibitor)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--inhibit-sleep",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(inhibitor_marker.exists())
            self.assertTrue(codex_marker.exists())
            marker_data = json.loads(codex_marker.read_text(encoding="utf-8"))
            self.assertEqual(marker_data["argv"][0], "exec")
            self.assertEqual(
                plan_executor.load_json_state(plan_file)["items"][0]["status"],
                "Completed",
            )

    def test_copy_mode_default_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = Path(tmp) / ".agent-runs" / "run"
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker)
            env = env_with_fake_git(tmp_path)

            completed = subprocess.run(
                CLI
                + [
                    str(REAL_PLAN),
                    "--copy-to-run-dir",
                    str(run_dir),
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((run_dir / "plan.md").exists())
            self.assertTrue(marker.exists())
            log_dirs = list((run_dir / "logs").glob("*-phase_01"))
            self.assertEqual(len(log_dirs), 1)
            logs_dir = log_dirs[0]
            for name in (
                "codex_prompt.txt",
                "codex_stdout.txt",
                "codex_stderr.txt",
                "codex_returncode.txt",
                "selection_before.json",
                "selection_after.json",
                "harness_checks.json",
                "plan_before.md",
                "plan_after.md",
            ):
                self.assertTrue((logs_dir / name).exists(), name)
            self.assertEqual(
                (logs_dir / "codex_returncode.txt").read_text(encoding="utf-8"),
                "0\n",
            )

    def test_continue_existing_run_advances_next_phase(self) -> None:
        original_text = REAL_PLAN.read_text(encoding="utf-8")
        repo_sandbox = Path("agent_loop_sandbox")
        repo_sandbox_existed = repo_sandbox.exists()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = Path(tmp) / ".agent-runs" / "run"
            plan_files = plan_executor.prepare_plan_file(REAL_PLAN, str(run_dir))
            state = plan_executor.load_json_state(plan_files.plan_file)
            state["items"][0]["status"] = "Completed"
            write_plan(plan_files.plan_file, state)
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)
            env = env_with_fake_git(tmp_path)

            completed = subprocess.run(
                CLI
                + [
                    str(run_dir / "plan.md"),
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            log_dirs = list((run_dir / "logs").glob("*-phase_02"))
            self.assertEqual(len(log_dirs), 1)
            after = json.loads(
                (log_dirs[0] / "selection_after.json").read_text(encoding="utf-8")
            )
            self.assertNotEqual(after["selected"]["id"], "phase_01")
            self.assertNotEqual(after["selected"]["id"], "phase_02")

        self.assertEqual(REAL_PLAN.read_text(encoding="utf-8"), original_text)
        if not repo_sandbox_existed:
            self.assertFalse(repo_sandbox.exists())

    def test_logs_do_not_overwrite_between_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = Path(tmp) / ".agent-runs" / "run"
            plan_files = plan_executor.prepare_plan_file(REAL_PLAN, str(run_dir))
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)
            env = env_with_fake_git(tmp_path)

            for _ in range(2):
                completed = subprocess.run(
                    CLI
                    + [
                        str(plan_files.plan_file),
                        "--codex-bin",
                        str(fake_codex),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

            log_dirs = [path for path in (run_dir / "logs").iterdir() if path.is_dir()]
            self.assertEqual(len(log_dirs), 2)
            self.assertNotEqual(log_dirs[0], log_dirs[1])

    def test_dry_run_prompt_does_not_create_execution_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker)

            completed = subprocess.run(
                CLI
                + [
                    str(REAL_PLAN),
                    "--copy-to-run-dir",
                    str(run_dir),
                    "--dry-run-prompt",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(f"Read {run_dir / 'plan.md'}.", completed.stdout)
            self.assertIn("Execute phase_01 only.", completed.stdout)
            self.assertFalse(marker.exists())
            self.assertFalse((run_dir / "logs").exists())
            self.assertEqual(list(run_dir.glob("**/plan_before.md")), [])
            self.assertEqual(list(run_dir.glob("**/plan_after.md")), [])

    def test_execution_writes_plan_before_and_after_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, two_phase_state())
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            logs_dir = next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/logs/*"))
            before_state = plan_executor.load_json_state(logs_dir / "plan_before.md")
            after_state = plan_executor.load_json_state(logs_dir / "plan_after.md")
            self.assertEqual(before_state["items"][0]["status"], "Not Started")
            self.assertEqual(after_state["items"][0]["status"], "Completed")

    def test_in_place_plan_execution_uses_backup_under_agent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "project_plan.md"
            write_plan(plan_file, two_phase_state())
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                plan_executor.load_json_state(plan_file)["items"][0]["status"],
                "Completed",
            )
            log_dirs = list((tmp_path / ".agent-runs").glob("in-place-test_plan-*/logs/*"))
            self.assertEqual(len(log_dirs), 1)
            self.assertTrue((log_dirs[0] / "plan_before.md").exists())
            self.assertTrue((log_dirs[0] / "plan_after.md").exists())
            self.assertFalse((tmp_path / "logs").exists())

    def test_copy_mode_execution_also_writes_plan_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = Path(tmp) / ".agent-runs" / "run"
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)

            completed = subprocess.run(
                CLI
                + [
                    str(REAL_PLAN),
                    "--copy-to-run-dir",
                    str(run_dir),
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                env=env_with_fake_git(tmp_path),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            logs_dir = next((run_dir / "logs").glob("*-phase_01"))
            self.assertTrue((logs_dir / "plan_before.md").exists())
            self.assertTrue((logs_dir / "plan_after.md").exists())

    def test_run_all_executes_multiple_fake_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(3)))
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--max-passes",
                    "5",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(len(marker_data["calls"]), 3)
            state = plan_executor.load_json_state(plan_file)
            self.assertTrue(all(item["status"] == "Completed" for item in state["items"]))
            summary = json.loads(
                next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/run_all_summary.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["stop_reason"], "plan complete")

    def test_run_all_reuses_single_in_place_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(2)))
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            run_dirs = list((tmp_path / ".agent-runs").glob("in-place-test_plan-*"))
            self.assertEqual(len(run_dirs), 1)
            log_dirs = [path for path in (run_dirs[0] / "logs").iterdir() if path.is_dir()]
            self.assertEqual(len(log_dirs), 2)
            self.assertTrue((run_dirs[0] / "run_all_summary.json").exists())

    def test_run_all_stops_at_max_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(4)))
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--max-passes",
                    "2",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Max-pass limit reached", completed.stdout)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(len(marker_data["calls"]), 2)
            summary = json.loads(
                next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/run_all_summary.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["stop_reason"], "max passes")

    def test_run_all_stops_when_no_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(2)))
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=False)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("no progress", completed.stdout)
            summary = json.loads(
                next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/run_all_summary.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["passes"][0]["status"], "no_progress")

    def test_run_all_stops_on_codex_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(3)))
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, fail_on_call=2)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertNotEqual(completed.returncode, 0)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(len(marker_data["calls"]), 2)
            summary = json.loads(
                next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/run_all_summary.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["passes"][-1]["status"], "codex_failed")

    def test_run_all_stops_when_selected_item_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(2)))
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, status_to_set="Partial")

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertNotEqual(completed.returncode, 0)
            summary = json.loads(
                next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/run_all_summary.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["passes"][0]["status"], "selected_item_not_completed")

    def test_run_all_stops_when_plan_expanded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(2)))
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, expand_plan=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertNotEqual(completed.returncode, 0)
            summary = json.loads(
                next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/run_all_summary.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["passes"][0]["status"], "plan_expanded_needs_review")

    def test_run_all_disallows_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker)

            completed = subprocess.run(
                CLI
                + [
                    str(REAL_PLAN),
                    "--run-all",
                    "--status",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("--run-all cannot be used with --status", completed.stderr)
            self.assertFalse(marker.exists())

    def test_run_all_disallows_dry_run_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker)

            completed = subprocess.run(
                CLI
                + [
                    str(REAL_PLAN),
                    "--run-all",
                    "--dry-run-prompt",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("--run-all cannot be used with --dry-run-prompt", completed.stderr)
            self.assertFalse(marker.exists())

    def test_run_all_disallows_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker)

            completed = subprocess.run(
                CLI
                + [
                    str(REAL_PLAN),
                    "--run-all",
                    "--json",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("--run-all --json is not implemented yet.", completed.stderr)
            self.assertFalse(marker.exists())

    def test_run_all_writes_summary_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            run_dir = next((tmp_path / ".agent-runs").glob("in-place-test_plan-*"))
            self.assertTrue((run_dir / "run_all_summary.json").exists())
            self.assertTrue((run_dir / "run_all_summary.txt").exists())

    def test_codex_output_streaming_keeps_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = Path(tmp) / "fake_codex.py"
            marker = Path(tmp) / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--codex-bin",
                    str(fake_codex),
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("[codex] fake stdout", completed.stdout)
            self.assertIn("[codex:stderr] fake stderr", completed.stderr)
            logs_dir = next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/logs/*"))
            self.assertIn("fake stdout", (logs_dir / "codex_stdout.txt").read_text())
            self.assertIn("fake stderr", (logs_dir / "codex_stderr.txt").read_text())

    def test_local_git_exclude_is_bootstrapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_temp_git_repo(repo, create_gitignore=False)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")

            completed = subprocess.run(
                CLI + [str(plan_file), "--status"],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            exclude_text = local_exclude_path(repo).read_text(encoding="utf-8")
            self.assertIn(".agent-runs/", exclude_text)
            self.assertIn("agent_loop_sandbox/", exclude_text)
            self.assertIn("__pycache__/", exclude_text)
            self.assertIn("*.pyc", exclude_text)
            self.assertFalse((repo / ".gitignore").exists())
            self.assertEqual(require_git(repo, "status", "--porcelain").stdout, "")

    def test_local_git_exclude_bootstrap_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            init_temp_git_repo(repo, create_gitignore=False)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")

            for _ in range(2):
                completed = subprocess.run(
                    CLI + [str(plan_file), "--status"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

            exclude_text = local_exclude_path(repo).read_text(encoding="utf-8")
            self.assertEqual(
                exclude_text.count("# plan_executor local transient outputs"),
                1,
            )
            self.assertEqual(exclude_text.count(".agent-runs/"), 1)
            self.assertEqual(exclude_text.count("agent_loop_sandbox/"), 1)
            self.assertEqual(exclude_text.count("__pycache__/"), 1)
            self.assertEqual(exclude_text.count("*.pyc"), 1)

    def test_commit_after_pass_bootstraps_exclude_before_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo, create_gitignore=False)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, create_artifact=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--commit-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout)
            output = completed.stdout
            self.assertIn(
                "Updated local git exclude with plan executor transient patterns:",
                output,
            )
            self.assertIn("Commit preflight: git worktree is clean.", output)
            self.assertLess(
                output.index("Updated local git exclude"),
                output.index("Commit preflight"),
            )
            self.assertEqual(commit_count(repo), 2)
            committed_paths = require_git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout
            self.assertIn("plan.md", committed_paths)
            self.assertIn("artifacts/phase_01.txt", committed_paths)
            self.assertNotIn(".agent-runs/", committed_paths)
            self.assertEqual(require_git(repo, "status", "--porcelain").stdout, "")

    def test_bootstrap_does_not_call_real_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo, create_gitignore=False)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--status",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(marker.exists())
            exclude_text = local_exclude_path(repo).read_text(encoding="utf-8")
            self.assertIn(".agent-runs/", exclude_text)

    def test_commit_after_pass_requires_clean_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")
            (repo / "dirty.txt").write_text("existing change\n", encoding="utf-8")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, create_artifact=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--commit-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Cannot use --commit-after-pass with a dirty worktree", completed.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(commit_count(repo), 1)

    def test_commit_after_successful_one_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            write_plan(
                plan_file,
                base_state(
                    [
                        {
                            "id": "phase_03",
                            "title": "Calculate sorted differences",
                            "type": "phase",
                            "status": "Not Started",
                        }
                    ]
                ),
            )
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, create_artifact=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--commit-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(commit_count(repo), 2)
            subject = require_git(repo, "log", "-1", "--format=%s").stdout.strip()
            self.assertEqual(
                subject,
                "plan: complete phase_03 - Calculate sorted differences",
            )
            self.assertEqual(require_git(repo, "status", "--short").stdout, "")
            committed_paths = require_git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout
            self.assertIn("artifacts/phase_03.txt", committed_paths)
            self.assertNotIn(".agent-runs/", committed_paths)
            logs_dir = next((repo / ".agent-runs").glob("in-place-test_plan-*/logs/*"))
            self.assertTrue((logs_dir / "git_commit_cached_diff_stat.txt").exists())
            self.assertTrue((logs_dir / "git_commit_cached_name_status.txt").exists())
            self.assertIn("artifacts/phase_03.txt", completed.stdout)

    def test_run_all_commits_each_successful_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(3)))
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, create_artifact=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--commit-after-pass",
                    "--max-passes",
                    "5",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(commit_count(repo), 4)
            state = plan_executor.load_json_state(plan_file)
            self.assertTrue(all(item["status"] == "Completed" for item in state["items"]))
            self.assertEqual(require_git(repo, "status", "--short").stdout, "")
            subjects = require_git(repo, "log", "--format=%s", "-3").stdout.splitlines()
            self.assertEqual(
                subjects,
                [
                    "plan: complete phase_03 - Phase 3",
                    "plan: complete phase_02 - Phase 2",
                    "plan: complete phase_01 - Phase 1",
                ],
            )

    def test_no_commit_when_plan_expanded_needs_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, expand_plan=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--commit-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("plan expanded needs review", completed.stdout)
            self.assertEqual(commit_count(repo), 1)

    def test_no_commit_on_codex_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                create_artifact=True,
                fail_on_call=1,
                fail_after_changes=True,
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--commit-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(commit_count(repo), 1)

    def test_review_after_successful_pass_writes_review_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, review_verdict="pass")

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual([call["kind"] for call in marker_data["calls"]], ["implementation", "review"])
            logs_dir = next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/logs/*"))
            for name in (
                "review_prompt.txt",
                "review_stdout.txt",
                "review_stderr.txt",
                "review_returncode.txt",
                "review_result.json",
                "review_result.md",
                "review_git_status_before.txt",
                "review_git_status_after.txt",
                "review_git_diff_fingerprint_before.txt",
                "review_git_diff_fingerprint_after.txt",
                "implementation_git_status_before.txt",
                "implementation_git_diff_before.patch",
                "implementation_git_diff_stat_before.txt",
                "implementation_git_status_after.txt",
                "implementation_git_diff_after.patch",
                "implementation_git_diff_stat_after.txt",
                "implementation_git_name_status_after.txt",
            ):
                self.assertTrue((logs_dir / name).exists(), name)
            self.assertIn("Verdict: pass", completed.stdout)

    def test_review_after_pass_stops_on_needs_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                create_artifact=True,
                review_verdict="needs_fix",
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--commit-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Stop reason: review_needs_fix", completed.stdout)
            self.assertEqual(commit_count(repo), 1)

    def test_review_after_pass_stops_on_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, review_verdict="needs_human")

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Stop reason: review_needs_human", completed.stdout)

    def test_review_after_pass_invalid_json_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, review_invalid_json=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Stop reason: review_invalid_json", completed.stdout)
            logs_dir = next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/logs/*"))
            self.assertIn(
                "invalid review JSON",
                (logs_dir / "review_parse_error.txt").read_text(encoding="utf-8"),
            )

    def test_review_after_pass_not_run_when_execution_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, fail_on_call=1)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual([call["kind"] for call in marker_data["calls"]], ["implementation"])

    def test_review_after_pass_detects_worktree_modification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            tracked_file = repo / "tracked.txt"
            write_plan(plan_file, base_state(phase_items(1)))
            tracked_file.write_text("initial\n", encoding="utf-8")
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdict="pass",
                review_modify_tracked=str(tracked_file),
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("review_modified_worktree", completed.stdout)

    def test_commit_after_pass_waits_for_review_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                create_artifact=True,
                review_verdict="pass",
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--commit-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(commit_count(repo), 2)
            body = require_git(repo, "log", "-1", "--format=%B").stdout
            self.assertIn("Review requested: True", body)
            self.assertIn("Review verdict: pass", body)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual([call["kind"] for call in marker_data["calls"]], ["implementation", "review"])

    def test_commit_after_pass_skips_commit_when_review_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                create_artifact=True,
                review_verdict="needs_fix",
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--commit-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(commit_count(repo), 1)

    def test_run_all_stops_on_review_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(3)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdicts=["pass", "needs_fix"],
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--review-after-pass",
                    "--max-passes",
                    "5",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("review diff is cumulative", completed.stderr)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(
                [call["kind"] for call in marker_data["calls"]],
                ["implementation", "review", "implementation", "review"],
            )
            summary = json.loads(
                next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/run_all_summary.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(summary["passes"][-1]["review_verdict"], "needs_fix")
            self.assertEqual(summary["passes"][-1]["review_stop_reason"], "review_needs_fix")
            state = plan_executor.load_json_state(plan_file)
            self.assertEqual(state["items"][2]["status"], "Not Started")

    def test_review_after_pass_uses_fake_codex_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, review_verdict="pass")
            trap_bin = tmp_path / "bin"
            trap_bin.mkdir()
            trap_codex = trap_bin / "codex"
            trap_codex.write_text(
                "#!/bin/sh\necho real codex should not run >&2\nexit 99\n",
                encoding="utf-8",
            )
            trap_codex.chmod(trap_codex.stat().st_mode | 0o111)
            make_fake_git(trap_bin / "git")
            env = dict(os.environ)
            env["PATH"] = f"{trap_bin}:{env['PATH']}"

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("real codex should not run", completed.stderr)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(len(marker_data["calls"]), 2)

    def test_fix_after_review_requires_review_after_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True)

            completed = subprocess.run(
                CLI + [str(plan_file), "--fix-after-review", "--codex-bin", str(fake_codex)],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("--fix-after-review requires --review-after-pass", completed.stderr)
            self.assertFalse(marker.exists())

    def test_fix_after_review_not_run_when_review_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, review_verdict="pass")

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual([call["kind"] for call in marker_data["calls"]], ["implementation", "review"])

    def test_fix_after_review_runs_on_needs_fix_and_rereview_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdicts=["needs_fix", "pass"],
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(
                [call["kind"] for call in marker_data["calls"]],
                ["implementation", "review", "fix", "rereview"],
            )
            logs_dir = next((tmp_path / ".agent-runs").glob("in-place-test_plan-*/logs/*"))
            for name in (
                "fix_prompt.txt",
                "fix_stdout.txt",
                "fix_stderr.txt",
                "fix_returncode.txt",
                "fix_result.md",
                "fix_harness_checks.json",
                "plan_before_fix.md",
                "plan_after_fix.md",
                "review_after_fix_prompt.txt",
                "review_after_fix_stdout.txt",
                "review_after_fix_stderr.txt",
                "review_after_fix_returncode.txt",
                "review_after_fix_result.json",
                "review_after_fix_result.md",
                "review_after_fix_git_status_before.txt",
                "review_after_fix_git_status_after.txt",
                "review_after_fix_git_diff_fingerprint_before.txt",
                "review_after_fix_git_diff_fingerprint_after.txt",
            ):
                self.assertTrue((logs_dir / name).exists(), name)

    def test_fix_after_review_not_run_on_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, review_verdict="needs_human")

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual([call["kind"] for call in marker_data["calls"]], ["implementation", "review"])

    def test_fix_after_review_stops_when_fix_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdict="needs_fix",
                fix_fail=True,
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Stop reason: fix_failed", completed.stdout)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual([call["kind"] for call in marker_data["calls"]], ["implementation", "review", "fix"])

    def test_fix_after_review_stops_when_fix_checks_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            tracked = repo / "tracked.txt"
            write_plan(plan_file, base_state(phase_items(1)))
            tracked.write_text("initial\n", encoding="utf-8")
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdict="needs_fix",
                fix_whitespace_error=True,
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Stop reason: fix_checks_failed", completed.stdout)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual([call["kind"] for call in marker_data["calls"]], ["implementation", "review", "fix"])

    def test_fix_after_review_stops_when_rereview_needs_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdicts=["needs_fix", "needs_fix"],
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Stop reason: fix_incomplete", completed.stdout)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(
                [call["kind"] for call in marker_data["calls"]],
                ["implementation", "review", "fix", "rereview"],
            )

    def test_fix_after_review_stops_when_rereview_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdicts=["needs_fix", "needs_human"],
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Stop reason: review_after_fix_needs_human", completed.stdout)

    def test_fix_after_review_stops_when_fix_breaks_plan_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdict="needs_fix",
                fix_break_plan_json=True,
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Stop reason: fix_plan_parse_failed", completed.stdout)

    def test_fix_after_review_stops_when_fix_changes_selected_status_to_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdict="needs_fix",
                fix_status_to_set="In Progress",
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Stop reason: fix_invalid_selected_status", completed.stdout)

    def test_fix_after_review_stops_when_fix_advances_unrelated_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(2)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdict="needs_fix",
                fix_advance_unrelated=True,
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Stop reason: fix_broadened_scope", completed.stdout)

    def test_commit_after_pass_waits_for_fix_and_rereview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                create_artifact=True,
                review_verdicts=["needs_fix", "pass"],
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--commit-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(commit_count(repo), 2)
            body = require_git(repo, "log", "-1", "--format=%B").stdout
            self.assertIn("Fix requested: True", body)
            self.assertIn("Fix attempted: True", body)
            self.assertIn("Review after fix verdict: pass", body)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(
                [call["kind"] for call in marker_data["calls"]],
                ["implementation", "review", "fix", "rereview"],
            )

    def test_commit_after_pass_not_created_when_fix_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                create_artifact=True,
                review_verdict="needs_fix",
                fix_fail=True,
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--commit-after-pass",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(commit_count(repo), 1)

    def test_run_all_stops_on_fix_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(3)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdicts=["pass", "needs_fix"],
                fix_fail=True,
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--review-after-pass",
                    "--fix-after-review",
                    "--max-passes",
                    "5",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(
                [call["kind"] for call in marker_data["calls"]],
                ["implementation", "review", "implementation", "review", "fix"],
            )
            state = plan_executor.load_json_state(plan_file)
            self.assertEqual(state["items"][2]["status"], "Not Started")

    def test_run_all_continues_after_fix_and_rereview_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(2)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdicts=["needs_fix", "pass", "pass"],
            )

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--run-all",
                    "--review-after-pass",
                    "--fix-after-review",
                    "--max-passes",
                    "5",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env_with_fake_git(tmp_path),
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(
                [call["kind"] for call in marker_data["calls"]],
                ["implementation", "review", "fix", "rereview", "implementation", "review"],
            )
            state = plan_executor.load_json_state(plan_file)
            self.assertTrue(all(item["status"] == "Completed" for item in state["items"]))

    def test_fix_after_review_uses_fake_codex_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_file = tmp_path / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(
                fake_codex,
                marker,
                update_plan=True,
                review_verdicts=["needs_fix", "pass"],
            )
            trap_bin = tmp_path / "bin"
            trap_bin.mkdir()
            trap_codex = trap_bin / "codex"
            trap_codex.write_text(
                "#!/bin/sh\necho real codex should not run >&2\nexit 99\n",
                encoding="utf-8",
            )
            trap_codex.chmod(trap_codex.stat().st_mode | 0o111)
            make_fake_git(trap_bin / "git")
            env = dict(os.environ)
            env["PATH"] = f"{trap_bin}:{env['PATH']}"

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--review-after-pass",
                    "--fix-after-review",
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("real codex should not run", completed.stderr)
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(
                [call["kind"] for call in marker_data["calls"]],
                ["implementation", "review", "fix", "rereview"],
            )

    def test_no_commit_when_commit_not_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            init_temp_git_repo(repo)
            plan_file = repo / "plan.md"
            write_plan(plan_file, base_state(phase_items(1)))
            commit_all(repo, "initial")
            fake_codex = tmp_path / "fake_codex.py"
            marker = tmp_path / "marker.json"
            make_fake_codex(fake_codex, marker, update_plan=True, create_artifact=True)

            completed = subprocess.run(
                CLI
                + [
                    str(plan_file),
                    "--codex-bin",
                    str(fake_codex),
                ],
                cwd=repo,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(commit_count(repo), 1)

    def test_execute_next_is_deprecated_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            completed = subprocess.run(
                CLI
                + [
                    str(REAL_PLAN),
                    "--copy-to-run-dir",
                    str(run_dir),
                    "--execute-next",
                    "--status",
                ],
                capture_output=True,
                text=True,
                cwd=tmp,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("--execute-next is deprecated", completed.stderr)


if __name__ == "__main__":
    unittest.main()
