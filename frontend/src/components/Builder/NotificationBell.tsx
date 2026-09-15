import { useState } from "react";
import {
  Badge,
  Box,
  Button,
  Divider,
  IconButton,
  List,
  ListItem,
  ListItemText,
  Menu,
  Tooltip,
  Typography,
} from "@mui/material";
import NotificationsIcon from "@mui/icons-material/Notifications";
import NotificationsNoneIcon from "@mui/icons-material/NotificationsNone";
import { useBuilderStore } from "../../store/builderStore";
import { useT } from "../../i18n";
import { statusColor } from "../../theme/tokens";

/**
 * User-requested (2026-08-25): the global Snackbar toast (App.tsx) auto-hides
 * after 6s — a message the user was away for (mid Save/Deploy, a long-running
 * export) was gone for good. This bell keeps a rolling history of the exact
 * same setGlobalMessage stream Canvas/panels already use, so nothing new to
 * wire per-caller: every existing setGlobalMessage call is already covered.
 */
export default function NotificationBell() {
  const t = useT();
  const messageHistory = useBuilderStore((s) => s.messageHistory);
  const unreadMessageCount = useBuilderStore((s) => s.unreadMessageCount);
  const markMessagesRead = useBuilderStore((s) => s.markMessagesRead);
  const clearMessageHistory = useBuilderStore((s) => s.clearMessageHistory);
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);
  const open = Boolean(anchorEl);

  function handleOpen(e: React.MouseEvent<HTMLElement>) {
    setAnchorEl(e.currentTarget);
    markMessagesRead();
  }

  function handleClose() {
    setAnchorEl(null);
  }

  return (
    <>
      <Tooltip title={t("builder.notificationsTooltip")}>
        <IconButton
          size="small"
          onClick={handleOpen}
          aria-label={t("builder.notificationsTooltip")}
          data-testid="btn-notifications"
        >
          <Badge
            badgeContent={unreadMessageCount}
            max={9}
            color="error"
            overlap="circular"
          >
            {messageHistory.length > 0 ? (
              <NotificationsIcon fontSize="small" />
            ) : (
              <NotificationsNoneIcon fontSize="small" />
            )}
          </Badge>
        </IconButton>
      </Tooltip>
      <Menu
        anchorEl={anchorEl}
        open={open}
        onClose={handleClose}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
        transformOrigin={{ vertical: "top", horizontal: "right" }}
        slotProps={{ paper: { sx: { width: 360, maxHeight: 420 } } }}
      >
        <Box sx={{ px: 2, py: 1, display: "flex", alignItems: "center" }}>
          <Typography variant="subtitle2" fontWeight={700} sx={{ flex: 1 }}>
            {t("builder.notificationsTitle")}
          </Typography>
          {messageHistory.length > 0 && (
            <Button size="small" onClick={clearMessageHistory}>
              {t("builder.notificationsClear")}
            </Button>
          )}
        </Box>
        <Divider />
        {messageHistory.length === 0 ? (
          <Box sx={{ px: 2, py: 3, textAlign: "center" }}>
            <Typography variant="body2" color="text.secondary">
              {t("builder.notificationsEmpty")}
            </Typography>
          </Box>
        ) : (
          <List dense sx={{ overflowY: "auto", maxHeight: 340, py: 0 }}>
            {messageHistory.map((m) => {
              const { fg } = statusColor(m.severity);
              return (
                <ListItem key={m.id} divider sx={{ alignItems: "flex-start" }}>
                  <Box
                    sx={{
                      width: 8,
                      height: 8,
                      borderRadius: "50%",
                      bgcolor: fg,
                      mt: 0.75,
                      mr: 1.5,
                      flexShrink: 0,
                    }}
                  />
                  <ListItemText
                    primary={m.text}
                    secondary={new Date(m.at).toLocaleTimeString()}
                    primaryTypographyProps={{ variant: "body2" }}
                    secondaryTypographyProps={{ variant: "caption" }}
                  />
                </ListItem>
              );
            })}
          </List>
        )}
      </Menu>
    </>
  );
}
