import json
import tempfile
import unittest
from pathlib import Path

from tools import plan_executor


REAL_PLAN = Path("docs/agent_loop_test_plan.md")


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


if __name__ == "__main__":
    unittest.main()
