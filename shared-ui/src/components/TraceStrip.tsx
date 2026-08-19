import { useEffect, useRef, useState } from "react";
import {
  Box,
  Collapse,
  IconButton,
  Stack,
  Typography,
} from "@mui/material";
import { ExpandMore, ExpandLess } from "@mui/icons-material";
import type { TurnResponse } from "../types/turn";
import { useChatContext } from "../providers/ChatProvider";
import { formatThoughtText } from "../utils/thoughtText";

function ThoughtBox({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const formatted = formatThoughtText(text);
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [formatted]);
  return (
    <Box
      ref={ref}
      sx={{
        height: "5.5em",
        overflowY: "auto",
        fontSize: "0.8125rem",
        lineHeight: 1.5,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
        color: "text.secondary",
        "&::-webkit-scrollbar": { width: 3 },
        "&::-webkit-scrollbar-thumb": { bgcolor: "divider", borderRadius: 2 },
      }}
    >
      {formatted}
    </Box>
  );
}

export interface TraceVisibility {
  showThoughtProcess: boolean;
  showSemanticQuery: boolean;
  showPhysicalQuery: boolean;
}

function Section({
  title,
  body,
  defaultOpen = false,
}: {
  title: string;
  body: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Box sx={{ borderTop: 1, borderColor: "divider", py: 0.5 }}>
      <Stack direction="row" alignItems="center" spacing={0.5}>
        <IconButton size="small" onClick={() => setOpen((v) => !v)}>
          {open ? (
            <ExpandLess fontSize="small" />
          ) : (
            <ExpandMore fontSize="small" />
          )}
        </IconButton>
        <Typography variant="caption" color="text.secondary">
          {title}
        </Typography>
      </Stack>
      <Collapse in={open} timeout="auto" unmountOnExit>
        <Box sx={{ pl: 4, pr: 1, pb: 1 }}>{body}</Box>
      </Collapse>
    </Box>
  );
}

export function TraceStrip({
  turn,
  visibility,
}: {
  turn: TurnResponse;
  visibility: TraceVisibility;
}) {
  const { t } = useChatContext();
  const sections: React.ReactNode[] = [];

  if (visibility.showThoughtProcess && turn.thought_summary) {
    sections.push(
      <Section
        key="thought"
        title={t("trace.howIThought")}
        body={<ThoughtBox text={turn.thought_summary} />}
      />,
    );
  }
  if (visibility.showSemanticQuery && turn.semantic_query) {
    sections.push(
      <Section
        key="semantic"
        title={t("trace.semanticQuery")}
        body={
          <Box
            component="pre"
            sx={{ fontSize: 11, m: 0, whiteSpace: "pre-wrap" }}
          >
            {JSON.stringify(turn.semantic_query, null, 2)}
          </Box>
        }
      />,
    );
  }
  if (visibility.showPhysicalQuery && turn.routed_sql) {
    sections.push(
      <Section
        key="physical"
        title={
          turn.route
            ? t("trace.physicalQueryRoute", { route: turn.route })
            : t("trace.physicalQuery")
        }
        body={
          <Box
            component="pre"
            sx={{ fontSize: 11, m: 0, whiteSpace: "pre-wrap" }}
          >
            {turn.routed_sql}
          </Box>
        }
      />,
    );
  }

  if (sections.length === 0) return null;
  return <Box sx={{ mt: 1 }}>{sections}</Box>;
}
