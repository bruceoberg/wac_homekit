"""The pure parts of the accessory layer.

Tier selection, AID derivation, and the firmware-string guard need no driver
and no hardware. The accessory itself does — it is verified against real
devices, the same way the device layer is.
"""

from __future__ import annotations  # Forward refs without quotes

import pytest

from wac_iot import FIXTUREK

from wac_homekit.accessory import (
	AID_MAX,
	AID_MIN,
	LIGHTTIER,
	NAidFromFixtureId,
	StrTryFirmware,
	TierTryFromFixturek,
)


# ---------------------------------------------------------------------------
# Tier selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
	"fixturek,tier",
	[
		(FIXTUREK.SingleColor,    LIGHTTIER.Dimmable),
		(FIXTUREK.Elv,            LIGHTTIER.Dimmable),
		(FIXTUREK.TunableWhite,   LIGHTTIER.White),
		(FIXTUREK.Controller24V,  LIGHTTIER.White),
		(FIXTUREK.DecorativeLow,  LIGHTTIER.White),
		(FIXTUREK.DecorativeHigh, LIGHTTIER.White),
		(FIXTUREK.Rgbw,           LIGHTTIER.Rgbw),
	],
)
def test_light_types_get_a_tier(fixturek: FIXTUREK, tier: LIGHTTIER) -> None:
	assert TierTryFromFixturek(fixturek) is tier


@pytest.mark.parametrize(
	"fixturek",
	[
		FIXTUREK.MotorizedTrackhead,
		FIXTUREK.WallStation,
		FIXTUREK.Fan,
		FIXTUREK.Unknown,
	],
)
def test_non_lights_get_no_tier(fixturek: FIXTUREK) -> None:
	assert TierTryFromFixturek(fixturek) is None


def test_every_fixture_type_is_decided() -> None:
	# A new FIXTUREK member must be a deliberate in-or-out call, not an
	# accident of which dict someone remembered to update.

	for fixturek in FIXTUREK:
		tier = TierTryFromFixturek(fixturek)

		assert tier is None or isinstance(tier, LIGHTTIER)


# ---------------------------------------------------------------------------
# Accessory IDs
# ---------------------------------------------------------------------------


def test_aid_is_stable_for_the_same_fixture() -> None:
	# The whole point: iOS remembers which accessory it paired with by AID.

	assert NAidFromFixtureId("aabbccddeeff_09fffffd") == NAidFromFixtureId("aabbccddeeff_09fffffd")


def test_aid_differs_between_fixtures() -> None:
	setAid = {
		NAidFromFixtureId(f"aabbccddeeff_{iAddr:08x}")
		for iAddr in range(1000)
	}

	assert len(setAid) == 1000


def test_aid_avoids_the_reserved_values() -> None:
	# 1 is the bridge itself, and HAP-python documents 7 as unusable.

	for iAddr in range(2000):
		nAid = NAidFromFixtureId(f"aabbccddeeff_{iAddr:08x}")

		assert AID_MIN <= nAid < AID_MAX
		assert nAid not in (1, 7)


# ---------------------------------------------------------------------------
# Firmware revision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strVer", ["1", "1.40", "01.04.0149"])
def test_firmware_accepts_dotted_numbers(strVer: str) -> None:
	assert StrTryFirmware(strVer) == strVer


@pytest.mark.parametrize("strVer", [None, "", "v1.40", "1.40-beta", "1.2.3.4", "gnipacsroloC"])
def test_firmware_rejects_anything_else(strVer: str | None) -> None:
	assert StrTryFirmware(strVer) is None
