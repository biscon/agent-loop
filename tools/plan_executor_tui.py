"""Textual status/configuration shell for plan_executor."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

try:
    from textual.widgets import Log as TextualLog
except ImportError:
    TextualLog = None  # type: ignore[assignment]

try:
    from textual.widgets import RichLog as TextualRichLog
except ImportError:
    TextualRichLog = None  # type: ignore[assignment]

try:
    from tools import plan_executor
except ImportError:
    import plan_executor  # type: ignore[no-redef]


class PlanBrowseDialog(ModalScreen[Path | None]):
    """Small keyboard-friendly picker for Markdown plans under docs/."""

    CSS = """
    PlanBrowseDialog {
        align: center middle;
    }

    #browse-dialog {
        width: 70%;
        height: 70%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    #browse-title {
        height: 1;
        text-style: bold;
    }

    #browse-list {
        height: 1fr;
        margin: 1 0;
    }

    #browse-empty {
        height: 1fr;
        margin: 1 0;
    }

    #browse-actions {
        height: 3;
        align-horizontal: right;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Close"),
    ]

    def __init__(self, paths: list[Path]) -> None:
        super().__init__()
        self.paths = paths

    def compose(self) -> ComposeResult:
        with Vertical(id="browse-dialog"):
            yield Static("Browse Markdown plans in docs/", id="browse-title")
            if self.paths:
                yield ListView(
                    *[ListItem(Label(str(path))) for path in self.paths],
                    id="browse-list",
                )
            else:
                yield Static("No Markdown files found under docs/.", id="browse-empty")
            with Horizontal(id="browse-actions"):
                yield Button("Cancel", id="browse-cancel")

    @on(ListView.Selected, "#browse-list")
    def plan_selected(self, event: ListView.Selected) -> None:
        self.dismiss(self.paths[event.index])

    @on(Button.Pressed, "#browse-cancel")
    def cancel_pressed(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlanExecutorTui(App[None]):
    """Read-only TUI for inspecting plans and composing runner commands."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #top {
        height: 4;
        padding: 0 1 0 1;
    }

    #path-row {
        height: 1;
        align-vertical: middle;
    }

    #paste-hint {
        height: 1;
        color: $text-muted;
    }

    #plan-label {
        width: 6;
        height: 1;
        content-align: left middle;
    }

    #plan-path {
        width: 1fr;
        margin: 0 1 0 0;
    }

    #load {
        min-width: 8;
        margin: 0 1 0 0;
    }

    #browse {
        min-width: 10;
        margin: 0;
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
        height: 13;
        border: round $accent;
        padding: 0 1;
    }

    #log {
        height: 4;
        border: round $accent;
        padding: 0 1;
    }

    .option-row {
        height: 1;
        margin: 0;
        align-vertical: middle;
    }

    .option-group {
        width: 18;
        height: 1;
        text-style: bold;
        content-align: left middle;
    }

    .option-label {
        width: 16;
        height: 1;
        content-align: left middle;
    }

    #options Input {
        margin: 0;
    }

    #options Checkbox {
        margin: 0 2 0 0;
    }

    #options Label {
        padding: 0;
        margin: 0;
    }

    .short-input {
        width: 12;
    }

    .medium-input {
        width: 28;
    }

    #command-preview {
        height: 4;
        border: round $accent;
        padding: 0 1;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("escape", "blur_input", "Blur input"),
    ]

    def __init__(self, initial_plan_path: str | None = None) -> None:
        super().__init__()
        self.initial_plan_path = initial_plan_path or ""
        self.plan_path_text = self.initial_plan_path
        self.loaded_view: plan_executor.PlanStatusView | None = None
        self.log_lines: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="top"):
            with Horizontal(id="path-row"):
                yield Label("Plan:", id="plan-label")
                yield Input(
                    value=self.initial_plan_path,
                    placeholder="docs/my_plan.md",
                    id="plan-path",
                    compact=True,
                )
                yield Button("Load", id="load", variant="primary", compact=True)
                yield Button("Browse", id="browse", compact=True)
            yield Static(
                "Paste paths using your terminal paste shortcut, usually Ctrl+Shift+V.",
                id="paste-hint",
            )
        with Horizontal(id="main"):
            yield Static("Progress\n\nNo plan loaded.", id="progress")
            yield Static("Current Selection\n\nNo plan loaded.", id="selection")
        with Vertical(id="options"):
            with Horizontal(classes="option-row"):
                yield Label("Run:", classes="option-group")
                yield Checkbox("Run all", id="run-all", compact=True)
                yield Label("Max passes:", classes="option-label")
                yield Input(value="10", id="max-passes", classes="short-input", compact=True)
            with Horizontal(classes="option-row"):
                yield Label("Quality gates:", classes="option-group")
                yield Checkbox("Review after pass", id="review-after-pass", compact=True)
                yield Checkbox("Fix after review", id="fix-after-review", compact=True)
            with Horizontal(classes="option-row"):
                yield Label("Git:", classes="option-group")
                yield Checkbox("Commit after pass", id="commit-after-pass", compact=True)
                yield Label("Commit prefix:", classes="option-label")
                yield Input(value="plan", id="commit-prefix", classes="short-input", compact=True)
            with Horizontal(classes="option-row"):
                yield Label("Plan copy:", classes="option-group")
                yield Checkbox("Copy to run dir", id="copy-to-run-dir", compact=True)
                yield Label("Run dir:", classes="option-label")
                yield Input(
                    placeholder=".agent-runs/example",
                    id="run-dir",
                    classes="medium-input",
                    compact=True,
                )
            with Horizontal(classes="option-row"):
                yield Label("Runtime:", classes="option-group")
                yield Checkbox("Inhibit sleep", id="inhibit-sleep", compact=True)
                yield Label("Codex bin:", classes="option-label")
                yield Input(value="codex", id="codex-bin", classes="medium-input", compact=True)
            yield Label("TUI execution is not implemented in V3.0. Quit: q or Ctrl+C")
            # Future views can replace or sit beside this preview: dashboard, raw stream, review/fix logs.
            yield Static("", id="command-preview")
        if TextualLog is not None:
            log_widget = TextualLog(highlight=False, max_lines=200, auto_scroll=True, id="log")
            log_widget.border_title = "Log"
            yield log_widget
        elif TextualRichLog is not None:
            log_widget = TextualRichLog(
                max_lines=200,
                wrap=True,
                highlight=False,
                markup=False,
                auto_scroll=True,
                id="log",
            )
            log_widget.border_title = "Log"
            yield log_widget
        else:
            yield Static("Log\n", id="log")
        yield Footer()

    def on_mount(self) -> None:
        self.append_log("TUI started.")
        self.update_command_preview()
        if self.initial_plan_path:
            if self.load_plan(self.initial_plan_path):
                self.clear_entry_focus()

    @on(Button.Pressed, "#load")
    def load_button_pressed(self) -> None:
        self.load_plan(self.plan_path_text)

    @on(Button.Pressed, "#browse")
    def browse_button_pressed(self) -> None:
        self.push_screen(
            PlanBrowseDialog(plan_executor.find_docs_markdown_plans()),
            self.browse_finished,
        )

    @on(Input.Submitted, "#plan-path")
    def plan_path_submitted(self, event: Input.Submitted) -> None:
        if self.load_plan(event.value):
            self.clear_entry_focus()

    @on(Input.Changed)
    def input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "plan-path":
            self.plan_path_text = event.value.strip()
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

    def action_blur_input(self) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            focused.blur()
            self.set_focus(None)

    def browse_finished(self, selected_path: Path | None) -> None:
        if selected_path is None:
            return
        if self.load_plan(str(selected_path)):
            self.clear_entry_focus()

    def clear_entry_focus(self) -> None:
        self.query_one("#plan-path", Input).blur()
        self.set_focus(None)

    def load_plan(self, plan_path: str | None = None) -> bool:
        if plan_path is None:
            plan_path = self.plan_path_text
        plan_text = plan_path.strip()
        self.plan_path_text = plan_text
        path_input = self.query_one("#plan-path", Input)
        if path_input.value != plan_text:
            path_input.value = plan_text
        self.append_log(f"Attempting to load plan: {plan_text}")
        state = plan_executor.load_tui_plan_state(plan_text)
        if state.view is None:
            error = state.load_error or "unknown error"
            log_error = error.removeprefix(f"{plan_text}: ")
            self.loaded_view = None
            self.render_invalid_plan(error)
            self.append_log(f"Failed to load plan: {plan_text}: {log_error}")
            self.update_command_preview()
            self.query_one("#plan-path", Input).focus()
            return False

        previous_selected = (
            self.loaded_view.selected.id
            if self.loaded_view is not None and self.loaded_view.selected is not None
            else None
        )
        view = state.view

        self.loaded_view = view
        self.render_progress(view)
        self.render_selection(view)
        selected_id = view.selected.id if view.selected is not None else None
        if selected_id is None:
            self.append_log("Plan complete.")
        else:
            self.append_log(f"Loaded plan. Selected {selected_id}.")
        if previous_selected is not None and previous_selected != selected_id:
            self.append_log(f"Selected item changed after reload: {previous_selected} -> {selected_id}.")
        self.update_command_preview()
        return True

    def render_progress(self, view: plan_executor.PlanStatusView) -> None:
        lines = ["Progress", ""]
        for item in view.items:
            indent = "  " if item.parent is not None else ""
            lines.append(f"{indent}{item.id} {item.status}")
        self.query_one("#progress", Static).update("\n".join(lines))

    def render_invalid_plan(self, error: str) -> None:
        self.query_one("#progress", Static).update("Progress\n\nNo valid plan loaded.")
        self.query_one("#selection", Static).update(
            f"Current Selection\n\nFailed to load plan: {error}"
        )

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
        preview = plan_executor.build_tui_command_preview(self.plan_path_text, options)
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
        line = f"[{stamp}] {message}"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-200:]
        log_widget = self.query_one("#log")
        if TextualLog is not None and isinstance(log_widget, TextualLog):
            log_widget.write_line(line, scroll_end=True)
        elif TextualRichLog is not None and isinstance(log_widget, TextualRichLog):
            log_widget.write(line, scroll_end=True)
        else:
            log_widget.update("Log\n" + "\n".join(self.log_lines[-5:]))


def run_tui(initial_plan_path: str | None = None) -> int:
    PlanExecutorTui(initial_plan_path).run()
    return 0
