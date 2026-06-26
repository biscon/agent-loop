# Runner-Compatible Plan Format

This document describes how to write a Markdown plan that can be executed by `tools/plan_executor.py`.

The plan runner uses two layers:

1. A **machine-readable JSON state block** that tells the runner what item is next.
2. Human-readable Markdown sections that tell Codex what the selected item means, what rules to follow, and how to update the plan after execution.

The JSON block is the runner’s index. The Markdown is the agent’s instruction manual.

## Quick Start

A runner-compatible plan should contain:

````markdown
# My Execution Plan

## How To Use This Plan

This is a living execution plan. Future agent runs must read this section first,
select exactly one unfinished item, execute only that item, update this plan, and
stop.

```plan-state-json id="example"
{
  "plan_id": "my_execution_plan",
  "status_values": [
    "Not Started",
    "Planned",
    "In Progress",
    "Completed",
    "Deferred",
    "Blocked",
    "Partial"
  ],
  "items": [
    {
      "id": "phase_01",
      "title": "First Phase",
      "type": "phase",
      "status": "Not Started"
    },
    {
      "id": "phase_01a",
      "title": "First Concrete Pass",
      "type": "pass",
      "parent": "phase_01",
      "status": "Not Started"
    }
  ]
}
````

## Current Progress

| Phase / Pass                  | Status      | Date | Notes                  |
| ----------------------------- | ----------- | ---- | ---------------------- |
| Phase 1: First Phase          | Not Started |      | Parent phase.          |
| Phase 1A: First Concrete Pass | Not Started |      | First executable pass. |

````

Then inspect it with:

```bash
python3 tools/plan_executor.py docs/my_plan.md --status
````

## Required JSON Block

Every runner-compatible plan must contain exactly one fenced code block marked:

````markdown
```plan-state-json
...
````

````

The fence may include metadata:

```markdown
```plan-state-json id="sector-engine"
...
````

````

The contents must be valid JSON.

Do not use comments, trailing commas, single quotes, or JSON5 syntax.

## Minimal JSON Schema

The top-level object must contain:

```json
{
  "plan_id": "stable_plan_id",
  "items": []
}
````

### `plan_id`

A stable identifier for the plan.

Use lowercase snake case:

```json
"sector_engine_extraction_plan"
```

Do not change `plan_id` after execution has started unless you intentionally want new log/run naming.

### `status_values`

Optional but recommended.

Example:

```json
"status_values": [
  "Not Started",
  "Planned",
  "In Progress",
  "Completed",
  "Deferred",
  "Blocked",
  "Partial"
]
```

The runner treats these statuses as finished:

```text
Completed
Deferred
```

Everything else is unfinished and selectable unless parent/child rules apply.

### `items`

A list of phases and passes.

Each item must contain:

```json
{
  "id": "phase_01",
  "title": "Some Work",
  "type": "phase",
  "status": "Not Started"
}
```

Required fields:

| Field    | Meaning                            |
| -------- | ---------------------------------- |
| `id`     | Stable unique machine-readable id. |
| `title`  | Human-readable title.              |
| `type`   | Either `phase` or `pass`.          |
| `status` | Current status.                    |

Passes should also contain:

```json
"parent": "phase_01"
```

## Item IDs

Use stable ids.

Recommended format:

```text
phase_01
phase_01a
phase_01b
phase_02
phase_03
```

Rules:

* Do not rename ids after work has started.
* Do not reuse ids.
* Do not use spaces.
* Prefer lowercase.
* Use ids that remain meaningful in logs and commit messages.

Good:

```json
{
  "id": "phase_02a",
  "title": "Extract Gameplay Input Collection",
  "type": "pass",
  "parent": "phase_02",
  "status": "Not Started"
}
```

Bad:

```json
{
  "id": "do the stuff",
  "title": "Stuff",
  "type": "pass",
  "status": "todo"
}
```

## Status Semantics

Recommended statuses:

| Status        | Meaning                                                  |
| ------------- | -------------------------------------------------------- |
| `Not Started` | No implementation or detailed pass planning has begun.   |
| `Planned`     | Scope is concrete, but source work has not begun.        |
| `In Progress` | Work started but is not complete.                        |
| `Completed`   | Work was executed, checked, and plan was updated.        |
| `Deferred`    | Intentionally skipped/postponed with a recorded reason.  |
| `Blocked`     | Cannot continue without a decision or dependency.        |
| `Partial`     | Some work landed, but intended scope remains incomplete. |

