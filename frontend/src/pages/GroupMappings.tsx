import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import AddIcon from "@mui/icons-material/Add";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import DeleteIcon from "@mui/icons-material/DeleteOutline";
import { groupMappingsApi } from "../api/client";
import { useProjects } from "../api/hooks";
import type { GroupMapping } from "../api/types";
import HelpIconButton from "../components/HelpIconButton";
import { useT } from "../i18n";

const ROLES = ["admin", "modeler", "viewer", "model_technical"] as const;

export default function GroupMappings({ embedded }: { embedded?: boolean } = {}) {
  const t = useT();
  const qc = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [groupName, setGroupName] = useState("");
  const [role, setRole] = useState<string>("viewer");
  const [projectScope, setProjectScope] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const { data: projects } = useProjects();

  const { data: mappings = [], isLoading } = useQuery({
    queryKey: ["group-mappings"],
    queryFn: () => groupMappingsApi.list(),
  });

  const createMut = useMutation({
    mutationFn: () =>
      groupMappingsApi.create({
        idp_group_name: groupName,
        role,
        project_id: projectScope || null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["group-mappings"] });
      setDialogOpen(false);
      setGroupName("");
      setRole("viewer");
      setProjectScope("");
      setError(null);
    },
    onError: (err: any) => {
      setError(err?.response?.data?.detail ?? t("groupMappings.createFailed"));
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => groupMappingsApi.delete(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["group-mappings"] }),
  });

  const updateMut = useMutation({
    mutationFn: ({ id, role }: { id: string; role: string }) =>
      groupMappingsApi.update(id, { role }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["group-mappings"] }),
  });

  return (
    <Box sx={{ p: embedded ? 0 : 3, maxWidth: 900, mx: "auto" }}>
      {!embedded && (
      <>
      <Box display="flex" alignItems="center" gap={1} mb={3}>
        <IconButton href="/admin" size="small">
          <ArrowBackIcon />
        </IconButton>
        <Typography variant="h5" fontWeight={700}>
          {t("groupMappings.title")}
        </Typography>
        <HelpIconButton href="/help/admin/group-mappings.html" />
      </Box>

      <Typography variant="body2" color="text.secondary" mb={3}>
        {t("groupMappings.description")}
      </Typography>
      </>
      )}

      <Box display="flex" justifyContent="flex-end" mb={2}>
        <Button
          startIcon={<AddIcon />}
          variant="contained"
          size="small"
          onClick={() => setDialogOpen(true)}
        >
          {t("groupMappings.addMapping")}
        </Button>
      </Box>

      <TableContainer component={Paper} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontWeight: 700 }}>{t("groupMappings.colIdpGroup")}</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>{t("groupMappings.colRole")}</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>{t("groupMappings.colProjectScope")}</TableCell>
              <TableCell width={80} />
            </TableRow>
          </TableHead>
          <TableBody>
            {isLoading && (
              <TableRow>
                <TableCell colSpan={4} align="center">
                  {t("common.loading")}
                </TableCell>
              </TableRow>
            )}
            {!isLoading && mappings.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} align="center">
                  {t("groupMappings.noMappings")}
                </TableCell>
              </TableRow>
            )}
            {mappings.map((m: GroupMapping) => (
              <TableRow key={m.id}>
                <TableCell>{m.idp_group_name}</TableCell>
                <TableCell>
                  <FormControl size="small" sx={{ minWidth: 120 }}>
                    <Select
                      value={m.role}
                      onChange={(e) =>
                        updateMut.mutate({ id: m.id, role: e.target.value })
                      }
                    >
                      {ROLES.map((r) => (
                        <MenuItem key={r} value={r}>
                          {t(`roles.${r}`)}
                        </MenuItem>
                      ))}
                    </Select>
                  </FormControl>
                </TableCell>
                <TableCell>
                  {m.project_id ? m.project_id : t("groupMappings.tenantWide")}
                </TableCell>
                <TableCell>
                  <Tooltip title={t("groupMappings.deleteMapping")}>
                    <IconButton
                      size="small"
                      onClick={() => deleteMut.mutate(m.id)}
                    >
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>{t("groupMappings.addDialogTitle")}</DialogTitle>
        <DialogContent>
          {error && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {error}
            </Alert>
          )}
          <TextField
            label={t("groupMappings.idpGroupLabel")}
            fullWidth
            margin="normal"
            value={groupName}
            onChange={(e) => setGroupName(e.target.value)}
            autoFocus
          />
          <FormControl fullWidth margin="normal">
            <InputLabel>{t("groupMappings.roleLabel")}</InputLabel>
            <Select
              value={role}
              label={t("groupMappings.roleLabel")}
              onChange={(e) => setRole(e.target.value)}
            >
              {ROLES.map((r) => (
                <MenuItem key={r} value={r}>
                  {t(`roles.${r}`)}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="normal">
            <InputLabel>{t("groupMappings.projectLabel")}</InputLabel>
            <Select
              value={projectScope}
              label={t("groupMappings.projectLabel")}
              onChange={(e) => setProjectScope(e.target.value)}
            >
              <MenuItem value="">{t("groupMappings.tenantWide")}</MenuItem>
              {(projects ?? []).map((p) => (
                <MenuItem key={p.id} value={p.id}>
                  {p.display_name || p.slug}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("groupMappings.cancelButton")}</Button>
          <Button
            variant="contained"
            disabled={!groupName.trim() || createMut.isPending}
            onClick={() => createMut.mutate()}
          >
            {t("groupMappings.addButton")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
