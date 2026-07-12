# Setup New Notion Work System

Use this route when the user wants to create a new Notion personal work/to-do system from scratch.

First version policy: plan-first, approval-gated, and non-destructive by default. Setup may explain, design, inspect, and draft config. It must not create databases, fields, archive tables, pages, ledgers, safety logs, or source cleanup without explicit approval for that specific action.

Before setup work, read `schema.md`.

## Setup Routing

Start by separating three cases:

- **Use existing organize config/state**: if the user already has organize-owned local profile/state or an active organize batch, do not run setup; route to `organize.md`.
- **Adopt existing Notion list**: if the user already has a Notion to-do list, route to `adopt-existing.md`.
- **Create a new system**: continue with this setup route only when the user wants a new Notion system.

If the request is ambiguous, ask whether the user wants to adopt an existing list or create a new one.

Do not treat unrelated Notion connector access, unrelated Notion database IDs, or other vibe coding project configs as existing organize config. Setup must not change global Notion connector behavior or other Notion-based workflows.

## Proposed System Shape

Propose the smallest portable system first:

1. Source to-do database for daily work capture.
2. Category routing field named `Category`.
3. Category archive tables.
4. Local config/profile.
5. Read-only preview before any real organize writes.

Use the minimum source schema:

- `Task`
- `Done` as a Notion checkbox property
- `Category`
- `Takeaway`
- `Improvement`

Use the minimum archive schema:

- `Task`
- `Takeaway`
- `Improvement`

For a newly created English-first system, prefer the public labels above. Existing personal systems may map localized field names, but setup for new users should not default to localized labels.

Do not make optional fields required. Fields such as `date`, `deadline`, `priority`, `status`, `project`, `tags`, and `estimate` may be suggested as user-specific extensions only.

## Setup Modes

### Sandbox/Test Setup

Use this for a safe first install or product trial.

- Source database: a test to-do database.
- Category archive targets: one archive database per category.
- Optional test ledger: allowed in test/sandbox mode for audit evidence.
- Config template: `config.example.json`.
- Default command path after setup: preflight, then preview/next in test mode.

Test/sandbox mode may use a Notion ledger if the user explicitly wants visible audit rows. It must still keep local backup/state under `.todo_archive/`.

### Real Setup

Use this for a real personal work system.

- Source database: real to-do database.
- Category routing: relation/select/text mapping chosen by the user.
- Archive targets: category archive tables with only `Task`, `Takeaway`, and `Improvement`.
- Config template: `real_profile.example.json`, copied by the user into `.todo_archive/real_profile.json`.
- Default command path after setup: read-only preview first.

Real mode must not create or update a Notion ledger, safety log, backend page, or machine-only page. Real bookkeeping stays local under `.todo_archive/`.

## Config Drafting

When drafting config, use sanitized placeholders first.

For sandbox/test setup, draft:

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

For real setup, draft:

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

Do not include private Notion IDs in public docs, skill references, or committed files. Real local profiles belong under `.todo_archive/` and should not be committed.

## Approval Gates

Before any Notion write, ask for explicit approval for the exact action.

Examples of actions that require explicit approval:

- Create source to-do database.
- Add or modify source fields.
- Create category archive tables.
- Create category/project pages.
- Create a test ledger.
- Add sample rows.
- Start a real organize write path.

Never combine setup approval with final source cleanup approval. Source cleanup belongs to the organize `done` plus `confirm` flow in `organize.md`.

## Verification Path

After a setup plan or approved setup action:

1. Check that source fields match the minimum schema.
2. Check that archive tables expose only the required archive surface.
3. Check that category mappings are present for the categories the user wants.
4. Confirm local/private profile files are not committed.
5. Recommend a read-only preview before any write path.

Use `organize.md` only after setup is complete and the user asks to review/archive records.
