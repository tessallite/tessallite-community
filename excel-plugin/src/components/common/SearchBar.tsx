import { useRef, useEffect, useState } from 'react';
import { TextField, InputAdornment, IconButton } from '@mui/material';
import { SearchOutlined, CloseOutlined } from '@mui/icons-material';
import { tokens } from '../../theme';

interface SearchBarProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
}

export default function SearchBar({
  value,
  onChange,
  placeholder = 'Search...',
  disabled,
}: SearchBarProps) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [localValue, setLocalValue] = useState(value);

  useEffect(() => {
    setLocalValue(value);
  }, [value]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const handleChange = (raw: string) => {
    setLocalValue(raw);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      onChange(raw);
    }, 300);
  };

  return (
    <TextField
      fullWidth
      size="small"
      placeholder={placeholder}
      value={localValue}
      onChange={e => handleChange(e.target.value)}
      disabled={disabled}
      InputProps={{
        startAdornment: (
          <InputAdornment position="start">
            <SearchOutlined sx={{ fontSize: 18, color: tokens.colorTextSecondary }} />
          </InputAdornment>
        ),
        endAdornment: localValue ? (
          <InputAdornment position="end">
            <IconButton
              size="small"
              aria-label="Clear search"
              onClick={() => {
                if (timerRef.current) clearTimeout(timerRef.current);
                setLocalValue('');
                onChange('');
              }}
            >
              <CloseOutlined sx={{ fontSize: 16 }} />
            </IconButton>
          </InputAdornment>
        ) : null,
        sx: { fontSize: 13 },
      }}
    />
  );
}
