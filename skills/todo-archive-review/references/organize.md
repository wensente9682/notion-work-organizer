# To-Do Archive Review

Use this skill for the user's Notion work-record archiving workflow.

Highest priority: do not lose data.

Core principle: reduce mechanical copying while preserving the user's manual recall/review. Do not silently automate final decisions.

## Workflow Modes

There are two supported execution modes. Keep them separate.

### Codex Connector Mode

This is the default and preferred mode whenever Codex is doing the work.

- Always use Codex's Notion connector tools for Notion reads and writes.
- Use tool discovery if Notion connector tools are not currently visible.
- Do not ask the user for `NOTION_TOKEN` in Codex connector mode.
- Do not run the fallback CLI for Notion reads/writes merely because it exists.
- Narrow exception: during real-mode `confirm` source cleanup only, if the connector cannot set source pages to Notion `archived: true`, a local fallback/API path may do that final archive operation after `done` -> `confirm` and after source/target verification passes. Do not use this exception for preview, `ok`, manual-match, or archive row creation.
- Use local helper scripts only for project-side state and backup files.
- If the connector is slow or temporarily rate-limited, stop early and report the blocked step. Do not keep stacking schema/source/ledger calls, and do not switch to a token workflow unless the user explicitly asks for external CLI operation.

### External CLI Fallback Mode

The `notion_todo_workflow.py` script is mainly for running outside Codex, or when the user explicitly requests an external command-line workflow. Inside Codex, its only built-in real-mode exception is the confirmed source-cleanup fallback described above.

- This fallback may require `NOTION_TOKEN` because it talks directly to the public Notion API.
- Treat the token as user-managed shell state only; never store it in repo files, skill docs, local backup, or chat-derived artifacts.
- Keep the CLI configured only for the test objects unless the user approves a separate real-mode design.
- If the connector is rate-limited and the user explicitly asks to try the fallback route, check whether `NOTION_TOKEN` is already present in the shell environment without printing it, or whether the macOS Keychain item `codex-notion-token` exists. If either is present, try the external CLI fallback. If absent, report that the fallback is ready but needs a shell environment token or Keychain item; do not ask the user to paste a token into chat.
- Prefer macOS Keychain for fallback credentials: service `codex-notion-token`, account `$USER`. The token must not be written into repo files, skill docs, local backup, local state, local cache, shell history, or chat-derived artifacts.
- Connector cooldown and external public-API cooldown are separate. A connector `429` must not block an explicitly requested external CLI fallback attempt; only a public API `429` from the CLI should block the CLI route.
- If the user asks Codex to organize or maintain Notion data without explicitly authorizing fallback, return to connector mode rather than asking for CLI token setup, except for the narrow confirmed source-cleanup fallback when an existing local credential is already available and the connector cannot archive source pages.

## Local Backup

Maintain a tiny project-side local backup for every organize session.

- Use the bundled script `scripts/todo_archive_backup.py`.
- When calling a local backup helper from a fallback CLI, resolve the bundled helper relative to the CLI or skill file location, never relative to the current working directory.
- Backup files live in `.todo_archive/backups/` under the working project.
- Backups are compact JSON, expire after 7 days, and may be pruned automatically by the helper.
- Every `organize todo-test` / `organize todo` start must call the backup helper's `begin` command or an equivalent code path. `begin` runs pruning in code before creating the new session. Do not rely on a prompt reminder for cleanup.
- Keep at most the latest 20 backup sessions.
- Backups must never contain Notion API tokens, private credentials, or unrelated workspace data.
- Backups should contain only workflow-related pointers needed for rollback:
  - session id
  - operation id
  - source row URL/id
  - target archive row URL/id after move
  - action status: candidate/moved/dismissed/skipped/undone/empty-pending-cleanup/removal-proposed/removed
  - timestamps
