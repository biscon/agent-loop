"""Textual status/configuration shell for plan_executor."""

from __future__ import annotations

import asyncio
import json
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
MAX_RAW_OUTPUT_RENDER_LINES = 1000
MAX_RAW_OUTPUT_RENDER_LINE_CHARS = 4000
RAW_OUTPUT_REFRESH_INTERVAL_SECONDS = 0.15
RAW_OUTPUT_TRUNCATION_TEXT = "[output truncated: oldest lines dropped]"
TUI_RESULT_JSON_PATH = plan_executor.DEFAULT_RUNS_DIR / "tui-latest-run-result.json"


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


@dataclass
class LatestRunMetadata:
    result_json_path: str
    plan_file: str | None = None
    original_plan_file: str | None = None
    run_dir: str | None = None
    logs_dir: str | None = None
    selected_before_id: str | None = None
    selected_before_title: str | None = None
    review_requested: bool | None = None
    fix_after_review_requested: bool | None = None
    review_verdict: str | None = None
    review_summary: str | None = None
    review_result_md: str | None = None
    review_result_json: str | None = None
    review_after_fix_verdict: str | None = None
    review_after_fix_summary: str | None = None
    review_after_fix_result_md: str | None = None
    review_after_fix_result_json: str | None = None
    load_error: str | None = None


def optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def selected_item_field(raw: dict[str, Any], key: str) -> str | None:
    selected_before = raw.get("selected_before")
    if not isinstance(selected_before, dict):
        return None
    value = selected_before.get(key)
    return value if isinstance(value, str) else None


def load_latest_run_metadata(path: Path) -> LatestRunMetadata:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return LatestRunMetadata(
            result_json_path=str(path),
            load_error=f"failed to read result metadata: {exc}",
        )
    except json.JSONDecodeError as exc:
        return LatestRunMetadata(
            result_json_path=str(path),
            load_error=f"failed to parse result metadata: {exc}",
        )
    if not isinstance(raw, dict):
        return LatestRunMetadata(
            result_json_path=str(path),
            load_error="result metadata root is not an object",
        )
    return LatestRunMetadata(
        result_json_path=str(path),
        plan_file=optional_str(raw, "plan_file"),
        original_plan_file=optional_str(raw, "original_plan_file"),
        run_dir=optional_str(raw, "run_dir"),
        logs_dir=optional_str(raw, "logs_dir"),
        selected_before_id=selected_item_field(raw, "id"),
        selected_before_title=selected_item_field(raw, "title"),
        review_requested=(
            raw.get("review_requested")
            if isinstance(raw.get("review_requested"), bool)
            else None
        ),
        fix_after_review_requested=(
            raw.get("fix_after_review_requested")
            if isinstance(raw.get("fix_after_review_requested"), bool)
            else None
        ),
        review_verdict=optional_str(raw, "review_verdict"),
        review_summary=optional_str(raw, "review_summary"),
        review_result_md=optional_str(raw, "review_result_md"),
        review_result_json=optional_str(raw, "review_result_json"),
        review_after_fix_verdict=optional_str(raw, "review_after_fix_verdict"),
        review_after_fix_summary=optional_str(raw, "review_after_fix_summary"),
        review_after_fix_result_md=optional_str(raw, "review_after_fix_result_md"),
        review_after_fix_result_json=optional_str(raw, "review_after_fix_result_json"),
    )


@dataclass(frozen=True)
class ReviewArtifactSelection:
    status: str
    artifact_path: Path | None = None
    artifact_label: str | None = None
    json_path: Path | None = None
    message: str | None = None
    error: str | None = None


def readable_file(path_text: str | None) -> Path | None:
    if not path_text:
        return None
    path = Path(path_text)
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def select_latest_review_artifact(
    metadata: LatestRunMetadata | None,
) -> ReviewArtifactSelection:
    if metadata is None:
        return ReviewArtifactSelection(
            status="no_metadata",
            message=(
                "No review result yet.\n"
                "Run a pass with Review after pass enabled to capture a review."
            ),
        )
    if metadata.load_error:
        return ReviewArtifactSelection(
            status="metadata_error",
            message=f"Latest run metadata could not be loaded:\n{metadata.load_error}",
            error=metadata.load_error,
        )
    if metadata.review_requested is False:
        return ReviewArtifactSelection(
            status="review_not_requested",
            message=(
                "No review was run for the latest pass.\n"
                "Enable Review after pass to show review output here."
            ),
        )

    candidates = [
        (
            "Review after fix",
            metadata.review_after_fix_result_md,
            metadata.review_after_fix_result_json,
        ),
        ("Review", metadata.review_result_md, metadata.review_result_json),
    ]
    missing_paths = []
    for label, markdown_path_text, json_path_text in candidates:
        if not markdown_path_text:
            continue
        markdown_path = Path(markdown_path_text)
        selected_path = readable_file(markdown_path_text)
        if selected_path is not None:
            return ReviewArtifactSelection(
                status="selected",
                artifact_path=selected_path,
                artifact_label=label,
                json_path=Path(json_path_text) if json_path_text else None,
            )
        missing_paths.append(str(markdown_path))

    if missing_paths:
        return ReviewArtifactSelection(
            status="artifact_unreadable",
            message=(
                "Review was requested, but no review markdown artifact was found."
            ),
            error="\n".join(missing_paths),
        )
    return ReviewArtifactSelection(
        status="artifact_missing",
        message="Review was requested, but no review markdown artifact was found.",
    )


