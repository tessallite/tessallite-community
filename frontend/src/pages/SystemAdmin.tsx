import { useEffect, useMemo, useState } from "react";
import { safeLocalGet } from "../utils/safeLocalStorage";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  IconButton,
  InputAdornment,
  InputLabel,
  List,
  ListItemButton,
  ListItemText,
  MenuItem,
  Select,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import EffectiveAccessPreview from "../components/Settings/EffectiveAccessPreview";
import { useConfirm } from "../components/Confirm";
import { useT } from "../i18n";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/DeleteOutline";
import EditIcon from "@mui/icons-material/EditOutlined";
import LockResetIcon from "@mui/icons-material/LockReset";
import SearchIcon from "@mui/icons-material/Search";
import { adminApi, authApi, tenantsApi } from "../api/client";
import type { LocalUserRole, Tenant, User } from "../api/types";
import HelpIconButton from "../components/HelpIconButton";

const USER_ROLES: LocalUserRole[] = ["member", "tenant_admin", "model_technical"];

type TenantDialog =
  | { kind: "new" }
  | { kind: "edit"; tenant: Tenant }
  | null;

type UserDialog =
  | { kind: "new" }
  | { kind: "edit"; user: User }
  | { kind: "reset-password"; user: User }
  | null;

export default function SystemAdmin() {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();

  const isSystemAdmin =
    typeof window !== "undefined" &&
    safeLocalGet("user_role", "") === "system_admin";

  const [selectedTenantSlug, setSelectedTenantSlug] = useState("");
  const [tenantFilter, setTenantFilter] = useState("");
  const [userFilter, setUserFilter] = useState("");
  const [tenantDialog, setTenantDialog] = useState<TenantDialog>(null);
  const [userDialog, setUserDialog] = useState<UserDialog>(null);

  // Only system admins can list tenants — firing the query for other roles
  // produces a 403 and the misleading "Failed to load tenants" banner.
  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: tenantsApi.list,
    enabled: isSystemAdmin,
  });
  const users = useQuery({
    queryKey: ["tenant-users", selectedTenantSlug],
    queryFn: () => authApi.listTenantUsers(selectedTenantSlug),
    enabled: isSystemAdmin && Boolean(selectedTenantSlug),
  });

  const filteredTenants = useMemo(() => {
    const q = tenantFilter.trim().toLowerCase();
    const list = tenants.data ?? [];
    if (!q) return list;
    return list.filter(
      (t) =>
        t.slug.toLowerCase().includes(q) ||
        t.display_name.toLowerCase().includes(q),
    );
  }, [tenants.data, tenantFilter]);

  const filteredUsers = useMemo(() => {
    const q = userFilter.trim().toLowerCase();
    const list = users.data ?? [];
    if (!q) return list;
    return list.filter(
      (u) =>
        u.email.toLowerCase().includes(q) ||
        u.username.toLowerCase().includes(q) ||
        u.role.toLowerCase().includes(q),
    );
  }, [users.data, userFilter]);

  const deleteTenant = useMutation({
    mutationFn: (slug: string) => tenantsApi.delete(slug),
    onSuccess: (_data, slug) => {
      qc.invalidateQueries({ queryKey: ["tenants"] });
      if (selectedTenantSlug === slug) setSelectedTenantSlug("");
    },
  });

  const toggleUserActive = useMutation({
    mutationFn: ({ userId, isActive }: { userId: string; isActive: boolean }) =>
      authApi.updateTenantUser(selectedTenantSlug, userId, { is_active: isActive }),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["tenant-users", selectedTenantSlug] }),
  });

  const deleteUser = useMutation({
    mutationFn: (userId: string) =>
      authApi.deleteTenantUser(selectedTenantSlug, userId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["tenant-users", selectedTenantSlug] }),
  });

  if (!isSystemAdmin) {
    return (
      <Box sx={{ p: 3 }}>
        <Alert severity="warning">
          {t("systemAdmin.accessDenied")}
        </Alert>
      </Box>
    );
  }

  return (
    <Box
      sx={{
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minHeight: 0,
        bgcolor: "background.default",
      }}
    >
      <Box sx={{ display: "flex", flex: 1, minHeight: 0 }}>
        <TenantPanel
          tenants={filteredTenants}
          loading={tenants.isLoading}
          error={tenants.error as unknown}
          selectedSlug={selectedTenantSlug}
          filter={tenantFilter}
          setFilter={setTenantFilter}
          onSelect={setSelectedTenantSlug}
          onCreate={() => setTenantDialog({ kind: "new" })}
          onEdit={(tenant) => setTenantDialog({ kind: "edit", tenant })}
          onDelete={async (tenant) => {
            const ok = await confirm({
              mode: "typed-name",
              title: t("systemAdmin.deleteTenantTitle"),
              message: t("systemAdmin.deleteTenantMessage"),
              confirmText: tenant.slug,
              confirmLabel: t("systemAdmin.deleteTenantConfirmLabel"),
            });
            if (ok) deleteTenant.mutate(tenant.slug);
          }}
        />

        <Divider orientation="vertical" flexItem />

        <UserPanel
          tenantSelected={Boolean(selectedTenantSlug)}
          tenantSlug={selectedTenantSlug}
          users={filteredUsers}
          loading={users.isLoading}
          error={users.error as unknown}
          filter={userFilter}
          setFilter={setUserFilter}
          onCreate={() => setUserDialog({ kind: "new" })}
          onEdit={(u) => setUserDialog({ kind: "edit", user: u })}
          onResetPassword={(u) => setUserDialog({ kind: "reset-password", user: u })}
          onDelete={async (u) => {
            const ok = await confirm({
              mode: "typed-name",
              title: t("systemAdmin.deleteUserTitle"),
              message: t("systemAdmin.deleteUserMessage"),
              confirmText: u.email,
              confirmLabel: t("systemAdmin.deleteUserConfirmLabel"),
            });
            if (ok) deleteUser.mutate(u.id);
          }}
          onToggleActive={(u, active) =>
            toggleUserActive.mutate({ userId: u.id, isActive: active })
          }
        />
      </Box>

      <TenantDialogForm
        dialog={tenantDialog}
        onClose={() => setTenantDialog(null)}
        onSaved={() => {
          setTenantDialog(null);
          qc.invalidateQueries({ queryKey: ["tenants"] });
        }}
      />

      <UserDialogForm
        dialog={userDialog}
        tenantSlug={selectedTenantSlug}
        onClose={() => setUserDialog(null)}
        onSaved={() => {
          setUserDialog(null);
          setUserFilter("");
          qc.invalidateQueries({ queryKey: ["tenant-users", selectedTenantSlug] });
        }}
      />
    </Box>
  );
}

