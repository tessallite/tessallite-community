/**
 * Layout panel — the control surface for coordinated table layout and
 * relationship routing.
 *
 * It replaces the previous inline preset menu, which offered presets only. The
 * hierarchical direction and spacing controls the specification requires
 * (spec §3, R03 — "Arrange all with useful hierarchical direction/spacing
 * controls") therefore had no home in the interface and were unreachable, even
 * though the layout engine accepted and honoured both values all along.
 *
 * Accessibility (R13): every control is a real `button`, so keyboard and
 * screen-reader reachability come from the platform rather than from bespoke key
 * handlers. Each option group is labelled with its own accessible name, the
 * current choice is exposed through `aria-pressed`, and the working state is
 * announced by the canvas' existing busy indicator, which stays visible when
 * this panel is closed; the panel deliberately does NOT repeat that live region,
 * because two polite status regions announcing the same work make a screen
 * reader say it twice. Read-only sessions never render this panel — the caller
 * gates it — because every control here mutates the saved layout.
 *
 * Arrange selected (spec §3, R04) arranges only the tables the user picked and
 * leaves everything else exactly where it is. Whether it can run is not a
 * property of the selection alone: a pinned table, or one docked by a locked
 * relationship, is not movable. The caller resolves that through the shared
 * movable-table rule and passes the resulting count, so the control is never
 * enabled for an arrangement the worker would refuse — and when it is disabled
 * the panel says which of the two reasons applies.
 *
 * Locking a table's position and locking a relationship's route sit in ONE
 * group under one verb, because to a user they are the same idea applied to two
 * subjects. They remain separate CONTROLS with separate state — locking a route
 * never locks a table's position, and releasing one never releases the other
 * (spec §4) — but the earlier vocabulary called one "pin" and the other "lock",
 * which sent users looking for the wrong word. The persisted fields are
 * unchanged; only the wording is.
 */
import { useT } from "../../../i18n";
import type { LayoutDirection, LayoutPreset, LayoutSpacing } from "./types";

export interface CanvasLayoutPreference {
  preset: LayoutPreset;
  direction: LayoutDirection;
  spacing: LayoutSpacing;
}

export interface CanvasLayoutPanelProps {
  preferences: CanvasLayoutPreference;
  busy: boolean;
  /** Tables on the canvas. Zero means there is nothing any action could do. */
  tableCount: number;
  /** Tables the user has selected, whether or not they can be moved. */
  selectedTableCount: number;
  /**
   * Selected tables this operation could actually move — the caller resolves it
   * through the shared movable-table rule, so a pinned selection or the endpoint
   * of a locked relationship does not count towards it and the control does not
   * offer an arrangement the worker would refuse.
   *
   * The two counts are separate because the difference between them is the
   * interesting case: something IS selected and none of it may move. Telling
   * that user to "select some tables" would be wrong.
   */
  movableSelectedCount: number;
  onPreferenceChange: (next: Partial<CanvasLayoutPreference>) => void;
  onArrangeAll: (preset: LayoutPreset) => void;
  onArrangeSelected: () => void;
  onRerouteLinks: () => void;
  /**
   * State of the selected relationship's route lock.
   *
   * `none` — nothing is selected; `invalid` — the selected relationship's
   * displayed route is not geometry a lock could freeze, so offering the action
   * would strand it in a broken shape no later action is allowed to repair.
   */
  routeLock: "none" | "invalid" | "locked" | "unlocked";
  onToggleRouteLock: () => void;

  /**
   * Pin state of the table selection. `unpinned` covers a mixed selection too:
   * the action then pins the rest, which is what a user asking to "pin these"
   * expects.
   */
  tablePins: "none" | "pinned" | "unpinned";
  onTogglePin: () => void;
}

const PRESETS: LayoutPreset[] = ["hierarchical", "compact", "radial"];
const DIRECTIONS: LayoutDirection[] = ["DOWN", "RIGHT"];
const SPACINGS: LayoutSpacing[] = ["normal", "dense"];

