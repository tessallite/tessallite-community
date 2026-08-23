---
title: "Save and Version a Model"
audience: modeller
area: modelling
updated: 2026-08-18
---

![Model Builder toolbar showing the Save, Deploy, and Versions buttons.](../assets/screencaps/model-builder-toolbar-save.png)

## What this covers

Every change you make in the Model Builder — adding a table, defining a join, editing a measure — lives in the live model state immediately. **Save** captures that state as an immutable version row, so you can roll back to it later or redeploy it without rebuilding the change. This article covers when to save, what the version history shows, and how to revert.

---

## Before you start

- Your project access role controls what you can do here, because each action carries a different level of risk:
  - **Viewers** can open the Versions list and look at past versions and their differences.
  - **Modellers** can additionally **Save** a new version and **Deploy** or **Undeploy** a model.
  - **Reverting** to an older version is the highest-impact action — it rewrites the live model and moves the deployed pointer (though it never deletes any version) — so it requires the **Admin** role on the project.

  Check your role in **Tenant Admin → Project Access**. If a button is missing or returns a "not permitted" message, your role is below the level that action needs.
- Save and Versions buttons appear on the Model Builder top toolbar, next to the model name.

---

## The Save dialog

When you click the **Save** button (disk icon) on the toolbar, Tessallite opens a dialog with two choices:

### Save Canvas Layout Only

Choose this option when you have only rearranged table cards, adjusted the viewport, or edited canvas notes -- and you have not changed any model content (tables, measures, dimensions, joins, etc.).

This option writes your current canvas positions and viewport to the server immediately. It does **not** create a new model version, so the version history stays clean and the version number does not increment.

**When to use it:**
- You dragged tables into a cleaner arrangement and want to preserve the layout.
- You added or edited canvas notes but did not change any model definitions.
- You want to save your visual workspace without cluttering the version timeline.

### Save All Model Changes

Choose this option when you have made substantive model edits -- adding tables, editing measures, defining joins, configuring aggregates, or any combination of content and layout changes.

This option first saves the canvas layout, then snapshots the entire model state into a new version row. The version number increments and the snapshot appears in the version history.

**When to use it:**
- You added or removed a table, measure, dimension, join, hierarchy, or any other model entity.
- You changed aggregate definitions, refresh policies, AI scheduler config, or model settings.
- You made both layout and content changes in the same editing session.

An optional **Change summary** field appears when this option is selected. Writing a short note (for example, "Added monthly revenue measure") makes it easier to find this version later in the history.

### How the dialog chooses its default

- If you have unsaved model content changes (the toolbar chip reads **Edited**), the dialog defaults to **Save All Model Changes**.
- If only the canvas layout has changed (no content edits), the dialog defaults to **Save Canvas Layout Only**.

You can always switch between the two options before clicking Save.

## Saving a version (step by step)

1. Open the Model Builder for any model.
2. Make whatever edits you intend (add a table, edit a measure, drag table cards on the Canvas).
3. The status chip on the toolbar reads **Edited** with an amber dot whenever the live state differs from the last saved version.
4. Click the **Save** button (disk icon). The Save dialog opens.
5. Choose **Save All Model Changes** (or leave it selected if it is already the default).
6. Optionally type a short summary describing what changed.
7. Click **Save**. Tessallite snapshots the entire per-model state -- tables, columns, joins, dimensions, measures, hierarchies, UDAs, aggregate definitions, refresh policies, AI scheduler config, model settings, and the canvas layout -- into a new version row.
8. The status chip switches to **Saved v{N}** and the dot turns off.

---

## Viewing version history

1. On the toolbar, click the **Versions** button (clock icon).
2. The Versions dialog lists every saved version with version number, who saved it, when, and an optional summary.
3. The version that is currently deployed shows a green badge.

---

## Reviewing and discarding pending changes

At any moment a model can be in three states at once: the **draft** you are editing, the **last saved** version, and the **deployed** version the gateway actually serves. When they drift apart it is easy to lose track of what you have changed but not saved, or saved but not yet deployed. The **Pending changes** tab in the Versions dialog lays this out and lets you throw work away cleanly.

Open the Versions dialog (clock icon) and select the **Pending changes** tab. It shows two groups:

- **Unsaved edits** — everything you have changed in the editor since your last Save, compared against the last saved version. This is the work that would be lost if you closed the model without saving.
- **Saved but not deployed** — the difference between your last saved version and the version currently serving queries. These are changes that are safely saved but that live BI users are not seeing yet, because only a Deploy makes a version live.

