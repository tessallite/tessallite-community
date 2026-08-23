"""Model-service dependency-analysis loader package (Bug-7787).

Builds a pure ``ModelDependencySnapshot`` from the live draft for the impact
engine in ``shared/model_dependency``. Read-only; never mutates and never
touches a source/target database.
"""
from .loader import ModelDependencyLoader

__all__ = ["ModelDependencyLoader"]
