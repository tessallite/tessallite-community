"""Shared JWT auth helpers used by every Tessallite service.

Token issuance (create_access_token) and password hashing live in
model-service because only that service owns user credentials and
issues tokens. Everything else — decoding tokens and the FastAPI
dependency wrappers that verify them — lives here so query-router,
scheduler, optimizer, and gateway can import a single canonical
implementation.
"""
