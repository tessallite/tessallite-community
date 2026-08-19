import {
  Box,
  Chip,
  Divider,
  Drawer,
  IconButton,
  Stack,
  Typography,
} from "@mui/material";
import { Close } from "@mui/icons-material";
import type { TurnResponse } from "../types/turn";
import type { TraceVisibility } from "./TraceStrip";
import { useChatContext } from "../providers/ChatProvider";
import { formatThoughtText } from "../utils/thoughtText";

export function TraceDrawer({
  open,
  onClose,
  turn,
  visibility,
}: {
  open: boolean;
  onClose: () => void;
  turn: TurnResponse | null;
  visibility: TraceVisibility;
}) {
  const { t } = useChatContext();
  if (!turn) return null;

  const sq = turn.semantic_query as
    | { executed_sql?: string; measures?: string[]; dimensions?: string[] }
    | null;

  return (
    <Drawer anchor="right" open={open} onClose={onClose}>
      <Box sx={{ width: 480, p: 2 }}>
        <Stack
          direction="row"
          alignItems="center"
          justifyContent="space-between"
          sx={{ mb: 1 }}
        >
          <Typography variant="subtitle1">
            {t("trace.answerTrace")}
          </Typography>
          <IconButton onClick={onClose} size="small">
            <Close fontSize="small" />
          </IconButton>
        </Stack>
        <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
          {turn.route && (
            <Chip
              size="small"
              label={t("trace.routeWithValue", { route: turn.route })}
              color={turn.route === "source" ? "warning" : "success"}
            />
          )}
          {turn.latency_ms != null && (
            <Chip
              size="small"
              label={t("trace.latencyMs", { ms: String(turn.latency_ms) })}
            />
          )}
          {typeof (turn.llm_plan as Record<string, unknown>)?.["tool"] ===
            "string" && (
            <Chip
              size="small"
              label={t("trace.toolWithValue", {
                tool: String(
                  (turn.llm_plan as Record<string, unknown>)["tool"],
                ),
              })}
            />
          )}
        </Stack>

        {visibility.showThoughtProcess && turn.thought_summary && (
          <>
            <Typography variant="overline">
              {t("trace.howIThought")}
            </Typography>
            <Typography
              variant="body2"
              sx={{ whiteSpace: "pre-wrap", mb: 2 }}
            >
              {formatThoughtText(turn.thought_summary)}
            </Typography>
            <Divider sx={{ mb: 2 }} />
          </>
        )}

        {visibility.showSemanticQuery && sq && (
          <>
            <Typography variant="overline">
              {t("trace.semanticQuery")}
            </Typography>
            <Box
              component="pre"
              sx={{
                bgcolor: "background.default",
                border: 1,
                borderColor: "divider",
                p: 1,
                fontSize: 12,
                overflowX: "auto",
                whiteSpace: "pre-wrap",
                mb: 2,
              }}
            >
              {JSON.stringify(turn.semantic_query, null, 2)}
            </Box>
            <Divider sx={{ mb: 2 }} />
          </>
        )}

        {visibility.showPhysicalQuery && turn.routed_sql && (
          <>
            <Typography variant="overline">
              {t("trace.physicalQuery")}
            </Typography>
            <Box
              component="pre"
              sx={{
                bgcolor: "background.default",
                border: 1,
                borderColor: "divider",
                p: 1,
                fontSize: 12,
                overflowX: "auto",
                whiteSpace: "pre-wrap",
              }}
            >
              {turn.routed_sql}
            </Box>
          </>
        )}
      </Box>
    </Drawer>
  );
}