Only `Completed` and `Deferred` are considered finished by the runner.

A phase containing child passes should only be marked `Completed` after all non-deferred child passes are `Completed`.

## Parent And Child Selection

A `phase` may contain child `pass` items.

Example:

```json
{
  "id": "phase_01",
  "title": "Decouple FPS Controller From Mesh Preview Pose",
  "type": "phase",
  "status": "Not Started"
},
{
  "id": "phase_01a",
  "title": "Introduce Neutral Sector View Pose",
  "type": "pass",
  "parent": "phase_01",
  "status": "Not Started"
},
{
  "id": "phase_01b",
  "title": "Update Call Sites",
  "type": "pass",
  "parent": "phase_01",
  "status": "Not Started"
}
```

Default behavior:

* The runner selects the first unfinished item.
* If an unfinished phase has unfinished child passes, the runner selects the first unfinished child pass.
* This keeps execution small and reviewable.

## Markdown Sections

The JSON block tells the runner what to select.

The Markdown tells Codex how to execute it.

A useful plan should include these sections:

```markdown
## How To Use This Plan
## Current Progress
## Execution Tracking Rules
## Goal And Desired End State
## Dependency Direction Rules
## Proposed Phases
## Deferred Decisions For Later Phases
```

For each phase/pass, include:

```markdown
### Phase 1: Example Phase

Goal:

Why it helps:

Files/functions likely touched:

Exact behavior that must remain unchanged:

Risks/goblins:

Non-goals:

Suggested tests/manual smoke checks:

Final report expectations:

How to update this plan after completion:
```

The exact headings are not mandatory, but the information is important.

## “How To Use This Plan” Template

Use this near the top of the plan:

```markdown
## How To Use This Plan

This is a living execution plan.

When an agent is asked to execute this plan, it must:

1. Read this section first.
2. Read the `plan-state-json` block.
3. Identify the selected phase/pass.
4. Execute only that selected phase/pass.
5. Do not skip ahead.
6. Do not execute multiple phases/passes in one run unless the selected item explicitly says it is a combined pass.
7. If the selected item is too broad, update this plan with smaller child passes and stop.
8. If smaller passes are added, do not also implement source changes in the same run unless explicitly instructed.
9. After executing a phase/pass, update this plan with status, date, summary, verification results, and behavior notes.
10. Do not claim manual verification unless it was actually performed.
11. Keep this plan self-tracking so future fresh-context runs can resume from it.
```

## Execution Tracking Rules Template

```markdown
## Execution Tracking Rules

- Each phase/pass must be independently buildable and testable.
- Each phase/pass final report must state whether source code changed.
- Each implementation phase/pass must update this document before finishing.
- The update should be small and local.
- Do not rewrite unrelated phases when marking progress.
- If behavior is intended to remain unchanged, explicitly state that.
- If a phase/pass changes serialization, generated data, public APIs, runtime behavior, cache invalidation, or build/test behavior, clearly say so.
- Do not claim manual GUI verification unless it was actually performed.
- If a phase/pass produces only a plan or audit and no source changes, state that clearly.
- If a phase is too broad, add smaller passes under that phase and stop.
```

## Current Progress Table

Keep a human-readable progress table near the top.

Example:

```markdown
## Current Progress

| Phase / Pass | Status | Date | Notes |
| --- | --- | --- | --- |
| Phase 1: Decouple FPS Controller From Mesh Preview Pose | Not Started |  | Parent phase. |
| Phase 1A: Introduce SectorViewPose And Switch FPS Controller Call Sites | Not Started |  | First executable pass. |
| Phase 1B: Update Call Sites And Compatibility Helpers | Deferred | 2026-06-26 | Folded into Phase 1A. |
```

The table should match the JSON block.

The runner reads the JSON block, not the table, but Codex and humans use the table.

Keep them synchronized.

## Updating The Plan After A Pass

After completing a pass, update both:

1. The JSON item status.
2. The human-readable progress table and relevant phase notes.

Example JSON update:

```json
{
  "id": "phase_01a",
  "title": "Introduce SectorViewPose And Switch FPS Controller Call Sites",
  "type": "pass",
  "parent": "phase_01",
  "status": "Completed"
}
```

Example table update:

```markdown
| Phase 1A: Introduce SectorViewPose And Switch FPS Controller Call Sites | Completed | 2026-06-26 | Added `SectorViewPose`, updated controller/call sites, build/tests passed. Behavior intended unchanged. |
```

If all non-deferred child passes are complete, update the parent phase too.

## Broad Work And Replanning

