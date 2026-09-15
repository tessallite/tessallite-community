---
title: "Arrange the Canvas"
audience: modeller
area: modelling
updated: 2026-09-14
---

## What this covers

Where tables sit on the Model Canvas and how relationship lines are drawn between them. Automatic arrangement, the controls that steer it, and the two ways of holding part of a diagram still while the rest is rearranged.

---

## Why layout is a modelling concern

A semantic model is a graph, and a graph has no inherent positions. Somebody has to decide where each table goes, and that decision is not cosmetic: the arrangement is how a reader understands the model. A star that looks like a star is read as a star. The same five tables strung out in a line, with connectors crossing each other, get read as "complicated" — and a reader who thinks a model is complicated asks fewer questions of it.

Layout is also shared. Positions are saved on the **model**, not per user, so every modeller opening it sees the same diagram. That is deliberate: it makes conversation possible ("the customer dimension, top right"). The cost is that the last save wins, so a large rearrangement is worth agreeing before you make it.

---

## Two kinds of decision

Everything on this page is one of two things, and it helps to keep them apart:

- **Placement** — where the table cards sit.
- **Routing** — how the relationship lines get from one card to another.

They are separate. You can rearrange the cards and keep a line you drew by hand. You can reroute every line without moving a single card. The controls are grouped along those lines.

---

## Arranging automatically

The layout panel (the **Layout presets** control on the canvas toolbar) offers three presets, plus two settings that steer them.

### Presets

| Preset | Shape it produces | Use it when |
|---|---|---|
| **Hierarchical** | Layered, flowing in one direction | The model has a clear direction of reading — a fact and its dimensions, or a snowflake with depth |
| **Compact Grid** | The same layered engine, tightened up | The model is large and you want more of it on screen at once |
| **Radial** | Facts at the centre, everything else on a ring around them | A star or constellation schema, where "what is at the centre" is the point |

Radial is fact-centred by construction: fact tables form the centre and every other card is placed on a measured ring around them. If a model has no fact table, one representative card takes the centre, so the result is still a star rather than an empty middle.

### Direction and spacing

**Direction** (top to bottom, or left to right) applies to the two layered presets and decides which way the layers run. Top-to-bottom suits a model read as a flow; left-to-right suits a wide model on a wide screen.

**Spacing** (normal or compact) trades readability against density. Compact is for getting a big model into one frame; it is not a good default for a model you are still explaining to someone.

Both settings travel with the action rather than sitting as a separate "apply" step: choose a direction, then choose a preset, and the arrangement uses both. The last combination that actually produced an arrangement is saved with the model, so reopening it starts where you left off.

### Arrange all, or just a selection

**Arrange Selected** rearranges only the tables you have selected and leaves everything else exactly where it is. This is the control to reach for when one corner of a large diagram has become messy and the rest is fine — a full arrange would undo work you want to keep.

The control tells you what it will do before you use it. With nothing selected it says so; with a selection it states how many tables will move. If every table you have selected is held in place (see below), the control stays unavailable rather than starting an arrangement that cannot move anything.

### Reroute Links

**Reroute Links** recomputes every relationship line and does not move a single card. Use it after you have dragged cards around by hand and the lines have become untidy, when you are happy with the placement and only want the connectors redrawn.

It also returns every relationship to the model's **Edge Pathing** setting. A single relationship can be given its own path style from the Joins panel, and that choice overrides the model setting for that one line — which is what you want when you set it deliberately, and confusing when you have forgotten. Reroute Links is the one action that clears those individual choices, so the whole diagram follows the model setting again. A relationship whose route is locked keeps its style: you froze that line, and its style is part of what you froze.

---

## Holding part of the diagram still

Automatic arrangement is all-or-nothing by default: it will move whatever it is allowed to move. Two mechanisms let you decide what it is not allowed to touch, and they protect **different things**.

### Locking a table's position

Locking a table's position protects **where it sits**. A position-locked table is not moved by any automatic arrangement, including the placement of newly added tables. You can still drag it yourself — the lock is about the machine, not about you.

