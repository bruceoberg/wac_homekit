"""Control state builders — ranges and mutually-exclusive groupings.

Nothing here has been written to real hardware yet. These tests pin the
rules the builders enforce, so relaxing one later is a deliberate edit
rather than a silent drift.
"""

from __future__ import annotations  # Forward refs without quotes

import pytest

from wac_iot import (
	LEVEL_MAX,
	LIGHTMODE,
	ObjStateFan,
	ObjStateLight,
	ObjStateRgbw,
	ObjStateWhite,
	WacError,
	WacValueError,
)


class TestObjStateLight:
	def test_uses_device_field_names(self) -> None:
		"""`fOn` is `status` on the wire, not `on`."""

		assert ObjStateLight(fOn=True, nLevel=5000) == {"status": True, "level": 5000}

	def test_omits_what_was_not_asked_for(self) -> None:
		"""A partial control must not carry fields the caller never set."""

		assert ObjStateLight(nLevel=0) == {"level": 0}

	def test_level_bounds_are_inclusive(self) -> None:
		assert ObjStateLight(nLevel=0)["level"] == 0
		assert ObjStateLight(nLevel=LEVEL_MAX)["level"] == LEVEL_MAX

	@pytest.mark.parametrize("nLevel", [-1, LEVEL_MAX + 1, 100000])
	def test_rejects_level_out_of_range(self, nLevel: int) -> None:
		with pytest.raises(WacValueError):
			ObjStateLight(nLevel=nLevel)

	def test_rejects_empty(self) -> None:
		with pytest.raises(WacValueError):
			ObjStateLight()

	def test_mode_travels_as_its_number(self) -> None:
		assert ObjStateLight(lightmode=LIGHTMODE.Hsv)["mode"] == int(LIGHTMODE.Hsv)


class TestObjStateWhite:
	def test_stepped_index(self) -> None:
		assert ObjStateWhite(nColorTempLevel=4) == {"colorTempLevel": 4}

	def test_absolute_kelvin(self) -> None:
		assert ObjStateWhite(nColorTemp=2700) == {"mixColorTemp": 2700}

	def test_refuses_both_color_temp_forms(self) -> None:
		"""The device takes one or the other, never both."""

		with pytest.raises(WacValueError):
			ObjStateWhite(nColorTempLevel=4, nColorTemp=2700)

	@pytest.mark.parametrize("nStep", [0, 8, -1])
	def test_rejects_step_out_of_range(self, nStep: int) -> None:
		with pytest.raises(WacValueError):
			ObjStateWhite(nColorTempLevel=nStep)

	def test_kelvin_is_not_range_checked(self) -> None:
		"""The usable span is per-fixture, so a constant bound would be wrong."""

		assert ObjStateWhite(nColorTemp=1)["mixColorTemp"] == 1
		assert ObjStateWhite(nColorTemp=99999)["mixColorTemp"] == 99999

	def test_carries_the_shared_light_fields(self) -> None:
		obj = ObjStateWhite(fOn=True, nLevel=1234, nColorTemp=3000)

		assert obj == {"status": True, "level": 1234, "mixColorTemp": 3000}


class TestObjStateRgbw:
	def test_hsv(self) -> None:
		assert ObjStateRgbw(nHue=5000, nSaturation=10000) == {
			"hue": 5000,
			"saturation": 10000,
		}

	def test_rgb_expands_to_three_fields(self) -> None:
		assert ObjStateRgbw(tplRgb=(1, 2, 3)) == {"red": 1, "green": 2, "blue": 3}

	@pytest.mark.parametrize(
		"mpKwargs",
		[
			{"nHue": 1, "tplRgb": (1, 2, 3)},
			{"nHue": 1, "nColorTemp": 2700},
			{"tplRgb": (1, 2, 3), "nColorTemp": 2700},
			{"nSaturation": 1, "tplRgb": (1, 2, 3)},
		],
	)
	def test_refuses_mixed_color_ways(self, mpKwargs: dict[str, object]) -> None:
		"""HSV, RGB and a white point describe different modes of one output."""

		with pytest.raises(WacValueError):
			ObjStateRgbw(**mpKwargs)  # type: ignore[arg-type]

	def test_hue_and_saturation_together_are_one_way(self) -> None:
		"""Both halves of HSV must not count as two conflicting groups."""

		assert ObjStateRgbw(nHue=0, nSaturation=0) == {"hue": 0, "saturation": 0}

	@pytest.mark.parametrize("nHue", [-1, 10001])
	def test_rejects_hue_out_of_range(self, nHue: int) -> None:
		with pytest.raises(WacValueError):
			ObjStateRgbw(nHue=nHue)

	def test_rgb_components_pass_through_unchecked(self) -> None:
		"""Their range is unconfirmed; inventing a bound would be worse."""

		assert ObjStateRgbw(tplRgb=(-5, 999, 0))["red"] == -5


class TestObjStateFan:
	def test_gears(self) -> None:
		assert ObjStateFan(fOn=True, nFanSpeed=6) == {"status": True, "fanSpeed": 6}

	@pytest.mark.parametrize("nSpeed", [0, 7, -1])
	def test_rejects_speed_out_of_range(self, nSpeed: int) -> None:
		"""Speed is a gear from 1, not a percentage from 0."""

		with pytest.raises(WacValueError):
			ObjStateFan(nFanSpeed=nSpeed)

	def test_rejects_empty(self) -> None:
		with pytest.raises(WacValueError):
			ObjStateFan()


def test_value_error_is_catchable_as_wac_error() -> None:
	"""The documented promise: one except clause catches everything."""

	with pytest.raises(WacError):
		ObjStateLight(nLevel=-1)