const PRESET_LABEL: Record<LayoutPreset, string> = {
  hierarchical: "canvas.layoutHierarchical",
  compact: "canvas.layoutCompact",
  radial: "canvas.layoutRadial",
};
const DIRECTION_LABEL: Record<LayoutDirection, string> = {
  DOWN: "canvas.layoutDirectionDown",
  RIGHT: "canvas.layoutDirectionRight",
};
const SPACING_LABEL: Record<LayoutSpacing, string> = {
  normal: "canvas.layoutSpacingNormal",
  dense: "canvas.layoutSpacingDense",
};

const CONTAINER: React.CSSProperties = {
  background: "#fff",
  border: "1px solid #cfd8dc",
  borderRadius: 4,
  padding: 6,
  display: "flex",
  flexDirection: "column",
  gap: 6,
  maxWidth: 220,
};
const GROUP: React.CSSProperties = { display: "flex", flexDirection: "column", gap: 2 };
const HEADING: React.CSSProperties = { fontSize: 10, textTransform: "uppercase", color: "#607d8b", padding: "0 2px" };
const ROW: React.CSSProperties = { display: "flex", gap: 2 };
const HINT: React.CSSProperties = { fontSize: 10, color: "#607d8b", padding: "0 2px", lineHeight: 1.4 };

const SELECTION_HINT_ID = "canvas-layout-selection-hint";
const LOCK_HINT_ID = "canvas-layout-route-lock-hint";
const PIN_HINT_ID = "canvas-layout-table-pin-hint";

function optionStyle(active: boolean, busy: boolean): React.CSSProperties {
  return {
    fontSize: 12,
    padding: "4px 10px",
    textAlign: "left",
    border: active ? "1px solid #1976d2" : "1px solid transparent",
    background: active ? "#e3f2fd" : "transparent",
    borderRadius: 3,
    cursor: busy ? "default" : "pointer",
  };
}