- Do not duplicate full `Name`, `收获`, or `改进` in local backup. Source and target Notion rows remain the canonical content stores.
- In real mode, do not create Notion safety/log/ledger pages or hidden backend pages for machine bookkeeping. Use only the minimal local `.todo_archive` pointers needed to resume, undo, and verify the current organize session.
- Old accidental safety/log/ledger pages in the user's visible workspace may be trashed after explicit user approval; do not move them into a new backend page unless the user specifically asks for that.
- A short `name_hint` is allowed only for human recognition and should be truncated.
- Create/start a backup session before the first move of an organize run.
- Use the backup session id as the organize `session_id`.
- Add/update backup records after every move, dismiss, skip, undo, save, done, stop, and final removal.
- Record candidate rows by immutable source row URL/id when a 5-item batch is presented. Do not rely on row position, current table order, or title text to identify later `ok` / `dismiss` / `skip` commands.
- If the user reopens within 1 week and wants to reverse the last organize operation, use local backup pointers to identify archive copies to undo. In test mode, the Notion ledger may be used as additional evidence.

## Local Runtime Cache

Maintain a separate tiny runtime cache at `.todo_archive/cache.json`.

- This cache is not a backup and must never contain Notion tokens, full `Name`, `收获`, or `改进` content.
- It may contain only short-lived operational metadata:
  - schema/preflight config signature and timestamp
  - Notion rate-limit cooldown mode, deadline, and blocked step
  - recent effective test-ledger source row ids and operation ids
- Check the relevant cooldown entry before any Notion connector/API call. Connector cooldown blocks connector calls; external API cooldown blocks external CLI calls. Do not let connector cooldown prevent a user-authorized external CLI fallback attempt.
- Use short-lived caches to avoid repeated Notion reads and reduce rate-limit pressure. The user may ask Codex to inspect or adjust cache settings.
- Before destructive actions or final removal, verify against current Notion source/target rows again; never rely on cache alone.

## Required Workflow Shape

Follow this sequence exactly:

1. Present up to `batch_size` candidate rows from the source table.
2. User approves/dismisses/skips individual rows with `ok` / `dismiss` / `skip`.
3. Move only approved rows to category archive tables. Dismissed rows are not archived.
4. Update the visible summary after the operation; in test mode, update the test ledger too.
5. Present the next configured-size candidate batch.
6. Repeat until either:
   - 30 rows have been successfully moved, or
   - there are no more eligible move candidates.
   The user may also say `done` at any time to end the loop early.
7. Present an end-of-run confirmation page/summary showing:
   - moved rows
   - manually matched rows that the user already put into category archive tables
   - dismissed rows that will not be archived but may be removed from source after confirmation
   - skipped rows
   - undone rows
   - already-categorized rows still present in the source table as `pending-removal`, grouped by whether `完成` is checked
   - source rows proposed for removal
8. Treat the user's `done` command as ending the organize loop and entering final confirmation. Show a short change summary, then ask whether to confirm today's changes. Only after the user replies `confirm`, remove/archive checked pending source rows and, in test mode, mark their ledger rows `removed`.

Never remove/archive source rows during the 5-item batch loop. A checked source row with both `收获` and `改进` empty is an ordered cleanup candidate, not an archive-content candidate: when reached in bottom-first order, record it in local backup/state and keep scanning for real move candidates. In both test and real mode, source cleanup requires the separate `done` -> `confirm` approval path. Empty cleanup candidates do not count toward the 5 candidate rows or the 30 moved-row limit.

Source cleanup archives the source page with Notion's recoverable `archived: true` state so it leaves the active source table. It is not a permanent destroy operation.

## Commands

