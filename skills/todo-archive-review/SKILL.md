---
name: todo-archive-review
description: Use when the user says "organize todo-test", "organize todo", asks to review/archive/整理 Notion to-do records by category with ok/dismiss/skip/undo approval, or asks to setup/adopt/configure a Notion personal work maintenance system. Supports existing organize workflows plus guarded setup/adopt routing. Normal Codex use does not require or store a Notion API token; use the Notion connector tools available to Codex.
metadata:
  short-description: Approval-first Notion work maintenance
---

# To-Do Archive Review

Use this skill for the user's Notion personal work maintenance system.

Highest priority: do not lose data.

Core principle: reduce mechanical copying while preserving the user's manual recall/review. Do not silently automate final decisions.

## Route First

Choose exactly one route from the user's request:

- **Monthly total**: use only when the user says `total YYYY-MM`. Validate the month before reading local config or calling Notion, then follow the read-only total route below.
- **Organize existing workflow**: use when the user says `organize todo-test`, `organize todo`, `next`, `ok`, `dismiss`, `skip`, `undo`, `save`, `done`, `confirm`, `check`, `status`, or asks to review/archive existing Notion to-do records. Before acting, read `references/organize.md`.
- **Adopt existing Notion list**: use when the user asks to configure, connect, adopt, migrate, inspect, or use an existing Notion to-do list with this system. Before acting, read `references/adopt-existing.md`.
- **One-profile adoption readiness**: after an approved private adoption profile exists, use this only when the user asks to verify that profile for a specific `YYYY-MM`. Read `references/adopt-existing.md`; verify Total first, then enter Organize read-only preview with the same frozen profile. Do not carry approval from either step or start a write path.
- **Setup new system**: use when the user asks to create, initialize, or set up a new Notion personal work/to-do system. Before acting, read `references/setup.md`.
- **Schema/config/label help**: use when the user asks what fields/config are required, wants to rename Notion labels/properties, or asks how custom labels affect organize. Read `references/schema.md` if present; otherwise use the project-level `docs/schema.md`.

If the request is an organize command and organize-owned local config/state already exists, stay on the organize route. Do not start setup/adopt onboarding unless the user explicitly asks for it.

## Monthly Total Route

- Accept only `total YYYY-MM`, with a real calendar month in that exact format. Missing or invalid months stop before config, credentials, or Notion access.
- Expected use order: run `total YYYY-MM` before using `organize todo` on that month's completed work. `total` is a read-only, point-in-time calculation over completed items still present in the To-do list when called; it does not cache, persist, or save a snapshot. After those items are organized or cleaned up, the workflow does not rely on that month's total again, and rerunning it is not expected to reproduce the earlier result. `total` itself never organizes or cleans up items.
- Run the installed skill's internal wrapper as `python3 -B <this-skill-directory>/scripts/run_total.py YYYY-MM`. It resolves the checked-out repository and private profile without depending on the current working directory. This is an implementation detail; do not present the advanced external organize CLI as the normal user entry.
- Use the existing ignored local profile and existing `NOTION_TOKEN` shell or `codex-notion-token` Keychain convention. Never ask the user to paste credentials, and never print or store the token.
- The private profile must contain a `total` object with a fixed `view_id` and `fields` mapping for `done`, `categories`, `timeboxing`, and `date_anchor`. A relation-backed category may additionally use a private `category_relations` ID-to-name mapping. Do not commit or display any real values.
- The adapter must use the stage 4 Views path and remain read-only. Do not use an ordinary database/data-source query, timestamp ordering, fallback ordering, approval, archive, cleanup, backup, or local state writes.
- Return only category block subtotals in category-name order and the overlapping all-category total. For an empty result, say that no category blocks were recorded and show a zero total.
- Treat invalid blocks and all config, API, paging, ordering, or field-contract failures as fail closed. Report only the adapter's sanitized error; never reveal item text, page titles, URLs, page IDs, view IDs, tokens, or raw diagnostics.

## Existing System Mode

Use Existing System Mode only for this skill's own state, not for general Notion access.

Treat these as organize-owned signals:

- `.todo_archive/` runtime state, cache, backup, real profile, or active batch files.
- `config.test.json`, `config.local.json`, or another explicit local profile for this organize workflow.
- A user command in the existing organize command set: `organize todo-test`, `organize todo`, `next`, `ok`, `dismiss`, `skip`, `undo`, `save`, `done`, `confirm`, `check`, or `status`.
- Backup/cache records created by this organize workflow.

