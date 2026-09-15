import type { SemanticQuery, ExecuteResponse, DiscoverMembersResponse, DrillOption, DrillThroughResponse } from '../types/tessallite';
export interface PluginExecuteParams {
    projectId: string;
    modelId: string;
    personaId?: string;
}
export declare function executeQuery(query: SemanticQuery, params: PluginExecuteParams): Promise<ExecuteResponse>;
export declare function discoverMembers(modelId: string, dimensionName: string, personaId?: string): Promise<DiscoverMembersResponse>;
export declare function getDrillOptions(measureId: string, context: Record<string, unknown>): Promise<DrillOption[]>;
export declare function drillThrough(measureId: string, context: Record<string, unknown>, cursor?: string): Promise<DrillThroughResponse>;