- `organize todo-test`: test-only mode. Only modify `test page`, `to-do-test`, and test category archive tables.
- `organize todo`: real mode. Read-only by default for real `To Do` / `to-do`; any write/delete requires explicit approval.
- External CLI real-mode profile: keep real IDs in an ignored file such as `.todo_archive/real_profile.json` with `"mode": "real"`. Use `preview` for read-only inspection; run write commands only when explicitly approved.
- Portable real profiles map the user's category/project field to configured archive targets. Category/project values are routing metadata only.
- The current maintainer real profile uses relation categories and same-name category archive tables. Each category target should resolve to one archive database/table, for example `<summary>example-category</summary>` containing database `example-category`.
- Each real archive table row must contain only `Name`, `收获`, and `改进`. Do not copy `category`, project relations, relation IDs, status fields, or workflow metadata into the archive table.
- If the exact category toggle/database pair is missing, do not guess silently. First look for similar existing names and ask in the batch summary whether to use one. If there is no similar name, or the user explicitly wants a new target, create the same-name toggle plus same-name inline database/table with only `Name`, `收获`, and `改进`.
- Real preview must print available commands after each candidate, including `ok N to <category>` and `ok N to all` for multi-category items. Show `dismiss N` after the `ok` choices and before `skip N`.
- Real preview/next must stay lightweight: do not scan archive databases for manual-match before the user approves an item. Manual-match is checked at `ok` time.
- `ok 1`, `ok 1 3 5`: move approved items from the active batch.
- `dismiss 1`: do not create an archive row, but mark the source row as eligible for final source removal after `done` and `confirm`.
- `skip 2`: skip active batch items.
- `undo 1`: undo a just-moved item by archiving/deleting the created archive copy, or undo a dismissed item by clearing only local pending-removal state. Never change the source item during undo.
- `check organize todo-test`: run a stability check for the test workflow without changing source rows.
- `preflight organize todo-test`: verify test schemas and safety fields without moving rows.
- `next`: show the next batch using the configured `batch_size`; allow manual `--batch-size` and `--move-limit` overrides at session start.
- `save`: save current progress, finish the current local backup session as `saved`, immediately start a new backup session, preserve the active batch, and allow more `ok` / `dismiss` / `skip` commands.
- `done`: stop the 5-item loop, show a short change summary, and ask whether to confirm today's changes.
- `confirm`: after `done`, finalize checked pending source rows in test mode.
- `remove-sources --confirm REMOVE_SOURCES`: maintenance/retry command for final source-row removal if `done` was interrupted; only for rows that were successfully moved, checked complete, and not undone.
- `discard`: clear only the local active batch after a stale-batch warning; never modifies Notion rows.
- `inspect`: inspect current local state, active batch, moved rows, and source/target pointers.
- `stop`: stop the workflow.

## Safety Rules