export default function CanvasLayoutPanel({
  preferences,
  busy,
  tableCount,
  selectedTableCount,
  movableSelectedCount,
  onPreferenceChange,
  onArrangeAll,
  onArrangeSelected,
  onRerouteLinks,
  routeLock,
  onToggleRouteLock,
  tablePins,
  onTogglePin,
}: CanvasLayoutPanelProps) {
  const t = useT();

  // An empty canvas disables every arrangement action: there is nothing to
  // place, and offering the action would report a failure the user cannot act
  // on. Arrange selected needs a movable selection on top of that.
  const emptyCanvas = tableCount === 0;
  const actionsDisabled = busy || emptyCanvas;
  const arrangeSelectedDisabled = actionsDisabled || movableSelectedCount === 0;

  // Why the selection action is unavailable, in the user's terms. It is plain
  // text rather than a live region: the canvas owns the only status region, and
  // a disabled button is not focusable, so the explanation has to be readable in
  // ordinary reading order instead of only on focus.
  const selectionHint = emptyCanvas
    ? t("canvas.layoutEmptyCanvasHint")
    : movableSelectedCount > 0
      ? t("canvas.layoutArrangeSelectedReady", { count: String(movableSelectedCount) })
      : selectedTableCount > 0
        // Selected, but every one of them is held in place. Saying "select some
        // tables" here would tell the user to do what they have already done.
        ? t("canvas.layoutArrangeSelectedHeld", { count: String(selectedTableCount) })
        : t("canvas.layoutArrangeSelectedHint");

  // The lock is a property of the selected relationship, not of a layout batch,
  // so it stays usable on an empty-of-selection canvas only in the sense that it
  // says why. It is still disabled while a batch runs: the batch may be about to
  // change the very route the lock would freeze.
  const lockDisabled = busy || routeLock === "none" || routeLock === "invalid";
  const lockLabel = routeLock === "locked" ? t("canvas.routeUnlock") : t("canvas.routeLock");
  const lockHint =
    routeLock === "none"
      ? t("canvas.routeLockNoSelection")
      : routeLock === "invalid"
        ? t("canvas.routeLockInvalid")
        : routeLock === "locked"
          ? t("canvas.routeLockedState")
          : t("canvas.routeUnlockedState");

  const pinDisabled = busy || tablePins === "none";
  const pinLabel = tablePins === "pinned" ? t("canvas.tableUnpin") : t("canvas.tablePin");
  const pinHint =
    tablePins === "none"
      ? t("canvas.tablePinNoSelection")
      : tablePins === "pinned"
        ? t("canvas.tablePinnedState")
        : t("canvas.tableUnpinnedState");

  return (
    <div role="group" aria-label={t("canvas.layoutPanelTitle")} style={CONTAINER}>
      <div role="group" aria-label={t("canvas.layoutDirection")} style={GROUP}>
        <span style={HEADING}>{t("canvas.layoutDirection")}</span>
        <div style={ROW}>
          {DIRECTIONS.map((direction) => (
            <button
              key={direction}
              type="button"
              aria-pressed={preferences.direction === direction}
              disabled={busy}
              onClick={() => onPreferenceChange({ direction })}
              style={optionStyle(preferences.direction === direction, busy)}
            >
              {t(DIRECTION_LABEL[direction])}
            </button>
          ))}
        </div>
      </div>

      <div role="group" aria-label={t("canvas.layoutSpacing")} style={GROUP}>
        <span style={HEADING}>{t("canvas.layoutSpacing")}</span>
        <div style={ROW}>
          {SPACINGS.map((spacing) => (
            <button
              key={spacing}
              type="button"
              aria-pressed={preferences.spacing === spacing}
              disabled={busy}
              onClick={() => onPreferenceChange({ spacing })}
              style={optionStyle(preferences.spacing === spacing, busy)}
            >
              {t(SPACING_LABEL[spacing])}
            </button>
          ))}
        </div>
      </div>

      <div role="group" aria-label={t("canvas.layoutPresets")} style={GROUP}>
        <span style={HEADING}>{t("canvas.layoutPresets")}</span>
        {PRESETS.map((preset) => (
          <button
            key={preset}
            type="button"
            aria-pressed={preferences.preset === preset}
            disabled={actionsDisabled}
            onClick={() => onArrangeAll(preset)}
            style={optionStyle(preferences.preset === preset, actionsDisabled)}
          >
            {t(PRESET_LABEL[preset])}
          </button>
        ))}
        <button
          type="button"
          disabled={arrangeSelectedDisabled}
          aria-describedby={SELECTION_HINT_ID}
          onClick={onArrangeSelected}
          style={{ ...optionStyle(false, arrangeSelectedDisabled), borderTop: "1px solid #eceff1" }}
        >
          {t("canvas.layoutArrangeSelected")}
        </button>
        <span id={SELECTION_HINT_ID} style={HINT}>{selectionHint}</span>
        <button
          type="button"
          disabled={actionsDisabled}
          onClick={onRerouteLinks}
          style={{ ...optionStyle(false, actionsDisabled), borderTop: "1px solid #eceff1" }}
        >
          {t("canvas.layoutRerouteLinks")}
        </button>
      </div>

      {/* One group, because these are two things you can LOCK, not a "pin" and
          a "lock". A table's position and a relationship's route are different
          subjects of the same idea, and presenting them as separate concepts
          made users hunt for the wrong word. The persisted field is still
          `tables[id].pinned` — this is the vocabulary changing, not the
          contract. */}
      <div role="group" aria-label={t("canvas.lockGroup")} style={GROUP}>
        <span style={HEADING}>{t("canvas.lockGroup")}</span>
        <button
          type="button"
          aria-pressed={tablePins === "pinned"}
          disabled={pinDisabled}
          aria-describedby={PIN_HINT_ID}
          onClick={onTogglePin}
          style={optionStyle(tablePins === "pinned", pinDisabled)}
        >
          {pinLabel}
        </button>
        <span id={PIN_HINT_ID} style={HINT}>{pinHint}</span>
        <button
          type="button"
          aria-pressed={routeLock === "locked"}
          disabled={lockDisabled}
          aria-describedby={LOCK_HINT_ID}
          onClick={onToggleRouteLock}
          style={{ ...optionStyle(routeLock === "locked", lockDisabled), borderTop: "1px solid #eceff1" }}
        >
          {lockLabel}
        </button>
        <span id={LOCK_HINT_ID} style={HINT}>{lockHint}</span>
      </div>

    </div>
  );
}