Lock a table's position when it carries meaning that the layout engine cannot know: the fact everyone looks at first, a dimension you have deliberately placed next to a related one, a card you have positioned to leave room for something you are about to add.

A position-locked card shows a padlock in its top-right corner and takes the lock colour on its border, so it is visible why an arrangement left it alone.

### Locking a relationship's route

A lock protects a **relationship's line** — its complete path, both attachment points, and the bends in between. Nothing recomputes a locked route.

Lock a route when you have drawn a line by hand to say something: routed it around a table to keep a diagram readable, or pulled it clear of a crowded area. Without a lock, the next Arrange or Reroute is entitled to replace it.

Locking captures the line **exactly as it is drawn at that moment**. This matters for a route you have never edited: an automatically drawn line has no stored path of its own, it is recalculated each time the diagram is drawn. Locking writes the current one down so it survives reopening the model.

Locked relationships show a padlock at the midpoint of the line, and their handles are withdrawn — you cannot accidentally drag a route you have frozen.

### The two are independent

The two locks sit under one heading because they are one idea applied to two subjects, but they stay independent: unlocking a relationship never unlocks a table's position, and releasing a table never releases a relationship.

A lock does imply one thing about placement, though: the two tables a locked relationship connects are held in place as well, because a frozen line cannot stay accurate if the cards it attaches to move. If you try to drag or resize one of them, the canvas refuses and tells you which relationship to unlock.

---

## What the canvas refuses, and why

Two refusals are worth understanding, because they look similar and behave differently on purpose.

**Moving a table that a locked relationship attaches to** is refused outright. The card does not move at all, and a message names the reason. Moving it would invalidate the frozen line by definition, so there is nothing to weigh up.

**Moving any other table across a locked line** is allowed while you drag and refused when you let go, at which point the tables go back where they were. Most moves do not go anywhere near a locked line, and refusing all of them up front would mean locking one relationship quietly froze the whole diagram. So the canvas waits to see where the card actually lands.

A line that merely runs along a card's edge is not "across" it. Connectors legitimately run alongside cards, and treating that as a collision would make ordinary tidying impossible.

---

## Editing a route by hand

Click a relationship to select it. Its line, both end tables, and the columns it joins on are highlighted together, so you can see the whole relationship rather than tracing the line by eye.

Selected routes can be edited directly:

- **Drag a bend** to move it.
- **Drag the line itself** where there is no bend to add one.
- **Drag an end point** along a card's border to change where the line attaches.
- **Double-click the line** to discard your edits and return it to automatic routing.

### Orthogonal routes stay orthogonal

Relationships are drawn in one of two path styles. **Orthogonal** routes are made of horizontal and vertical segments; **straight** routes are direct lines at any angle.

When you edit an orthogonal route, the canvas keeps it orthogonal. Drag a bend anywhere you like: neighbouring bends follow, and where a bend's neighbour is a fixed end point the canvas inserts an extra corner instead. You never produce a diagonal segment, and you never have to think about which direction you are allowed to drag.

An edit that cannot be drawn at all — a bend dropped inside one of the relationship's own tables, for instance — is discarded, and the route returns to what it was. The canvas restores rather than saving a broken connector.

---

## What is saved, and what is not

| Saved with the model | Session only |
|---|---|
| Table positions and sizes | The undo/redo history |
| Table position locks | Which panel is open |
| Route locks, and the frozen path itself | The current zoom and pan |
| Hand-drawn bends and attachment points | |
| The last layout preset, direction and spacing that produced an arrangement |  |

A layout written by an older version of Tessallite opens unchanged — opening a model never rewrites its saved layout — and anything in a saved layout that this version does not recognise is kept, not discarded.

Every change on this page participates in undo. See [Canvas Undo/Redo](canvas-undo-redo.md).

---

## Worked example — tidying a model that has grown

**Context.** A `retail` model started as one fact and four dimensions. Over three months it has grown to eleven tables, and the original arrangement no longer makes sense. Two of the relationships have been routed by hand around a crowded corner and those routes are worth keeping.

