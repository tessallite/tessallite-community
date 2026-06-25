import type {
  Dimension,
  FieldCompatibilityReasonCode,
  FieldCompatibilityResponse,
} from "../../api/types";

const ACCESS_POLICY_LIMITATION_CODES = new Set<FieldCompatibilityReasonCode>([
  "PERSONA_FIELD_UNAVAILABLE",
]);

export type MeasureCompatibilityState = "loading" | "unavailable" | "ready";

export type MeasureCompatibilityLimitation =
  | "accessPolicyAggregateOnly"
  | "none";

export interface MeasureCompatibilitySummary {
  state: MeasureCompatibilityState;
  compatibleDimensionNames: string[];
  limitation: MeasureCompatibilityLimitation;
}

export function summarizeMeasureCompatibility(args: {
  measureId: string;
  matrix: FieldCompatibilityResponse | undefined;
  dimensions: Dimension[];
  loading: boolean;
  unavailable: boolean;
}): MeasureCompatibilitySummary {
  if (args.loading) {
    return {
      state: "loading",
      compatibleDimensionNames: [],
      limitation: "none",
    };
  }

  if (args.unavailable || !args.matrix) {
    return {
      state: "unavailable",
      compatibleDimensionNames: [],
      limitation: "none",
    };
  }

  const measureEntry = args.matrix.measures[args.measureId];
  if (!measureEntry) {
    return {
      state: "unavailable",
      compatibleDimensionNames: [],
      limitation: "none",
    };
  }

  const dimensionNameById = new Map(
    args.dimensions
      .filter((dimension) => !dimension.is_hidden)
      .map((dimension) => [
        dimension.id,
        dimension.display_name || dimension.name,
      ]),
  );
  const compatibleDimensionNames = measureEntry.compatible_dimension_ids
    .map((dimensionId) => dimensionNameById.get(dimensionId))
    .filter((name): name is string => Boolean(name));

  if (compatibleDimensionNames.length > 0) {
    return {
      state: "ready",
      compatibleDimensionNames,
      limitation: "none",
    };
  }

  const hasAccessPolicyLimitation = Object.values(
    measureEntry.incompatible_dimensions ?? {},
  ).some((issue) => ACCESS_POLICY_LIMITATION_CODES.has(issue.code));

  return {
    state: "ready",
    compatibleDimensionNames: [],
    limitation: hasAccessPolicyLimitation
      ? "accessPolicyAggregateOnly"
      : "none",
  };
}
