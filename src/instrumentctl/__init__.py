"""Operator CLI for instruments whose daemons run as systemd template units.

An instrument ships an ``instrument.toml`` describing its names and paths, and
calls :func:`run` from its own console script. Nothing here is specific to any
one instrument.
"""
from .cli import run
from .product import Product

__all__ = ["run", "Product"]
