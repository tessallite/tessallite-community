import type { Zone } from '../../types/tessallite';
export interface ZoneItem {
    id: string;
    name: string;
    zone: Zone;
    operator?: string;
    values?: string[];
    /**
     * The dimension's physical data-type spelling (e.g. "bigint", "date",
     * "character varying"). Threaded in when the field is added so the filter
     * dialog can gate gt/lt validation by semantic category rather than assuming
     * every comparison is numeric (a date/text gt is server-valid).
     */
    data_type?: string;
    /**
     * Kind of zone item. Defaults to a measure/dimension field. Named-set and
     * hierarchy-level items are NOT directly bindable by their UUID token, so
     * F-025-11 resolves them at add-time into a bindable dimension (+ member
     * list for named sets) and stores the resolution here.
     */
    kind?: 'field' | 'named_set' | 'hierarchy_level';
    /**
     * The technical dimension name this item binds to. For a named set this is
     * the set's underlying dimension; for a hierarchy level it is the level's
     * key-attribute dimension. The query builder uses this on the axis instead
     * of the raw UUID token (F-025-11).
     */
    bindDimension?: string;
    /**
     * For a named set: the member keys that define the set. The query builder
     * emits an `in` filter over these on `bindDimension` (F-025-11).
     */
    memberKeys?: string[];
}
interface ZoneMappingGridProps {
    items: ZoneItem[];
    onRemove: (id: string, zone: Zone) => void;
    onClear: () => void;
    onInsertTable: () => void;
    onInsertChart?: () => void;
    onInsertLocalPivot?: () => void;
    onOpenTemplates: () => void;
    onUpdateFilter?: (id: string, operator: string, values: string[]) => void;
    compatibilityWarning?: {
        title: string;
        messages: string[];
        compatibleDimensionNames?: string[];
    } | null;
    insertDisabledReason?: string | null;
}
export default function ZoneMappingGrid({ items, onRemove, onClear, onInsertTable, onInsertChart, onInsertLocalPivot, onOpenTemplates, onUpdateFilter, compatibilityWarning, insertDisabledReason, }: ZoneMappingGridProps): import("react/jsx-runtime").JSX.Element;
export {};
