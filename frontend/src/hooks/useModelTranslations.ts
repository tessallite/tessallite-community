import { useQuery } from "@tanstack/react-query";
import { translationsApi, type TranslationRecord } from "../api/client";
import { useBuilderStore } from "../store/builderStore";

export function useModelTranslations(projectId: string, modelId: string) {
  const displayLocale = useBuilderStore((s) => s.displayLocale);

  return useQuery({
    queryKey: ["translations", projectId, modelId, displayLocale],
    queryFn: () =>
      translationsApi.list(projectId, modelId, {
        locale: displayLocale!,
      }),
    enabled: Boolean(projectId && modelId && displayLocale),
    staleTime: 60_000,
  });
}

export function translatedName(
  translations: TranslationRecord[] | undefined,
  entityType: string,
  entityId: string,
  fieldName: string,
  defaultName: string,
): string {
  if (!translations?.length) return defaultName;
  const t = translations.find(
    (r) =>
      r.entity_type === entityType &&
      r.entity_id === entityId &&
      r.field_name === fieldName,
  );
  return t?.translated_text ?? defaultName;
}
