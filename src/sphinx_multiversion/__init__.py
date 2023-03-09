# -*- coding: utf-8 -*-
"""Command line interface for building multiversion sphinx documentation"""

from .main import main
from .sphinx import setup

__version__ = "0.2.13"

__all__ = [
    "setup",
    "main",
]
