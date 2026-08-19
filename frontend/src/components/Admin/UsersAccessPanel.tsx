import { useEffect, useMemo, useState } from "react";
import { safeLocalGet } from "../../utils/safeLocalStorage";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  Drawer,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/DeleteOutline";
import EditIcon from "@mui/icons-material/EditOutlined";
import KeyIcon from "@mui/icons-material/VpnKeyOutlined";
import LockResetIcon from "@mui/icons-material/LockReset";
import { useConfirm } from "../Confirm";
import { useT } from "../../i18n";
import EffectiveAccessPreview from "../Settings/EffectiveAccessPreview";
import { accessApi, authApi, modelsApi } from "../../api/client";
import { grantAccessWithSupersede } from "./grantAccessWithSupersede";
import { meetsPasswordPolicy, showsPasswordPolicyError } from "../../auth/passwordPolicy";
import type {
  AccessRole,
  LocalUserRole,
  Model,
  RoleSource,
  User,
  UserAccessBinding,
} from "../../api/types";

const ACCESS_ROLES: AccessRole[] = ["admin", "modeler", "viewer", "model_viewer"];
const USER_ROLES: LocalUserRole[] = ["member", "tenant_admin", "model_technical"];

type DrawerKind =
  | { kind: "user"; user?: User }
  | { kind: "grant" }
  | { kind: "reset-password"; user: User }
  | null;

function roleKey(role: string): string {
  return `users.role${role.charAt(0).toUpperCase() + role.slice(1).replace(/_([a-z])/g, (_, c: string) => c.toUpperCase())}`;
}

// Bug-6642: show the provenance of a user's role so an operator can distinguish
// an SSO-elevated admin (auto-revocable via group mapping) from a manually-set
// one. `role_source` is optional on the API type; treat anything other than the
// explicit "sso" value as manual.
function RoleSourceBadge({ source }: { source?: RoleSource }) {
  const t = useT();
  const isSso = source === "sso";
  const tooltip = isSso
    ? t("users.roleSourceSsoTooltip")
    : t("users.roleSourceManualTooltip");
  return (
    <Tooltip title={tooltip}>
      <Chip
        size="small"
        variant="outlined"
        color={isSso ? "info" : "default"}
        label={isSso ? t("users.roleSourceSso") : t("users.roleSourceManual")}
      />
    </Tooltip>
  );
}

export default function UsersAccessPanel({
  projectId,
  projectName,
}: {
  projectId: string;
  projectName: string;
}) {
  const t = useT();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const tenantId = safeLocalGet("tenant_id", "");

  const [drawer, setDrawer] = useState<DrawerKind>(null);
  const [error, setError] = useState<string | null>(null);

  const users = useQuery({
    queryKey: ["tenant-users"],
    queryFn: () => authApi.listTenantUsers(tenantId),
  });

  const access = useQuery({
    queryKey: ["access", projectId],
    queryFn: () => accessApi.list(projectId),
    enabled: Boolean(projectId),
  });

  const models = useQuery({
    queryKey: ["models", projectId],
    queryFn: () => modelsApi.list(projectId),
    enabled: Boolean(projectId),
  });

  const usersById = useMemo(() => {
    const map = new Map<string, User>();
    (users.data ?? []).forEach((u) => map.set(u.email, u));
    return map;
  }, [users.data]);

  function close() {
    setDrawer(null);
    setError(null);
  }

  function refreshAll() {
    qc.invalidateQueries({ queryKey: ["tenant-users"] });
    qc.invalidateQueries({ queryKey: ["access", projectId] });
  }

  return (
    <Stack spacing={2}>
      <UsersTable
        users={users.data ?? []}
        loading={users.isLoading}
        onCreateUser={() => setDrawer({ kind: "user" })}
        onEditUser={(u) => setDrawer({ kind: "user", user: u })}
        onResetPassword={(u) => setDrawer({ kind: "reset-password", user: u })}
        onDeleteUser={async (u) => {
          const ok = await confirm({
            mode: "typed-name",
            title: t("users.deleteUserConfirm"),
            message: t("users.deleteUserMessage"),
            confirmText: u.email,
            confirmLabel: t("users.deleteUserLabel"),
          });
          if (!ok) return;
          await authApi.deleteTenantUser(tenantId, u.id);
          refreshAll();
        }}
      />

      <AccessTable
        projectName={projectName}
        access={access.data ?? []}
        loading={access.isLoading}
        usersById={usersById}
        onGrant={() => setDrawer({ kind: "grant" })}
        onRevoke={async (b) => {
          const ok = await confirm({
            title: t("users.revokeAccess"),
            message: t("users.revokeMessage", {
              level: b.model_id ? "model" : "project",
            }),
            confirmLabel: t("users.revokeTooltip"),
          });
          if (!ok) return;
          await accessApi.revoke(projectId, b.id);
          refreshAll();
        }}
      />

      <EditDrawer
        drawer={drawer}
        users={users.data ?? []}
        models={models.data ?? []}
        projectId={projectId}
        error={error}
        setError={setError}
        onClose={close}
        onSaved={() => {
          close();
          refreshAll();
        }}
      />
    </Stack>
  );
}