- Data preservation is more important than speed or convenience.
- Never ask for or store a private Notion API token.
- Use Codex's Notion connector tools, not a user token, whenever available.
- Never commit or expose tokens, real database IDs, real Notion URLs, private configs, `.todo_archive/`, local backup/cache/state files, or chat-derived credentials.
- Do not make the user set up tokens, database IDs, or connector wiring when Codex can discover or operate through available Notion tools.
- If Notion connector tools are not visible, first use tool discovery for Notion tools. If an explicitly requested Notion connector/plugin is not installed and install tools are available, offer/request that installation instead of asking for a token.
- Treat a direct Notion API token workflow as an external fallback only, not the normal Codex workflow.
- The one Codex-side fallback/API exception is real-mode final source cleanup: after `done` and explicit `confirm`, and only after source/target verification passes, the fallback may set verified source pages to Notion `archived: true` when the connector lacks that page-archive capability.
- In test mode, only modify the test objects listed below.
- Real `To Do`, real `to-do`, real `category`, and real project pages are read-only unless the user gives action-specific approval.
- Never delete moved source rows during the move phase.
- `ok` and `dismiss` must not remove source rows. Source removal is allowed only after `done`, explicit `confirm`, and the required source/target or dismissed-source verification.
- Deletion is a separate approval phase.
- Source removal happens only after the run ends at 30 successful moves, no more eligible candidates, or the user says `done`.
- In test mode, `done` ends the run but is not final deletion approval. It must show a short summary and ask for confirmation. After the user replies `confirm`, finalize checked pending source rows. The finalize step must still verify source/target/test-ledger scope and `完成` before removing anything.
- If `confirm` leaves pending rows in `needs-review`, keep the confirmation state active so the user can fix Notion rows and run `confirm` again without restarting the loop.
- `save` is not an end-of-run command. It preserves prior moves/skips, marks the current backup session as saved, creates a new backup session, snapshots the current active batch into the new session, and then continues. After `save`, later `ok` / `skip` operations use the new `session_id`, so they can be treated as a fresh organize run while still referring to the same snapshotted active batch.
- `skip` rows are not removal candidates.
- `undone` rows are not removal candidates.
- Only successfully moved/manual-matched and not-undone source rows, plus dismissed and not-undone source rows, are removal candidates.
- Do not show `undo` in the candidate preview before anything has been moved. Show `undo` only after an archive copy exists.
- Maintain a visible Notion ledger in test mode so the user can inspect moved/skipped/undone records.
- In real mode, never create or update a Notion ledger, safety log, or machine-only log page. The user-facing archive tables and source rows are the only Notion writes.
- Maintain the local 7-day backup alongside the test Notion ledger. In real mode, the local backup is the only machine bookkeeping store.
- Source rows with `完成` unchecked are never candidates.
- Checked rows with both `收获` and `改进` empty are ordered cleanup candidates, not move candidates. Do not bulk-delete all such rows at startup. Only when one is reached during the normal bottom-first scan, record it in local backup/state and continue scanning. Leave it for the separate `done` -> `confirm` source-cleanup approval path in both modes. It does not count as one of the 5 approval candidates.
- A successful move means creating one row in the category archive table with only `Name`, `收获`, and `改进`.
- Category archive rows must not inherit source relations or source-only fields. In particular, do not copy `project-test` relation or any other relation from `to-do-test`; use `category` only to choose the target archive table.
- The successful move limit comes from session `move_limit` defaulting from config. Empty cleanup candidates do not count toward the limit.
- If an organize run is interrupted, never assume copied rows were also removed. Run the stability check before resuming or deleting anything.
- The workflow is an on-demand organizer, not the owner/maintainer of the whole Notion list. Do not scan, repair, deduplicate, or maintain all source/category rows unless the user explicitly asks for a separate audit.
- When considering a checked candidate, before copying it, check only its configured category archive table for a strict manual match: current `Name`, `收获`, and `改进` must all match. If found, record a local `manual-match`; in test mode also record ledger action `manual-match` with stability `pending-removal`. Skip the duplicate copy and continue scanning. Do not treat name-only matches as sufficient.
- When candidate selection ends with no eligible rows, run a visibility check for effective local moved/manual-match records, plus test ledger records in test mode, whose source row still exists in the source table. Check each source row's `完成` checkbox. Show checked rows as “already categorized and completed, still waiting for final source removal”; show unchecked rows as `needs-review` and do not propose them for removal.
- User edits during an organize run are allowed. New `to-do-test` rows must not affect the active 5-item batch, because `ok` / `dismiss` / `skip` refer only to the snapshotted source row URLs shown in that batch.
- Every move must have a stable `operation_id`. Before creating an archive row, check both `operation_id` and source row id against local state; in test mode also check the effective Notion ledger. If either the operation or source row is already effectively moved, do not create another archive copy.

## Test Objects

Use only the configured project test objects for `organize todo-test`. Keep concrete test database IDs and sandbox location details in ignored local files such as `config.test.json` and a private sandbox locator, not in normal user-facing output or committed public files.

## Preflight

`organize todo-test` must start with a lightweight health gate. This is automatic; the user should not need to ask for a separate health-check command.

Keep the health gate small so the user does not wait through repeated setup work:

- Always do cheap local checks:
  - current project path is usable for local helper commands
  - `config.test.json` exists and has the expected source, ledger, target categories, `source_order`, `batch_size`, and `move_limit`
  - if local state has an active batch, show it automatically before any move instead of asking the user to run a separate resume command
  - backup helper exists and prior backups are in the expected directory
