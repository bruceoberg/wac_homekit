"""Fixture type resolution.

Mostly about the unknown-type path: a poll loop rebuilds every fixture on
every tick, so anything logged here is logged forever.
"""

from __future__ import annotations  # Forward refs without quotes

import logging

from collections.abc import Iterator

import pytest

from wac_iot import FIXTUREK, CFixture
from wac_iot import models


@pytest.fixture(autouse=True)
def clear_seen() -> Iterator[None]:
	"""The warned-about set is process-global, so tests must not inherit it."""

	models.g_setNTypeUnknownSeen.clear()

	yield

	models.g_setNTypeUnknownSeen.clear()


class TestUnknownTypes:
	def test_unknown_type_resolves_instead_of_raising(self) -> None:
		fixture = CFixture({"addr": 1, "type": 4})

		assert fixture.fixturek is FIXTUREK.Unknown
		assert fixture.nType == 4
		assert not fixture.FIsKnown()

	def test_unknown_type_warns_once_not_once_per_fixture(
		self, caplog: pytest.LogCaptureFixture
	) -> None:
		# The type-4 pseudo-fixture is permanent on ColorScaping hardware, so
		# a 5s poll would otherwise log this ~17,000 times a day.

		with caplog.at_level(logging.WARNING, logger="wac_iot.models"):
			for _ in range(100):
				CFixture({"addr": 1, "type": 4})

		assert len(caplog.records) == 1
		assert "4" in caplog.records[0].getMessage()

	def test_each_distinct_unknown_type_warns_once(
		self, caplog: pytest.LogCaptureFixture
	) -> None:
		# Saying it once must not mean saying it once ever — a genuinely new
		# type from later firmware still deserves its own warning.

		with caplog.at_level(logging.WARNING, logger="wac_iot.models"):
			for nType in (4, 4, 99, 99, 4, 100):
				CFixture({"addr": 1, "type": nType})

		assert len(caplog.records) == 3

	def test_missing_type_is_unknown(self) -> None:
		assert CFixture({"addr": 1}).fixturek is FIXTUREK.Unknown


class TestKnownTypes:
	@pytest.mark.parametrize(
		"nType,fixturek",
		[
			(0, FIXTUREK.SingleColor),
			(2, FIXTUREK.Rgbw),
			(6, FIXTUREK.Elv),
			(13, FIXTUREK.Fan),
		],
	)
	def test_known_types_resolve_quietly(
		self, nType: int, fixturek: FIXTUREK, caplog: pytest.LogCaptureFixture
	) -> None:
		with caplog.at_level(logging.WARNING, logger="wac_iot.models"):
			fixture = CFixture({"addr": 1, "type": nType})

		assert fixture.fixturek is fixturek
		assert fixture.FIsKnown()
		assert not caplog.records
