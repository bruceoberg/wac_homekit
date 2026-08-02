"""Address keying and the identifiers a consumer stores permanently.

Synthetic addresses and MACs throughout, so no particular installation is
baked into the suite.
"""

from __future__ import annotations  # Forward refs without quotes

import pytest

from wac_iot import CFixture, CSnapshot, SDeviceInfo, StrNormMac, WacResponseError


def SnapMake(strStaMac: str | None, *lObjFixture: dict[str, object]) -> CSnapshot:
	devi = SDeviceInfo.model_validate({"staMac": strStaMac} if strStaMac else {})

	return CSnapshot(devi, [CFixture(obj) for obj in lObjFixture])


class TestStrNormMac:
	@pytest.mark.parametrize(
		"strMac",
		["AA:BB:CC:09:FF:FD", "aa-bb-cc-09-ff-fd", "AABBCC09FFFD", "aabbcc09fffd"],
	)
	def test_separators_and_case_fold_away(self, strMac: str) -> None:
		"""A firmware that changes separators must not change every unique ID."""

		assert StrNormMac(strMac) == "aabbcc09fffd"


class TestSnapshotKeying:
	def test_keys_fixtures_by_address(self) -> None:
		snap = SnapMake("AA:BB:CC:09:FF:FD", {"addr": 167772157, "type": 2}, {"addr": 5, "type": 0})

		assert set(snap.mpAddrFixture) == {167772157, 5}
		assert snap.FixtureTry(5) is not None

	def test_missing_address_returns_none(self) -> None:
		snap = SnapMake("AABBCC09FFFD", {"addr": 5, "type": 0})

		assert snap.FixtureTry(999) is None

	def test_addressless_fixture_stays_out_of_the_map(self) -> None:
		"""It cannot be addressed or identified, but it is still what arrived.

		Hypothetical: no fixture observed on real hardware has lacked an
		address, the type-4 pseudo-fixture included. This pins the behaviour
		for a response that omits one rather than describing a known device.
		"""

		snap = SnapMake("AABBCC09FFFD", {"type": 0}, {"addr": 5, "type": 0})

		assert set(snap.mpAddrFixture) == {5}
		assert len(snap.lFixture) == 2


class TestSnapshotKnownFilter:
	def test_unmodeled_type_is_excluded(self) -> None:
		"""Observed on hardware: type 4 is addressable but carries empty state."""

		snap = SnapMake(
			"AABBCC09FFFD",
			{"addr": 17044171, "type": 4, "name": "New Fixture 17044171", "state": {}},
			{"addr": 100869120, "type": 2, "name": "sky"},
		)

		assert set(snap.mpAddrFixture) == {17044171, 100869120}
		assert set(snap.mpAddrFixtureKnown) == {100869120}

	def test_known_types_all_survive(self) -> None:
		"""Single color, RGBW and ELV are the three seen on real hardware."""

		snap = SnapMake(
			"AABBCC09FFFD",
			{"addr": 1, "type": 0},
			{"addr": 2, "type": 2},
			{"addr": 3, "type": 6},
		)

		assert set(snap.mpAddrFixtureKnown) == {1, 2, 3}

	def test_addressless_is_absent_from_both(self) -> None:
		snap = SnapMake("AABBCC09FFFD", {"type": 2})

		assert not snap.mpAddrFixture
		assert not snap.mpAddrFixtureKnown


class TestSnapshotIdentity:
	def test_device_id_is_the_normalized_mac(self) -> None:
		assert SnapMake("AA:BB:CC:09:FF:FD").StrDeviceId() == "aabbcc09fffd"

	def test_fixture_id_is_scoped_by_device(self) -> None:
		"""A fixture address is only unique within its own transformer."""

		snap = SnapMake("AA:BB:CC:09:FF:FD", {"addr": 167772157, "type": 2})

		assert snap.StrFixtureId(167772157) == "aabbcc09fffd_09fffffd"

	def test_fixture_id_survives_a_rename(self) -> None:
		"""Names change; identifiers a consumer stored must not."""

		snapA = SnapMake("AABBCC09FFFD", {"addr": 5, "type": 0, "name": "Kitchen"})
		snapB = SnapMake("AABBCC09FFFD", {"addr": 5, "type": 0, "name": "Hallway"})

		assert snapA.StrFixtureId(5) == snapB.StrFixtureId(5)

	def test_no_mac_raises_rather_than_returning_none(self) -> None:
		"""A device that cannot be identified cannot be registered either."""

		with pytest.raises(WacResponseError):
			SnapMake(None).StrDeviceId()