- The normal `organize todo-test` path must not read the whole skill back to the user, fetch schemas, fetch source rows, and fetch ledger rows as one large preamble. In a healthy state, the startup gate should finish from local evidence before any Notion call.
- Use cached successful schema/preflight results when the config hash and test object IDs have not changed and the cache is fresh.
- Treat schema/preflight cache freshness as a short-lived runtime setting; keep exact defaults in developer maintenance notes.
- Store schema/preflight freshness in `.todo_archive/cache.json`; do not make the user ask for a separate health-check command.
- Run full schema preflight only when:
  - there is no fresh cache
  - `config.test.json` changed
  - Notion object IDs changed
  - a previous Notion write/read failed because of schema or property mismatch
  - resuming after interruption with uncertain state
  - the user explicitly asks for `preflight organize todo-test`
- For connector row-query readiness, use the smallest useful query, such as `LIMIT 1`, unless selecting the actual next batch.
- If Notion returns a rate limit during the health gate, or a connector query takes tens of seconds, stop and report the blocked step instead of burning more time/tokens. Do not wait through repeated 30-second retry cycles during normal organize startup.
- Do not fetch every archive table, ledger row, or source row just to start. Fetch only what proves readiness for the next action.
- If a source query succeeds but the ledger duplicate check fails or is rate-limited, do not present the batch for approval. Report the candidate preview as blocked until duplicate safety can be verified.

Full preflight verifies test schemas without moving rows. If a required field is missing or has an incompatible type, stop before presenting candidates or moving rows. Keep the exact test ledger schema in the developer checklist, not in the product-facing skill.

## Active Batch Auto-Continue

If local state already has an active batch at the start of `organize todo-test` / `next`, automatically continue that batch by showing it again. Never overwrite or ignore it silently, and do not require the user to run a separate `resume` command.

Show a compact continuation page:

- active batch count and batch number
- moved pending count
- skipped count
- the active batch preview if available

Then continue accepting the normal commands: `ok`, `dismiss`, `skip`, `undo`, `save`, `done`, and `next --force` if the user intentionally wants to replace the local batch.

`discard` remains a maintenance command and must not archive, delete, or modify any Notion row. If moved rows exist in the active session, prefer `inspect` or `done` before discard so pending archive copies are visible.

## Batch Selection

For `organize todo-test`:

1. Use the Notion connector to read/fetch the `to-do-test` schema if needed.
2. Gather candidate source rows bottom-first, interpreted as oldest checked rows first.
3. Include only rows where `完成` is checked.
4. If a checked row has both `收获` and `改进` empty, handle it only if it has been reached in this scan order. Record it as pending cleanup in local backup/state for the separate `done` -> `confirm` path. Do not add it to the active batch, and continue scanning for the next row. Never clear all empty rows globally before selection.
5. Include only rows whose text `category` matches an available test archive table.
6. Exclude rows already in an effective `moved` + not-`undone` ledger state.
7. Present at most the configured `batch_size` move candidates. Empty cleanup candidates do not count toward this size.
8. Persist the active batch, plus session `batch_size` and `move_limit`, as local state before accepting `ok`, `dismiss`, or `skip`. Snapshot source row URLs/ids, category, a short `name_hint`, and a content fingerprint in local backup/state.
9. If empty checked rows were handled while selecting the batch, show a compact ordered-cleanup summary before the candidate list. In real mode, describe them as proposed cleanup only.

If the user adds new rows to `to-do-test` while an active batch is waiting, do not re-rank or replace that batch. New rows may appear only in a later `next` batch after the current batch is resolved.

If the connector cannot query database rows directly, use available search/fetch results and be transparent about any coverage limit. Do not fall back to asking for a private token.

## Connector-Minimizing Operation

Avoid using the Notion connector for information already captured in local state, backup, or a fresh health/preflight cache. The connector is rate-limited, so keep Notion calls to the minimum needed for correctness.

