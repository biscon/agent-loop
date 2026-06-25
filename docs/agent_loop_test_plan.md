# Agent Loop Test Plan

## How To Use This Plan

This is a disposable self-tracking test plan for experimenting with an automated plan executor.

Future Codex runs may be given a minimal prompt such as:

```text
Read docs/agent_loop_test_plan.md and execute the next unfinished step.
```

When that happens, Codex must:

1. Read this “How To Use This Plan” section first.
2. Read the `plan-state-json` block below.
3. Identify the first item whose status is not `Completed` or `Deferred`.
4. Plan only that next unfinished item.
5. Execute only that one item.
6. Do not skip ahead.
7. Do not execute multiple phases/passes in one run unless this plan explicitly marks them as one combined pass.
8. If the selected phase/pass is too broad, stop and propose smaller passes under that phase instead of implementing it.
9. If new passes are proposed, update both the human-readable plan and the `plan-state-json` block, then stop.
10. After successfully executing a phase/pass, update this document in the same run:

    * mark the item `Completed`
    * set the date
    * add a short note
    * leave future items untouched
11. Keep this plan self-tracking so a future fresh-context run can resume from it.

This plan is intentionally stupid and low-risk. It exists only to test agent-loop mechanics.

All generated files must live under:

```text
agent_loop_sandbox/
```

Do not modify source code, build files, project configuration, or unrelated docs.

## Machine-Readable Plan State

```plan-state-json
{
  "plan_id": "agent_loop_test_plan",
  "version": 1,
  "plan_file": "docs/agent_loop_test_plan.md",
  "sandbox_dir": "agent_loop_sandbox",
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
      "title": "Create sandbox and fixed number list",
      "type": "phase",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_02",
      "title": "Calculate number sum",
      "type": "phase",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_03",
      "title": "Calculate sorted differences",
      "type": "phase",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_04",
      "title": "Calculate basic statistics",
      "type": "phase",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_05",
      "title": "Classify numbers as even or odd",
      "type": "phase",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_06",
      "title": "Word artifact mini-project",
      "type": "phase",
      "status": "Not Started",
      "date": "",
      "notes": "Contains subpasses phase_06a, phase_06b, and phase_06c."
    },
    {
      "id": "phase_06a",
      "title": "Create word list",
      "type": "pass",
      "parent": "phase_06",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_06b",
      "title": "Count word lengths",
      "type": "pass",
      "parent": "phase_06",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_06c",
      "title": "Write word summary",
      "type": "pass",
      "parent": "phase_06",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_07",
      "title": "Create CSV report",
      "type": "phase",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_08",
      "title": "Create ASCII bar chart",
      "type": "phase",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_09",
      "title": "Validation script mini-project",
      "type": "phase",
      "status": "Not Started",
      "date": "",
      "notes": "Contains subpasses phase_09a and phase_09b."
    },
    {
      "id": "phase_09a",
      "title": "Create validation script",
      "type": "pass",
      "parent": "phase_09",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_09b",
      "title": "Run validation script and record result",
      "type": "pass",
      "parent": "phase_09",
      "status": "Not Started",
      "date": "",
      "notes": ""
    },
    {
      "id": "phase_10",
      "title": "Write final summary",
      "type": "phase",
      "status": "Not Started",
      "date": "",
      "notes": ""
    }
  ]
}
```

## Status Legend

* `Not Started`: no work has been performed yet.
* `Planned`: Codex created a plan but did not execute it.
* `In Progress`: work started but did not complete.
* `Completed`: work completed and required checks passed.
* `Deferred`: intentionally skipped; reason must be written in notes.
* `Blocked`: cannot proceed without human input.
* `Partial`: some work completed, but the item is not done.

A parent phase that contains passes is only `Completed` when all non-deferred child passes are `Completed`.

## Current Progress

