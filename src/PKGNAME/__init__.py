#!/usr/bin/env python3
"""TODO: one-line package description."""

from importlib.metadata import version, metadata
from pathlib import Path

# Package metadata — read from pyproject.toml at install time so there is
# exactly one place to update the version / author.
__project__ = __name__
__version__ = version(__project__)
__author_email__ = metadata(__project__)["Author-email"]

# Useful for locating data files bundled alongside the source.
g_pathCode = Path(__file__).parent
