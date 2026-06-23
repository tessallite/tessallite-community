import { ReactNode } from 'react';
import { List } from 'react-window';
import { Typography, Box } from '@mui/material';
import { tokens } from '../../theme';

interface VirtualListProps<T> {
  items: T[];
  itemHeight: number;
  maxVisibleItems?: number;
  renderItem: (item: T, index: number) => ReactNode;
  listHeight?: number;
}

interface RowProps<T> {
  items: T[];
  renderItem: (item: T, index: number) => ReactNode;
}

function Row<T>({
  index,
  style,
  items,
  renderItem,
}: {
  index: number;
  style: React.CSSProperties;
  items: T[];
  renderItem: (item: T, index: number) => ReactNode;
}) {
  return <div style={style}>{renderItem(items[index], index)}</div>;
}

export default function VirtualList<T>({
  items,
  itemHeight,
  maxVisibleItems = 50,
  renderItem,
  listHeight,
}: VirtualListProps<T>) {
  if (items.length === 0) return null;

  const height = listHeight ?? Math.min(items.length * itemHeight, maxVisibleItems * itemHeight);

  if (items.length <= maxVisibleItems) {
    return (
      <Box sx={{ overflow: 'visible' }}>
        {items.map((item, i) => (
          <Box key={i}>{renderItem(item, i)}</Box>
        ))}
      </Box>
    );
  }

  return (
    <Box>
      <List<RowProps<T>>
        rowCount={items.length}
        rowHeight={itemHeight}
        defaultHeight={height}
        rowComponent={Row<T>}
        rowProps={{ items, renderItem }}
        style={{ overflowX: 'hidden' }}
      />
      <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, textAlign: 'center', py: 0.5 }}>
        Showing {Math.min(items.length, maxVisibleItems)} of {items.length} items. Use search to filter.
      </Typography>
    </Box>
  );
}
