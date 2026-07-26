# Schema Reference

Use this reference when the user asks about required Notion fields, config files, setup, or adopting an existing Notion to-do list.

This is the Codex execution reference. The public user-facing version is `docs/schema.md`.

## Minimum Source Database

Every portable source to-do database must provide these properties:

| Property | Required type | Required | Purpose |
| --- | --- | --- | --- |
| `Task` | title | yes | Source task title; copied to archive `Task`. |
| `Done` | checkbox | yes | Completion gate. Unchecked rows are never organize candidates. |
| `Category` | rich_text, relation, select, multi_select, status, or mapped equivalent | yes | Routes the source row to archive category targets. |
| `Takeaway` | rich_text | yes | Learning/takeaway copied to archive rows. |
| `Improvement` | rich_text | yes | Improvement note copied to archive rows. |

Rows with `Done` unchecked must not be moved or removed. Rows with both `Takeaway` and `Improvement` empty are cleanup candidates, not archive-content candidates.

Existing personal workspaces may use localized labels such as `Name`, `完成`, `category`, `收获`, and `改进`. Treat those as mapped field names in a private profile, not as the public default for new users.

## Field Mapping

Runtime code must read and write Notion properties through `field_mapping` when it is present in config:

```json
{
  "field_mapping": {
    "task": "Task",
    "done": "Done",
    "category": "Category",
    "takeaway": "Takeaway",
    "improvement": "Improvement"
  }
}
```

Do not ask users to rename existing Notion properties when mapping is enough. The v0.1 public default is English, while private/local profiles may map to any existing labels.

## Label Rename Guidance

When the user wants to rename a Notion property label, do not treat it as a casual Notion edit. Treat it as a schema/config change for this organize workflow.

Use this safe direction:

1. Identify the organize role first: `task`, `done`, `category`, `takeaway`, or `improvement`.
2. Inspect the current local config/profile and determine whether it already has `field_mapping`.
3. Inspect the source database and relevant archive tables before any write. Confirm the old label, new label, property type, and whether both source and archive properties must be renamed.
4. For real Notion writes, ask for explicit approval for the exact property/database rename. Do not bundle this with archive or source-cleanup approval.
5. Update local `field_mapping` so the organize role points to the new label. If source and archive labels differ, stop and report that v0.1 label mapping is shared by role and may need a narrower future binding design before proceeding.
6. Run preflight/schema validation after the label change. The changed property must still have the expected type.
7. Run focused regression tests when working in the repo, especially `test_notion_todo_workflow.py` and `test_setup_preflight.py`.
8. Use a read-only preview/check before resuming `organize todo`; do not immediately archive or remove source rows after a label rename.

If the user renamed a label manually outside Codex, do not guess silently. Inspect schema, update mapping if unambiguous, and fail closed if the role cannot be identified.

Current v0.1 mapping is label-based, not property-id stable binding. Do not promise that arbitrary Notion label changes are automatically recovered without mapping/schema repair.

## Archive Tables

Every archive table used by organize must provide only this required archive surface:

| Property | Required type | Required | Purpose |
| --- | --- | --- | --- |
| `Task` | title | yes | Copied from source `Task`. |
| `Takeaway` | rich_text | yes | Copied from source `Takeaway`. |
| `Improvement` | rich_text | yes | Copied from source `Improvement`. |

Do not copy source-only workflow fields into archive rows. In particular, do not copy `Done`, `Category`, project relations, status, dates, tags, source URLs, operation IDs, local-state fields, or private metadata unless a future optional extension explicitly adds and tests that behavior.

## Category Mapping

Categories are user configuration, not product constants.

- Test/sandbox profiles map category names to target archive database IDs through `target_databases`.
- Real profiles may map category names through both `project_categories` and `archive_tables`.
- Relation values use the private relation ID-to-name mapping. Select,
  multi-select, status, and mapped text values use their exact user-visible
  names as category mapping keys.
- Category values route target selection only. They are not copied into archive
  rows.
- Empty, malformed, unknown, or unmapped categories must fail closed or remain
  explicitly unresolved. Do not guess silently.

Example category names in templates are placeholders only.

## Test Ledger

The visible Notion ledger is a test/sandbox aid, not a real-mode requirement.

If a test profile uses a ledger, expected ledger properties are:

| Property | Expected type | Purpose |
| --- | --- | --- |
| `Name` | title | Human-readable ledger row title. |
| `action` | select | `moved`, `manual-match`, `undone`, or related action. |
| `stability` | select | `pending-removal`, `removed`, `undone`, `needs-review`, or related state. |
| `session_id` | rich_text | Organize session identifier. |
| `operation_id` | rich_text | Stable operation identifier. |
| `source` | url | Source row URL. |
| `target` | url | Archive target row URL. |
| `category` | rich_text | Category used for routing. |
| `batch` | rich_text | Batch identifier. |
| `number` | number | Displayed batch item number. |
| `note` | rich_text | Short operation note. |

Real mode must not create or update a Notion ledger, safety log, backend page, or machine-only bookkeeping page. Real mode bookkeeping stays local under `.todo_archive/`.

## Optional User Fields

Users may keep additional source fields such as:

- `date`
- `deadline`
- `priority`
- a separate, non-routing `status`
- `project`
- `tags`
- `estimate`

These are optional. Do not require them for setup/adopt/organize, and do not copy them into archive rows by default.

## Local Config And State

Public templates:

- `config.example.json`
- `real_profile.example.json`

Private/local files:

- `.todo_archive/real_profile.json`
- `.todo_archive/state.json`
- `.todo_archive/cache.json`
- `.todo_archive/backups/`
- `config.test.json`
- `config.local.json`
- `*.local.json`

Do not commit private/local files. Do not store Notion tokens in config, docs, skill references, backups, local state, or chat-derived artifacts.

## Route Usage

- Setup route: use this schema to propose fields and config before creating anything.
- Adopt-existing route: use this schema to inspect an existing Notion list and report missing or incompatible fields.
- Organize route: use this schema only as supporting reference; the full organize behavior is in `organize.md`.

Setup/adopt remain read-only by default. Do not create, update, archive, or delete Notion pages unless the user explicitly approves that specific action.
