"""
Pydantic v2 request/response schemas for all Tessallite entities.

This module re-exports all schemas from the `domains/` sub-package.
All public names remain importable from this path for backward compatibility:

    from shared.schemas.pydantic_models import ModelResponse, KPICreate, ...

Convention:
  {Entity}Create  -- request body for POST
  {Entity}Update  -- request body for PATCH (all fields optional)
  {Entity}Response -- response (includes id, created_at, updated_at where present)
"""
# Re-export in dependency order — later modules may reference types from earlier ones.
from .domains._base import *  # noqa: F401,F403
from .domains.auth import *  # noqa: F401,F403
from .domains.tenants_projects import *  # noqa: F401,F403
from .domains.models_sources import *  # noqa: F401,F403
from .domains.columns_analysis import *  # noqa: F401,F403
from .domains.hierarchies import *  # noqa: F401,F403
from .domains.dimensions_measures import *  # noqa: F401,F403
from .domains.aggregates_security import *  # noqa: F401,F403
from .domains.governance_advanced import *  # noqa: F401,F403

# Rebuild models that have cross-module forward references.
# After all star-imports, every class name is in this module's namespace.
# We pass locals() so Pydantic can resolve string annotations like 'ModelResponse'.
_ns = {k: v for k, v in locals().items() if not k.startswith("_")}
ModelExportResponse.model_rebuild(_types_namespace=_ns)  # refs ModelResponse, DataSourceResponse, etc.
ModelImportRequest.model_rebuild(_types_namespace=_ns)  # refs MeasureResponse, HierarchyExportResponse
MeasureResponse.model_rebuild(_types_namespace=_ns)  # refs RedundantPartnerInfo
DimensionResponse.model_rebuild(_types_namespace=_ns)  # refs RedundantPartnerInfo
ModelRevalidationReportResponse.model_rebuild(_types_namespace=_ns)  # refs MeasureWarningResponse
TableAnalysisResponse.model_rebuild(_types_namespace=_ns)  # refs ColumnSuggestionResponse, MeasureWarningResponse
PocketDefinitionResponse.model_rebuild(_types_namespace=_ns)  # refs PocketPredicateResponse, PocketRefreshPolicyResponse
HierarchyDetailResponse.model_rebuild(_types_namespace=_ns)  # refs HierarchyLevelResponse
HierarchyLevelResponse.model_rebuild(_types_namespace=_ns)  # refs HierarchyLevelAttributeResponse
HierarchyGeneratedResponse.model_rebuild(_types_namespace=_ns)  # refs HierarchyDetailResponse
KPIBatchResponse.model_rebuild(_types_namespace=_ns)  # refs KPIEvaluateResponse
KPIEvaluateResponse.model_rebuild(_types_namespace=_ns)  # refs KPITrendPoint
AIOptimizerRunResponse.model_rebuild(_types_namespace=_ns)  # refs AIAggregateRecommendationResponse
GlossaryEntryResponse.model_rebuild(_types_namespace=_ns)  # refs GlossaryAttachmentResponse
HierarchyLevelExportResponse.model_rebuild(_types_namespace=_ns)  # refs HierarchyLevelAttributeExportResponse
HierarchyExportResponse.model_rebuild(_types_namespace=_ns)  # refs HierarchyLevelExportResponse
del _ns