function UsersTable({
  users,
  loading,
  onCreateUser,
  onEditUser,
  onResetPassword,
  onDeleteUser,
}: {
  users: User[];
  loading: boolean;
  onCreateUser: () => void;
  onEditUser: (u: User) => void;
  onResetPassword: (u: User) => void;
  onDeleteUser: (u: User) => void;
}) {
  const t = useT();

  return (
    <Paper variant="outlined">
      <Box
        sx={{
          px: 1.5,
          py: 0.75,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <Typography variant="overline" color="text.secondary">
          {t("users.tenantUsersLabel")}
        </Typography>
        <Button
          size="small"
          startIcon={<AddIcon sx={{ fontSize: 16 }} />}
          onClick={onCreateUser}
        >
          {t("users.newUser")}
        </Button>
      </Box>
      <Divider />
      {loading ? (
        <Box sx={{ p: 2 }}>
          <CircularProgress size={18} />
        </Box>
      ) : users.length === 0 ? (
        <Box sx={{ p: 2 }}>
          <Typography variant="caption" color="text.secondary">
            {t("users.noUsers")}
          </Typography>
        </Box>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>
                {t("users.emailHeader")}
              </TableCell>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>
                {t("users.usernameHeader")}
              </TableCell>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>
                {t("users.roleHeader")}
              </TableCell>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>
                {t("users.sourceHeader")}
              </TableCell>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>
                {t("users.activeHeader")}
              </TableCell>
              <TableCell
                sx={{ fontSize: 12, color: "text.secondary" }}
                align="right"
              />
            </TableRow>
          </TableHead>
          <TableBody>
            {users.map((u) => (
              <TableRow key={u.id}>
                <TableCell sx={{ fontSize: 13, py: 0.5 }}>{u.email}</TableCell>
                <TableCell sx={{ fontSize: 13, py: 0.5 }}>
                  {u.username}
                </TableCell>
                <TableCell sx={{ fontSize: 13, py: 0.5 }}>{t(roleKey(u.role))}</TableCell>
                <TableCell sx={{ py: 0.5 }}>
                  <RoleSourceBadge source={u.role_source} />
                </TableCell>
                <TableCell sx={{ fontSize: 13, py: 0.5 }}>
                  {u.is_active ? t("users.activeLabel") : t("users.inactiveLabel")}
                </TableCell>
                <TableCell
                  align="right"
                  sx={{ py: 0.5, whiteSpace: "nowrap" }}
                >
                  <Tooltip title={t("users.editUserTooltip")}>
                    <IconButton size="small" onClick={() => onEditUser(u)}>
                      <EditIcon sx={{ fontSize: 16 }} />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("users.resetPasswordTooltip")}>
                    <IconButton size="small" onClick={() => onResetPassword(u)}>
                      <LockResetIcon sx={{ fontSize: 16 }} />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={t("users.deleteUserTooltip")}>
                    <IconButton size="small" onClick={() => onDeleteUser(u)}>
                      <DeleteIcon sx={{ fontSize: 16 }} />
                    </IconButton>
                  </Tooltip>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Paper>
  );
}

function AccessTable({
  projectName,
  access,
  loading,
  usersById,
  onGrant,
  onRevoke,
}: {
  projectName: string;
  access: UserAccessBinding[];
  loading: boolean;
  usersById: Map<string, User>;
  onGrant: () => void;
  onRevoke: (b: UserAccessBinding) => void;
}) {
  const t = useT();

  return (
    <Paper variant="outlined">
      <Box
        sx={{
          px: 1.5,
          py: 0.75,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <Typography variant="overline" color="text.secondary">
          {t("users.accessTitle", { project: projectName })}
        </Typography>
        <Button
          size="small"
          startIcon={<AddIcon sx={{ fontSize: 16 }} />}
          onClick={onGrant}
        >
          {t("users.grantAccess")}
        </Button>
      </Box>
      <Divider />
      {loading ? (
        <Box sx={{ p: 2 }}>
          <CircularProgress size={18} />
        </Box>
      ) : access.length === 0 ? (
        <Box sx={{ p: 2 }}>
          <Typography variant="caption" color="text.secondary">
            {t("users.noExplicitAccess")}
          </Typography>
        </Box>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>
                {t("users.userHeader")}
              </TableCell>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>
                {t("users.roleHeader")}
              </TableCell>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>
                {t("users.scopeHeader")}
              </TableCell>
              <TableCell
                sx={{ fontSize: 12, color: "text.secondary" }}
                align="right"
              />
            </TableRow>
          </TableHead>
          <TableBody>
            {access.map((b) => (
              <TableRow key={b.id}>
                <TableCell sx={{ fontSize: 13, py: 0.5 }}>
                  {usersById.get(b.user_identity)?.email ?? b.user_identity}
                </TableCell>
                <TableCell sx={{ fontSize: 13, py: 0.5 }}>{t(roleKey(b.role))}</TableCell>
                <TableCell sx={{ fontSize: 13, py: 0.5 }}>
                  {b.model_id ? t("users.modelLevelScope") : t("users.projectWide")}
                </TableCell>
                <TableCell align="right" sx={{ py: 0.5 }}>
                  <Tooltip title={t("users.revokeTooltip")}>
                    <IconButton size="small" onClick={() => onRevoke(b)}>
                      <DeleteIcon sx={{ fontSize: 16 }} />
                    </IconButton>
                  </Tooltip>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Paper>
  );
}

function EditDrawer({
  drawer,
  users,
  models,
  projectId,
  error,
  setError,
  onClose,
  onSaved,
}: {
  drawer: DrawerKind;
  users: User[];
  models: Model[];
  projectId: string;
  error: string | null;
  setError: (e: string | null) => void;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useT();
  const confirm = useConfirm();
  const tenantId = safeLocalGet("tenant_id", "");
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<LocalUserRole>("member");
  const [grantUser, setGrantUser] = useState("");
  const [grantRole, setGrantRole] = useState<AccessRole>("viewer");
  const [grantModelId, setGrantModelId] = useState<string>("");

  useEffect(() => {
    setError(null);
    if (drawer?.kind === "user") {
      setEmail(drawer.user?.email ?? "");
      setUsername(drawer.user?.username ?? "");
      setPassword("");
      setRole((drawer.user?.role as LocalUserRole) ?? "member");
    } else if (drawer?.kind === "grant") {
      setGrantUser("");
      setGrantRole("viewer");
      setGrantModelId("");
    } else if (drawer?.kind === "reset-password") {
      setPassword("");
    }
  }, [drawer, setError]);

  // Bug-8184: the server refuses a password that misses the complexity rule,
  // so refuse it here too rather than letting the admin submit and read the
  // rule out of a 422.
  const passwordOk = meetsPasswordPolicy(password);
  const passwordInvalid = showsPasswordPolicyError(password);

  const formValid = (() => {
    if (drawer?.kind === "user") {
      const baseValid = email.trim() !== "" && username.trim() !== "";
      return drawer.user ? baseValid : baseValid && passwordOk;
    }
    if (drawer?.kind === "grant") return grantUser !== "";
    if (drawer?.kind === "reset-password") return passwordOk;
    return false;
  })();

  const save = useMutation({
    mutationFn: async (): Promise<"saved" | "cancelled"> => {
      if (drawer?.kind === "user") {
        if (drawer.user) {
          await authApi.updateTenantUser(tenantId, drawer.user.id, {
            email,
            username,
            role,
          });
        } else {
          await authApi.createTenantUser(tenantId, {
            email,
            username,
            password,
            role,
          });
        }
      } else if (drawer?.kind === "grant") {
        // Bug-8101: run the Modeller-supersedes-Model-viewer confirmation
        // before granting. On cancel, nothing changes and the drawer stays.
        const outcome = await grantAccessWithSupersede(
          projectId,
          {
            user_identity: grantUser,
            role: grantRole,
            model_id: grantModelId === "" ? null : grantModelId,
          },
          confirm,
          {
            title: t("users.supersedeTitle"),
            message: t("users.supersedeMessage"),
            confirmLabel: t("users.supersedeConfirm"),
          },
        );
        if (outcome === "cancelled") return "cancelled";
      } else if (drawer?.kind === "reset-password") {
        await authApi.resetTenantUserPassword(tenantId, drawer.user.id, {
          password,
        });
      }
      return "saved";
    },
    onSuccess: (outcome) => {
      if (outcome === "cancelled") return;
      onSaved();
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      setError(detail ?? t("users.saveFailed"));
    },
  });

  function drawerTitle(): string {
    if (drawer?.kind === "user") {
      return drawer.user ? t("users.editUser") : t("users.newUserTitle");
    }
    if (drawer?.kind === "grant") return t("users.grantAccessTitle");
    if (drawer?.kind === "reset-password") return t("users.resetPasswordTitle");
    return "";
  }

  return (
    <Drawer anchor="right" open={drawer !== null} onClose={onClose}>
      <Box
        sx={{
          width: 360,
          p: 2,
          display: "flex",
          flexDirection: "column",
          height: "100%",
        }}
      >
        <Typography variant="h6" sx={{ mb: 2 }}>
          {drawerTitle()}
        </Typography>

        {error ? (
          <Alert severity="error" sx={{ mb: 1 }}>
            {error}
          </Alert>
        ) : null}

        {drawer?.kind === "user" && (
          <Stack spacing={2} sx={{ flex: 1 }}>
            <TextField
              label={t("users.emailLabel")}
              size="small"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <TextField
              label={t("users.usernameLabel")}
              size="small"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
            {!drawer.user && (
              <TextField
                label={t("users.passwordLabel")}
                type="password"
                size="small"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                error={passwordInvalid}
                helperText={t("errors.form.passwordComplexity")}
                InputProps={{
                  startAdornment: (
                    <KeyIcon
                      sx={{ fontSize: 16, mr: 1, color: "text.secondary" }}
                    />
                  ),
                }}
              />
            )}
            <FormControl size="small">
              <InputLabel>{t("users.roleLabel")}</InputLabel>
              <Select
                label={t("users.roleLabel")}
                value={role}
                onChange={(e) => setRole(e.target.value as LocalUserRole)}
              >
                {USER_ROLES.map((r) => (
                  <MenuItem key={r} value={r}>
                    {t(`users.role${r.charAt(0).toUpperCase() + r.slice(1).replace(/_([a-z])/g, (_, c: string) => c.toUpperCase())}`)}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <EffectiveAccessPreview role={role} />
          </Stack>
        )}

        {drawer?.kind === "grant" && (
          <Stack spacing={2} sx={{ flex: 1 }}>
            <FormControl size="small">
              <InputLabel>{t("users.userLabel")}</InputLabel>
              <Select
                label={t("users.userLabel")}
                value={grantUser}
                onChange={(e) => setGrantUser(e.target.value)}
              >
                {users.map((u) => (
                  <MenuItem key={u.id} value={u.email}>
                    {u.email}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <FormControl size="small">
              <InputLabel>{t("users.roleLabel")}</InputLabel>
              <Select
                label={t("users.roleLabel")}
                value={grantRole}
                onChange={(e) => setGrantRole(e.target.value as AccessRole)}
              >
                {ACCESS_ROLES.map((r) => (
                  <MenuItem key={r} value={r}>
                    {t(roleKey(r))}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <FormControl size="small">
              <InputLabel>{t("users.scopeLabel")}</InputLabel>
              <Select
                label={t("users.scopeLabel")}
                value={grantModelId}
                onChange={(e) => setGrantModelId(e.target.value)}
              >
                <MenuItem value="">{t("users.projectWideScope")}</MenuItem>
                {models.map((m) => (
                  <MenuItem key={m.id} value={m.id}>
                    {t("users.modelScope", { name: m.display_name })}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </Stack>
        )}

        {drawer?.kind === "reset-password" && (
          <Stack spacing={2} sx={{ flex: 1 }}>
            <Typography variant="body2" color="text.secondary">
              {drawer.user.email}
            </Typography>
            <TextField
              label={t("users.newPasswordLabel")}
              type="password"
              size="small"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              error={passwordInvalid}
              helperText={t("errors.form.passwordComplexity")}
            />
          </Stack>
        )}

        <Box
          sx={{ display: "flex", justifyContent: "flex-end", gap: 1, mt: 2 }}
        >
          <Button size="small" onClick={onClose}>
            {t("users.cancel")}
          </Button>
          <Button
            size="small"
            variant="contained"
            disabled={save.isPending || !formValid}
            onClick={() => save.mutate()}
          >
            {save.isPending ? <CircularProgress size={16} /> : t("users.saveButton")}
          </Button>
        </Box>
      </Box>
    </Drawer>
  );
}
