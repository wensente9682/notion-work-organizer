[English](README.md) | [简体中文](README.zh-CN.md)

# Organize — Codex + Notion Daily Work Tracker

> An approval-first Codex + Notion starter kit for tracking daily work and turning completed tasks into reusable records.

Organize helps you adopt, track, review, and maintain a lightweight Notion
daily work system. It keeps active work simple while adding a deliberate
completed-work loop, so useful takeaways do not disappear when a task is done.
Every supported Notion write and cleanup step remains visible and
approval-first.

[**Use the current Existing List path →**](#existing-notion-list-path)

> **Product status:** Existing List adoption is available: read-only inspection,
> an approved ignored private profile, and one-profile Total/Organize readiness
> are implemented. New System provisioning is planned for v0.3 and is not a
> current executable path.

<a id="choose-your-starting-path"></a>

## Choose Your Starting Path

### Use an Existing Notion List — Available Now

Start from the work system you already use. Organize inspects it read-only,
reports what is ready, missing, or incompatible, maps the supported fields and
archive targets, and proposes the private configuration needed by Total and
Organize.

[See the Existing List path](#existing-notion-list-path)

### Start a New Notion System — Planned for v0.3

This future path will help someone without a compatible workspace create the
source list, category archives, ordered view, date anchors, block field, and
private configuration. The current release does not create or adjust those
workspace structures.

## Who It Is For

Use Organize if you want to:

- keep a lightweight Notion list for daily work rather than maintain a complex
  project-management system;
- review completed work before deciding what to preserve or clean up;
- turn selected takeaways and improvements into reusable, category-organized
  records; and
- keep Notion writes and cleanup under explicit control.

## Who It Is Not For

Organize is not a standalone task manager, separate GUI, automation platform,
or unattended workspace manager. It is not for silent database creation,
one-click migration, broad repair of existing data, or source cleanup without
review and explicit confirmation.

## Core Workflow

`Adopt → Track → total YYYY-MM → organize todo → Preserve → done → confirm`

1. **Adopt:** inspect an existing compatible system, approve its private local
   profile separately, and verify one-profile readiness.
2. **Track:** record daily work, completion, category, block text, and optional
   takeaways or improvements in Notion.
3. **Review the month:** run `total YYYY-MM` before Organize when you want a
   read-only, point-in-time view of completed blocks by category. This step is
   recommended, not required, and it does not save a historical report.
4. **Review completed work:** run `organize todo`, then choose `ok`, `dismiss`,
   or `skip` for each item. Use numeric `undo` only within its supported active
   batch scope.
5. **Preserve:** `Category` routes an approved record to the selected archive
   target. Archive rows contain only `Task`, `Takeaway`, and `Improvement`.
6. **Confirm cleanup separately:** `done` shows the summary. Only a subsequent
   `confirm` can clean up eligible source rows that still pass all checks.

![Workflow overview: capture, track, complete, reflect, organize, and archive daily work](docs/workflow-overview.svg)

_The diagram highlights the daily-work and completed-work review loop. It does
not depict every read or validation performed by `total YYYY-MM`._

## Quick Start

Prerequisites: Git, Codex, and access to the Notion connector.

1. Clone the repository and install the Skill:

   ```sh
   git clone https://github.com/wensente9682/notion-work-organizer.git
   cd notion-work-organizer
   python3 -B skills/todo-archive-review/scripts/install.py
   ```

2. Start a Codex task and enable the Notion connector only for the pages and
   databases Organize should use.
3. Start with the currently available Existing List path:

   **Existing List**

   ```text
   Use Organize with my existing Notion to-do list. Start read-only.
   ```

4. After the one-profile readiness check passes, review a month and then
   organize its completed work:

   ```text
   total 2026-07
   organize todo
   ```

   `total` returns category block subtotals and an overlapping grand total; it
   does not display task-level details or change Notion.
5. Review every proposed write. Source cleanup always remains behind the
   separate `done` → `confirm` gate.

See a sanitized Organize review in
[examples/session-output.example.txt](examples/session-output.example.txt).

If Organize helps you build a more useful daily work practice, consider
starring the repository.

## Approval-First Safety

- Normal operation uses Codex + the Notion connector.
- Existing List adoption begins with read-only inspection.
- Planning, inspection, mapping, and preview do not authorize a Notion write.
- Every archive write or local private-profile change requires approval for
  that specific action. Future New System provisioning must retain the same
  action-specific approval boundary.
- `ok` and `dismiss` do not remove source rows. `done` is a summary; only a
  separate `confirm` can finalize eligible cleanup.
- Cleanup uses Notion's recoverable archived state, not permanent deletion.
- Total is read-only, returns no partial result, and never grants Organize
  approval.
- Public examples contain no real credentials, IDs, URLs, private task content,
  usernames, or machine-specific paths.

## Current Boundaries

- Organize runs in Codex with the Notion connector; it does not ship a separate
  application or GUI.
- New System provisioning is planned for v0.3. The current release does not
  create or adjust databases, fields, views, or archive targets.
- Adoption does not broadly scan, deduplicate, repair, or maintain unrelated
  Notion content.
- Total reads only the configured ordered view, counts completed items, and
  does not read archives or reconstruct work already cleaned up by Organize.
- Total is a point-in-time calculation, not a saved dashboard or historical
  reporting system.
- Archive records intentionally omit source-only workflow fields.
- The Python CLI is an advanced fallback, not the normal user path.

## Existing Notion List Path

Use this path when you already have a Notion to-do list or work log.

1. Select the source list and relevant category archive targets.
2. Let Organize inspect them read-only.
3. Review the sanitized `ready`, `missing`, and `incompatible` report covering:
   - source work fields;
   - supported Category routing and archive targets;
   - the archive payload surface;
   - the configured ordered view, structured date anchors, and block field; and
   - the mappings needed by Total and Organize.
4. Confirm the proposed mappings.
5. Approve creation or update of the ignored private profile as a separate local
   action.
6. Use that same profile to validate Total read-only, then enter an Organize
   read-only preview.

Inspection or profile approval never authorizes a Notion mutation, archive
write, or source cleanup. Ambiguous, inaccessible, incomplete, unsupported, or
externally changed inputs stop safely.

## New Notion System Path — Planned for v0.3

This is the planned path for users who do not yet have a compatible daily work
system. It is not executable in the current release, and there is no current
Quick Start prompt or command that provisions a workspace.

The v0.3 path is expected to:

- begin with a read-only plan and `ready` / `missing` inspection;
- create or adjust each database, field, view, archive target, or configuration
  only after approval for that exact action; and
- finish only when one approved private profile passes Total's read-only
  compatibility validation and reaches an Organize read-only preview.

Until that capability is implemented and accepted, use the Existing List path
with a compatible Notion work system. Future readiness still will not authorize
archive records or source cleanup.

## Required Schema

Field labels may differ when an inspected mapping is supported end to end.

### Source work list

| Role | Typical Notion type | Purpose |
| --- | --- | --- |
| `Task` | title | Daily work item; copied to archive `Task`. |
| `Done` | checkbox | Completion gate for Total and Organize. |
| `Category` | supported select, multi-select, relation, status, or mapped text | Routes attribution and archive records. |
| `Takeaway` | rich text | Optional reusable takeaway. |
| `Improvement` | rich text | Optional improvement note. |
| Date anchor | structured date | Defines the ordered date section inherited by following rows. |
| Block field | rich text | Stores one non-negative block value for Total. |

Total also requires one explicitly configured ordered view. It follows that
view's saved order and fails closed when ordering, pagination, field shape, or
the requested month boundary cannot be verified.

### Category archive targets

| Role | Typical Notion type | Purpose |
| --- | --- | --- |
| `Task` | title | Copied from the source item. |
| `Takeaway` | rich text | Copied from the source item. |
| `Improvement` | rich text | Copied from the source item. |

Archive targets do not mirror every source property.

<details>
<summary><strong>Advanced configuration and sanitized examples</strong></summary>

Public examples use placeholders only:

- [config.example.json](config.example.json)
- [real_profile.example.json](real_profile.example.json)
- [examples/](examples/)

Example Organize configuration:

```json
{
  "source_database_id": "YOUR_TODO_SOURCE_DATABASE_ID",
  "source_order": "bottom_first",
  "batch_size": 5,
  "move_limit": 30,
  "field_mapping": {
    "task": "Task",
    "done": "Done",
    "category": "Category",
    "takeaway": "Takeaway",
    "improvement": "Improvement"
  },
  "target_databases": {
    "example-category": "YOUR_EXAMPLE_CATEGORY_ARCHIVE_DATABASE_ID"
  }
}
```

Private profiles belong in ignored local storage. They may contain verified
source, archive, ordered-view, and field mappings required by Total and
Organize. Never paste a private profile, token, or real identifier into chat or
commit it to the repository.

</details>

<details>
<summary><strong>Python CLI fallback and Keychain</strong></summary>

Skip this section for normal Codex use. The Python CLI is for regression
testing, local debugging, or explicitly requested outside-Codex operation.

For outside-Codex use, credentials may be held in a temporary environment
variable or macOS Keychain. They must not be written to repository config:

```sh
read -s NOTION_TOKEN
security add-generic-password -U -a "$USER" -s codex-notion-token -w "$NOTION_TOKEN"
unset NOTION_TOKEN
```

The fallback CLI must retain the same approval, privacy, and fail-closed
boundaries as the normal Codex path.

</details>

<details>
<summary><strong>Maintainer and local preflight commands</strong></summary>

```sh
python3 notion_todo_workflow.py preflight
python3 notion_todo_workflow.py check
python3 notion_todo_workflow.py next
python3 notion_todo_workflow.py ok 1 3
python3 notion_todo_workflow.py dismiss 1
python3 notion_todo_workflow.py skip 2
python3 notion_todo_workflow.py done
python3 notion_todo_workflow.py confirm
python3 notion_todo_workflow.py status
python3 notion_todo_workflow.py undo 1
python3 setup_preflight.py --config config.example.json
```

Local preflight reads local fixtures and reports configuration or schema gaps.
It does not call Notion or modify workspace content.

</details>

## Testing and License

The repository maintains regression tests for setup, adoption, Organize, and
Total, together with Skill validation. Exact counts are intentionally omitted;
release evidence should report the checks run against the candidate being
accepted.

Licensed under the [MIT License](LICENSE).