1. **Protect what is already right.** Select the two hand-routed relationships in turn and **Lock Route** on each. Their paths are now frozen, and their end tables are held with them.
2. **Protect the anchor.** The `orders` fact sits where everyone expects it. Select it and **Lock Table Position**.
3. **Arrange the rest.** Choose **Left to right**, then **Hierarchical**. The eleven tables are laid out around the locked fact; the locked relationships and their end tables do not move.
4. **Assess.** The layered result has spread the model wider than the screen. Switch **Spacing** to **Compact** and run **Hierarchical** again.
5. **Tidy the connectors.** Placement is now right but a few lines wander. **Reroute Links** redraws every unlocked connector against the final positions without moving any card.
6. **Release what no longer needs protecting.** The position lock on `orders` can stay — it will keep the anchor stable through future arrangements. The route locks can stay too, or be released now that the surrounding tables have settled.

The order matters. Protecting first and arranging second means one arrangement, not an arrangement followed by an attempt to reconstruct what it overwrote.

---

## Common pitfalls

**Arranging before locking.** The most common way to lose work. Automatic arrangement will move anything it is allowed to move, and undo is a single step back — not a way of recovering a hand-built layout three arrangements later. Lock first.

**Expecting a lock to survive without a path.** Locking a relationship you have never edited freezes the line as currently drawn, which is usually what you want. But if you meant to freeze a *particular* path, draw it first and then lock — locking is a snapshot, not an instruction.

**Using Compact spacing as a default.** It fits more on screen and reads worse. Use it to see a large model whole, then switch back.

**Treating a lock as documentation.** Locking a table's position says nothing about the model — not that a table is important, not that it is a fact, not that it is finished. It only stops automatic placement moving it.

**Locking so much that there is nowhere left to go.** Locked tables are obstacles the arrangement must work around. Lock enough of them, close enough together, and a table has no room left between them — it is placed outside the group rather than squeezed into a space that does not exist. Lock the few cards whose position genuinely matters, not every card you are happy with.

**Fighting a preset.** If an arrangement is repeatedly not what you want, the model shape and the preset probably disagree. A snowflake will not lay out as a star. Try the other preset before rearranging eleven tables by hand.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Arrange Selected is unavailable although tables are selected | Every selected table is locked in place | The panel says so and gives the count; unlock their position, or unlock the relationship |
| A table will not move when dragged | A locked relationship attaches to it | The message names the situation; unlock that relationship first |
| Tables snapped back after a drag | The move left a table lying across a locked relationship's path | Move it elsewhere, or unlock that relationship |
| Lock Route is unavailable on a selected relationship | Its current line cannot be frozen as drawn | Run Reroute Links, then lock |
| Changing Edge Pathing does not change some relationships | Those relationships have their own path style, set from the Joins panel | Run Reroute Links, which returns them to the model setting |
| A relationship's path style cannot be set back to "follow the model" | The Joins panel control cycles through three states | Click it until it returns to following the model setting |
| An action on a relationship is refused with a message about the route being locked | Reset Path and path style cannot change a frozen line | Unlock the route first, from the Joins panel or the layout panel |
| An arrangement moved a table that should have stayed | Its position was not locked — arrangement moves everything it may | Undo, lock its position, arrange again |
| A hand-drawn route came back different after Arrange | Unlocked routes may be replaced by an explicit arrangement | Lock routes you want kept |
| Reopening the model lost a bend | The edit could not be drawn and was discarded rather than saved | Redraw it; the canvas will not save a route it cannot draw |
| A table was placed well away from the rest after an arrangement | Locked tables left it no room in the cluster | Nothing is wrong with the model. Unlock a neighbouring table's position, or move the parked table by hand |

---

## Related

- [Model Canvas Tour](model-canvas-tour.md)
- [Canvas Undo/Redo](canvas-undo-redo.md)
- [Define Joins](define-joins.md)
- [Add Tables to a Model](add-tables-to-a-model.md)

---

← [Model Canvas Tour](model-canvas-tour.md) | [Home](../index.md) | [Canvas Undo/Redo →](canvas-undo-redo.md)
