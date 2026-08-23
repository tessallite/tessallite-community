"""
Office Data Connection (.odc) file generation endpoint.

Generates a downloadable .odc file for a deployed model so that Excel
users can create the correctly-named OLAP workbook connection with two
clicks instead of the manual wizard ritual (Phase C of the
connectionless-Excel plan, Bug-6727 context).

Role requirements:
  GET /odc  -> viewer+ (embed users forbidden)
"""
from __future__ import annotations

import html
import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from shared.config.settings import get_settings
from shared.db.models import Model
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}",
    tags=["odc"],
)

# -- .odc template -----------------------------------------------------------
# The .odc format is an HTML file with an embedded XML
# <xml><o:OfficeDataConnection> block.  The connection NAME inside
# must be exactly "Tessallite" because the Excel plugin's CUBE formulas
# reference that name.  Credentials are NEVER included (Excel prompts).
#
# Placeholders: {xmla_url}, {catalog_xml}, {catalog_attr}
# {catalog_xml}  is HTML-entity-escaped for the XML text node.
# {catalog_attr} is HTML-attribute-escaped for the HTML attribute value.

_ODC_TEMPLATE = """\
<html xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns="http://www.w3.org/TR/REC-html40">
<head>
<meta http-equiv="Content-Type" content="text/x-ms-odc; charset=utf-8">
<meta name="ProgId" content="ODC.Database">
<meta name="SourceType" content="OLEDB">
<title>Tessallite</title>
<xml id="docprops"></xml>
<xml id="msodc">
 <odc:OfficeDataConnection
  xmlns:odc="urn:schemas-microsoft-com:office:odc"
  xmlns="http://www.w3.org/TR/REC-html40">
  <odc:Connection odc:Type="OLEDB">
   <odc:ConnectionString>Provider=MSOLAP.8;Data Source={xmla_url};Initial Catalog={catalog_xml};Persist Security Info=False</odc:ConnectionString>
   <odc:CommandType>Cube</odc:CommandType>
   <odc:CommandText>{catalog_xml}</odc:CommandText>
  </odc:Connection>
 </odc:OfficeDataConnection>
</xml>
</head>
<body>
 <object type="text/x-ms-odc" classid="clsid:00000535-0000-0010-8000-00AA006D2EA4"
  odc:ConnectionString="Provider=MSOLAP.8;Data Source={xmla_url};Initial Catalog={catalog_attr};Persist Security Info=False"
  odc:CommandType="Cube"
  odc:CommandText="{catalog_attr}">
  <param name="Name" value="Tessallite">
 </object>
</body>
</html>
"""

# Filename-safe character filter: keep alphanumerics, hyphens, underscores.
_FILENAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _safe_filename(slug: str) -> str:
    """Sanitise a model slug for use in a Content-Disposition filename."""
    cleaned = _FILENAME_UNSAFE.sub("_", slug)
    return cleaned or "model"


def render_odc(xmla_base_url: str, catalog: str) -> str:
    """Render the .odc file content for the given XMLA URL and catalog name.

    The XMLA URL has the tenantless ``/api/v1/xmla/`` path appended
    (the gateway resolves the tenant from the login credentials).
    The catalog is HTML-entity-escaped for safe embedding inside XML
    text nodes and HTML attribute values.
    """
    xmla_url = xmla_base_url.rstrip("/") + "/api/v1/xmla/"
    # Escape the URL for safe embedding in XML/HTML (defense-in-depth:
    # the value is operator-controlled, but URLs can contain & characters).
    xmla_url = html.escape(xmla_url, quote=True)
    # Escape for XML text nodes (covers &, <, >, ", ')
    catalog_xml = html.escape(catalog, quote=True)
    # For HTML attributes we use the same escaping (html.escape covers
    # the required characters for double-quoted attribute values).
    catalog_attr = html.escape(catalog, quote=True)
    return _ODC_TEMPLATE.format(
        xmla_url=xmla_url,
        catalog_xml=catalog_xml,
        catalog_attr=catalog_attr,
    )


@router.get(
    "/odc",
    dependencies=[require_role("viewer")],
)
async def download_odc(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> Response:
    """Generate and return an Office Data Connection (.odc) file.

    The model must exist in the project and be deployed (have a
    deployed_version_id). Returns 404 if the model does not exist or
    does not belong to the project, and 409 if the model is not deployed.
    """
    settings = get_settings()
    xmla_base_url = settings.GATEWAY_XMLA_PUBLIC_URL

    async for db in get_tenant_db(current_user.tenant_id):
        model = await db.get(Model, model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Model not found in this project.",
            )
        if model.deployed_version_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Deploy the model before downloading the connection file.",
            )

        catalog = model.slug
        content = render_odc(xmla_base_url, catalog)
        filename = f"tessallite-{_safe_filename(catalog)}.odc"

        return Response(
            content=content,
            media_type="text/x-ms-odc",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
