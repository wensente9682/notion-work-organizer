# Setup New Notion Work System

Use this route when the user wants to create a new Notion personal work/to-do system from scratch.

Setup is approval-gated and non-destructive by default. It must not create any
Notion object or perform source cleanup without explicit approval for that
exact action.

## Setup Routing

Start by separating three cases:

- **Use existing organize config/state**: if the user already has organize-owned local profile/state or an active organize batch, do not run setup; route to `organize.md`.
- **Adopt existing Notion list**: if the user already has a Notion to-do list, route to `adopt-existing.md`.
- **Create a new system**: continue with this setup route only when the user wants a new Notion system.

If the request is ambiguous, ask whether the user wants to adopt an existing list or create a new one.

Do not treat unrelated Notion connector access, unrelated Notion database IDs, or other vibe coding project configs as existing organize config. Setup must not change global Notion connector behavior or other Notion-based workflows.

## New System Onboarding

1. Ask the user to select a blank or dedicated Notion page.
2. Suggest Categories as optional starting ideas. Suggestions are not a
   confirmation or setup input.
3. Let the user accept, remove, rename, or add Categories.
4. Ask the user to explicitly confirm the final current Category list.
5. Only then pass that confirmed list to the backend.

After confirmation, the backend owns the canonical Daily Work schema, source
creation, the verified Total saved view ordered by `Work Date DESC`, archive
creation, Category-to-archive mappings, verified database and data source
identities, private profile generation, and progression to `setup-complete`.
Do not ask the user to design routing, provide IDs, copy or edit
JSON/configuration, or construct or maintain a private profile.

## Approval Gates

Before any Notion write, ask for explicit approval for the exact action.

Examples of actions that require explicit approval:

- Create the Daily Work database with its canonical fields.
- Create and verify the Total saved view ordered by `Work Date DESC`.
- Create a Category archive database.

Never combine setup approval with final source cleanup approval. Source cleanup belongs to the organize `done` plus `confirm` flow in `organize.md`.

## Verification Path

After Category confirmation and each approved backend action:

1. Use the backend-owned next action and exact preview.
2. Obtain approval for that one action, then execute it once.
3. Continue only after backend result validation confirms success.
4. After `setup-complete`, recommend the read-only Total/Organize path.

Use `organize.md` only after setup is complete and the user asks to review/archive records.