- At `organize todo-test` / `next`, run the lightweight health gate, run full preflight only if required by the cache rules above, read source candidates with a narrow query, read only the ledger evidence needed for those source rows when possible, snapshot the active batch locally, and write candidate pointers to local backup before showing the batch.
- Prefer per-candidate or small-batch ledger checks over full-ledger scans during startup. Full-ledger scans are reserved for explicit `check organize todo-test`, final removal safety checks, or suspected inconsistency.
- Use a small default source query window, around 5-15 rows, not a broad 30+ row query. Expand only if the narrow query returns too few eligible candidates and Notion is responding quickly.
- Do not trade away duplicate safety for startup speed. If there is no fresh effective-ledger cache, the fallback workflow must verify ledger state before presenting a batch; otherwise source rows already copied but not yet removed can reappear as candidates.
- If a source query takes tens of seconds or the connector returns `429 rate_limited`, do not issue additional schema/source/ledger queries in the same startup attempt. Stop, report the exact blocked step, and let the next `organize todo-test` retry the smallest necessary query.
- After the active batch is snapshotted, `ok N` / `dismiss N` / `skip N` must resolve `N` from local state/backup, not by re-querying candidate ordering from Notion.
- Do not re-run schema preflight on every `ok`, `dismiss`, or `skip`; use the preflight result from the start of the active run unless resuming after interruption or the user requests `preflight`.
- Do not re-query the ledger before every item in the same active batch. Use local `operation_id`, moved/skipped state, and the ledger snapshot gathered at batch creation to prevent duplicates.
- A single source-row re-read before moving is still allowed for safety, because it verifies that `Name`, `category`, `收获`, and `改进` did not change after the batch was shown. Avoid any additional source reads unless that check fails or the user asks to refresh.
- In real mode, cache the configured archive-table container structure briefly. In the maintainer profile this container is named `Archive 表格`; portable profiles may use another page/container name. Cache only structure IDs/names, not archived item content. Keep exact defaults in developer maintenance notes.
- In real mode, within a single `ok` command, reuse archive database scans per category for manual-match checks. Do not persist archive row content beyond the command.
- Backup is written from local state and Notion write responses. Do not fetch newly created archive or ledger rows only to back them up; use the IDs/URLs returned by the create calls.
- `save` should be entirely local except for any already-completed Notion writes: finish the old backup session as `saved`, start a new backup session, and re-snapshot the active batch locally. It must not query Notion just to save progress.

## Connector Handling

Default behavior:

1. Search for Notion connector tools if they are not already visible.
2. Use the connector's fetch/search/create/update tools to perform the workflow.
3. Keep all private credentials out of chat, config files, state files, and generated docs.
4. If the connector is unavailable, explain that the blocker is connector availability and offer to install/enable the Notion connector if supported by the current Codex environment.
5. Only mention an API-token CLI fallback if the user explicitly wants an external command-line workflow outside Codex, except for the narrow real-mode `confirm` source-cleanup fallback when the connector cannot archive verified source pages.

## Batch Output

Before the batch, show one settings line such as `Batch size: 5 | Session limit: 30`. Then present exactly this compact shape. `added` must come from Notion's system `createdTime`; `last edited` must come from Notion's system `lastEditedTime` and must not be labeled as done/completed time:

```text
1. [category] Name
added: YYYY-MM-DD HH:MM
last edited: YYYY-MM-DD HH:MM
收获: ...
改进: ...
```

Then remind:

```text
Reply: ok 1 3 | dismiss 2 | skip 4 | next | save | done | stop
```

After a batch operation, provide a compact operation summary:

```text
Moved: 1, 3
Skipped: 2
Undo available: undo 1 | undo 3
Next: say next, save, or done to finish early
```

## Moving Approved Items

On `ok N`:

1. Use the snapshotted active batch only. `N` refers to the displayed batch index, not the row's current position in Notion.
2. Re-read the source row and compare `Name`, `category`, `收获`, and `改进` to the batch fingerprint. If changed, stop and ask the user to approve refreshing the batch.
3. Compute `operation_id` from `session_id`, source row id, and batch number. Check local state for that `operation_id` and source row before creating anything; in test mode also check the ledger.
4. If the same `operation_id` or source row is already effectively moved and not undone, skip it as a duplicate and report that no archive copy was created.
5. Check the matching category archive table for a strict manual match using current `Name`, `收获`, and `改进`. If found, record local `manual-match`; in test mode also create a `manual-match` ledger row. Do not create a duplicate archive row.
6. Create a page in the matching category archive table only if no manual match exists.
7. Copy only:
   - `Name`
   - `收获`
   - `改进`