function tenantsErrorMessage(error: unknown, t: ReturnType<typeof useT>): string {
  const status = (error as { response?: { status?: number } })?.response?.status;
  if (status === 401) return t("systemAdmin.sessionExpired");
  if (status === 403) return t("systemAdmin.accessDenied");
  const detail = (error as { response?: { data?: { detail?: string } } })
    ?.response?.data?.detail;
  return detail ?? t("systemAdmin.loadTenantsFailed");
}

// ---------------------------------------------------------------------------
// Left pane — tenants
// ---------------------------------------------------------------------------

function TenantPanel({
  tenants,
  loading,
  error,
  selectedSlug,
  filter,
  setFilter,
  onSelect,
  onCreate,
  onEdit,
  onDelete,
}: {
  tenants: Tenant[];
  loading: boolean;
  error: unknown;
  selectedSlug: string;
  filter: string;
  setFilter: (v: string) => void;
  onSelect: (slug: string) => void;
  onCreate: () => void;
  onEdit: (t: Tenant) => void;
  onDelete: (t: Tenant) => void;
}) {
  const t = useT();
  return (
    <Box sx={{ width: 340, display: "flex", flexDirection: "column", minHeight: 0 }}>
      <Box
        sx={{
          px: 1.5,
          py: 1.25,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          borderBottom: 1,
          borderColor: "divider",
          bgcolor: "grey.50",
        }}
      >
        <Typography variant="subtitle2" fontWeight={700}>
          {t("systemAdmin.tenantsHeading")}
        </Typography>
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
          <HelpIconButton href="/help/admin/create-a-workspace.html" />
          <Button
            size="small"
            variant="contained"
            color="primary"
            startIcon={<AddIcon fontSize="small" />}
            onClick={onCreate}
          >
            {t("systemAdmin.addTenantButton")}
          </Button>
        </Box>
      </Box>
      <Box sx={{ px: 1.5, pb: 1 }}>
        <TextField
          fullWidth
          size="small"
          placeholder={t("systemAdmin.searchTenantsPlaceholder")}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon sx={{ fontSize: 16, color: "text.secondary" }} />
              </InputAdornment>
            ),
          }}
        />
      </Box>
      <Divider />
      <Box sx={{ flex: 1, overflow: "auto", minHeight: 0 }}>
        {loading && (
          <Box sx={{ p: 2 }}>
            <CircularProgress size={18} />
          </Box>
        )}
        {Boolean(error) && (
          <Alert severity="error" sx={{ m: 1 }}>
            {tenantsErrorMessage(error, t)}
          </Alert>
        )}
        {!loading && tenants.length === 0 && !error && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", p: 2 }}>
            {filter ? t("systemAdmin.noTenantsMatch") : t("systemAdmin.noTenantsYet")}
          </Typography>
        )}
        <List dense disablePadding>
          {tenants.map((tenant) => (
            <ListItemButton
              key={tenant.id}
              selected={tenant.slug === selectedSlug}
              onClick={() => onSelect(tenant.slug)}
              sx={{
                py: 0.5,
              }}
            >
              <ListItemText
                primary={tenant.display_name}
                secondary={`${tenant.slug}${tenant.is_active ? "" : t("systemAdmin.disabledSuffix")}`}
                primaryTypographyProps={{
                  variant: "body2",
                  noWrap: true,
                  sx: { fontWeight: 700 },
                }}
                secondaryTypographyProps={{ variant: "caption" }}
              />
              <Box className="row-actions" sx={{ display: "flex", gap: 0.25 }}>
                <Tooltip title={t("systemAdmin.editTenantTooltip")}>
                  <IconButton
                    size="small"
                    onClick={(e) => {
                      e.stopPropagation();
                      onEdit(tenant);
                    }}
                  >
                    <EditIcon sx={{ fontSize: 16 }} />
                  </IconButton>
                </Tooltip>
                <Tooltip title={t("systemAdmin.deleteTenantTooltip")}>
                  <IconButton
                    size="small"
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(tenant);
                    }}
                  >
                    <DeleteIcon sx={{ fontSize: 16 }} />
                  </IconButton>
                </Tooltip>
              </Box>
            </ListItemButton>
          ))}
        </List>
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Right pane — users
// ---------------------------------------------------------------------------

