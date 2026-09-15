import {
  useState,
  useCallback,
  useRef,
  type KeyboardEvent,
  type ClipboardEvent,
} from "react";
import { Box, IconButton, Typography, Tooltip, useTheme } from "@mui/material";
import { Send, Stop } from "@mui/icons-material";
import { useChatContext } from "../providers/ChatProvider";

export interface ChatComposerProps {
  onSend: (text: string) => void;
  onAbort?: () => void;
  isStreaming: boolean;
  disabled?: boolean;
  initialText?: string;
  maxChars?: number;
  placeholder?: string;
  compact?: boolean;
}

export function ChatComposer({
  onSend,
  onAbort,
  isStreaming,
  disabled,
  initialText = "",
  maxChars = 4000,
  placeholder: placeholderProp,
  compact = false,
}: ChatComposerProps) {
  const theme = useTheme();
  const { t } = useChatContext();
  const [text, setText] = useState(initialText);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const trimmed = text.trim();
  const isOverMax = text.length > maxChars;
  const showCounter = text.length > maxChars * 0.8;

  const placeholder = placeholderProp ?? t("composer.placeholder");

  const handleSend = useCallback(() => {
    if (!trimmed || isOverMax || isStreaming || disabled) return;
    onSend(trimmed);
    setText("");
    if (textareaRef.current) {
      textareaRef.current.style.height = compact ? "30px" : "48px";
    }
  }, [trimmed, isOverMax, isStreaming, disabled, onSend, compact]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      // Bug-7738 — do NOT send while an IME composition is active. Pressing
      // Enter to commit a Japanese/Chinese/Korean candidate must confirm the
      // composition, not submit the half-composed message. `isComposing` (and
      // the legacy keyCode 229) mark the keystroke as part of an in-progress
      // composition; browsers still fire keydown for the committing Enter.
      if (
        e.nativeEvent.isComposing ||
        (e.nativeEvent as unknown as { keyCode?: number }).keyCode === 229
      ) {
        return;
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const handlePaste = useCallback(
    (e: ClipboardEvent<HTMLTextAreaElement>) => {
      const textData = e.clipboardData.getData("text/plain");
      if (textData) {
        e.preventDefault();
        const el = e.currentTarget;
        const start = el.selectionStart;
        const end = el.selectionEnd;
        const before = el.value.slice(0, start);
        const after = el.value.slice(end);
        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
          HTMLTextAreaElement.prototype,
          "value",
        )?.set;
        nativeInputValueSetter?.call(el, before + textData + after);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        const cursor = start + textData.length;
        el.setSelectionRange(cursor, cursor);
      }
    },
    [],
  );

  const handleInput = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = compact ? "30px" : "48px";
    el.style.height = Math.min(el.scrollHeight, 144) + "px";
  }, [compact]);

  return (
    <Box
      sx={{
        px: compact ? 1.25 : 2,
        py: compact ? 0.75 : 1.5,
        borderTop: 1,
        borderColor: "divider",
        bgcolor: "background.paper",
        display: "flex",
        alignItems: "flex-end",
        gap: compact ? 0.75 : 1,
        flexShrink: compact ? 0 : undefined,
        maxWidth: compact ? "none" : 960,
        mx: compact ? 0 : "auto",
        width: "100%",
      }}
    >
      <Box sx={{ flex: 1, position: "relative" }}>
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onInput={handleInput}
          placeholder={placeholder}
          rows={compact ? 1 : undefined}
          disabled={disabled || isStreaming}
          aria-label={t("composer.messageInputAria")}
          style={{
            width: "100%",
            minHeight: compact ? 30 : 48,
            maxHeight: 144,
            padding: compact ? "6px 8px" : "12px 14px",
            borderRadius: compact ? 2 : 12,
            border: "1px solid",
            borderColor: isOverMax
              ? theme.palette.error.main
              : theme.palette.divider,
            resize: "none",
            fontFamily: "inherit",
            fontSize: compact ? 12 : 15,
            lineHeight: compact ? 1.4 : 1.5,
            outline: "none",
            background: "transparent",
            color: "inherit",
            overflow: "auto",
          }}
        />
        {showCounter && (
          <Typography
            variant="caption"
            color={isOverMax ? "error" : "text.secondary"}
            sx={{ position: "absolute", bottom: -16, right: 4 }}
          >
            {text.length}/{maxChars}
          </Typography>
        )}
        {isOverMax && (
          <Typography
            variant="caption"
            color="error"
            sx={{ mt: 0.5, display: "block" }}
          >
            {t("composer.tooLong", { max: String(maxChars) })}
          </Typography>
        )}
      </Box>

      {isStreaming ? (
        <Tooltip title={t("composer.stopAria")}>
          <IconButton
            onClick={onAbort}
            aria-label={t("composer.stopAria")}
            color="error"
            sx={{
              bgcolor: "error.main",
              color: "white",
              "&:hover": { bgcolor: "error.dark" },
              width: compact ? 30 : 40,
              height: compact ? 30 : 40,
              borderRadius: compact ? 0.5 : 2,
            }}
          >
            <Stop fontSize="small" sx={{ fontSize: compact ? 14 : undefined }} />
          </IconButton>
        </Tooltip>
      ) : (
        <Tooltip title={t("composer.sendAria")}>
          <span>
            <IconButton
              onClick={handleSend}
              aria-label={t("composer.sendAria")}
              disabled={!trimmed || isOverMax || disabled}
              color="primary"
              sx={{
                bgcolor: "primary.main",
                color: "white",
                "&:hover": { bgcolor: "primary.dark" },
                "&:disabled": {
                  bgcolor: "action.disabledBackground",
                  color: "action.disabled",
                },
                width: compact ? 30 : 40,
                height: compact ? 30 : 40,
                borderRadius: compact ? 0.5 : 2,
              }}
            >
              <Send fontSize="small" sx={{ fontSize: compact ? 15 : undefined }} />
            </IconButton>
          </span>
        </Tooltip>
      )}
    </Box>
  );
}
