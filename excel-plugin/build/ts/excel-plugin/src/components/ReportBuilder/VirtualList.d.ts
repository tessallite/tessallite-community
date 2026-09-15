import { ReactNode } from 'react';
interface VirtualListProps<T> {
    items: T[];
    itemHeight: number;
    maxVisibleItems?: number;
    renderItem: (item: T, index: number) => ReactNode;
    listHeight?: number;
}
export default function VirtualList<T>({ items, itemHeight, maxVisibleItems, renderItem, listHeight, }: VirtualListProps<T>): import("react/jsx-runtime").JSX.Element | null;
export {};