function UserPanel({
  tenantSelected,
  tenantSlug,
  users,
  loading,
  error,
  filter,
  setFilter,
  onCreate,
  onEdit,
  onResetPassword,
  onDelete,
  onToggleActive,
}: {
  tenantSelected: boolean;
  tenantSlug: string;
  users: User[];
  loading: boolean;
  error: unknown;
  filter: string;
  setFilter: (v: string) => void;
  onCreate: () => void;
  onEdit: (u: User) => void;
  onResetPassword: (u: User) => void;
  onDelete: (u: User) => void;
  onToggleActive: (u: User, active: boolean) => void;
}) {
  const t = useT();
  return (
    <Box sx={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0, minWidth: 0 }}>
      <Box
        sx={{
          px: 1.5,
          py: 1.25,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          borderBottom: 1,
          borderColor: "divider",
          bgcolor: "grey.50",
        }}
      >
        <Typography variant="subtitle2" fontWeight={700}>
          {t("systemAdmin.usersHeading")} {tenantSelected ? `— ${tenantSlug}` : ""}
        </Typography>
        <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
          <HelpIconButton href="/help/admin/manage-users.html" />
          <Tooltip title={tenantSelected ? t("systemAdmin.newUser") : t("systemAdmin.selectTenantFirst")}>
            <span>
              <Button
                size="small"
                variant="contained"
                color="primary"
                startIcon={<AddIcon fontSize="small" />}
                disabled={!tenantSelected}
                onClick={onCreate}
              >
                {t("systemAdmin.addUser")}
              </Button>
            </span>
          </Tooltip>
        </Box>
      </Box>
      <Box sx={{ px: 1.5, pb: 1 }}>
        <TextField
          fullWidth
          size="small"
          placeholder={tenantSelected ? t("systemAdmin.searchUsers") : t("systemAdmin.selectTenantToSearch")}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          disabled={!tenantSelected}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon sx={{ fontSize: 16, color: "text.secondary" }} />
              </InputAdornment>
            ),
          }}
        />
      </Box>
      <Divider />
      <Box sx={{ flex: 1, overflow: "auto", minHeight: 0 }}>
        {!tenantSelected && (
          <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
            {t("systemAdmin.selectFromLeft")}
          </Typography>
        )}
        {tenantSelected && loading && (
          <Box sx={{ p: 2 }}>
            <CircularProgress size={18} />
          </Box>
        )}
        {tenantSelected && Boolean(error) && (
          <Alert severity="error" sx={{ m: 1 }}>
            {t("systemAdmin.loadUsersFailed", { slug: tenantSlug })}
          </Alert>
        )}
        {tenantSelected && !loading && !error && users.length === 0 && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", p: 2 }}>
            {filter ? t("systemAdmin.noUsersMatch") : t("systemAdmin.noUsersYet", { slug: tenantSlug })}
          </Typography>
        )}
        <List dense disablePadding>
          {users.map((u) => (
            <Box
              key={u.id}
              sx={{
                px: 1.5,
                py: 0.75,
                display: "flex",
                alignItems: "center",
                gap: 1,
                borderBottom: 1,
                borderColor: "divider",
              }}
            >
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Typography variant="body2" noWrap sx={{ fontWeight: 700 }}>
                  {u.username}
                </Typography>
                <Typography variant="caption" color="text.secondary" noWrap>
                  {u.email} - {t(`roles.${u.role}`)}
                </Typography>
              </Box>
              <Switch
                size="small"
                checked={u.is_active}
                onChange={(_e, checked) => onToggleActive(u, checked)}
              />
              <Box className="row-actions" sx={{ display: "flex", gap: 0.25 }}>
                <Tooltip title={t("systemAdmin.editUserTooltip")}>
                  <IconButton size="small" onClick={() => onEdit(u)}>
                    <EditIcon sx={{ fontSize: 16 }} />
                  </IconButton>
                </Tooltip>
                <Tooltip title={t("systemAdmin.resetPasswordTooltip")}>
                  <IconButton size="small" onClick={() => onResetPassword(u)}>
                    <LockResetIcon sx={{ fontSize: 16 }} />
                  </IconButton>
                </Tooltip>
                <Tooltip title={t("systemAdmin.deleteUserTooltip")}>
                  <IconButton size="small" onClick={() => onDelete(u)}>
                    <DeleteIcon sx={{ fontSize: 16 }} />
                  </IconButton>
                </Tooltip>
              </Box>
            </Box>
          ))}
        </List>
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Tenant create/edit dialog
// ---------------------------------------------------------------------------

