"""Smoke tests for wac_homekit."""

from __future__ import annotations  # Forward refs without quotes

import pytest

from wac_homekit import __version__


def test_version_defined() -> None:
	"""Package version should be a non-empty string."""
	assert isinstance(__version__, str)
	assert len(__version__) > 0
