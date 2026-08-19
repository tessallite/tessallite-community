import { useState } from "react";
import {
  IconButton,
  Tooltip,
  Menu,
  MenuItem,
  ListItemIcon,
  ListItemText,
  Divider,
  Typography,
} from "@mui/material";
import { Dataset, Check } from "@mui/icons-material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useChatContext } from "../providers/ChatProvider";
import { useConversationStore } from "../stores/conversationStore";

export function ModelPicker({
  onError,
}: {
  onError?: (msg: string) => void;
}) {
  const { adapter, t, projectId } = useChatContext();
  const activeConversationId = useConversationStore(
    (s) => s.activeConversationId,
  );
  const pendingModelId = useConversationStore((s) => s.pendingModelId);
  const setPendingModelId = useConversationStore((s) => s.setPendingModelId);
  const queryClient = useQueryClient();

  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null);

  const { data: models = [] } = useQuery({
    queryKey: ["selectable-models", projectId],
    queryFn: () => adapter.getSelectableModels(projectId),
    enabled: !!projectId,
  });

  const { data: conversation } = useQuery({
    queryKey: ["conversation", projectId, activeConversationId],
    queryFn: () =>
      adapter.getConversation(projectId, activeConversationId!),
    enabled: !!projectId && !!activeConversationId,
  });

  const rawModelId = activeConversationId
    ? (conversation?.pinned_model_id ?? null)
    : pendingModelId;
  const currentModelId = models.some((m) => m.id === rawModelId)
    ? rawModelId
    : null;

  async function handleSelect(modelId: string | null) {
    setAnchorEl(null);
    if (modelId === currentModelId) return;
    if (activeConversationId && projectId) {
      try {
        await adapter.updateConversation(
          projectId,
          activeConversationId,
          { pinned_model_id: modelId },
        );
        queryClient.invalidateQueries({
          queryKey: ["conversation", projectId, activeConversationId],
        });
      } catch {
        onError?.(t("modelPicker.updateFailed"));
      }
    } else {
      setPendingModelId(modelId);
    }
  }

  const hasPin = !!currentModelId;

  return (
    <>
      <Tooltip title={t("modelPicker.tooltip")}>
        <IconButton
          size="small"
          onClick={(e) => setAnchorEl(e.currentTarget)}
          aria-label={t("modelPicker.ariaLabel")}
          sx={{ color: hasPin ? "primary.main" : undefined }}
        >
          <Dataset fontSize="small" />
        </IconButton>
      </Tooltip>

      <Menu
        anchorEl={anchorEl}
        open={!!anchorEl}
        onClose={() => setAnchorEl(null)}
        transformOrigin={{ horizontal: "right", vertical: "top" }}
        anchorOrigin={{ horizontal: "right", vertical: "bottom" }}
      >
        <MenuItem onClick={() => handleSelect(null)}>
          <ListItemIcon>
            {!hasPin && <Check fontSize="small" color="primary" />}
          </ListItemIcon>
          <ListItemText
            primary={t("modelPicker.projectDefault")}
            secondary={t("modelPicker.projectDefaultHint")}
          />
        </MenuItem>

        <Divider />
        <MenuItem disabled sx={{ opacity: 1 }}>
          <Typography variant="caption" color="text.secondary">
            {t("modelPicker.sectionLabel")}
          </Typography>
        </MenuItem>

        {models.length === 0 ? (
          <MenuItem disabled>
            <ListItemText primary={t("modelPicker.none")} />
          </MenuItem>
        ) : (
          models.map((m) => (
            <MenuItem
              key={m.id}
              onClick={() => handleSelect(m.id)}
              selected={m.id === currentModelId}
            >
              <ListItemIcon>
                {m.id === currentModelId && (
                  <Check fontSize="small" color="primary" />
                )}
              </ListItemIcon>
              <ListItemText primary={m.name} />
            </MenuItem>
          ))
        )}
      </Menu>
    </>
  );
}