function TenantDialogForm({
  dialog,
  onClose,
  onSaved,
}: {
  dialog: TenantDialog;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useT();
  const [slug, setSlug] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [isActive, setIsActive] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    if (dialog?.kind === "edit") {
      setSlug(dialog.tenant.slug);
      setDisplayName(dialog.tenant.display_name);
      setIsActive(dialog.tenant.is_active);
    } else if (dialog?.kind === "new") {
      setSlug("");
      setDisplayName("");
      setIsActive(true);
    }
  }, [dialog]);

  const save = useMutation({
    mutationFn: async () => {
      if (dialog?.kind === "new") {
        await tenantsApi.create({ slug, display_name: displayName });
        await adminApi.migrateTenant(slug);
      } else if (dialog?.kind === "edit") {
        await tenantsApi.update(dialog.tenant.slug, {
          display_name: displayName,
          is_active: isActive,
        });
      }
    },
    onSuccess: onSaved,
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      const message = err instanceof Error ? err.message : t("systemAdmin.saveFailed");
      setError(detail ?? message);
    },
  });

  const isEdit = dialog?.kind === "edit";
  const isValid =
    dialog?.kind === "edit"
      ? displayName.trim() !== ""
      : slug.trim() !== "" && displayName.trim() !== "";

  return (
    <Dialog open={dialog !== null} onClose={onClose} maxWidth="xs" fullWidth>
      <DialogTitle>{isEdit ? t("systemAdmin.editTenant") : t("systemAdmin.newTenant")}</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
          {isEdit
            ? t("systemAdmin.slugImmutable")
            : t("systemAdmin.createTenantInfo")}
        </Typography>
        <TextField
          label={t("systemAdmin.slugLabel")}
          fullWidth
          margin="normal"
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
          autoFocus={!isEdit}
          disabled={isEdit}
          helperText={isEdit ? t("systemAdmin.slugCantChange") : t("systemAdmin.lowercaseNoSpaces")}
        />
        <TextField
          label={t("systemAdmin.displayNameLabel")}
          fullWidth
          margin="normal"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          autoFocus={isEdit}
        />
        {isEdit && (
          <Stack direction="row" alignItems="center" spacing={1} sx={{ mt: 1 }}>
            <Switch
              size="small"
              checked={isActive}
              onChange={(_e, checked) => setIsActive(checked)}
            />
            <Typography variant="body2">{isActive ? t("systemAdmin.activeLabel") : t("systemAdmin.disabledLabel")}</Typography>
          </Stack>
        )}
        {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("systemAdmin.cancelButton")}</Button>
        <Button
          variant="contained"
          disabled={!isValid || save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? <CircularProgress size={16} /> : isEdit ? t("systemAdmin.saveButton") : t("systemAdmin.createButton")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// User create/edit/reset-password dialog
// ---------------------------------------------------------------------------

function UserDialogForm({
  dialog,
  tenantSlug,
  onClose,
  onSaved,
}: {
  dialog: UserDialog;
  tenantSlug: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<LocalUserRole>("member");
  const [error, setError] = useState<string | null>(null);
  const t = useT();

  useEffect(() => {
    setError(null);
    if (dialog?.kind === "edit") {
      setUsername(dialog.user.username);
      setEmail(dialog.user.email);
      setRole(dialog.user.role);
      setPassword("");
    } else if (dialog?.kind === "new") {
      setUsername("admin");
      setEmail("");
      setPassword("");
      setRole("member");
    } else if (dialog?.kind === "reset-password") {
      setPassword("");
    }
  }, [dialog]);

  const save = useMutation({
    mutationFn: async () => {
      if (dialog?.kind === "new") {
        await authApi.createTenantUser(tenantSlug, {
          username,
          email,
          password,
          role,
        });
      } else if (dialog?.kind === "edit") {
        await authApi.updateTenantUser(tenantSlug, dialog.user.id, {
          username,
          email,
          role,
        });
      } else if (dialog?.kind === "reset-password") {
        await authApi.resetTenantUserPassword(tenantSlug, dialog.user.id, {
          password,
        });
      }
    },
    onSuccess: onSaved,
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      const message = err instanceof Error ? err.message : t("systemAdmin.saveFailed");
      setError(detail ?? message);
    },
  });

  const isEdit = dialog?.kind === "edit";
  const isReset = dialog?.kind === "reset-password";
  const title = isEdit
    ? t("systemAdmin.editUser")
    : isReset
      ? t("systemAdmin.resetPassword")
      : t("systemAdmin.newUser");

  const isValid = (() => {
    if (isReset) return password.length > 0;
    if (isEdit) return username.trim() !== "" && email.trim() !== "";
    return username.trim() !== "" && email.trim() !== "" && password.length > 0;
  })();

  return (
    <Dialog open={dialog !== null} onClose={onClose} maxWidth="xs" fullWidth>
      <DialogTitle>{title}</DialogTitle>
      <DialogContent>
        {isReset ? (
          <>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              {t("systemAdmin.resetPasswordFor", { email: dialog?.kind === "reset-password" ? dialog.user.email : "" })}
            </Typography>
            <TextField
              label={t("systemAdmin.newPasswordLabel")}
              type="password"
              fullWidth
              margin="normal"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoFocus
            />
          </>
        ) : (
          <>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              {isEdit
                ? t("systemAdmin.editUserIn", { email: dialog?.kind === "edit" ? dialog.user.email : "", slug: tenantSlug })
                : t("systemAdmin.addUserTo", { slug: tenantSlug })}
            </Typography>
            <TextField
              label={t("systemAdmin.usernameLabel")}
              fullWidth
              margin="normal"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
            />
            <TextField
              label={t("systemAdmin.emailLabel")}
              type="email"
              fullWidth
              margin="normal"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            {!isEdit && (
              <TextField
                label={t("systemAdmin.passwordLabel")}
                type="password"
                fullWidth
                margin="normal"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            )}
            <FormControl fullWidth size="small" margin="normal">
              <InputLabel>{t("systemAdmin.roleLabel")}</InputLabel>
              <Select
                label={t("systemAdmin.roleLabel")}
                value={role}
                onChange={(e) => setRole(e.target.value as LocalUserRole)}
              >
                {USER_ROLES.map((r) => (
                  <MenuItem key={r} value={r}>{t(`roles.${r}`)}</MenuItem>
                ))}
              </Select>
            </FormControl>
            <EffectiveAccessPreview role={role} />
          </>
        )}
        {error && <Alert severity="error" sx={{ mt: 1 }}>{error}</Alert>}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("systemAdmin.cancelButton")}</Button>
        <Button
          variant="contained"
          disabled={!isValid || save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? (
            <CircularProgress size={16} />
          ) : isEdit ? (
            t("systemAdmin.saveButton")
          ) : isReset ? (
            t("systemAdmin.resetButton")
          ) : (
            t("systemAdmin.createButton")
          )}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
