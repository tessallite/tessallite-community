interface InsertActionsProps {
    data: Record<string, unknown>[];
    headers: string[];
    onInsertTable?: () => void;
    onInsertChart?: () => void;
    onLocalPivot?: () => void;
    onCubeFormulas?: () => void;
    onLiveConnection?: () => void;
    onShowQuery?: () => void;
    recommendedAction?: 'table' | 'chart' | 'pivot' | 'cube';
}
export default function InsertActions({ data, headers, onInsertTable, onInsertChart, onLocalPivot, onCubeFormulas, onLiveConnection, onShowQuery, recommendedAction, }: InsertActionsProps): import("react/jsx-runtime").JSX.Element;
export {};
