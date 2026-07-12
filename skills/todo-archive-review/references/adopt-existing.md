# Adopt Existing Notion List

Use this route when the user wants to connect an existing Notion to-do list to this work maintenance system.

First version policy: read-only by default. Inspect, compare, report, and draft config. Do not automatically create, update, migrate, archive, delete, or reshape Notion data.

## Required References

Read `schema.md` before evaluating compatibility. If the user later switches to `organize todo`, read `organize.md` and follow the existing organize workflow. If the user decides to create a new system instead of adopting an existing list, switch to `setup.md`.

## Route Decision

- If organize-owned local config/state or an active batch already exists and the user asks to continue organizing, stay on the organize route.
- If the user has an existing Notion to-do database, use this adopt route.
- If the user has no existing Notion to-do database and wants a new one, use the setup route.
- If the request is ambiguous, ask whether they want to adopt an existing list or create a new one.
- Do not treat unrelated Notion connector access, unrelated Notion database IDs, or other vibe coding project configs as organize configuration.

## Adoption Workflow

1. Identify the source database.
   Ask for the existing Notion to-do database or inspect it with the Notion connector if the user has already provided enough context.

2. Inspect source fields.
   Check whether the database can satisfy the portable source schema: `Task`, `Done`, `Category`, `Takeaway`, and `Improvement`. Report missing fields, incompatible field types, and fields that can be mapped without changing Notion.

3. Identify category representation.
   Determine whether `Category` is represented by relation, select, multi-select, rich text, title text, status, or another user-specific convention. Treat this as routing information only.

4. Identify archive targets.
   Find or ask for candidate archive databases/pages. Each archive target must support only the portable archive row fields: `Task`, `Takeaway`, and `Improvement`. Do not require archive tables to mirror the user's source database.

5. Draft category mapping.
   Map each source category/project label or relation page to an archive target. Unknown or unmapped categories must be reported for user decision; do not guess silently.

6. Draft local config.
   Explain or draft a local profile such as `.todo_archive/real_profile.json`, based on `real_profile.example.json`. For sandbox testing, explain or draft a profile based on `config.example.json`. Do not commit profiles that contain real database IDs.

7. Report compatibility.
   Summarize source readiness, category mapping readiness, archive target readiness, config readiness, and the safest next command.

8. Recommend read-only preview.
   Before any write path, recommend previewing what would be organized and where it would route. Do not combine adoption with final source cleanup.

## Field Compatibility

Required source fields:

- `Task`: task title.
- `Done`: completion checkbox or equivalent completion signal.
- `Category`: category/project/routing field, mapped through config.
- `Takeaway`: reflection/learning text.
- `Improvement`: improvement text.

Required archive fields:

- `Task`
- `Takeaway`
- `Improvement`

Existing personal systems may use localized or custom labels. Adopt them through explicit field mapping rather than treating localized labels as the public default.

Optional fields such as date, deadline, priority, status, project, tags, estimate, notes, or custom personal fields remain optional. They may help the user's own workflow, but they must not become portable requirements and must not be copied into archive rows by default.

## Category Mapping

- Relation-based project/category fields map naturally to `project_categories` and `archive_tables` in a real profile.
- Select or multi-select fields can map by option name.
- Rich text or title conventions can map by parsed label only if the rule is explicit and user-approved.
- Empty, unknown, or ambiguous categories should be skipped or reported for manual mapping.
- Personal example category names may appear in private config, but public examples should use neutral placeholder names.

## Approval Gates

Ask for explicit approval before any of these actions:

- Adding missing fields to an existing Notion database.
- Creating archive tables or pages.
- Editing relation targets, select options, formulas, filters, views, or existing rows.
- Writing a local profile that contains real Notion database/page IDs.
- Running any real archive/write path.
- Removing, archiving, or deleting completed source rows.

Approval must be specific to the action. Approval to inspect or draft config is not approval to modify Notion.

## Forbidden In First Version

- Do not automatically migrate old rows.
- Do not automatically change the user's source database schema.
- Do not automatically create archive databases.
- Do not automatically archive, delete, or remove source rows.
- Do not create real Notion ledgers, safety logs, backend pages, or machine-only bookkeeping pages.
- Do not commit private config, real database IDs, local cache, backup files, or generated state.
- Do not force the user's personal workflow into the example schema beyond the minimum compatibility contract.
- Do not change unrelated Notion workflows, global Notion connector behavior, or other vibe coding project configs.

## Output Shape

When reporting adoption results, use this compact structure:

- Source database: ready / missing fields / needs mapping.
- Category mapping: ready / partial / blocked.
- Archive targets: ready / missing / needs approval.
- Local config: draftable / drafted locally / blocked.
- Next safest step: preview, add missing field with approval, create archive target with approval, or continue organize.

After adoption is complete and the user asks to organize, switch to `organize.md`. Preserve all existing organize behavior, including active batch continuation, `ok`, `dismiss`, `skip`, manual match, `undo`, `done`, and `confirm`.
