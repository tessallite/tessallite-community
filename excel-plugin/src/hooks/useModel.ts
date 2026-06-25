/**
 * Model loading hooks.
 * Phase 2: Used by the Report Builder component.
 */
import { useQuery } from '@tanstack/react-query';
import {
  getProjects, getModels, getModel,
  getMeasures, getDimensions, getHierarchies, getKpis, getNamedSets, getPersonas, getGlossary,
  getAliasMap, getFieldCompatibility,
} from '../api/modelService';

export function useProjects() {
  return useQuery({ queryKey: ['projects'], queryFn: getProjects, enabled: false });
}

export function useModels(projectId: string | null) {
  return useQuery({
    queryKey: ['models', projectId],
    queryFn: () => getModels(projectId!),
    enabled: !!projectId,
  });
}

export function useModel(projectId: string | null, modelId: string | null) {
  return useQuery({
    queryKey: ['model', projectId, modelId],
    queryFn: () => getModel(projectId!, modelId!),
    enabled: !!projectId && !!modelId,
  });
}

export function useMeasures(projectId: string | null, modelId: string | null, personaId?: string | null) {
  const { data: model } = useModel(projectId, modelId);
  const versionKey = model?.deployed_version_id || 'latest';
  return useQuery({
    queryKey: ['measures', projectId, modelId, personaId ?? 'base', versionKey],
    queryFn: () => getMeasures(projectId!, modelId!, personaId || undefined),
    enabled: !!projectId && !!modelId,
    staleTime: 5 * 60 * 1000,
  });
}

export function useDimensions(projectId: string | null, modelId: string | null, personaId?: string | null) {
  const { data: model } = useModel(projectId, modelId);
  const versionKey = model?.deployed_version_id || 'latest';
  return useQuery({
    queryKey: ['dimensions', projectId, modelId, personaId ?? 'base', versionKey],
    queryFn: () => getDimensions(projectId!, modelId!, personaId || undefined),
    enabled: !!projectId && !!modelId,
    staleTime: 5 * 60 * 1000,
  });
}

export function useHierarchies(projectId: string | null, modelId: string | null, personaId?: string | null) {
  const { data: model } = useModel(projectId, modelId);
  const versionKey = model?.deployed_version_id || 'latest';
  return useQuery({
    queryKey: ['hierarchies', projectId, modelId, personaId ?? 'base', versionKey],
    queryFn: () => getHierarchies(projectId!, modelId!, personaId || undefined),
    enabled: !!projectId && !!modelId,
    staleTime: 5 * 60 * 1000,
  });
}

export function useKpis(projectId: string | null, modelId: string | null) {
  const { data: model } = useModel(projectId, modelId);
  const versionKey = model?.deployed_version_id || 'latest';
  return useQuery({
    queryKey: ['kpis', projectId, modelId, versionKey],
    queryFn: () => getKpis(projectId!, modelId!),
    enabled: !!projectId && !!modelId,
    staleTime: 5 * 60 * 1000,
  });
}

export function useNamedSets(projectId: string | null, modelId: string | null) {
  const { data: model } = useModel(projectId, modelId);
  const versionKey = model?.deployed_version_id || 'latest';
  return useQuery({
    queryKey: ['namedSets', projectId, modelId, versionKey],
    queryFn: () => getNamedSets(projectId!, modelId!),
    enabled: !!projectId && !!modelId,
    staleTime: 5 * 60 * 1000,
  });
}

export function usePersonas(projectId: string | null, modelId: string | null) {
  return useQuery({
    queryKey: ['personas', projectId, modelId],
    queryFn: () => getPersonas(projectId!, modelId!),
    enabled: !!projectId && !!modelId,
  });
}

export function useGlossary(projectId: string | null, modelId: string | null, personaId?: string | null) {
  return useQuery({
    queryKey: ['glossary', projectId, modelId, personaId ?? 'base'],
    queryFn: () => getGlossary(projectId!, modelId!, personaId || undefined),
    enabled: !!projectId && !!modelId,
    staleTime: 5 * 60 * 1000,
  });
}

export function useAliasMap(projectId: string | null, modelId: string | null, personaId?: string | null) {
  return useQuery({
    queryKey: ['aliasMap', projectId, modelId, personaId ?? 'base'],
    queryFn: () => getAliasMap(projectId!, modelId!, personaId || undefined),
    enabled: !!projectId && !!modelId,
    staleTime: 5 * 60 * 1000,
  });
}

export function useFieldCompatibility(
  projectId: string | null,
  modelId: string | null,
  personaId?: string | null,
  measureIds: string[] = [],
  dimensionIds: string[] = [],
) {
  const sortedMeasureIds = [...new Set(measureIds)].sort();
  const sortedDimensionIds = [...new Set(dimensionIds)].sort();
  return useQuery({
    queryKey: [
      'fieldCompatibility',
      projectId,
      modelId,
      personaId ?? 'base',
      sortedMeasureIds,
      sortedDimensionIds,
    ],
    queryFn: () => getFieldCompatibility(projectId!, modelId!, {
      personaId,
      measureIds: sortedMeasureIds,
      dimensionIds: sortedDimensionIds,
    }),
    enabled: !!projectId && !!modelId && sortedMeasureIds.length > 0,
    staleTime: 60 * 1000,
  });
}