If the selected phase/pass is too broad, Codex should not guess its way through.

Instead it should:

1. Add smaller child passes under the current phase.
2. Mark the current item as `Planned`, `Partial`, or keep it `Not Started`, depending on the situation.
3. Stop without making source changes.

Example:

```json
{
  "id": "phase_02",
  "title": "Extract Gameplay Preview Update Boundary",
  "type": "phase",
  "status": "Planned"
},
{
  "id": "phase_02a",
  "title": "Extract Gameplay Preview Input Snapshot",
  "type": "pass",
  "parent": "phase_02",
  "status": "Not Started"
},
{
  "id": "phase_02b",
  "title": "Extract Horizontal Movement And Collision Step",
  "type": "pass",
  "parent": "phase_02",
  "status": "Not Started"
}
```

The runner’s `--run-all` mode should stop when a pass expands/replans work instead of completing the selected item. This allows a human to review the new subpasses.

## Optional `sandbox_dir`

Toy plans may include a top-level `sandbox_dir`.

Example:

```json
{
  "plan_id": "agent_loop_test_plan",
  "sandbox_dir": "agent_loop_sandbox",
  "items": []
}
```

When copy mode is used, the runner may patch this value in the copied plan so test artifacts land under the copied run workspace.

For real project plans, usually do not use `sandbox_dir`.

Real project plans should edit the actual repository files described by the selected phase/pass.

## Copy Mode Versus Real Project Mode

Copy mode is for testing the runner:

```bash
python3 tools/plan_executor.py docs/test_plan.md --copy-to-run-dir
```

This creates a copied plan under `.agent-runs/.../plan.md`.

Continue that copied run by pointing at the copied plan:

```bash
python3 tools/plan_executor.py .agent-runs/<run>/plan.md
```

For real project work, do not use copy mode:

```bash
python3 tools/plan_executor.py docs/real_project_plan.md
```

The real plan is updated in place and should be committed along with source changes.

## Runner Commands

Inspect next item:

```bash
python3 tools/plan_executor.py docs/my_plan.md --status
```

Execute one selected item:

```bash
python3 tools/plan_executor.py docs/my_plan.md
```

Execute one item with review:

```bash
python3 tools/plan_executor.py docs/my_plan.md --review-after-pass
```

Execute one item with review, bounded fix, and commit:

```bash
python3 tools/plan_executor.py docs/my_plan.md \
  --review-after-pass \
  --fix-after-review \
  --commit-after-pass
```

Run multiple items:

```bash
python3 tools/plan_executor.py docs/my_plan.md \
  --run-all \
  --max-passes 3
```

Run multiple items with review/fix/commit:

```bash
python3 tools/plan_executor.py docs/my_plan.md \
  --run-all \
  --max-passes 3 \
  --review-after-pass \
  --fix-after-review \
  --commit-after-pass
```

Prevent Linux idle sleep while running:

```bash
python3 tools/plan_executor.py docs/my_plan.md \
  --run-all \
  --inhibit-sleep
```

## Commit Mode Expectations

When using:

```bash
--commit-after-pass
```

the runner expects a clean worktree before execution.

Commit mode should commit:

* source changes made for the selected item
* test changes made for the selected item
* the active plan progress update

Commit mode should not commit:

* `.agent-runs/`
* local logs
* transient review/fix artifacts
* Python cache files

The runner may bootstrap local git excludes under:

```text
.git/info/exclude
```

This is local-only and should not dirty the worktree.

## Review And Fix Expectations

When using:

```bash
--review-after-pass
```

the runner asks a separate fresh Codex process to review the implementation diff.

The review should be read-only and must write review artifacts under `.agent-runs/`.

Allowed review verdicts:

```text
pass
needs_fix
needs_human
```

When using:

```bash
--fix-after-review
```

the runner may run one bounded fix pass only if the review verdict is `needs_fix`.

The fix pass must:

* fix only review-listed issues
* not broaden the selected item
* not start the next phase/pass
* not commit
* not push
* not use `gh`
* not create branches

After fixing, the runner checks the plan, reruns harness checks, and reruns review.

## Agent Prompt: Make An Existing Plan Runner-Compatible

Use this prompt when you already have a Markdown plan and want an agent to convert it.