Each group shows a count and a **Review changes** button. Reviewing opens the same field-by-field difference view you get when comparing two saved versions, so you can see exactly which measures, dimensions, calendars, named queries, and relationships changed before you decide.

### Discard unsaved edits

If you have been experimenting and want to throw the experiment away, click **Discard unsaved edits**. Tessallite restores the editor to your last saved version and drops everything you changed since. This affects only your draft — what the gateway serves does not change, so discarding is safe to do at any time. Because the discarded edits were never saved, they cannot be recovered, so you are asked to confirm first.

### Discard back to the deployed version

**Discard to deployed version** goes further: it resets the whole draft back to the version that is currently live, throwing away both your unsaved edits and any saved-but-undeployed changes. Your saved versions are **not** deleted — they stay in the history and an admin can redeploy or revert to them later. This is the "start again from what is in production" button, so it carries the same admin permission and typed confirmation as Revert, and it never changes governance (personas, row security, data tags, or certification).

---

## Reverting to an older version

> Reverting does not delete anything. It brings back an old version's shape by saving it again as a brand-new version on top of your history — so nothing is ever lost, and you can always move forward again.

1. Open the Versions dialog.
2. Find the version you want to roll back to and click **Revert**.
3. Type `revert to v{N}` in the confirmation box (where N is the version number) and click **Revert**.
4. Tessallite copies the chosen version's definition into the live state and records it as a **new** version at the top of the history (its summary reads "Revert to v{N}"). Every version you already had — including the ones newer than the one you reverted to — stays in the history untouched.
5. Aggregate definitions that exist in the live state but not in the chosen snapshot are marked **retired**; the next retirement sweep drops their physical tables.
6. If the model was deployed when you clicked Revert, the deploy pointer is retargeted to the **new** version Tessallite just created (its shape is what is now live) and `last_deployed_at` is refreshed. Query routing and the XMLA gateway switch to the reverted shape immediately. Models that were undeployed stay undeployed.

Because the revert is itself just another saved version, it is fully reversible: if you change your mind, revert again to whichever version you want. Think of it as "check out an old snapshot and commit it as the new tip", never as "erase what came after".

---

## What is and is not in a version

| In the version | Not in the version |
|---|---|
| Tables, columns, joins, hierarchies, UDAs | Project connections (credentials never leave the source DB) |
| Dimensions, measures | System / tenant / project settings |
| Aggregate definitions and refresh policies | Query logs, miss logs, alerts |
| Per-model AI scheduler config and model settings | Deployment pointer (lives separately on the model row) |
| Canvas layout (table positions, viewport) | |

---

## Git Timeline

Every save, deploy, and restore is also recorded as a git commit in a local repository on the server. You can view this commit history in the **Timeline** tab of the Versions dialog.

### What the timeline shows

- **Model version commits** (large blue dots) -- created whenever you save all model changes. Each one carries the version number and your optional summary.
- **Layout-only commits** (small grey dots) -- created when you save canvas layout only.
- **Restore commits** (orange dots) -- created when an admin reverts to an older version.
- **Deploy tags** -- a green badge appears on version commits that have been deployed.

### Viewing differences

Click **View changes** on any commit to see a unified diff between that commit and the one before it. The diff shows exactly which lines changed in the model YAML and canvas layout JSON files.

### Git Integration (Enterprise)

Enterprise users can push the tenant's model history to a remote git repository (GitHub, GitLab, or any HTTPS-accessible git remote). Configure this in **Tenant Admin > Git Integration**:

1. Enter the remote repository URL (e.g. `https://github.com/your-org/models.git`).
2. Enter a Personal Access Token with push access.
3. Click **Test connection** to verify.
4. Once saved, every model commit is automatically pushed to the remote after it is created locally.

---

## Tips

- Click Save before any large refactor -- having a clean rollback target is cheaper than reconstructing the change.
- Use the optional summary on Save to record *why* the change was made; the field is plain text and travels with the version row.
- The first time you click Save on an existing model, version 1 captures the current live state.
- After rearranging the canvas for readability, use **Save Canvas Layout Only** to preserve your arrangement without inflating the version count. Other team members who open the model will see your improved layout.
- The canvas layout is also included in every full model version, so you do not need to save layout separately before saving all changes -- **Save All Model Changes** captures both.

---

## Related articles

- [Deploy a Model](deploy-a-model.md)
- [Export and Import a Model](export-and-import-a-model.md)
- [View Model Lineage](view-model-lineage.md)

---

← [View Diagnostics](view-diagnostics.md) | [Home](../index.md) | [Deploy a Model →](deploy-a-model.md)
