import { Box, FormControl, IconButton, InputLabel, MenuItem, Select, Stack, TextField, Typography, Button } from "@mui/material";
import DeleteIcon from "@mui/icons-material/Delete";
import AddIcon from "@mui/icons-material/Add";
import { useT } from "../../i18n";
import { ExprNode, RecipeStep } from "../../api/agentApi";

// Bug-5346 — the combine expression is built as a typed semantic tree of data,
// never typed as a formula string. Each node is a constant, a reference to a
// step's measure, or an operation over child nodes. Step/measure names are
// chosen from the steps the user already defined, so they are pure data and can
// never collide with any grammar's reserved words.

type OpMeta = { min: number; max: number | null; label: string };

// Mirrors the backend _OP_ARITY table (src/recipes/eval.py).
const OPS: Record<string, OpMeta> = {
  add: { min: 2, max: 2, label: "+ (add)" },
  sub: { min: 2, max: 2, label: "− (subtract)" },
  mul: { min: 2, max: 2, label: "× (multiply)" },
  div: { min: 2, max: 2, label: "÷ (divide)" },
  floordiv: { min: 2, max: 2, label: "// (floor divide)" },
  mod: { min: 2, max: 2, label: "% (modulo)" },
  pow: { min: 2, max: 2, label: "^ (power)" },
  eq: { min: 2, max: 2, label: "= (equals)" },
  ne: { min: 2, max: 2, label: "≠ (not equals)" },
  lt: { min: 2, max: 2, label: "< (less than)" },
  le: { min: 2, max: 2, label: "≤ (less or equal)" },
  gt: { min: 2, max: 2, label: "> (greater than)" },
  ge: { min: 2, max: 2, label: "≥ (greater or equal)" },
  and: { min: 2, max: null, label: "and" },
  or: { min: 2, max: null, label: "or" },
  not: { min: 1, max: 1, label: "not" },
  neg: { min: 1, max: 1, label: "negate" },
  round: { min: 1, max: 2, label: "round()" },
  min: { min: 1, max: null, label: "min()" },
  max: { min: 1, max: null, label: "max()" },
  sum: { min: 1, max: null, label: "sum()" },
  abs: { min: 1, max: 1, label: "abs()" },
  len: { min: 1, max: 1, label: "len()" },
  if: { min: 3, max: 3, label: "if / else" },
};

type Kind = "none" | "const" | "ref" | "op";

function kindOf(node: ExprNode | null): Kind {
  if (node == null) return "none";
  if ("const" in node) return "const";
  if ("ref" in node) return "ref";
  return "op";
}

function defaultNodeForKind(kind: Kind, steps: RecipeStep[]): ExprNode | null {
  if (kind === "none") return null;
  if (kind === "const") return { const: 0 };
  if (kind === "ref") {
    const step = steps[0];
    return { ref: { step: step?.name ?? "", measure: step?.measures[0] ?? "" } };
  }
  return { op: "div", args: [defaultNodeForKind("ref", steps)!, defaultNodeForKind("ref", steps)!] };
}

