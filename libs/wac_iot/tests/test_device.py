"""What a device says about itself, and the one thing acted on before asking
for anything else.

`FIsFixtureHost` exists because discovery cannot tell an InvisiLED wall
station from a ColorScaping transformer — identical service, protocol and
protocol version — and the wall station 404s every endpoint but `/device`.
"""

from __future__ import annotations  # Forward refs without quotes

from typing import Any

import pytest

from wac_iot import SDeviceInfo


def DeviMake(**kwargs: Any) -> SDeviceInfo:
	return SDeviceInfo.model_validate(kwargs)


class TestFIsFixtureHost:
	@pytest.mark.parametrize(
		"strSystemType",
		["invisiLED_Wall", "invisiled_wall", "INVISILED_WALL", "  invisiLED_Wall  "],
	)
	def test_a_wall_station_hosts_none(self, strSystemType: str) -> None:
		"""Case is not something the firmware has promised to keep."""

		assert not DeviMake(systemType=strSystemType).FIsFixtureHost()

	def test_a_transformer_hosts_fixtures(self) -> None:
		assert DeviMake(systemType="colorscaping", deviceName="hub").FIsFixtureHost()

	@pytest.mark.parametrize("strSystemType", ["strut", "gen3fan", "somethingNewWacShipped"])
	def test_an_unknown_system_type_is_a_yes(self, strSystemType: str) -> None:
		"""A denylist, not an allowlist: the documented enumeration is already
		incomplete, so an allowlist would drop the next product silently."""

		assert DeviMake(systemType=strSystemType).FIsFixtureHost()

	def test_an_absent_system_type_is_a_yes(self) -> None:
		assert DeviMake(deviceName="hub").FIsFixtureHost()

	@pytest.mark.parametrize("anySystemType", [1, 0, None, ["invisiLED_Wall"], {}])
	def test_a_non_string_answers_rather_than_raising(self, anySystemType: Any) -> None:
		"""systemType is documented as a number and observed as a string, so
		the one thing this must not do is blow up a poll over the type."""

		assert DeviMake(systemType=anySystemType).FIsFixtureHost()
