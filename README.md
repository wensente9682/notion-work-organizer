# Organize

> An approval-first Codex workflow for reviewing completed Notion work and turning useful takeaways into reusable records.

Organize is an open-source workflow for building, using, and maintaining a
lightweight Notion daily work record. Capture active work in your own Notion
list, review completed work in Codex, and preserve useful takeaways without
turning your workspace into a complex project-management system.

[**Install the workflow →**](#quick-start)

![Workflow overview: capture, track, complete, reflect, organize, and archive daily work](docs/workflow-overview.svg)

_The diagram describes the current Codex + Notion workflow. Organize does not
ship a separate GUI._

## Who It Is For

Use Organize if you want to:

- keep a small Notion to-do or work-log database for day-to-day work;
- review completed items before deciding what to preserve or clean up; and
- turn selected takeaways and improvements into category-organized records.

## Who It Is Not For

Organize is not for workflows that automatically create or migrate a Notion
workspace, bulk-repair existing data, or write to Notion or clean up source
rows without user review and explicit confirmation.

## Core Workflow

1. Record daily work in a Notion source list with `Task`, `Done`, `Category`,
   `Takeaway`, and `Improvement`.
2. Mark work `Done`, then add a takeaway or improvement when there is something
   worth preserving.
3. Run `organize todo` to review a small configured batch. Choose `ok`,
   `dismiss`, or `skip` for each item; use `undo` only for the allowed local or
   archive-copy reversal scope.
4. `Category` chooses the archive destination. An approved archive row copies
   only `Task`, `Takeaway`, and `Improvement`.
5. Run `done` to see the summary. Source cleanup happens only after a separate
   `confirm`, and only for eligible rows that pass the workflow's checks.

## Quick Start

Prerequisites: Git, Codex, and access to the Notion connector.

1. Clone the repository and install the skill in Codex's user skill directory:

   ```sh
   git clone https://github.com/wensente9682/notion-work-organizer.git
   cd notion-work-organizer
   mkdir -p ~/.agents/skills
   cp -R skills/todo-archive-review ~/.agents/skills/
   ```

2. Start a Codex task and enable the Notion connector with access only to the
   Notion databases this workflow should use.
3. Choose the prompt that matches your starting point:

   ```text
   Use Organize with my existing Notion to-do list. Start read-only.
   ```

   ```text
   Set up a new Notion daily work record. Start with a plan only.
   ```

   If it is already configured, start a review with:

   ```text
   organize todo
   ```

4. Review the proposed field mapping and the read-only preview. Approve each
   Notion write separately; source cleanup has its own `done` → `confirm` gate.

For normal Codex use, do not paste a Notion token into chat or add one to
project config. See a sanitized review in
[examples/session-output.example.txt](examples/session-output.example.txt).

## Approval-First Safety

- Codex uses the Notion connector for normal operation; the Python CLI is an
  advanced fallback for outside-Codex work or an explicitly requested command
  line path.
- Setup and adoption begin read-only. Real Notion writes require approval for
  the specific action.
- `ok` and `dismiss` do not remove source rows. `done` shows the run summary;
  only a subsequent `confirm` can finalize eligible source cleanup.
- Cleanup archives a source page with Notion's recoverable `archived: true`
  state. It is not a permanent destruction operation.
- Public examples never include tokens, real database IDs, real Notion URLs,
  or private page IDs. Local runtime state stays in ignored files such as
  `.todo_archive/`.

## Current Boundaries

- Organize does not automatically create databases, fields, archive tables, or
  rows; migrate existing workspaces; or reshape a Notion workspace.
- It does not broadly scan, repair, deduplicate, or maintain an entire
  workspace.
- Archive rows intentionally contain only `Task`, `Takeaway`, and
  `Improvement`; source-only fields stay in the source list.
- The normal path is Codex + the Notion connector. The Python CLI is an
  advanced fallback, not the default installation or operating path.

## Existing Notion List Path

Use this path if you already have a Notion to-do or work-log database.

Codex should inspect the existing database in read-only mode, compare it with
the portable schema, and report:

- whether the source fields can satisfy `Task`, `Done`, `Category`,
  `Takeaway`, and `Improvement`;
- how your category/project/routing field can map to archive targets;
- whether each archive table supports `Task`, `Takeaway`, and `Improvement`;
- what local config would be needed; and
- the safest next step, usually a read-only preview.

Adoption must not automatically migrate old rows, change schemas, create archive
tables, or remove source rows.

## New Notion System Path

Use this path if you want a fresh daily work record.

The setup route first proposes the smallest portable shape:

- one source to-do database;
- one category routing field named `Category`;
- one archive table per configured category;
- a local private profile based on the example config; and
- a read-only preview before any write path.

Setup may draft a plan and config. It must not automatically create databases,
fields, rows, ledgers, or archive tables without explicit approval for that
specific Notion write.

## Required Schema

See [docs/schema.md](docs/schema.md) for the full portable contract.

Required source database properties:

| Property | Notion type | Purpose |
| --- | --- | --- |
| `Task` | title | Work item title; copied to archive `Task`. |
| `Done` | checkbox | Completion gate. |
| `Category` | rich_text, relation, select, or mapped equivalent | Routes the row to one or more archive categories. |
| `Takeaway` | rich_text | Takeaway copied into the archive row. |
| `Improvement` | rich_text | Improvement note copied into the archive row. |

Required archive table properties:

| Property | Notion type | Purpose |
| --- | --- | --- |
| `Task` | title | Copied from the source row. |
| `Takeaway` | rich_text | Copied from the source row. |
| `Improvement` | rich_text | Copied from the source row. |

Archive tables should not mirror the full source database. Source-only workflow
fields such as `Done`, `Category`, relations, status, dates, and private metadata
remain source-side unless a future tested extension adds them.

## Configuration and Examples

Public templates are sanitized:

- [config.example.json](config.example.json)
- [real_profile.example.json](real_profile.example.json)

See [examples/](examples/) for minimal source schema, archive schema, category
mapping, and sample session output using neutral placeholder data.

Sandbox/test-style profile:

```json
{
  "source_database_id": "YOUR_TODO_SOURCE_DATABASE_ID",
  "ledger_database_id": "YOUR_OPTIONAL_TEST_LEDGER_DATABASE_ID",
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

Real-profile shape:

```json
{
  "mode": "real",
  "source_database_id": "YOUR_REAL_TODO_SOURCE_DATABASE_ID",
  "archive_tables_page_id": "YOUR_ARCHIVE_TABLES_PAGE_ID",
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
  "archive_tables": {
    "example-category": "example-category"
  },
  "project_categories": {
    "example-category": "YOUR_EXAMPLE_CATEGORY_PROJECT_PAGE_ID"
  }
}
```

Private profiles belong in ignored local files such as
`.todo_archive/real_profile.json`, `config.local.json`, or `*.local.json`.
Maintainers may also keep a private test profile in `config.test.json`; that
file is intentionally ignored and is not part of the public setup contract.

## Python CLI Fallback

Skip this section for normal Codex use.

`notion_todo_workflow.py` supports regression testing, local debugging, and
outside-Codex operation. Inside Codex, the skill should prefer the Notion
connector and should not switch to the CLI unless the user explicitly requests
an external command-line workflow.

The CLI does not store credentials in repository config. For outside-Codex CLI
use, credentials are user-managed shell or Keychain state. Public templates do
not include token fields.

For outside-Codex CLI use, create an internal Notion integration, share only the
databases this workflow should access, then store the token in macOS Keychain:

```sh
read -s NOTION_TOKEN
security add-generic-password -U -a "$USER" -s codex-notion-token -w "$NOTION_TOKEN"
unset NOTION_TOKEN
```

The fallback CLI also accepts a temporary shell environment variable named
`NOTION_TOKEN`, but do not write it into project files.

Inside Codex, the Notion connector remains the default. The only real-mode
fallback/API exception is final source cleanup after `done` and `confirm`: if
the connector cannot set source pages to Notion's recoverable `archived: true`
state, maintainers may use the local fallback/API only after source rows and
target archive rows have been re-read and verified. This exception must not be
used for preview, batch selection, `ok`, manual-match checks, or archive row
creation.

Useful maintainer commands:

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
python3 notion_todo_workflow.py --config .todo_archive/real_profile.json preview
```

Local-only setup/adopt preflight:

```sh
python3 setup_preflight.py --config config.example.json
python3 setup_preflight.py --config config.local.json --schema local-schema.json
```

The setup preflight reads only local JSON files. It reports placeholder config,
private credential-like keys, category mapping gaps, and missing/incompatible
fields in a local schema fixture. It does not call Notion or modify any Notion
database, field, archive table, row, ledger, or safety log.

Maintenance/retry only:

```sh
python3 notion_todo_workflow.py remove-sources --confirm REMOVE_SOURCES
```

## Testing and License

Release testing status: 77 tests passed; skill validation passed.

The repository includes Python regression tests for the workflow and setup
preflight. Run them only when validating a code change or a release candidate;
this documentation update does not change product behavior.

Licensed under the [MIT License](LICENSE).
