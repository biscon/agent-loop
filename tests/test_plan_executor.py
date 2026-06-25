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


def two_phase_state(phase_01_status: str = "Not Started") -> dict:
    return base_state(
        [
            {
                "id": "phase_01",
                "title": "First phase",
                "type": "phase",
                "status": phase_01_status,
            },
            {
                "id": "phase_02",
                "title": "Second phase",
                "type": "phase",
                "status": "Not Started",
            },
        ]
    )


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


def make_fake_codex(path: Path, marker_path: Path, update_plan: bool = False) -> None:
    script = f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

marker_path = Path({str(marker_path)!r})
prompt = sys.argv[2] if len(sys.argv) > 2 else ""
marker_path.write_text(json.dumps({{"argv": sys.argv[1:], "prompt": prompt}}, indent=2), encoding="utf-8")
print("fake stdout")
print("fake stderr", file=sys.stderr)

if {update_plan!r}:
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
    for item in state["items"]:
        if item["id"] == selected_id:
            item["status"] = "Completed"
            break
    patched = lines[:start + 1] + json.dumps(state, indent=2).splitlines() + lines[end:]
    plan_path.write_text("\\n".join(patched) + "\\n", encoding="utf-8")
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


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

    def test_run_all_is_not_implemented(self) -> None:
        completed = subprocess.run(
            CLI + [str(REAL_PLAN), "--run-all"],
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--run-all is not implemented yet.", completed.stderr)

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
