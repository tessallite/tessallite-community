import { useState } from "react";
import {
  Box,
  Button,
  Card,
  CardActionArea,
  CardContent,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import {
  KPI_TEMPLATES,
  NAMED_SET_TEMPLATES,
  KPI_CATEGORIES,
  NS_CATEGORIES,
  type KpiTemplate,
  type NamedSetTemplate,
} from "./templates";
import { useT } from "../../i18n";

interface TemplateGalleryDialogProps {
  open: boolean;
  onClose: () => void;
  entityType: "kpi" | "named_set";
  onApplyKpi?: (template: KpiTemplate) => void;
  onApplyNamedSet?: (template: NamedSetTemplate) => void;
}

export default function TemplateGalleryDialog({
  open,
  onClose,
  entityType,
  onApplyKpi,
  onApplyNamedSet,
}: TemplateGalleryDialogProps) {
  const t = useT();
  const [search, setSearch] = useState("");
  const [categoryFilter, setCategoryFilter] = useState<string | null>(null);

  const categories = entityType === "kpi" ? KPI_CATEGORIES : NS_CATEGORIES;

  const filteredKpis = KPI_TEMPLATES.filter((tmpl) => {
    if (categoryFilter && tmpl.category !== categoryFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      return (
        tmpl.display_name.toLowerCase().includes(q) ||
        tmpl.description.toLowerCase().includes(q) ||
        tmpl.category.toLowerCase().includes(q)
      );
    }
    return true;
  });

  const filteredSets = NAMED_SET_TEMPLATES.filter((tmpl) => {
    if (categoryFilter && tmpl.category !== categoryFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      return (
        tmpl.display_name.toLowerCase().includes(q) ||
        tmpl.description.toLowerCase().includes(q) ||
        tmpl.category.toLowerCase().includes(q)
      );
    }
    return true;
  });

  const handleClose = () => {
    setSearch("");
    setCategoryFilter(null);
    onClose();
  };

  return (
    <Dialog open={open} onClose={handleClose} maxWidth="md" fullWidth>
      <DialogTitle>
        {entityType === "kpi" ? t("templateGallery.kpiTitle") : t("templateGallery.namedSetTitle")}
      </DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="text.secondary" mb={2}>
          {t("templateGallery.description")}
        </Typography>

        <Stack direction="row" spacing={1} alignItems="center" mb={2} flexWrap="wrap" useFlexGap>
          <TextField
            size="small"
            placeholder={t("templateGallery.searchPlaceholder")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            sx={{ minWidth: 200 }}
          />
          <Chip
            label={t("templateGallery.allFilter")}
            size="small"
            variant={categoryFilter === null ? "filled" : "outlined"}
            color={categoryFilter === null ? "primary" : "default"}
            onClick={() => setCategoryFilter(null)}
          />
          {categories.map((cat) => (
            <Chip
              key={cat}
              label={cat}
              size="small"
              variant={categoryFilter === cat ? "filled" : "outlined"}
              color={categoryFilter === cat ? "primary" : "default"}
              onClick={() =>
                setCategoryFilter(categoryFilter === cat ? null : cat)
              }
            />
          ))}
        </Stack>

        <Box
          sx={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
            gap: 1.5,
            maxHeight: 480,
            overflow: "auto",
          }}
        >
          {entityType === "kpi" &&
            filteredKpis.map((tmpl) => (
              <Card key={tmpl.name} variant="outlined">
                <CardActionArea
                  onClick={() => {
                    onApplyKpi?.(tmpl);
                    handleClose();
                  }}
                >
                  <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
                    <Box display="flex" alignItems="center" gap={1} mb={0.5}>
                      <Typography variant="subtitle2" sx={{ flex: 1 }}>
                        {tmpl.display_name}
                      </Typography>
                      <Chip label={t(`templateGallery.category.${tmpl.category}`)} size="small" variant="outlined" />
                    </Box>
                    <Typography
                      variant="body2"
                      color="text.secondary"
                      sx={{ fontSize: 12, mb: 1 }}
                    >
                      {tmpl.description}
                    </Typography>
                    <Box display="flex" gap={0.5} flexWrap="wrap">
                      <Chip
                        label={t(`templateGallery.status.${tmpl.status_graphic}`)}
                        size="small"
                        variant="outlined"
                        sx={{ fontSize: 10 }}
                      />
                      <Chip
                        label={t(`templateGallery.trend.${tmpl.trend_graphic}`)}
                        size="small"
                        variant="outlined"
                        sx={{ fontSize: 10 }}
                      />
                    </Box>
                    <Box
                      display="flex"
                      alignItems="center"
                      gap={0.5}
                      mt={1}
                      color="primary.main"
                    >
                      <ContentCopyIcon sx={{ fontSize: 14 }} />
                      <Typography variant="caption" color="primary">
                        {t("templateGallery.useTemplate")}
                      </Typography>
                    </Box>
                  </CardContent>
                </CardActionArea>
              </Card>
            ))}

          {entityType === "named_set" &&
            filteredSets.map((tmpl) => (
              <Card key={tmpl.name} variant="outlined">
                <CardActionArea
                  onClick={() => {
                    onApplyNamedSet?.(tmpl);
                    handleClose();
                  }}
                >
                  <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
                    <Box display="flex" alignItems="center" gap={1} mb={0.5}>
                      <Typography variant="subtitle2" sx={{ flex: 1 }}>
                        {tmpl.display_name}
                      </Typography>
                      <Chip label={t(`templateGallery.category.${tmpl.category}`)} size="small" variant="outlined" />
                    </Box>
                    <Typography
                      variant="body2"
                      color="text.secondary"
                      sx={{ fontSize: 12, mb: 1 }}
                    >
                      {tmpl.description}
                    </Typography>
                    <Typography
                      variant="caption"
                      sx={{
                        fontFamily: "monospace",
                        display: "block",
                        bgcolor: "action.hover",
                        p: 0.5,
                        borderRadius: 0.5,
                        fontSize: 10,
                        maxHeight: 48,
                        overflow: "hidden",
                      }}
                    >
                      {tmpl.set_expression}
                    </Typography>
                    <Box
                      display="flex"
                      alignItems="center"
                      gap={0.5}
                      mt={1}
                      color="primary.main"
                    >
                      <ContentCopyIcon sx={{ fontSize: 14 }} />
                      <Typography variant="caption" color="primary">
                        {t("templateGallery.useTemplate")}
                      </Typography>
                    </Box>
                  </CardContent>
                </CardActionArea>
              </Card>
            ))}
        </Box>

        {((entityType === "kpi" && filteredKpis.length === 0) ||
          (entityType === "named_set" && filteredSets.length === 0)) && (
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{ textAlign: "center", py: 4 }}
          >
            {t("templateGallery.noResults")}
          </Typography>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={handleClose}>{t("common.close")}</Button>
      </DialogActions>
    </Dialog>
  );
}