def load_review_markdown(selection: ReviewArtifactSelection) -> tuple[str | None, str | None]:
    if selection.artifact_path is None:
        return None, selection.message
    try:
        return selection.artifact_path.read_text(encoding="utf-8"), None
    except OSError as exc:
        return (
            None,
            f"Could not read review artifact:\n{selection.artifact_path}\n{exc}",
        )


def read_review_summary_from_json(path: Path | None) -> tuple[str | None, str | None]:
    if path is None:
        return None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    verdict = raw.get("verdict")
    summary = raw.get("summary")
    return (
        verdict if isinstance(verdict, str) and verdict else None,
        summary if isinstance(summary, str) and summary else None,
    )


def review_run_text(metadata: LatestRunMetadata | None) -> str:
    if metadata is None or metadata.selected_before_id is None:
        return "Not available"
    if metadata.selected_before_title:
        return f"{metadata.selected_before_id} - {metadata.selected_before_title}"
    return metadata.selected_before_id


def review_status_text(
    metadata: LatestRunMetadata | None,
    selection: ReviewArtifactSelection,
) -> str:
    if selection.status != "selected":
        return "Unavailable"
    if metadata is None:
        return "Review available"
    if selection.artifact_label == "Review after fix":
        verdict = metadata.review_after_fix_verdict
        summary = metadata.review_after_fix_summary
    else:
        verdict = metadata.review_verdict
        summary = metadata.review_summary
    if verdict is None and summary is None:
        verdict, summary = read_review_summary_from_json(selection.json_path)
    if verdict and summary:
        return f"{verdict} - {summary}"
    if verdict:
        return verdict
    if summary:
        return summary
    return "Review available"


def render_review_details(
    metadata: LatestRunMetadata | None,
    selection: ReviewArtifactSelection,
) -> str:
    if selection.status != "selected":
        return "Review"
    return "\n".join(
        [
            "Review",
            "",
            "Run:",
            f"  {review_run_text(metadata)}",
            "",
            "Source:",
            f"  {selection.artifact_path}",
            "",
            "Status:",
            f"  {review_status_text(metadata, selection)}",
        ]
    )


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


def render_raw_output_line(line: RawOutputLine) -> str:
    text = line.text
    if len(text) > MAX_RAW_OUTPUT_RENDER_LINE_CHARS:
        text = f"{text[:MAX_RAW_OUTPUT_RENDER_LINE_CHARS]} [line truncated]"
    return f"[{line.stream}] {text}"


