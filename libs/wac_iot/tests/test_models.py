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

	models.g_setUnknownSeen.clear()

	yield

	models.g_setUnknownSeen.clear()


# 99 stands in for a fixture type this library has never heard of. It used to
# be 4, which is now a named member — see FIXTUREK.Pseudo.

NTYPE_UNMODELED = 99


class TestUnknownTypes:
	def test_unknown_type_resolves_instead_of_raising(self) -> None:
		fixture = CFixture({"addr": 1, "type": NTYPE_UNMODELED})

		assert fixture.fixturek is FIXTUREK.Unknown
		assert fixture.nType == NTYPE_UNMODELED
		assert not fixture.FIsKnown()

	def test_unknown_type_warns_once_not_once_per_fixture(
		self, caplog: pytest.LogCaptureFixture
	) -> None:
		# An unmodeled type on a track is permanent until someone reflashes
		# something, so a 5s poll would otherwise log this ~17,000 times a
		# day.

		with caplog.at_level(logging.WARNING, logger="wac_iot.models"):
			for _ in range(100):
				CFixture({"addr": 1, "type": NTYPE_UNMODELED})

		assert len(caplog.records) == 1
		assert str(NTYPE_UNMODELED) in caplog.records[0].getMessage()

	def test_each_distinct_unknown_type_warns_once(
		self, caplog: pytest.LogCaptureFixture
	) -> None:
		# Saying it once must not mean saying it once ever — a genuinely new
		# type from later firmware still deserves its own warning.

		with caplog.at_level(logging.WARNING, logger="wac_iot.models"):
			for nType in (99, 99, 100, 100, 99, 101):
				CFixture({"addr": 1, "type": nType})

		assert len(caplog.records) == 3

	def test_unknown_type_names_the_device_it_came_from(
		self, caplog: pytest.LogCaptureFixture
	) -> None:
		# "fixture type 99" on its own does not say which transformer to go
		# and look at, which is the whole reason the host is carried down.

		with caplog.at_level(logging.WARNING, logger="wac_iot.models"):
			CFixture({"addr": 1, "type": NTYPE_UNMODELED}, "192.0.2.10")

		assert "192.0.2.10" in caplog.records[0].getMessage()

	def test_each_device_warns_about_the_same_unknown_type(
		self, caplog: pytest.LogCaptureFixture
	) -> None:
		# Deduping on the type alone would name the first device and then go
		# quiet about every other one carrying it.

		with caplog.at_level(logging.WARNING, logger="wac_iot.models"):
			for strHost in ("192.0.2.10", "192.0.2.10", "192.0.2.11", "192.0.2.10"):
				CFixture({"addr": 1, "type": NTYPE_UNMODELED}, strHost)

		assert len(caplog.records) == 2

	def test_missing_type_is_unknown(self) -> None:
		assert CFixture({"addr": 1}).fixturek is FIXTUREK.Unknown


class TestPseudoFixture:
	# The ColorScaping transformer's type-4 entry, as a full dump showed it:
	# empty state and tune, and a detail carrying reversed strings and the
	# device's own scmVer.

	OBJ_PSEUDO = {
		"addr": 17044171,
		"type": 4,
		"name": "New Fixture 17044171",
		"state": {},
		"tune": {},
		"detail": {
			"model": "gnipacsroloC",
			"ledDriver": "gnipacsroloC",
			"fwVer": "07.68",
			"factory": 41,
			"pcbVer": "\u0001.\u0001",
		},
	}

	def test_pseudo_fixture_parses_without_warning(
		self, caplog: pytest.LogCaptureFixture
	) -> None:
		# Silence follows from it being a member at all, with no special case
		# in the warning — which is the point of naming it.

		with caplog.at_level(logging.WARNING, logger="wac_iot.models"):
			fixture = CFixture(dict(self.OBJ_PSEUDO), "192.0.2.10")

		assert fixture.fixturek is FIXTUREK.Pseudo
		assert not caplog.records

	def test_pseudo_fixture_keeps_its_raw_detail(self) -> None:
		# Raw passthrough, so extra="allow" has to be carrying the fields —
		# a dump of one is the recourse if the pseudo-fixture reading is ever
		# wrong.

		fixture = CFixture(dict(self.OBJ_PSEUDO))

		assert fixture.detail.model_dump()["model"] == "gnipacsroloC"

	def test_known_and_usable_disagree_about_the_pseudo_fixture(self) -> None:
		fixture = CFixture(dict(self.OBJ_PSEUDO))

		assert fixture.FIsKnown()
		assert not fixture.FIsUsable()

	def test_known_and_usable_agree_about_a_real_fixture(self) -> None:
		fixture = CFixture({"addr": 1, "type": 2})

		assert fixture.fixturek is FIXTUREK.Rgbw
		assert fixture.FIsKnown()
		assert fixture.FIsUsable()

	def test_known_and_usable_agree_about_an_unmodeled_type(self) -> None:
		fixture = CFixture({"addr": 1, "type": NTYPE_UNMODELED})

		assert not fixture.FIsKnown()
		assert not fixture.FIsUsable()


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
