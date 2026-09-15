---
title: "Canvas Undo/Redo"
audience: modeller
area: modelling
updated: 2026-06-12
---

## What this covers

The Model Canvas records your editing actions and lets you step backwards and forwards through them with **Ctrl+Z** (undo) and **Ctrl+Y** (redo). This page explains what gets tracked, what happens when you reload the page, the keyboard shortcuts, and the toolbar buttons.

---

## What is tracked

One user action equals one undo step. The canvas tracks these kinds of actions:

| Action | What undo does |
|---|---|
| **Moving a table** (one drag gesture, however far) | Puts the table back where the drag started, together with any relationship lines the move caused to be redrawn. |
| **Resizing a table** | Restores the previous size, and the lines that were redrawn to fit it. |
| **Arranging the model** (any preset, whole model or selection) | Restores every table to its position before the arrangement, and every relationship to the route it had — the whole arrangement is one step. |
| **Rerouting the links** | Restores the previous routes. No table moved, so nothing else changes. |
| **Editing a route by hand** (a bend, or an attachment point) | Restores the previous path. |
| **Locking or unlocking a relationship** | Reverses the lock, and with it the path the lock froze. |
| **Locking or unlocking a table's position** | Reverses the lock. The tables do not move — locking a position changes no coordinate. |
| **Renaming a table** | Restores the previous display name. |
| **Creating or deleting a join** | Deletes the created join, or recreates the deleted one. |

A move or resize is one step even though it changes two things — the table's
geometry and the relationship lines that had to be redrawn around it. Undo puts
both back together, because splitting them would let you undo a move and be left
with lines drawn for where the table used to be.

A gesture the canvas refuses records nothing. Dragging a table across a locked
relationship's path puts the tables back, and there is no undo step for it: the
move did not happen.

The very first drag is immediately undoable — move a table, press Ctrl+Z, and it snaps back. Undo always reverses the **most recent** action first; pressing it again walks further back, one action at a time. Redo replays the undone actions in order. Making a new change after an undo clears the redo history, just like a text editor.

---

## How to undo and redo

### Keyboard shortcuts

| Shortcut | Action |
|---|---|
| **Ctrl+Z** (Windows) / **Cmd+Z** (Mac) | Undo the most recent action |
| **Ctrl+Y** (Windows) / **Cmd+Y** (Mac) | Redo the last undone action |

### Toolbar buttons

The canvas toolbar shows **Undo** and **Redo** buttons. When there is nothing to undo or redo, the corresponding button is greyed out (disabled). The disabled state updates immediately after each action.

---

## What survives a reload

Two different things are at play here, and they behave differently:

- **The result of an undo or redo is saved.** When undo moves a table back, that restored position is written to the model's saved layout exactly as if you had dragged it there yourself. Reload the page and the table is still where the undo put it. The same goes for renames and join changes — they are real model edits.
- **The history stack itself is session-scoped.** Refreshing the page or switching models clears the list of steps you could undo or redo. You cannot reload and then undo something from before the reload.

In practice: undo freely, reload safely — but do your undoing before you leave the page.

---

## Limitations

- Dimension, measure, and other property edits are not undoable from the canvas toolbar — change them back from their panels.
- The stack has a fixed maximum depth. Very long editing sessions may lose the earliest steps.
- The history stack clears on reload or when you switch to a different model (see above — the *results* of your undos are kept).

---

## Related

- [Model Canvas Tour](model-canvas-tour.md)
- [Arrange the Canvas](arrange-the-canvas.md)
- [Define Joins](define-joins.md)
- [Define Dimensions](define-dimensions.md)

---

← [Arrange the Canvas](arrange-the-canvas.md) | [Home](../index.md) | [Define Joins →](define-joins.md)