| Phase / Pass                                       | Status      | Date | Notes                     |
| -------------------------------------------------- | ----------- | ---- | ------------------------- |
| Phase 01: Create sandbox and fixed number list     | Not Started |      |                           |
| Phase 02: Calculate number sum                     | Not Started |      |                           |
| Phase 03: Calculate sorted differences             | Not Started |      |                           |
| Phase 04: Calculate basic statistics               | Not Started |      |                           |
| Phase 05: Classify numbers as even or odd          | Not Started |      |                           |
| Phase 06: Word artifact mini-project               | Not Started |      | Parent phase for 06A–06C. |
| Phase 06A: Create word list                        | Not Started |      |                           |
| Phase 06B: Count word lengths                      | Not Started |      |                           |
| Phase 06C: Write word summary                      | Not Started |      |                           |
| Phase 07: Create CSV report                        | Not Started |      |                           |
| Phase 08: Create ASCII bar chart                   | Not Started |      |                           |
| Phase 09: Validation script mini-project           | Not Started |      | Parent phase for 09A–09B. |
| Phase 09A: Create validation script                | Not Started |      |                           |
| Phase 09B: Run validation script and record result | Not Started |      |                           |
| Phase 10: Write final summary                      | Not Started |      |                           |

## Execution Tracking Rules

* Execute exactly one unfinished phase/pass per Codex run.
* Do not continue to the next item after completing one item.
* Keep all generated artifacts under `agent_loop_sandbox/`.
* Do not modify source code.
* Do not modify build files.
* Do not modify unrelated documentation.
* Every completed item must update:

  * the `plan-state-json` block
  * the Current Progress table
  * the relevant phase/pass status section
* Use the local date in `YYYY-MM-DD` format.
* Required checks after each item:

  * `git diff --check`
  * `git diff --stat`
  * `git status --short`
* Do not claim manual verification unless actually performed.
* If an item is too broad, split it into passes in this file and stop.

## Phase 01: Create Sandbox And Fixed Number List

### Goal

Create the sandbox folder and a deterministic list of ten numbers.

### Work

Create:

```text
agent_loop_sandbox/numbers.txt
```

The file must contain exactly these numbers, one per line:

```text
17
4
23
8
15
42
16
9
31
2
```

Also create:

```text
agent_loop_sandbox/README.md
```

The README should explain that this folder is generated by the agent loop test plan.

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_01` as `Completed`.

## Phase 02: Calculate Number Sum

### Goal

Calculate the sum of the numbers in `numbers.txt`.

### Work

Read:

```text
agent_loop_sandbox/numbers.txt
```

Create:

```text
agent_loop_sandbox/sum.txt
```

Expected content:

```text
167
```

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_02` as `Completed`.

## Phase 03: Calculate Sorted Differences

### Goal

Sort the numbers ascending and calculate the differences between adjacent sorted numbers.

### Work

Create:

```text
agent_loop_sandbox/sorted_numbers.txt
agent_loop_sandbox/differences.txt
```

Expected sorted numbers:

```text
2
4
8
9
15
16
17
23
31
42
```

Expected adjacent differences:

```text
2
4
1
6
1
1
6
8
11
```

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_03` as `Completed`.

## Phase 04: Calculate Basic Statistics

### Goal

Create a small JSON statistics file for the number list.

### Work

Create:

```text
agent_loop_sandbox/stats.json
```

Expected values:

* count: `10`
* sum: `167`
* min: `2`
* max: `42`
* average: `16.7`

The JSON should be pretty-printed with two-space indentation.

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_04` as `Completed`.

## Phase 05: Classify Numbers As Even Or Odd

### Goal

Create a markdown table classifying each number as even or odd.

### Work

Create:

```text
agent_loop_sandbox/even_odd.md
```

The table should have columns:

```text
Number | Classification
```

Use `even` or `odd`.

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_05` as `Completed`.

## Phase 06: Word Artifact Mini-Project

### Goal

Create a tiny word-list artifact through subpasses.

### Passes

* Phase 06A: Create word list.
* Phase 06B: Count word lengths.
* Phase 06C: Write word summary.

The parent phase should be marked `Completed` only after all non-deferred child passes are `Completed`.

## Phase 06A: Create Word List

### Goal

Create a deterministic word list.

### Work

Create:

```text
agent_loop_sandbox/words.txt
```

Content:

```text
goblin
cache
iterator
abstraction
coverage
dependency
```

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_06a` as `Completed`.