function NodeEditor({
  node,
  steps,
  onChange,
  depth = 0,
  allowNone = false,
}: {
  node: ExprNode | null;
  steps: RecipeStep[];
  onChange: (next: ExprNode | null) => void;
  depth?: number;
  allowNone?: boolean;
}) {
  const t = useT();
  const kind = kindOf(node);

  return (
    <Box
      sx={{
        borderLeft: depth > 0 ? 2 : 0,
        borderColor: "divider",
        pl: depth > 0 ? 1.5 : 0,
        py: 0.5,
      }}
    >
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
        <FormControl size="small" sx={{ minWidth: 130 }}>
          <InputLabel>{t("recipes.combineNodeKind")}</InputLabel>
          <Select
            label={t("recipes.combineNodeKind")}
            value={kind}
            onChange={(e) => onChange(defaultNodeForKind(e.target.value as Kind, steps))}
          >
            {allowNone && <MenuItem value="none">{t("recipes.combineKindNone")}</MenuItem>}
            <MenuItem value="op">{t("recipes.combineKindOp")}</MenuItem>
            <MenuItem value="ref">{t("recipes.combineKindRef")}</MenuItem>
            <MenuItem value="const">{t("recipes.combineKindConst")}</MenuItem>
          </Select>
        </FormControl>

        {kind === "const" && node && "const" in node && (
          <TextField
            label={t("recipes.combineConstValue")}
            size="small"
            type="number"
            value={String(node.const)}
            onChange={(e) => onChange({ const: Number(e.target.value) })}
            sx={{ width: 140 }}
          />
        )}

        {kind === "ref" && node && "ref" in node && (
          <RefEditor node={node} steps={steps} onChange={onChange} />
        )}

        {kind === "op" && node && "op" in node && (
          <FormControl size="small" sx={{ minWidth: 160 }}>
            <InputLabel>{t("recipes.combineOperation")}</InputLabel>
            <Select
              label={t("recipes.combineOperation")}
              value={node.op}
              onChange={(e) => {
                const op = String(e.target.value);
                const meta = OPS[op];
                const args = [...node.args];
                while (args.length < meta.min) args.push(defaultNodeForKind("ref", steps)!);
                if (meta.max != null && args.length > meta.max) args.length = meta.max;
                onChange({ op, args });
              }}
            >
              {Object.entries(OPS).map(([op, meta]) => (
                <MenuItem key={op} value={op}>
                  {meta.label}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
        )}
      </Stack>

      {kind === "op" && node && "op" in node && (
        <Box sx={{ mt: 0.5 }}>
          {node.args.map((arg, i) => {
            const meta = OPS[node.op];
            const canRemove = node.args.length > meta.min;
            return (
              <Stack key={i} direction="row" spacing={0.5} alignItems="flex-start">
                <Box sx={{ flex: 1, minWidth: 0 }}>
                  <NodeEditor
                    node={arg}
                    steps={steps}
                    depth={depth + 1}
                    onChange={(next) => {
                      const args = [...node.args];
                      args[i] = next ?? { const: 0 };
                      onChange({ ...node, args });
                    }}
                  />
                </Box>
                {canRemove && (
                  <IconButton
                    size="small"
                    onClick={() => onChange({ ...node, args: node.args.filter((_, j) => j !== i) })}
                  >
                    <DeleteIcon fontSize="small" />
                  </IconButton>
                )}
              </Stack>
            );
          })}
          {OPS[node.op].max == null && (
            <Button
              size="small"
              startIcon={<AddIcon />}
              onClick={() => onChange({ ...node, args: [...node.args, defaultNodeForKind("ref", steps)!] })}
            >
              {t("recipes.combineAddArgument")}
            </Button>
          )}
        </Box>
      )}
    </Box>
  );
}

function RefEditor({
  node,
  steps,
  onChange,
}: {
  node: { ref: { step: string; measure: string } };
  steps: RecipeStep[];
  onChange: (next: ExprNode) => void;
}) {
  const t = useT();
  const step = steps.find((s) => s.name === node.ref.step);
  return (
    <>
      <FormControl size="small" sx={{ minWidth: 140 }}>
        <InputLabel>{t("recipes.combineRefStep")}</InputLabel>
        <Select
          label={t("recipes.combineRefStep")}
          value={steps.some((s) => s.name === node.ref.step) ? node.ref.step : ""}
          onChange={(e) => {
            const stepName = String(e.target.value);
            const m = steps.find((s) => s.name === stepName)?.measures[0] ?? "";
            onChange({ ref: { step: stepName, measure: m } });
          }}
        >
          {steps.map((s, i) => (
            <MenuItem key={i} value={s.name}>
              {s.name || `(step ${i + 1})`}
            </MenuItem>
          ))}
        </Select>
      </FormControl>
      <FormControl size="small" sx={{ minWidth: 140 }}>
        <InputLabel>{t("recipes.combineRefMeasure")}</InputLabel>
        <Select
          label={t("recipes.combineRefMeasure")}
          value={step?.measures.includes(node.ref.measure) ? node.ref.measure : ""}
          onChange={(e) => onChange({ ref: { step: node.ref.step, measure: String(e.target.value) } })}
        >
          {(step?.measures ?? []).map((m, i) => (
            <MenuItem key={i} value={m}>
              {m}
            </MenuItem>
          ))}
        </Select>
      </FormControl>
    </>
  );
}

export default function CombineExpressionBuilder({
  value,
  steps,
  onChange,
}: {
  value: ExprNode | null;
  steps: RecipeStep[];
  onChange: (next: ExprNode | null) => void;
}) {
  const t = useT();
  return (
    <Box sx={{ border: 1, borderColor: "divider", p: 1.5, borderRadius: 1 }}>
      <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
        {t("recipes.combineLabel")}
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
        {t("recipes.combineExpressionHelp")}
      </Typography>
      <NodeEditor node={value} steps={steps} onChange={onChange} allowNone />
    </Box>
  );
}
