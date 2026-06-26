"""Textual status/configuration shell for plan_executor."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
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
    from tools import plan_executor
except ImportError:
    import plan_executor  # type: ignore[no-redef]


MAX_RAW_OUTPUT_LINES = 5000
RAW_OUTPUT_TRUNCATION_TEXT = "[output truncated: oldest lines dropped]"


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


class QuitAfterRunDialog(ModalScreen[bool]):
    """Confirm quitting after the active pass finishes."""

    CSS = """
    QuitAfterRunDialog {
        align: center middle;
    }

    #quit-after-run-dialog {
        width: 54;
        height: 11;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }

    #quit-after-run-title {
        height: 1;
        text-style: bold;
    }

    #quit-after-run-message {
        height: 2;
        margin: 1 0;
    }

    #quit-after-run-actions {
        height: 3;
        align-horizontal: right;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-after-run-dialog"):
            yield Static("Run in progress", id="quit-after-run-title")
            yield Static(
                "Wait for the current pass before quitting.",
                id="quit-after-run-message",
            )
            with Horizontal(id="quit-after-run-actions"):
                yield Button("Cancel", id="quit-cancel", compact=True)
                yield Button("Exit after run", id="quit-after-run", compact=True)

    @on(Button.Pressed, "#quit-cancel")
    def cancel_pressed(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#quit-after-run")
    def quit_after_run_pressed(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


def render_recent_log_lines(lines: list[str], visible_count: int = 3) -> str:
    """Render the newest log lines in chronological order."""
    if visible_count <= 0:
        return ""
    return "\n".join(lines[-visible_count:])


@dataclass(frozen=True)
class ActiveRunSnapshot:
    selected_id: str
    selected_title: str
    started_at: datetime
    options_summary: str
    codex_bin: str


@dataclass
class LastRunResult:
    selected_id: str
    selected_title: str
    finished_at: datetime
    return_code: int | None = None
    failure_message: str | None = None
    reload_error: str | None = None


@dataclass(frozen=True)
class RawOutputLine:
    stream: str
    text: str


@dataclass
class RawOutputState:
    selected_id: str | None = None
    selected_title: str | None = None
    command: str | None = None
    status: str | None = None
    lines: list[RawOutputLine] = field(default_factory=list)
    truncated: bool = False


def format_elapsed_duration(started_at: datetime, now: datetime) -> str:
    elapsed_seconds = max(0, int((now - started_at).total_seconds()))
    hours, remainder = divmod(elapsed_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def summarize_tui_options(options: plan_executor.TuiOptions) -> str:
    enabled = []
    if options.review_after_pass:
        enabled.append("review")
    if options.fix_after_review:
        enabled.append("fix")
    if options.commit_after_pass:
        enabled.append("commit")
    if options.copy_to_run_dir:
        enabled.append("copy")
    if options.inhibit_sleep:
        enabled.append("inhibit sleep")
    return ", ".join(enabled) if enabled else "none"


def format_run_result(result: LastRunResult) -> str:
    if result.return_code is not None:
        if result.return_code == 0:
            return f"Finished with return code {result.return_code}"
        return f"Failed with return code {result.return_code}"
    if result.failure_message:
        return f"Failed: {result.failure_message}"
    return "Failed"


def reset_raw_output_state(
    state: RawOutputState,
    *,
    selected_id: str,
    selected_title: str,
    command: str,
    status: str,
) -> None:
    state.selected_id = selected_id
    state.selected_title = selected_title
    state.command = command
    state.status = status
    state.lines.clear()
    state.truncated = False


def append_raw_output_line(
    state: RawOutputState,
    stream: str,
    text: str,
    *,
    max_lines: int = MAX_RAW_OUTPUT_LINES,
) -> None:
    if max_lines <= 0:
        state.lines.clear()
        state.truncated = True
        return
    state.lines.append(RawOutputLine(stream=stream, text=text))
    if len(state.lines) > max_lines:
        state.truncated = True
        del state.lines[: len(state.lines) - max_lines]


def render_raw_output_details(state: RawOutputState) -> str:
    if (
        state.selected_id is None
        and state.command is None
        and state.status is None
        and not state.lines
    ):
        return "Raw Output\n\nNo run output yet.\nRun a pass to capture stdout/stderr."

    run_text = "Not available"
    if state.selected_id is not None:
        if state.selected_title:
            run_text = f"{state.selected_id} - {state.selected_title}"
        else:
            run_text = state.selected_id
    return "\n".join(
        [
            "Raw Output",
            "",
            "Run:",
            f"  {run_text}",
            "",
            "Command:",
            f"  {state.command or 'Not available'}",
            "",
            "Status:",
            f"  {state.status or 'Not available'}",
        ]
    )


def render_raw_output_lines(state: RawOutputState) -> str:
    if (
        state.selected_id is None
        and state.command is None
        and state.status is None
        and not state.lines
    ):
        return ""
    rendered = []
    if state.truncated:
        rendered.append(RAW_OUTPUT_TRUNCATION_TEXT)
    rendered.extend(f"[{line.stream}] {line.text}" for line in state.lines)
    if not rendered:
        rendered.append("No output captured yet.")
    return "\n".join(rendered)


def render_raw_output_state(state: RawOutputState) -> str:
    output = render_raw_output_lines(state)
    if output:
        return f"{render_raw_output_details(state)}\n\nOutput:\n{output}"
    return render_raw_output_details(state)


def append_current_selection_lines(
    lines: list[str], view: plan_executor.PlanStatusView | None, load_error: str | None
) -> None:
    if load_error:
        lines.extend(["Current selection:", f"  Load failed: {load_error}"])
        return
    if view is None:
        lines.extend(["Current selection:", "  No valid plan loaded."])
        return
    if view.selected is None:
        lines.extend(["Current selection:", "  Plan complete.", "  No runnable item selected."])
        return
    item = view.selected
    lines.extend(["Current selection:", f"  {item.id} - {item.title}"])


def build_selection_panel_text(
    loaded_view: plan_executor.PlanStatusView | None,
    active_run: ActiveRunSnapshot | None,
    last_run: LastRunResult | None,
    load_error: str | None,
    now: datetime,
) -> str:
    lines = ["Current Selection", ""]
    if active_run is not None:
        lines.extend(
            [
                "Running:",
                f"  {active_run.selected_id} - {active_run.selected_title}",
                "",
                "Started:",
                f"  {active_run.started_at.strftime('%H:%M:%S')}",
                "",
                "Elapsed:",
                f"  {format_elapsed_duration(active_run.started_at, now)}",
                "",
                "Command:",
                "  one pass",
                "",
                "Options:",
                f"  {active_run.options_summary}",
                "",
                "Codex:",
                f"  {active_run.codex_bin}",
            ]
        )
        return "\n".join(lines)

    if last_run is not None:
        lines.extend(
            [
                "Last run:",
                f"  {last_run.selected_id} - {last_run.selected_title}",
                "",
                "Result:",
                f"  {format_run_result(last_run)}",
                "",
            ]
        )
        reload_error = last_run.reload_error or load_error
        if reload_error:
            lines.extend(["Reload:", f"  Load failed: {reload_error}"])
        else:
            append_current_selection_lines(lines, loaded_view, None)
        return "\n".join(lines)

    if load_error:
        lines.extend(["Load failed:", load_error])
        return "\n".join(lines)

    if loaded_view is None:
        lines.append("No valid plan loaded.")
        return "\n".join(lines)

    if loaded_view.selected is None:
        lines.extend(["Plan complete.", "No runnable item selected."])
        return "\n".join(lines)

    item = loaded_view.selected
    lines.extend(
        [
            "Selected:",
            f"  {item.id} - {item.title}",
            "",
            "Type:",
            f"  {item.type}",
            "",
            "Status:",
            f"  {item.status}",
        ]
    )
    if loaded_view.selected_parent is not None:
        parent = loaded_view.selected_parent
        lines.extend(["", "Parent:", f"  {parent.id} - {parent.title}"])
    if loaded_view.warning is not None:
        lines.extend(["", "Warning:", f"  {loaded_view.warning}"])
    return "\n".join(lines)


class PlanExecutorTui(App[None]):
    """Read-only TUI for inspecting plans and composing runner commands."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #top {
        height: 2;
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
        margin: 0 1 0 0;
    }

    #run-pass-button {
        min-width: 12;
        margin: 0;
    }

    #main {
        height: 1fr;
    }

    #progress-panel {
        width: 42%;
        height: 100%;
        border: round $accent;
        padding: 0 1;
    }

    #progress {
        height: auto;
    }

    #selection {
        width: 58%;
        height: 100%;
        border: round $accent;
        padding: 0 1;
    }

    #options {
        height: 8;
        border: round $accent;
        padding: 0 1;
    }

    #log {
        height: 5;
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

    .option-primary {
        width: 28;
        height: 1;
    }

    .option-secondary-label {
        width: 16;
        height: 1;
        content-align: left middle;
    }

    .option-secondary-control {
        height: 1;
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

    #dashboard-view {
        height: 1fr;
    }

    #raw-output-view {
        height: 1fr;
        padding: 0 1;
    }

    #raw-output-details {
        height: 10;
        border: round $accent;
        padding: 0 1;
    }

    #raw-output-scroll {
        height: 1fr;
        border: round $accent;
        padding: 0 1;
        margin: 1 0 0 0;
    }

    #raw-output-text {
        height: auto;
    }
    """

    BINDINGS = [
        Binding("f2", "show_dashboard", "Dashboard"),
        Binding("f3", "show_output", "Output"),
        Binding("q", "safe_quit", "Quit", priority=True),
        Binding("ctrl+c", "safe_quit", "Quit", priority=True),
        Binding("r", "run_pass", "Run pass"),
        Binding("escape", "blur_input", "Blur input"),
    ]

    def __init__(self, initial_plan_path: str | None = None) -> None:
        super().__init__()
        self.initial_plan_path = initial_plan_path or ""
        self.plan_path_text = self.initial_plan_path
        self.loaded_view: plan_executor.PlanStatusView | None = None
        self.log_lines: list[str] = []
        self.run_in_progress = False
        self.run_process: asyncio.subprocess.Process | None = None
        self.quit_after_run = False
        self.safe_quit_dialog_open = False
        self.current_run_selected_id: str | None = None
        self.current_run_started_at: datetime | None = None
        self.active_run: ActiveRunSnapshot | None = None
        self.last_run_result: LastRunResult | None = None
        self.current_load_error: str | None = None
        self.elapsed_refresh_timer: Any | None = None
        self.raw_output_state = RawOutputState()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="dashboard-view"):
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
                    yield Button("Run Pass", id="run-pass-button", compact=True)
                yield Static(
                    "Paste paths using your terminal paste shortcut, usually Ctrl+Shift+V.",
                    id="paste-hint",
                )
            with Horizontal(id="main"):
                progress_panel = ScrollableContainer(id="progress-panel")
                progress_panel.border_title = "Progress"
                with progress_panel:
                    yield Static("No plan loaded.", id="progress")
                yield Static("Current Selection\n\nNo valid plan loaded.", id="selection")
            with Vertical(id="options"):
                with Horizontal(classes="option-row"):
                    yield Label("Run:", classes="option-group")
                    yield Checkbox("Run all", id="run-all", classes="option-primary", compact=True)
                    yield Label("Max passes:", classes="option-secondary-label")
                    yield Input(
                        value="10",
                        id="max-passes",
                        classes="short-input option-secondary-control",
                        compact=True,
                    )
                with Horizontal(classes="option-row"):
                    yield Label("Quality gates:", classes="option-group")
                    yield Checkbox(
                        "Review after pass",
                        id="review-after-pass",
                        classes="option-primary",
                        compact=True,
                    )
                    yield Checkbox(
                        "Fix after review",
                        id="fix-after-review",
                        classes="option-secondary-control",
                        compact=True,
                    )
                with Horizontal(classes="option-row"):
                    yield Label("Git:", classes="option-group")
                    yield Checkbox(
                        "Commit after pass",
                        id="commit-after-pass",
                        classes="option-primary",
                        compact=True,
                    )
                    yield Label("Commit prefix:", classes="option-secondary-label")
                    yield Input(
                        value="plan",
                        id="commit-prefix",
                        classes="short-input option-secondary-control",
                        compact=True,
                    )
                with Horizontal(classes="option-row"):
                    yield Label("Plan copy:", classes="option-group")
                    yield Checkbox(
                        "Copy to run dir",
                        id="copy-to-run-dir",
                        classes="option-primary",
                        compact=True,
                    )
                    yield Label("Run dir:", classes="option-secondary-label")
                    yield Input(
                        placeholder=".agent-runs/example",
                        id="run-dir",
                        classes="medium-input option-secondary-control",
                        compact=True,
                    )
                with Horizontal(classes="option-row"):
                    yield Label("Runtime:", classes="option-group")
                    yield Checkbox(
                        "Inhibit sleep",
                        id="inhibit-sleep",
                        classes="option-primary",
                        compact=True,
                    )
                    yield Label("Codex bin:", classes="option-secondary-label")
                    yield Input(
                        value="codex",
                        id="codex-bin",
                        classes="medium-input option-secondary-control",
                        compact=True,
                    )
                yield Label("Idle", id="run-status")
            yield Static("", id="command-preview")
            log_widget = Static("", id="log")
            log_widget.border_title = "Log"
            yield log_widget
        with Vertical(id="raw-output-view"):
            yield Static("", id="raw-output-details")
            output_scroll = ScrollableContainer(id="raw-output-scroll", can_focus=True)
            output_scroll.border_title = "Output"
            with output_scroll:
                yield Static("", id="raw-output-text")
        yield Footer()

    def on_mount(self) -> None:
        self.append_log("TUI started.")
        self.query_one("#raw-output-view").display = False
        self.refresh_raw_output_view(auto_scroll=False)
        self.elapsed_refresh_timer = self.set_interval(
            1.0, self.refresh_running_selection
        )
        self.update_command_preview()
        self.update_control_state()
        if self.initial_plan_path:
            if self.load_plan(self.initial_plan_path):
                self.clear_entry_focus()

    @on(Button.Pressed, "#load")
    def load_button_pressed(self) -> None:
        if self.run_in_progress:
            self.append_log("Cannot load while a run is in progress.")
            return
        self.load_plan(self.plan_path_text)

    @on(Button.Pressed, "#browse")
    def browse_button_pressed(self) -> None:
        if self.run_in_progress:
            self.append_log("Cannot browse while a run is in progress.")
            return
        self.push_screen(
            PlanBrowseDialog(plan_executor.find_docs_markdown_plans()),
            self.browse_finished,
        )

    @on(Button.Pressed, "#run-pass-button")
    def run_pass_button_pressed(self) -> None:
        self.action_run_pass()

    @on(Input.Submitted, "#plan-path")
    def plan_path_submitted(self, event: Input.Submitted) -> None:
        if self.run_in_progress:
            self.append_log("Cannot load while a run is in progress.")
            return
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

    def action_show_dashboard(self) -> None:
        self.query_one("#raw-output-view").display = False
        self.query_one("#dashboard-view").display = True
        self.set_focus(None)

    def action_show_output(self) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            focused.blur()
        self.query_one("#dashboard-view").display = False
        self.query_one("#raw-output-view").display = True
        output_scroll = self.query_one("#raw-output-scroll", ScrollableContainer)
        self.set_focus(output_scroll)
        self.refresh_raw_output_view(auto_scroll=True)

    def request_safe_quit(self) -> None:
        if not self.run_in_progress:
            self.exit()
            return
        if self.safe_quit_dialog_open:
            return
        self.safe_quit_dialog_open = True
        self.push_screen(QuitAfterRunDialog(), self.quit_after_run_finished)

    def action_safe_quit(self) -> None:
        self.request_safe_quit()

    def action_quit(self) -> None:
        self.request_safe_quit()

    def action_help_quit(self) -> None:
        self.request_safe_quit()

    def action_run_pass(self) -> None:
        if self.run_in_progress:
            self.append_log("Run already in progress.")
            return
        if self.loaded_view is None or self.loaded_view.selected is None:
            self.append_log("Cannot run: no valid plan loaded.")
            return
        options = self.current_options()
        if options.run_all:
            self.append_log(
                "Run all is not implemented in the TUI yet. "
                "Uncheck Run all to run one pass."
            )
            return

        selected_id = self.loaded_view.selected.id
        selected_title = self.loaded_view.selected.title
        plan_path = self.plan_path_text
        self.run_in_progress = True
        self.quit_after_run = False
        self.last_run_result = None
        self.current_load_error = None
        self.current_run_selected_id = selected_id
        started_at = datetime.now()
        self.current_run_started_at = started_at
        self.active_run = ActiveRunSnapshot(
            selected_id=selected_id,
            selected_title=selected_title,
            started_at=started_at,
            options_summary=summarize_tui_options(options),
            codex_bin=options.codex_bin,
        )
        self.render_selection_panel()
        self.update_run_status(f"Running {selected_id}")
        self.update_control_state()
        self.run_worker(
            self.run_selected_pass(plan_path, selected_id, options),
            name="run-pass",
            exclusive=True,
        )

    def browse_finished(self, selected_path: Path | None) -> None:
        if self.run_in_progress:
            self.append_log("Cannot browse while a run is in progress.")
            return
        if selected_path is None:
            return
        if self.load_plan(str(selected_path)):
            self.clear_entry_focus()

    def quit_after_run_finished(self, should_quit: bool) -> None:
        self.safe_quit_dialog_open = False
        if should_quit:
            self.quit_after_run = True
            self.append_log("Will quit after current pass finishes.")

    def clear_entry_focus(self) -> None:
        self.query_one("#plan-path", Input).blur()
        self.set_focus(None)

    def load_plan(
        self,
        plan_path: str | None = None,
        *,
        log_load: bool = True,
        preserve_last_run: bool = False,
    ) -> bool:
        if not preserve_last_run:
            self.last_run_result = None
        if plan_path is None:
            plan_path = self.plan_path_text
        plan_text = plan_path.strip()
        self.plan_path_text = plan_text
        path_input = self.query_one("#plan-path", Input)
        if path_input.value != plan_text:
            path_input.value = plan_text
        if log_load:
            self.append_log(f"Attempting to load plan: {plan_text}")
        state = plan_executor.load_tui_plan_state(plan_text)
        if state.view is None:
            error = state.load_error or "unknown error"
            log_error = error.removeprefix(f"{plan_text}: ")
            self.loaded_view = None
            self.current_load_error = error
            self.render_invalid_plan(error)
            if log_load:
                self.append_log(f"Failed to load plan: {plan_text}: {log_error}")
            self.update_command_preview()
            if not self.quit_after_run:
                self.update_control_state()
            self.query_one("#plan-path", Input).focus()
            return False

        previous_selected = (
            self.loaded_view.selected.id
            if self.loaded_view is not None and self.loaded_view.selected is not None
            else None
        )
        view = state.view

        self.loaded_view = view
        self.current_load_error = None
        self.render_progress(view)
        self.render_selection_panel()
        selected_id = view.selected.id if view.selected is not None else None
        if log_load:
            if selected_id is None:
                self.append_log("Plan complete.")
            else:
                self.append_log(f"Loaded plan. Selected {selected_id}.")
        if log_load and previous_selected is not None and previous_selected != selected_id:
            self.append_log(f"Selected item changed after reload: {previous_selected} -> {selected_id}.")
        self.update_command_preview()
        if not self.quit_after_run:
            self.update_control_state()
        return True

    def render_progress(self, view: plan_executor.PlanStatusView) -> None:
        lines = []
        for item in view.items:
            indent = "  " if item.parent is not None else ""
            lines.append(f"{indent}{item.id} {item.status}")
        self.query_one("#progress", Static).update("\n".join(lines))

    def render_invalid_plan(self, error: str) -> None:
        self.query_one("#progress", Static).update("No valid plan loaded.")
        self.current_load_error = error
        if self.last_run_result is not None:
            self.last_run_result.reload_error = error
        self.render_selection_panel()

    def render_selection(self, view: plan_executor.PlanStatusView) -> None:
        self.loaded_view = view
        self.current_load_error = None
        self.render_selection_panel()

    def render_selection_panel(self, now: datetime | None = None) -> None:
        self.query_one("#selection", Static).update(
            build_selection_panel_text(
                self.loaded_view,
                self.active_run,
                self.last_run_result,
                self.current_load_error,
                now or datetime.now(),
            )
        )

    def refresh_running_selection(self) -> None:
        if self.run_in_progress:
            self.render_selection_panel()

    def update_command_preview(self) -> None:
        options = self.current_options()
        preview = plan_executor.build_tui_command_preview(self.plan_path_text, options)
        self.query_one("#command-preview", Static).update(f"Command preview:\n{preview}")

    def update_run_status(self, message: str) -> None:
        self.query_one("#run-status", Label).update(message)

    def refresh_raw_output_view(self, *, auto_scroll: bool = True) -> None:
        self.query_one("#raw-output-details", Static).update(
            render_raw_output_details(self.raw_output_state)
        )
        self.query_one("#raw-output-text", Static).update(
            render_raw_output_lines(self.raw_output_state)
        )
        if auto_scroll:
            output_scroll = self.query_one("#raw-output-scroll", ScrollableContainer)
            output_scroll.scroll_end(animate=False)

    def update_raw_output_status(self, message: str) -> None:
        self.raw_output_state.status = message
        self.refresh_raw_output_view(auto_scroll=True)

    def append_raw_output(self, stream: str, text: str) -> None:
        append_raw_output_line(self.raw_output_state, stream, text)
        self.refresh_raw_output_view(auto_scroll=True)

    def update_control_state(self) -> None:
        has_selected_item = (
            self.loaded_view is not None and self.loaded_view.selected is not None
        )
        controls_disabled = self.run_in_progress
        for selector, widget_type in [
            ("#plan-path", Input),
            ("#load", Button),
            ("#browse", Button),
            ("#run-all", Checkbox),
            ("#max-passes", Input),
            ("#review-after-pass", Checkbox),
            ("#fix-after-review", Checkbox),
            ("#commit-after-pass", Checkbox),
            ("#commit-prefix", Input),
            ("#copy-to-run-dir", Checkbox),
            ("#run-dir", Input),
            ("#inhibit-sleep", Checkbox),
            ("#codex-bin", Input),
        ]:
            self.query_one(selector, widget_type).disabled = controls_disabled
        self.query_one("#run-pass-button", Button).disabled = (
            controls_disabled or not has_selected_item
        )

    async def drain_subprocess_stream(
        self, stream: asyncio.StreamReader | None, stream_name: str
    ) -> int:
        if stream is None:
            return 0
        line_count = 0
        while True:
            line = await stream.readline()
            if not line:
                return line_count
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            self.append_raw_output(stream_name, text)
            line_count += 1

    async def run_selected_pass(
        self,
        plan_path: str,
        selected_id: str,
        options: plan_executor.TuiOptions,
    ) -> None:
        return_code: int | None = None
        failed = False
        failure_message: str | None = None
        if self.active_run is None:
            selected_title = selected_id
            if self.loaded_view is not None and self.loaded_view.selected is not None:
                selected_title = self.loaded_view.selected.title
            self.active_run = ActiveRunSnapshot(
                selected_id=selected_id,
                selected_title=selected_title,
                started_at=datetime.now(),
                options_summary=summarize_tui_options(options),
                codex_bin=options.codex_bin,
            )
            self.render_selection_panel()
        self.append_log(f"Starting pass {selected_id}.")
        try:
            argv = plan_executor.build_tui_subprocess_argv(plan_path, options)
            active_run = self.active_run
            selected_title = (
                active_run.selected_title if active_run is not None else selected_id
            )
            reset_raw_output_state(
                self.raw_output_state,
                selected_id=selected_id,
                selected_title=selected_title,
                command=shlex.join(argv),
                status="Running",
            )
            self.refresh_raw_output_view(auto_scroll=False)
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.run_process = process
            stdout_task = asyncio.create_task(
                self.drain_subprocess_stream(process.stdout, "stdout")
            )
            stderr_task = asyncio.create_task(
                self.drain_subprocess_stream(process.stderr, "stderr")
            )
            return_code = await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
            if return_code == 0:
                self.update_run_status(f"Finished with return code {return_code}")
                self.update_raw_output_status(f"Finished with return code {return_code}")
            else:
                self.update_run_status(f"Failed with return code {return_code}")
                self.update_raw_output_status(f"Failed with return code {return_code}")
            self.append_log(f"Runner exited with return code {return_code}.")
        except Exception as exc:
            failed = True
            failure_message = str(exc)
            if return_code is None:
                self.update_run_status("Failed")
                self.update_raw_output_status(f"Failed: {failure_message}")
            else:
                self.update_run_status(f"Failed with return code {return_code}")
                self.update_raw_output_status(f"Failed with return code {return_code}")
            self.append_raw_output("runner", f"Failed: {failure_message}")
            self.append_log(f"Run failed: {exc}")
        finally:
            active_run = self.active_run
            if active_run is not None:
                self.last_run_result = LastRunResult(
                    selected_id=active_run.selected_id,
                    selected_title=active_run.selected_title,
                    finished_at=datetime.now(),
                    return_code=return_code,
                    failure_message=failure_message,
                )
            self.run_in_progress = False
            self.run_process = None
            self.current_run_selected_id = None
            self.current_run_started_at = None
            self.active_run = None
            try:
                if self.load_plan(plan_path, log_load=False, preserve_last_run=True):
                    reloaded_id = (
                        self.loaded_view.selected.id
                        if self.loaded_view is not None and self.loaded_view.selected is not None
                        else None
                    )
                    if reloaded_id is None:
                        self.append_log("Reloaded plan. No selected item.")
                    else:
                        self.append_log(f"Reloaded plan. Selected {reloaded_id}.")
                else:
                    self.append_log("Reload failed after run.")
            except Exception as exc:
                failed = True
                self.append_log(f"Reload failed after run: {exc}")
                self.loaded_view = None
                self.current_load_error = str(exc)
                if self.last_run_result is not None:
                    self.last_run_result.reload_error = str(exc)
                self.render_invalid_plan(str(exc))
                self.update_command_preview()
            if failed:
                self.append_log("Run failed.")
            if failed and return_code is None:
                self.update_run_status("Failed")
            elif return_code is None and not failed:
                self.update_run_status("Idle")
            if self.quit_after_run:
                self.exit()
            else:
                if self.safe_quit_dialog_open:
                    self.safe_quit_dialog_open = False
                    self.pop_screen()
                self.update_control_state()

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
        self.query_one("#log", Static).update(render_recent_log_lines(self.log_lines))


def run_tui(initial_plan_path: str | None = None) -> int:
    PlanExecutorTui(initial_plan_path).run()
    return 0
