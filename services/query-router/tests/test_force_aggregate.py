"""Tests for force_route=aggregate/pocket routing."""
import pytest
from src.api.routes import _validate_force_route


def test_force_source_accepted():
    _validate_force_route("source")


def test_force_aggregate_accepted():
    _validate_force_route("aggregate")


def test_force_pocket_accepted():
    _validate_force_route("pocket")


def test_invalid_force_route_raises():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _validate_force_route("unknown_route")


def test_none_force_route_accepted():
    _validate_force_route(None)
