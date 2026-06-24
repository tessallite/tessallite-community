/**
 * FunctionPicker — categorised, searchable catalog of DSL functions.
 *
 * Follows the same insert-template pattern as AttributesTab's function
 * picker: user selects a function, sees its signature and description,
 * clicks "Insert" to append the snippet into the expression textarea.
 */
import { useMemo, useState } from "react";
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Box,
  Button,
  List,
  ListItemButton,
  ListItemText,
  TextField,
  Typography,
} from "@mui/material";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import { useT } from "../../../i18n";
import {
  CATEGORY_LABELS,
  CATEGORY_ORDER,
  DSL_FUNCTIONS,
  type DslFunctionCategory,
  type DslFunctionDef,
} from "./functionCatalog";

interface Props {
  onInsert: (snippet: string) => void;
}

export default function FunctionPicker({ onInsert }: Props) {
  const t = useT();
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<DslFunctionDef | null>(null);

  const filtered = useMemo(() => {
    if (!search.trim()) return DSL_FUNCTIONS;
    const lower = search.toLowerCase();
    return DSL_FUNCTIONS.filter(
      (f) =>
        f.name.toLowerCase().includes(lower) ||
        f.descriptionFallback.toLowerCase().includes(lower),
    );
  }, [search]);

  const grouped = useMemo(() => {
    const map = new Map<DslFunctionCategory, DslFunctionDef[]>();
    for (const cat of CATEGORY_ORDER) map.set(cat, []);
    for (const fn of filtered) {
      map.get(fn.category)!.push(fn);
    }
    return map;
  }, [filtered]);

  const handleInsert = () => {
    if (!selected) return;
    // Replace $1, $2, etc. with empty strings for plain textarea insert
    const clean = selected.insertSnippet.replace(/\$\d+/g, "");
    onInsert(clean);
  };

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <TextField
        size="small"
        fullWidth
        placeholder={t("kpis.formula.searchFunctions")}
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        sx={{ mb: 1 }}
      />

      <Box sx={{ flex: 1, overflow: "auto" }}>
        {CATEGORY_ORDER.map((cat) => {
          const fns = grouped.get(cat);
          if (!fns || fns.length === 0) return null;
          const label = CATEGORY_LABELS[cat];
          return (
            <Accordion
              key={cat}
              disableGutters
              defaultExpanded={cat === "references"}
              sx={{ "&:before": { display: "none" }, boxShadow: "none" }}
            >
              <AccordionSummary expandIcon={<ExpandMoreIcon />} sx={{ minHeight: 36 }}>
                <Typography variant="caption" fontWeight={600}>
                  {t(label.key) || label.fallback}
                </Typography>
              </AccordionSummary>
              <AccordionDetails sx={{ p: 0 }}>
                <List dense disablePadding>
                  {fns.map((fn) => (
                    <ListItemButton
                      key={fn.name}
                      selected={selected?.name === fn.name}
                      onClick={() => setSelected(fn)}
                      onDoubleClick={() => {
                        setSelected(fn);
                        const clean = fn.insertSnippet.replace(/\$\d+/g, "");
                        onInsert(clean);
                      }}
                      sx={{ py: 0.25, px: 1.5 }}
                    >
                      <ListItemText
                        primary={
                          <Typography variant="body2" fontFamily="monospace" fontSize={12}>
                            {fn.name}
                          </Typography>
                        }
                      />
                    </ListItemButton>
                  ))}
                </List>
              </AccordionDetails>
            </Accordion>
          );
        })}
      </Box>

      {/* Selected function details + insert button */}
      {selected && (
        <Box sx={{ borderTop: 1, borderColor: "divider", pt: 1, mt: 1 }}>
          <Typography variant="body2" fontFamily="monospace" fontSize={12} fontWeight={600}>
            {selected.signature}
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
            {t(selected.descriptionKey) || selected.descriptionFallback}
          </Typography>
          <Typography
            variant="caption"
            color="text.secondary"
            fontFamily="monospace"
            fontSize={11}
            sx={{ display: "block", mt: 0.5, whiteSpace: "pre-wrap" }}
          >
            {selected.example}
          </Typography>
          <Button size="small" variant="outlined" onClick={handleInsert} sx={{ mt: 1 }}>
            {t("kpis.formula.insertFunction")}
          </Button>
        </Box>
      )}
    </Box>
  );
}
