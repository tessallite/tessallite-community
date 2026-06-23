"""Shared semantic-layer helpers.

Modules here are consumed by both `services/model-service` (creation-time
resolution) and `services/scheduler` (refresh-time DDL generation) and
`services/query-router` (rewrite-time column lookup). Keep this package
free of service-specific imports.
"""