```text
Read this Markdown plan and make it compatible with `tools/plan_executor.py`.

Use the format described in `docs/runner_compatible_plans.md`.

Requirements:

1. Add exactly one valid `plan-state-json` fenced block near the top of the plan.
2. Add or update a `How To Use This Plan` section.
3. Add or update a `Current Progress` table.
4. Give every executable phase/pass a stable id.
5. Use statuses from:
   - Not Started
   - Planned
   - In Progress
   - Completed
   - Deferred
   - Blocked
   - Partial
6. Use `Completed` and `Deferred` only for items that should be considered finished.
7. If a phase is broad, add child pass items and make the first executable pass small enough for one agent run.
8. Keep the JSON block and progress table synchronized.
9. Resolve only decisions needed for the first executable pass.
10. Move later unresolved questions into a `Deferred Decisions For Later Phases` section.
11. Do not change source code.
12. Do not mark implementation work completed unless it is already known to be complete.
13. Run:
    - `python3 tools/plan_executor.py <plan path> --status`
    - `git diff --check`
    - `git diff --stat`
    - `git status --short`

Final report:
- State what plan ids were created.
- State which item the runner selects first.
- State any decisions resolved for the first pass.
- State any deferred decisions left for later.
- Confirm source code was not changed.
```

## Agent Prompt: Create A New Runner-Compatible Plan

Use this prompt when starting from a goal instead of an existing plan.

```text
Create a runner-compatible Markdown execution plan for this goal:

<describe goal here>

Use the format described in `docs/runner_compatible_plans.md`.

Requirements:

1. Include a `How To Use This Plan` section.
2. Include exactly one valid `plan-state-json` fenced block.
3. Include a `Current Progress` table.
4. Split work into small phases/passes that can be executed one at a time.
5. Give every phase/pass a stable id.
6. Make the first selected item narrow enough for one agent run.
7. Include for each phase/pass:
   - goal
   - why it helps
   - likely files/functions touched
   - behavior that must remain unchanged
   - risks/goblins
   - non-goals
   - suggested checks
   - final report expectations
   - plan update instructions
8. Include dependency direction rules if architecture is involved.
9. Include deferred decisions for later phases.
10. Do not write source code.
11. Run:
    - `python3 tools/plan_executor.py <plan path> --status`
    - `git diff --check`
    - `git diff --stat`
    - `git status --short`

Final report:
- State the plan path.
- State the generated plan id.
- State the first selected item.
- State why the first item is safe to execute first.
- Confirm source code was not changed.
```

## Compatibility Checklist

Before using a plan with the runner, check:

```text
[ ] Exactly one `plan-state-json` block exists.
[ ] The JSON is valid.
[ ] `plan_id` is stable and meaningful.
[ ] Every item has `id`, `title`, `type`, and `status`.
[ ] Every pass has a valid `parent`.
[ ] Item ids are unique.
[ ] Status values use the plan’s status vocabulary.
[ ] Only `Completed` and `Deferred` mean finished.
[ ] The first selected item is intentionally the next task.
[ ] The progress table matches the JSON.
[ ] Broad phases are split into child passes.
[ ] The first pass has enough prose context for Codex to execute safely.
[ ] Non-goals and behavior-preservation rules are clear.
[ ] Checks/final-report expectations are listed.
[ ] Deferred decisions are not blocking the first pass.
[ ] `python3 tools/plan_executor.py <plan> --status` selects the expected item.
```

## Common Mistakes

### Multiple JSON Blocks

Bad:

````markdown
```plan-state-json
...
````

```plan-state-json
...
```

````

There must be exactly one.

### Markdown Table Updated But JSON Not Updated

The runner reads the JSON block.

If the table says `Completed` but JSON says `Not Started`, the runner will still select that item.

### JSON Updated But Prose Not Updated

Codex reads the prose.

If JSON says an item is selected but the phase section still contains stale instructions, Codex may do the wrong thing.

### Broad Phase As First Item

Bad first item:

```text
Refactor runtime architecture
````

Better first item:

```text
Introduce neutral view pose and switch controller call sites
```

### Open Questions Blocking First Pass

Do not leave first-pass decisions unresolved.

Resolve only what is needed for the next executable item. Move later questions to deferred decisions.

### Treating `.agent-runs/` As Progress

`.agent-runs/` is transient local audit/debug data.

The active plan file is the progress source of truth.

## Mental Model

A runner-compatible plan is a tiny state machine embedded in a Markdown document.

```text
JSON block:
  tells the runner what state the plan is in

Markdown:
  tells Codex what the state means

Executor:
  selects one item, builds a prompt, runs Codex, checks/reviews/fixes/commits, and stops

Plan update:
  advances the state machine
```

Keep the state machine boring. That is how you keep the goblins small.