def render_raw_output_lines(
    state: RawOutputState,
    *,
    max_render_lines: int = MAX_RAW_OUTPUT_RENDER_LINES,
) -> str:
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
    if max_render_lines > 0 and len(state.lines) > max_render_lines:
        rendered.append(
            "[output view showing latest "
            f"{max_render_lines} of {len(state.lines)} retained lines]"
        )
        lines = state.lines[-max_render_lines:]
    else:
        lines = state.lines
    rendered.extend(render_raw_output_line(line) for line in lines)
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

    #review-view {
        height: 1fr;
        padding: 0 1;
    }

    #review-details {
        height: 10;
        border: round $accent;
        padding: 0 1;
    }

    #review-scroll {
        height: 1fr;
        border: round $accent;
        padding: 0 1;
        margin: 1 0 0 0;
    }

    #review-markdown {
        height: auto;
    }
    """

    BINDINGS = [
        Binding("f2", "show_dashboard", "Dashboard"),
        Binding("f3", "show_output", "Output"),
        Binding("f4", "show_review", "Review"),
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
        self.latest_run_metadata: LatestRunMetadata | None = None
        self.current_result_json_path: Path | None = None
        self.current_load_error: str | None = None
        self.elapsed_refresh_timer: Any | None = None
        self.raw_output_refresh_timer: Any | None = None
        self.raw_output_state = RawOutputState()
        self.raw_output_dirty = False
        self.active_view = "dashboard"

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
        with Vertical(id="review-view"):
            yield Static("", id="review-details")
            review_scroll = ScrollableContainer(id="review-scroll", can_focus=True)
            review_scroll.border_title = "Review"
            with review_scroll:
                yield Static("", id="review-markdown")
        yield Footer()

    def on_mount(self) -> None:
        self.append_log("TUI started.")
        self.query_one("#raw-output-view").display = False
        self.query_one("#review-view").display = False
        self.refresh_raw_output_view(auto_scroll=False)
        self.refresh_review_view(auto_scroll=False)
        self.elapsed_refresh_timer = self.set_interval(
            1.0, self.refresh_running_selection
        )
        self.raw_output_refresh_timer = self.set_interval(
            RAW_OUTPUT_REFRESH_INTERVAL_SECONDS, self.refresh_dirty_raw_output_view
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
        self.active_view = "dashboard"
        self.query_one("#raw-output-view").display = False
        self.query_one("#review-view").display = False
        self.query_one("#dashboard-view").display = True
        self.set_focus(None)

    def action_show_output(self) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            focused.blur()
        self.active_view = "output"
        self.query_one("#dashboard-view").display = False
        self.query_one("#review-view").display = False
        self.query_one("#raw-output-view").display = True
        output_scroll = self.query_one("#raw-output-scroll", ScrollableContainer)
        self.set_focus(output_scroll)
        if self.refresh_raw_output_view(auto_scroll=True):
            self.raw_output_dirty = False

    def action_show_review(self) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            focused.blur()
        self.active_view = "review"
        self.query_one("#dashboard-view").display = False
        self.query_one("#raw-output-view").display = False
        self.query_one("#review-view").display = True
        review_scroll = self.query_one("#review-scroll", ScrollableContainer)
        self.set_focus(review_scroll)
        self.refresh_review_view(auto_scroll=False)

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
        self.latest_run_metadata = None
        self.refresh_review_view(auto_scroll=False)
        self.current_load_error = None
        self.current_run_selected_id = selected_id
        self.current_result_json_path = TUI_RESULT_JSON_PATH
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
            self.run_selected_pass(
                plan_path,
                selected_id,
                options,
                self.current_result_json_path,
            ),
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

    def refresh_raw_output_view(self, *, auto_scroll: bool = True) -> bool:
        try:
            raw_output_details = self.query_one("#raw-output-details", Static)
            raw_output_text = self.query_one("#raw-output-text", Static)
            output_scroll = self.query_one("#raw-output-scroll", ScrollableContainer)
        except Exception:
            return False
        raw_output_details.update(render_raw_output_details(self.raw_output_state))
        raw_output_text.update(render_raw_output_lines(self.raw_output_state))
        if auto_scroll:
            output_scroll.scroll_end(animate=False)
        return True

    def refresh_dirty_raw_output_view(self) -> None:
        if not self.raw_output_dirty or self.active_view != "output":
            return
        if self.refresh_raw_output_view(auto_scroll=True):
            self.raw_output_dirty = False

    def refresh_raw_output_if_visible(self, *, auto_scroll: bool = True) -> None:
        if self.active_view != "output":
            return
        if self.refresh_raw_output_view(auto_scroll=auto_scroll):
            self.raw_output_dirty = False

    def refresh_review_view(self, *, auto_scroll: bool = False) -> None:
        if self.run_in_progress:
            self.query_one("#review-details", Static).update("Review")
            self.query_one("#review-markdown", Static).update(
                "Run in progress. Review output will be available after completion if review is enabled."
            )
        else:
            selection = select_latest_review_artifact(self.latest_run_metadata)
            self.query_one("#review-details", Static).update(
                render_review_details(self.latest_run_metadata, selection)
            )
            markdown_text, error = load_review_markdown(selection)
            self.query_one("#review-markdown", Static).update(
                markdown_text or error or selection.message or ""
            )
        if auto_scroll:
            review_scroll = self.query_one("#review-scroll", ScrollableContainer)
            review_scroll.scroll_home(animate=False)

    def update_raw_output_status(self, message: str) -> None:
        self.raw_output_state.status = message
        self.raw_output_dirty = True

    def append_raw_output(self, stream: str, text: str) -> None:
        append_raw_output_line(self.raw_output_state, stream, text)
        self.raw_output_dirty = True

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
        result_json_path: Path | None = None,
    ) -> None:
        return_code: int | None = None
        failed = False
        failure_message: str | None = None
        result_json_path = result_json_path or TUI_RESULT_JSON_PATH
        self.current_result_json_path = result_json_path
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
            argv = plan_executor.build_tui_subprocess_argv(
                plan_path,
                options,
                result_json_path=result_json_path,
            )
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
            self.raw_output_dirty = True
            self.refresh_raw_output_if_visible(auto_scroll=False)
            try:
                result_json_path.unlink(missing_ok=True)
            except OSError as exc:
                self.append_log(f"Could not clear prior result metadata: {exc}")
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
            self.latest_run_metadata = load_latest_run_metadata(result_json_path)
            self.refresh_review_view(auto_scroll=False)
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
            self.refresh_raw_output_if_visible(auto_scroll=True)
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
            self.current_result_json_path = None
            self.active_run = None
            self.refresh_review_view(auto_scroll=False)
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