## Phase 06B: Count Word Lengths

### Goal

Create a word length report.

### Work

Read:

```text
agent_loop_sandbox/words.txt
```

Create:

```text
agent_loop_sandbox/word_lengths.json
```

Expected values:

```json
{
  "goblin": 6,
  "cache": 5,
  "iterator": 8,
  "abstraction": 11,
  "coverage": 8,
  "dependency": 10
}
```

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_06b` as `Completed`.

## Phase 06C: Write Word Summary

### Goal

Write a short markdown summary of the word artifact.

### Work

Create:

```text
agent_loop_sandbox/word_summary.md
```

It should mention:

* total word count
* longest word
* shortest word
* average word length

Expected facts:

* total word count: `6`
* longest word: `abstraction`
* shortest word: `cache`
* average word length: `8.0`

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_06c` as `Completed`.

If `phase_06a`, `phase_06b`, and `phase_06c` are all completed, also mark parent `phase_06` as `Completed`.

## Phase 07: Create CSV Report

### Goal

Create a CSV version of the number artifact.

### Work

Create:

```text
agent_loop_sandbox/numbers.csv
```

Columns:

```text
index,number,classification
```

Index should be 1-based and preserve the original `numbers.txt` order.

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_07` as `Completed`.

## Phase 08: Create ASCII Bar Chart

### Goal

Create a simple text bar chart of the numbers.

### Work

Create:

```text
agent_loop_sandbox/bar_chart.txt
```

Each line should contain the number followed by a bar made of `#`.

Example format:

```text
17 | #################
4  | ####
```

Preserve the original `numbers.txt` order.

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_08` as `Completed`.

## Phase 09: Validation Script Mini-Project

### Goal

Create and run a validation script through subpasses.

### Passes

* Phase 09A: Create validation script.
* Phase 09B: Run validation script and record result.

The parent phase should be marked `Completed` only after all non-deferred child passes are `Completed`.

## Phase 09A: Create Validation Script

### Goal

Create a small Python validation script.

### Work

Create:

```text
agent_loop_sandbox/validate_outputs.py
```

The script should validate that expected files exist and that:

* `sum.txt` contains `167`
* `stats.json` has count `10`, sum `167`, min `2`, max `42`, average `16.7`
* `sorted_numbers.txt` matches the expected sorted list
* `differences.txt` matches the expected differences
* `word_lengths.json` matches the expected word lengths

The script should print `VALIDATION PASSED` and exit `0` on success.

It should print a useful error and exit nonzero on failure.

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_09a` as `Completed`.

## Phase 09B: Run Validation Script And Record Result

### Goal

Run the validation script and record its output.

### Work

Run:

```bash
python3 agent_loop_sandbox/validate_outputs.py
```

Create:

```text
agent_loop_sandbox/validation_result.txt
```

Expected content should include:

```text
VALIDATION PASSED
```

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_09b` as `Completed`.

If `phase_09a` and `phase_09b` are both completed, also mark parent `phase_09` as `Completed`.

## Phase 10: Write Final Summary

### Goal

Write a final markdown summary of the generated sandbox artifacts.

### Work

Create:

```text
agent_loop_sandbox/final_summary.md
```

It should summarize:

* the number list
* sum
* min/max/average
* even/odd classification artifact
* word artifact
* validation result

### Checks

Run:

```bash
git diff --check
git diff --stat
git status --short
```

### Completion Update

Mark `phase_10` as `Completed`.

## Completion Criteria

This test plan is complete when all items in `plan-state-json` are either `Completed` or `Deferred`, and no item is `Not Started`, `Planned`, `In Progress`, `Blocked`, or `Partial`.

Generated files should remain under:

```text
agent_loop_sandbox/
```

Do not delete the sandbox automatically.
