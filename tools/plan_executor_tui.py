"""Textual status/configuration shell for plan_executor."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Static

try:
    from tools import plan_executor
except ImportError:
    import plan_executor  # type: ignore[no-redef]


class PlanExecutorTui(App[None]):
    """Read-only TUI for inspecting plans and composing runner commands."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #top {
        height: 3;
        padding: 0 1;
    }

    #plan-path {
        width: 1fr;
    }

    #main {
        height: 1fr;
    }

    #progress {
        width: 42%;
        border: round $accent;
        padding: 0 1;
    }

    #selection {
        width: 58%;
        border: round $accent;
        padding: 0 1;
    }

    #options {
        height: 10;
        border: round $accent;
        padding: 0 1;
    }

    #log {
        height: 7;
        border: round $accent;
        padding: 0 1;
    }

    .option-row {
        height: 1;
    }

    .short-input {
        width: 16;
    }

    .medium-input {
        width: 28;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self, initial_plan_path: str | None = None) -> None:
        super().__init__()
        self.initial_plan_path = initial_plan_path or ""
        self.loaded_view: plan_executor.PlanStatusView | None = None
        self.log_lines: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top"):
            yield Label("Plan:")
            yield Input(
                value=self.initial_plan_path,
                placeholder="docs/my_plan.md",
                id="plan-path",
            )
            yield Button("Load", id="load", variant="primary")
        with Horizontal(id="main"):
            yield Static("Progress\n\nNo plan loaded.", id="progress")
            yield Static("Current Selection\n\nNo plan loaded.", id="selection")
        with Vertical(id="options"):
            with Horizontal(classes="option-row"):
                yield Checkbox("Run all", id="run-all")
                yield Label("Max passes:")
                yield Input(value="10", id="max-passes", classes="short-input")
                yield Checkbox("Review after pass", id="review-after-pass")
                yield Checkbox("Fix after review", id="fix-after-review")
            with Horizontal(classes="option-row"):
                yield Checkbox("Commit after pass", id="commit-after-pass")
                yield Label("Commit prefix:")
                yield Input(value="plan", id="commit-prefix", classes="short-input")
                yield Checkbox("Copy to run dir", id="copy-to-run-dir")
                yield Label("Run dir:")
                yield Input(placeholder=".agent-runs/example", id="run-dir", classes="medium-input")
            with Horizontal(classes="option-row"):
                yield Checkbox("Inhibit sleep", id="inhibit-sleep")
                yield Label("Codex bin:")
                yield Input(value="codex", id="codex-bin", classes="medium-input")
                yield Label("Quit: q or Ctrl+C")
            yield Static("", id="command-preview")
        yield Static("Log\n", id="log")
        yield Footer()

    def on_mount(self) -> None:
        self.append_log("TUI started.")
        self.update_command_preview()
        if self.initial_plan_path:
            self.load_plan()

    @on(Button.Pressed, "#load")
    def load_button_pressed(self) -> None:
        self.load_plan()

    @on(Input.Submitted, "#plan-path")
    def plan_path_submitted(self) -> None:
        self.load_plan()

    @on(Input.Changed)
    def input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {
            "plan-path",
            "max-passes",
            "commit-prefix",
            "run-dir",
            "codex-bin",
        }:
            self.update_command_preview()

    @on(Checkbox.Changed)
    def checkbox_changed(self) -> None:
        self.update_command_preview()

    def load_plan(self) -> None:
        plan_text = self.query_one("#plan-path", Input).value.strip()
        if not plan_text:
            self.append_log("Failed to load plan: plan path is empty.")
            return

        previous_selected = (
            self.loaded_view.selected.id
            if self.loaded_view is not None and self.loaded_view.selected is not None
            else None
        )
        try:
            view = plan_executor.build_plan_status_view(Path(plan_text))
        except plan_executor.PlanError as exc:
            self.append_log(f"Failed to load plan: {exc}")
            return

        self.loaded_view = view
        self.render_progress(view)
        self.render_selection(view)
        self.append_log(f"Loaded plan path: {view.plan_file}")
        selected_id = view.selected.id if view.selected is not None else None
        if selected_id is None:
            self.append_log("Plan complete.")
        else:
            self.append_log(f"Loaded plan. Selected {selected_id}.")
        if previous_selected is not None and previous_selected != selected_id:
            self.append_log(f"Selected item changed after reload: {previous_selected} -> {selected_id}.")
        self.update_command_preview()

    def render_progress(self, view: plan_executor.PlanStatusView) -> None:
        lines = ["Progress", ""]
        for item in view.items:
            indent = "  " if item.parent is not None else ""
            lines.append(f"{indent}{item.id} {item.status}")
        self.query_one("#progress", Static).update("\n".join(lines))

    def render_selection(self, view: plan_executor.PlanStatusView) -> None:
        lines = [
            "Current Selection",
            "",
            f"Plan file: {view.plan_file}",
            "Mode: in-place",
            f"Plan ID: {view.plan_id}",
        ]
        if view.selected is None:
            lines.append("Plan complete: no unfinished items remain.")
        else:
            item = view.selected
            lines.extend(
                [
                    f"Selected ID: {item.id}",
                    f"Title: {item.title}",
                    f"Type: {item.type}",
                    f"Status: {item.status}",
                ]
            )
            if view.selected_parent is not None:
                parent = view.selected_parent
                lines.append(f"Parent: {parent.id} - {parent.title}")
            if view.warning is not None:
                lines.append(f"Warning: {view.warning}")
            lines.append(f"Suggested prompt: {view.suggested_prompt}")
        self.query_one("#selection", Static).update("\n".join(lines))

    def update_command_preview(self) -> None:
        options = self.current_options()
        plan_path = self.query_one("#plan-path", Input).value.strip()
        preview = plan_executor.build_tui_command_preview(plan_path, options)
        self.query_one("#command-preview", Static).update(f"Command preview:\n{preview}")

    def current_options(self) -> plan_executor.TuiOptions:
        max_passes_text = self.query_one("#max-passes", Input).value.strip()
        try:
            max_passes = plan_executor.positive_int(max_passes_text)
        except Exception:
            max_passes = 10
        run_dir = self.query_one("#run-dir", Input).value.strip() or None
        return plan_executor.TuiOptions(
            run_all=self.query_one("#run-all", Checkbox).value,
            max_passes=max_passes,
            review_after_pass=self.query_one("#review-after-pass", Checkbox).value,
            fix_after_review=self.query_one("#fix-after-review", Checkbox).value,
            commit_after_pass=self.query_one("#commit-after-pass", Checkbox).value,
            commit_prefix=self.query_one("#commit-prefix", Input).value.strip() or "plan",
            copy_to_run_dir=self.query_one("#copy-to-run-dir", Checkbox).value,
            run_dir=run_dir,
            inhibit_sleep=self.query_one("#inhibit-sleep", Checkbox).value,
            codex_bin=self.query_one("#codex-bin", Input).value.strip() or "codex",
        )

    def append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {message}")
        self.log_lines = self.log_lines[-8:]
        self.query_one("#log", Static).update("Log\n" + "\n".join(self.log_lines))


def run_tui(initial_plan_path: str | None = None) -> int:
    PlanExecutorTui(initial_plan_path).run()
    return 0
