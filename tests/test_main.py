"""Smoke tests for wac_homekit."""

from __future__ import annotations  # Forward refs without quotes

import argparse

import pytest

from wac_homekit import __version__
from wac_homekit.main import StrPincode


def test_version_defined() -> None:
	"""Package version should be a non-empty string."""
	assert isinstance(__version__, str)
	assert len(__version__) > 0


# Every grouping a user might plausibly type, including the two that matter:
# 3-2-3 as printed on an accessory label, and bare digits as the Home app's
# manual entry asks for them.


@pytest.mark.parametrize(
	"strArg",
	[
		"426-83-591",
		"42683591",
		"4268 3591",
		"426 83 591",
		"4-2-6-8-3-5-9-1",
		"  42683591  ",
	],
)
def test_pincode_normalizes_to_hyphenated(strArg: str) -> None:
	"""Any grouping of the eight digits becomes the 3-2-3 form SRP hashes."""
	assert StrPincode(strArg) == "426-83-591"


def test_pincode_preserves_digit_order() -> None:
	"""The slice boundaries must not transpose anything."""
	assert StrPincode("12345678") == "123-45-678"


@pytest.mark.parametrize(
	"strArg",
	[
		"",
		"4268359",  # seven
		"426835911",  # nine
		"426-83-59a",  # not a digit
		"426.83.591",  # separator we deliberately do not strip
	],
)
def test_pincode_rejects_bad_input(strArg: str) -> None:
	"""Anything that is not exactly eight digits is refused at the CLI."""
	with pytest.raises(argparse.ArgumentTypeError):
		StrPincode(strArg)
