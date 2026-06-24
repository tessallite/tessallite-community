"""SAML 2.0 Service Provider backend.

Provides SP metadata generation, AuthnRequest initiation (redirect to IdP),
and Assertion Consumer Service (ACS) processing.  Uses python3-saml
(OneLogin) under the hood.
"""
from __future__ import annotations

import logging
from typing import Any

from shared.auth.backend import UserIdentity
from shared.config.settings import get_settings

logger = logging.getLogger(__name__)


def _get_saml_settings(base_url: str) -> dict[str, Any] | None:
    """Build python3-saml settings dict from bootstrap config."""
    settings = get_settings()
    if not settings.SAML_IDP_METADATA_URL and not settings.SAML_IDP_METADATA_XML:
        return None

    sp_entity_id = settings.SAML_SP_ENTITY_ID or f"{base_url}/api/v1/auth/saml/metadata"
    acs_url = f"{base_url}/api/v1/auth/saml/acs"

    saml_cfg: dict[str, Any] = {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": sp_entity_id,
            "assertionConsumerService": {
                "url": acs_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:2.0:nameid-format:emailAddress",
        },
        "security": {
            "authnRequestsSigned": False,
            "wantAssertionsSigned": True,
            "wantNameIdEncrypted": False,
        },
    }

    if settings.SAML_IDP_METADATA_URL:
        saml_cfg["idp_metadata_url"] = settings.SAML_IDP_METADATA_URL
    elif settings.SAML_IDP_METADATA_XML:
        from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
        idp_data = OneLogin_Saml2_IdPMetadataParser.parse(settings.SAML_IDP_METADATA_XML)
        saml_cfg.update(idp_data)

    return saml_cfg


def get_sp_metadata(base_url: str) -> str | None:
    """Generate SP metadata XML for the IdP to consume."""
    saml_cfg = _get_saml_settings(base_url)
    if saml_cfg is None:
        return None
    try:
        from onelogin.saml2.settings import OneLogin_Saml2_Settings
        sp_settings = OneLogin_Saml2_Settings(saml_cfg, custom_base_path=None)
        metadata = sp_settings.get_sp_metadata()
        errors = sp_settings.validate_metadata(metadata)
        if errors:
            logger.warning("SAML SP metadata validation errors: %s", errors)
        return metadata.decode() if isinstance(metadata, bytes) else metadata
    except Exception:
        logger.exception("Failed to generate SAML SP metadata")
        return None


def build_authn_request(base_url: str, relay_state: str | None = None) -> str | None:
    """Build SAML AuthnRequest and return the redirect URL."""
    saml_cfg = _get_saml_settings(base_url)
    if saml_cfg is None:
        return None
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        request_data = {
            "https": "on" if base_url.startswith("https") else "off",
            "http_host": base_url.split("//", 1)[1].split("/", 1)[0],
            "script_name": "/api/v1/auth/saml/acs",
            "get_data": {},
            "post_data": {},
        }
        auth = OneLogin_Saml2_Auth(request_data, saml_cfg)
        return auth.login(return_to=relay_state)
    except Exception:
        logger.exception("Failed to build SAML AuthnRequest")
        return None


def process_saml_response(
    base_url: str,
    saml_response: str,
    relay_state: str | None = None,
) -> UserIdentity | None:
    """Parse and validate a SAML response from the IdP ACS POST.

    Returns a UserIdentity on success or None on failure.
    """
    settings_obj = get_settings()
    saml_cfg = _get_saml_settings(base_url)
    if saml_cfg is None:
        return None

    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        request_data = {
            "https": "on" if base_url.startswith("https") else "off",
            "http_host": base_url.split("//", 1)[1].split("/", 1)[0],
            "script_name": "/api/v1/auth/saml/acs",
            "get_data": {},
            "post_data": {"SAMLResponse": saml_response},
        }
        if relay_state:
            request_data["post_data"]["RelayState"] = relay_state

        auth = OneLogin_Saml2_Auth(request_data, saml_cfg)
        auth.process_response()

        if auth.get_errors():
            logger.warning("SAML response errors: %s — reason: %s",
                           auth.get_errors(), auth.get_last_error_reason())
            return None

        if not auth.is_authenticated():
            logger.warning("SAML response: user not authenticated")
            return None

        attrs = auth.get_attributes()
        email_attr = settings_obj.SAML_ATTR_EMAIL
        display_attr = settings_obj.SAML_ATTR_DISPLAY_NAME
        groups_attr = settings_obj.SAML_ATTR_GROUPS

        email = attrs.get(email_attr, [None])[0] or auth.get_nameid()
        if not email:
            logger.warning("SAML response: no email found in attributes or NameID")
            return None

        display_name = (attrs.get(display_attr, [""]) or [""])[0]
        groups = attrs.get(groups_attr, [])

        return UserIdentity(
            email=email,
            display_name=display_name,
            groups=groups,
            source_backend="saml",
            raw_claims=dict(attrs),
        )
    except Exception:
        logger.exception("Failed to process SAML response")
        return None
