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
) -> None:
    script = f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

marker_path = Path({str(marker_path)!r})
prompt = sys.argv[2] if len(sys.argv) > 2 else ""
if marker_path.exists():
    marker_data = json.loads(marker_path.read_text(encoding="utf-8"))
else:
    marker_data = {{"calls": []}}
call_number = len(marker_data["calls"]) + 1
marker_data["calls"].append({{"argv": sys.argv[1:], "prompt": prompt}})
marker_data["argv"] = sys.argv[1:]
marker_data["prompt"] = prompt
marker_path.write_text(json.dumps(marker_data, indent=2), encoding="utf-8")
print("fake stdout")
print("fake stderr", file=sys.stderr)

fail_this_call = {fail_on_call!r} is not None and call_number == {fail_on_call!r}
if fail_this_call and not {fail_after_changes!r}:
    raise SystemExit(7)

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


def init_temp_git_repo(repo: Path) -> None:
    repo.mkdir()
    require_git(repo, "init")
    require_git(repo, "config", "user.name", "Plan Executor Test")
    require_git(repo, "config", "user.email", "plan-executor-test@example.com")
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
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(marker.exists())
            self.assertFalse((run_dir / "logs").exists())
            self.assertEqual(list(run_dir.glob("**/plan_before.md")), [])
            self.assertEqual(list(run_dir.glob("**/plan_after.md")), [])

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
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("--execute-next is deprecated", completed.stderr)


if __name__ == "__main__":
    unittest.main()