Do not treat these as organize-owned signals:

- Codex's general Notion connector being available.
- Unrelated Notion database IDs, scripts, configs, notes, project docs, meeting workflows, knowledge bases, CRM systems, or other vibe coding projects.
- Other skills/plugins that also use Notion.

When organize-owned state or an active batch exists, default to continuing the current organize workflow. Do not trigger setup/adopt, rebuild databases, rewrite config, or change schema unless the user explicitly asks to configure this organize system and approves the specific action.

If unrelated Notion context is present, ignore it for organize routing. If it might be relevant, ask whether the user wants to adopt a specific existing to-do database into this organize workflow.

## Hard Safety Rules

These rules apply across all routes:

- Data preservation is more important than speed or convenience.
- Use Codex's Notion connector tools for normal Codex operation; never ask the user to paste or store a private Notion API token in chat or repo files.
- Do not write tokens, real database IDs, real Notion URLs, private configs, backups, cache, or local state into public docs, examples, committed files, or chat-derived artifacts.
- Keep this skill namespaced to organize-owned state. Do not change or claim unrelated Notion/vibe coding workflows.
- Do not modify, create, archive, or delete Notion rows/pages unless the active route explicitly allows it and the user has given the required approval.
- `organize todo` is the real production workflow; keep its existing behavior stable.
- If local state has an active unfinished batch, resume/show that batch before replacing it.
- `done` is not source-cleanup approval. Source cleanup requires a separate `confirm` after the user sees the done summary and source/target safety checks pass.
- If the connector cannot archive real source pages during confirmed cleanup, a narrow local fallback/API exception may set verified source pages to Notion `archived: true` only after `done` -> `confirm`. Do not use that exception for preview, `ok`, manual-match, or archive row creation.
- `dismiss` is a reviewed cleanup decision that creates no archive row; the source row still requires `done` and then `confirm` before removal.
- `undo` only reverses archive copies created by this workflow; it never changes the source row.
- Real mode must not create Notion ledgers, safety logs, backend pages, or machine-only bookkeeping pages.
- Archive rows contain only the mapped task, takeaway, and improvement fields. Public defaults are `Task`, `Takeaway`, and `Improvement`.
- Category/project/relation fields route target selection only; they are not copied into archive rows.
- Respect `field_mapping` in local config. Do not rename Notion properties or assume public default labels when a user maps existing labels.
- If the user wants to rename Notion labels/properties, treat it as schema/config work: identify the organize role, inspect current schema/config, require explicit approval for any Notion write, update mapping, and run preflight/tests before resuming organize.
- Setup/adopt routes are read-only by default. They may inspect schema, explain missing fields, and draft config, but they must not automatically change Notion without explicit approval.
- If Notion is slow or rate-limited, stop early and report the blocked step instead of stacking more connector/API calls.

## Local State

- Runtime state, cache, backups, and real profiles live under `.todo_archive/` and must not be committed.
- `config.test.json`, `config.local.json`, and `*.local.json` are local/private profiles.
- Public examples should use sanitized templates such as `config.example.json` and `real_profile.example.json`.

## CLI Fallback Boundary

- `notion_todo_workflow.py` is developer-facing core plus an advanced-user external CLI fallback.
- In Codex, prefer the Notion connector route. Do not run the CLI for Notion reads/writes merely because it exists.
- The CLI is useful for regression testing, local debugging, and outside-Codex operation.
- The CLI may use user-managed shell/Keychain credentials outside Codex, but credentials must never be written into repo config, examples, docs, local state, backup files, or chat-derived artifacts.
- Ordinary users should not need to understand or run the Python file when using this skill in Codex.

## References

- `references/organize.md`: complete existing organize workflow, including batch selection, moving, dismiss, manual-match, undo, done/confirm, real mode, test mode, backup/cache, and connector rules.
- `references/adopt-existing.md`: guarded route for connecting an existing Notion to-do list.
- `references/setup.md`: guarded route for creating a new Notion personal work system.
- `references/schema.md` or `docs/schema.md`: portable Notion field/config contract.

When a reference is selected, read it before taking route-specific action.