8. Do not copy `category`, `project-test`, or any source relation into the archive row.
9. Record the created target URL in the response so `undo N` can reverse it.
10. Record the move in local state. In test mode, also add a row to `todo-test organize ledger` with `session_id`, `operation_id`, action `moved`, stability `pending-removal`, batch number, item number, category, source URL, target URL, and a short note.
11. Do not modify or delete the source row.
12. After moving, tell the user which `undo N` commands are now available. For `manual-match`, do not offer to undo the user-created archive row.

On `dismiss N`:

1. Re-read the source row and verify it still matches the active batch snapshot.
2. Do not create an archive row.
3. Record a compact local dismissed pointer with source URL/id, operation id, batch number, category/name hint, and source content fingerprint.
4. Hide the dismissed row from later candidate batches until it is undone or finalized.
5. Keep the source row untouched until the user later says `done` and then `confirm`.

## Stability Check

Use this lightweight check whenever the user asks whether the workflow is stable, when a session resumes after interruption, before final source removal, and for `check organize todo-test`. Keep it scoped to schema, local runtime state, active batch counts, and current pending-removal source/target safety; do not turn it into a full-table audit.

In test mode, the ledger should include a `stability` select property:

- `pending-removal`: archive copy exists and the source row is intentionally still present.
- `dismissed`: the user chose not to archive this source row, but it may be removed after final confirmation if source verification still passes.
- `manual-match`: ledger action for a user-created archive row that strictly matched the source row and was adopted only for duplicate prevention/final confirmation.
- `removed`: archive copy exists and the source row was removed only after final approval.
- `undone`: the archive copy was undone; source row must remain untouched.
- `needs-review`: ledger, backup, source, or target evidence is incomplete or inconsistent.

Quick check procedure:

1. Read local moved/manual-match records. In test mode, also read ledger rows with `action = moved` or `action = manual-match`.
2. Exclude any source rows that have a later/effective local or test-ledger `undone` entry.
3. Flag duplicate `operation_id` values or duplicate effective moved source rows as `needs-review`.
4. For each remaining moved row, verify:
   - `source` URL is present in `to-do-test` unless stability is `removed`.
   - `target` URL is present in the matching category archive table.
   - local backup has a matching source/target pointer when the move happened in the last 7 days.
5. Report counts:
   - copied and waiting for final source removal (`pending-removal`)
   - already finalized (`removed`)
   - undone
   - needs review
6. If any row is `needs-review`, stop before moving/deleting more rows and ask the user how to proceed.

`pending-removal` is a safe intermediate state, not an error. It means the user can resume later and decide whether to remove the source rows.

## Undo

On `undo N`, only undo rows created during the current active workflow. Prefer archiving the created archive row. Never alter the source item, and do not promise to restore a source row that was changed outside this workflow. Record `undone` locally; in test mode also append an `undone` row to the ledger with the same `session_id`, `operation_id`, source URL, target URL, batch, number, and category.

For dismissed rows, `undo N` only removes the local dismissed pending-removal state and records `undone` locally. It must not archive, delete, or otherwise change the source row.

For bulk rollback after `stop`, support:

- `undo recent`: undo archive copies created recently in this workflow, defaulting to the last 10 minutes.
- `undo last 30m`: undo archive copies created in the last 30 minutes.
- `undo session`: undo all not-yet-undone archive copies from the current organize session.

All bulk undo commands must use local backup. In test mode, use the Notion ledger as extra evidence. They must only undo archive copies, never source rows.

## End-of-Run Confirmation

When the workflow reaches 30 successful moved rows, no more eligible candidates, or the user says `done`:

1. Stop presenting new 5-item batches.
2. Produce a short final summary. Keep it concise:
   - items categorized
   - empty completed source rows already handled or proposed during ordered cleanup
   - skipped count if nonzero
