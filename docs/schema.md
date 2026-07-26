# Notion schema

This project maintains a personal work system by moving completed to-do records into category archive tables after review.

The schema below is the portable contract for open-source users. A Notion workspace can have more fields and more personal structure, but organize should only depend on the minimum fields listed here.

## Source To-Do Database

The source database is where daily work records live before review.

Required public-default properties:

| Property | Notion type | Purpose |
| --- | --- | --- |
| `Task` | title | The work item title. This becomes the archive row title. |
| `Done` | checkbox | Marks a row as complete and eligible for organize. Unchecked rows are never candidates. |
| `Category` | rich_text, relation, select, multi_select, status, or equivalent mapping | Routes the row to one or more archive categories. |
| `Takeaway` | rich_text | The takeaway or learning copied into the archive row. |
| `Improvement` | rich_text | The improvement note copied into the archive row. |

Only completed rows should be considered for organize. Rows with both `Takeaway` and `Improvement` empty are cleanup candidates, not archive-content candidates.

Existing personal workspaces may keep localized or custom labels. Adopt them through `field_mapping`; do not require users to rename Notion properties when mapping is enough.

Example:

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

A private/local profile may map the same roles to labels such as `Name`, `完成`, `category`, `收获`, and `改进`. Those labels are compatibility mappings, not the public default.

## Archive Tables

Archive tables store reviewed knowledge by category.

Required properties for each archive table:

| Property | Notion type | Purpose |
| --- | --- | --- |
| `Task` | title | Copied from the source row. |
| `Takeaway` | rich_text | Copied from the source row. |
| `Improvement` | rich_text | Copied from the source row. |

Archive rows should not copy source-only workflow fields such as `Done`, `Category`, project relations, status, dates, or private metadata.

## Category Mapping

Categories must be configurable. Do not hard-code another user's categories into the product.
Names such as `example-category` are placeholders only; users should replace them with their own categories.

For database-to-database test or sandbox profiles, category names map directly to archive database IDs:

```json
{
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

For real profiles that use a relation property, category names map to project page IDs and archive table names:

```json
{
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

Relation categories use the private relation ID-to-name mapping. Select,
multi-select, status, and mapped text categories use their exact user-visible
value as the category name; that name must have an explicit archive target.
Empty, malformed, unknown, or unmapped values must not be guessed or routed.
The Category field routes the source row only and is not copied into archive
rows.

## Optional Fields

Users may keep additional Notion fields, for example:

- `date`
- `deadline`
- `priority`
- a separate, non-routing `status`
- `project`
- `tags`
- `estimate`

These fields are allowed, but they are outside the portable schema. Organize should not require them, copy them, or use them for final source cleanup unless a future feature explicitly adds a tested optional extension.

## Local Files

Local runtime files are not part of the public schema and should not be committed:

- `.todo_archive/real_profile.json`
- `.todo_archive/state.json`
- `.todo_archive/cache.json`
- `.todo_archive/backups/`
- `config.test.json`
- `config.local.json`
- `*.local.json`

Commit sanitized templates such as `config.example.json` and `real_profile.example.json` instead.

## Compatibility Rule

The minimum schema is a public protocol. Existing personal Notion systems can use richer layouts, but the organize workflow must continue to operate through this small shared surface:

```text
Task + Done + Category + Takeaway + Improvement
```

When a workspace uses different labels, keep the same five roles and express the labels through `field_mapping`.
