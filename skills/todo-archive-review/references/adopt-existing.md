# Adopt Existing Notion List

Use this route when the user wants to connect an existing Notion to-do list to this work maintenance system.

Inspection is read-only: inspect, compare, and report without local or Notion writes. A private profile may be created or updated only through the separate approved step below. Do not create, update, migrate, archive, delete, or reshape Notion data during inspection or readiness verification.

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

6. Run the compatibility inspector.
   With the Notion connector, collect only the bounded structural evidence needed by the public `inspect_existing_system(reader, request)` seam: source and archive schemas, observed Category routing coverage, and proof that the configured view is accessible, completely read, saved-order verifiable, and contains structured date anchors. Do not include raw Notion payloads or task text in the report.

7. Report compatibility.
   Classify every source, routing, archive, ordered-view, and Total mapping requirement as `ready`, `missing`, or `incompatible`, with the bounded next action returned by the inspector. Ambiguous databases, inaccessible views, unsupported types, unverifiable ordering, and incomplete reads fail closed.

8. Recommend read-only preview.
   A fully ready report may recommend separately approving generation or update of the ignored private profile. Inspection itself never writes that profile. Organize preview remains a later step after an approved profile exists; do not combine adoption with an archive write or source cleanup.

## Approved Private Profile Step

Only after the user accepts a current successful report, or explicitly accepts
a partial report without incompatible findings:

1. Use the Inspector-generated opaque bindings evidence with
   `freeze_inspection_evidence(...)` to require an exact match with the
   bindings the user confirmed. A ready status by itself is not sufficient
   evidence for a different profile. Do not display or log the opaque digest.
2. Use `propose_adoption_profile(...)` with that frozen evidence and the same
   bindings to produce the sanitized proposal summary and its
   proposal-specific approval phrase.
3. Show that summary and ask for exact approval of this local profile write.
   This approval does not authorize any Notion mutation.
4. Only after the exact approval, call
   `persist_approved_adoption_profile(...)`.

Rejected or expired approval, stale inspection evidence, an ignore-boundary
failure, an externally changed destination, or interrupted persistence must
stop safely. Do not describe the profile as ready after any such failure.

## One-Profile Readiness

After the approved private profile has been saved, the normal Codex + Notion
route may call `verify_one_profile_readiness(...)` for an explicit `YYYY-MM`.
Use the Inspector evidence and the exact same ignored profile:
if that evidence is not current in this session, run the read-only Inspector
again instead of trusting the profile alone.

1. Verify Total first against the configured ordered view and mappings.
2. Only if Total succeeds, use the same frozen profile to enter an Organize
   read-only preview that checks the source fields, Category routing, and
   archive targets.
3. Return only Total's category subtotals and overlapping grand total plus a
   sanitized preview count. Never return item text, IDs, URLs, mappings,
   credentials, or local paths.

This readiness check grants no approval and creates no archive record, local
batch, backup, cache, or cleanup state. If either step fails, if the profile or
inspected bindings drift, or if a required read is incomplete, report only
overall `not-ready`; do not return partial readiness. The external Python CLI
remains an optional advanced fallback and is not required for this route.

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
- Status fields map by their exact user-visible status name.
- Rich text or title conventions can map by parsed label only if the rule is explicit and user-approved.
- Empty, malformed, unknown, or unmapped categories must fail closed or be
  reported as unresolved; never guess an archive target.
- Personal example category names may appear in private config, but public examples should use neutral placeholder names.

## Approval Gates

Ask for explicit approval before any of these actions:

- Adding missing fields to an existing Notion database.
- Creating archive tables or pages.
- Editing relation targets, select options, formulas, filters, views, or existing rows.
- Generating or updating an ignored local profile that contains real Notion bindings.
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
- Ordered view and Total mappings: ready / missing / incompatible.
- Next safest step: resolve one bounded finding or separately approve private profile generation.

After adoption is complete and the user asks to organize, switch to `organize.md`. Preserve all existing organize behavior, including active batch continuation, `ok`, `dismiss`, `skip`, manual match, `undo`, `done`, and `confirm`.
