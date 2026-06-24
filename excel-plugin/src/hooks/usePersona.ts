import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getPersonas } from '../api/modelService';

// F-025-21: the client-side filterMeasures/filterDimensions/filterHierarchies
// helpers were computed but never consumed — server-side persona filtering is
// used everywhere instead (every metadata fetch threads persona_id). Removed to
// drop dead code and the now-unused Measure/Dimension/Hierarchy type imports.
export function usePersonaFiltered(
  projectId: string | null,
  modelId: string | null,
  activePersonaId: string | null,
) {
  const { data: personas } = useQuery({
    queryKey: ['personas', projectId, modelId],
    queryFn: () => getPersonas(projectId!, modelId!),
    enabled: !!projectId && !!modelId,
  });

  const activePersona = useMemo(
    () => (activePersonaId ? personas?.find(p => p.id === activePersonaId) : null),
    [personas, activePersonaId],
  );

  return {
    personas: personas ?? [],
    activePersona,
  };
}
