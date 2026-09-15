export type AppMode = 'ask' | 'report-builder' | 'kpi';
/** Rendered order of the task-pane tabs; drives keyboard navigation. */
export declare const TAB_ORDER: readonly AppMode[];
interface ModeTabsProps {
    mode: AppMode;
    onModeChange: (mode: AppMode) => void;
    /** Label of the Ask tab: the agent's display name when configured. */
    askLabel: string;
}
/**
 * The three-section tab strip under the task-pane header.
 *
 * Bug-5965: roving tabindex. Only the active tab is in the tab order; Arrow
 * keys, Home and End move focus AND activate; Enter/Space activate the
 * focused tab. `aria-controls` targets `tabpanel-<mode>` rendered by App.
 */
export default function ModeTabs({ mode, onModeChange, askLabel }: ModeTabsProps): import("react/jsx-runtime").JSX.Element;
export {};
