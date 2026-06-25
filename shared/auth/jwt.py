"""JWT decode helper used across every Tessallite service.

Token issuance lives in model-service (`src.auth.local_backend`) because
only that service talks to bcrypt and the local_users table. This module
holds the inverse operation — decoding a token that was issued elsewhere —
so internal services can verify incoming JWTs without pulling in
bcrypt/passlib.
"""
from __future__ import annotations

from jose import jwt

from shared.config.settings import get_settings

_settings = get_settings()


def decode_access_token(token: str) -> dict:
    """Verify the signature and return the JWT payload.

    Raises `jose.JWTError` on any failure (expired, bad signature,
    wrong algorithm, unexpected audience). Callers wrap this in an
    HTTPException 401 at the FastAPI dependency layer.

    Regular access tokens carry no ``aud`` claim. Embed tokens carry
    ``aud=embed``. Any other audience value is rejected.
    """
    payload = jwt.decode(
        token,
        _settings.JWT_SECRET_KEY,
        algorithms=[_settings.JWT_ALGORITHM],
        options={"verify_aud": False},
    )
    aud = payload.get("aud")
    if aud is not None:
        from jose import JWTError
        if isinstance(aud, list) or aud != "embed":
            raise JWTError(f"Unexpected audience: {aud}")
    return payload