3. List effective local moved/manual-match rows, dismissed rows, plus test-ledger rows in test mode, that are still present in the source table as `pending-removal`; these are intentionally hidden from future candidate batches to prevent duplicate archive copies or repeated dismissals.
4. Split pending source rows by `完成`: checked rows may be proposed for removal; unchecked rows must be shown as `needs-review` and must not be removed.
5. Ask: “Confirm today's changes?” The user may reply `confirm`.
6. On `confirm`, finalize checked pending source rows. The user should not need to type `remove-sources --confirm REMOVE_SOURCES`.

Do not combine final source removal with the last `ok` operation or with `done`. Final source removal happens only after the user sees the done summary and confirms.

`remove-sources --confirm REMOVE_SOURCES` remains as a maintenance/retry command if `done` was interrupted or a historical ledger pending state needs cleanup. Normal use should not require the user to type it.

Before final source removal, verify that moved/manual-match/dismissed source rows are present in local backup/state, then re-read each source row to confirm it is still in the source database and `完成` is still checked. For moved/manual-match rows, re-read the configured archive-table container, resolve the selected category's archive database, and confirm the current source `Name`, `收获`, and `改进` are saved in the selected archive database row. For dismissed rows, do not require an archive target; instead compare the current source row against the dismissed source fingerprint. In test mode, also verify the ledger for moved/manual-match rows. If backup/state evidence is missing or inconsistent, the source row is not checked complete, the target database row is missing, duplicated, or source content changed while pending, keep that row as `needs-review` and do not remove the source.

When final source removal succeeds, update local state. In test mode, update the matching ledger rows from `pending-removal` to `removed`. If source removal is not performed, keep the row as `pending-removal`.

## Real Mode

For `organize todo`, formal real use is approval-first: `next` may stage the active real batch, `ok` may append approved rows to the selected configured archive database, `dismiss` may mark source rows for removal without archiving, `undo` may undo database rows created by this workflow or clear a dismissed pending-removal, and `done` may stop with a summary. After the user replies `confirm`, checked pending source rows may be removed only after the relevant archive-database or dismissed-source verification above passes.

Real source cleanup should use the connector when it can archive pages. If the connector cannot expose page `archived: true`, a local fallback/API path may set only the verified source pages to `archived: true` after `done` -> `confirm`. This exception is limited to final source cleanup and must not become the default path for preview, `ok`, manual-match checks, or archive row creation.

For the external CLI fallback, `mode = real` may run the formal batch loop only when explicitly configured and approved:

- `preview` remains read-only and never modifies Notion.
- `next` stages a real active batch but must not delete empty completed source rows.
- `ok N` / `ok N to <category>` / `ok N to all` are explicit approval to append real configured archive database rows.
- `dismiss N` is explicit approval to skip archive creation and consider the source row for final removal only after `done` and `confirm`.
- `undo N` may only archive/delete the database row created by this workflow or clear local dismissed state, never the source row.
- `done` produces a summary and waits for `confirm`.
- `confirm` and `remove-sources` may archive checked source rows only after verifying the corresponding configured category archive database row still contains the current `Name`, `收获`, and `改进`, or for dismissed rows after verifying the current source still matches the dismissed fingerprint.

Real preview rules:

- Resolve each source row's configured category/project relation to category names.
- Show single-category candidates with choices `ok N`, `dismiss N`, and `skip N`, in that order.
- Show multi-category candidates with choices `ok N to <category>` for each project, `ok N to all`, `dismiss N`, and `skip N`, in that order; do not expect the user to remember syntax.
- Treat bare `ok N` for a multi-category real item as invalid in any future write path.
- Interpret `ok N to all` as one appended row in each selected category database.
- Real manual-match must compare only `Name`, `收获`, and `改进` inside the selected configured category archive database.
- After implementing or changing real rules, doublecheck category routing, toggle/database lookup, three-column row shape, multi-category choices, manual-match database matching, empty cleanup read-only behavior, undo scope, source-row cleanup verification, and output usability before further real writes.
