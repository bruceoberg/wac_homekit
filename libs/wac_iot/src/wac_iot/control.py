#!/usr/bin/env python3
"""Building a fixture's control state, in device units.

`CFixtures.ObjControl` takes whatever dict it is handed. That pushes the
wire format onto every consumer, so the bridge and a Home Assistant
integration would each rediscover the same rules — which fields may travel
together, what a level's upper bound is — and each get them subtly wrong.
The builders here are the one place that knows.

Pure and I/O-free, which also makes them the part of the control path worth
testing: the arithmetic is the consumer's job, but the range checks and the
mutually-exclusive groupings are ours.

Values stay in device units. Converting from a platform's ranges is the
consumer's job, deliberately — see the repo's CLAUDE.md.

Nothing here has been exercised against real hardware; action 4 is still
unwritten territory. The exclusions below are the conservative reading, and
are meant to be relaxed once a device says otherwise rather than worked
around at the call site.
"""

from __future__ import annotations  # Forward refs without quotes

from typing import Any

from .errors import WacValueError
from .models import LIGHTMODE

# Device-unit bounds. Exported because a consumer converting into these
# ranges needs them by name — hardcoding 10000 in a brightness conversion is
# exactly the sort of thing that goes wrong quietly.

LEVEL_MIN = 0
LEVEL_MAX = 10000           # brightness, in 0.01% steps

HUE_MIN = 0
HUE_MAX = 10000

SATURATION_MIN = 0
SATURATION_MAX = 10000

COLOR_TEMP_LEVEL_MIN = 1    # stepped white index
COLOR_TEMP_LEVEL_MAX = 7

FAN_SPEED_MIN = 1           # gears, not a percentage
FAN_SPEED_MAX = 6


def _CheckRange(strField: str, n: int, nMin: int, nMax: int) -> None:
	"""Reject a value the device would reject, before spending a request."""

	if not nMin <= n <= nMax:
		raise WacValueError(f"{strField} must be between {nMin} and {nMax}, got {n}")


def _CheckNotEmpty(obj: dict[str, Any]) -> dict[str, Any]:
	"""A control request with no fields set would be a wasted round trip."""

	if not obj:
		raise WacValueError("no control fields set")

	return obj


def _ObjLightFields(
	*,
	fOn: bool | None = None,
	nLevel: int | None = None,
	fFindme: bool | None = None,
	lightmode: LIGHTMODE | None = None,
) -> dict[str, Any]:
	"""The fields every light shares.

	Separate from `ObjStateLight` so the richer builders can start from it
	without tripping the empty check before they add their own fields.
	"""

	obj: dict[str, Any] = {}

	if fOn is not None:
		obj["status"] = fOn

	if nLevel is not None:
		_CheckRange("level", nLevel, LEVEL_MIN, LEVEL_MAX)
		obj["level"] = nLevel

	if fFindme is not None:
		obj["findme"] = fFindme

	if lightmode is not None:
		obj["mode"] = int(lightmode)

	return obj


def ObjStateLight(
	*,
	fOn: bool | None = None,
	nLevel: int | None = None,
	fFindme: bool | None = None,
	lightmode: LIGHTMODE | None = None,
) -> dict[str, Any]:
	"""Control state for a fixture that dims but does not change color.

	Single color (0) and ELV (6).
	"""

	return _CheckNotEmpty(
		_ObjLightFields(fOn=fOn, nLevel=nLevel, fFindme=fFindme, lightmode=lightmode)
	)


def ObjStateWhite(
	*,
	fOn: bool | None = None,
	nLevel: int | None = None,
	fFindme: bool | None = None,
	lightmode: LIGHTMODE | None = None,
	nColorTempLevel: int | None = None,
	nColorTemp: int | None = None,
) -> dict[str, Any]:
	"""Control state for a tunable white fixture (1, 12, 14, 15).

	`nColorTempLevel` is the stepped index; `nColorTemp` is absolute Kelvin.
	The device takes one or the other, never both in a single request.

	Kelvin is not range-checked here: the usable span is per-fixture and
	arrives in that fixture's own `detail` as minColorTemp / maxColorTemp.
	Clamp against those, not against a constant.
	"""

	if nColorTempLevel is not None and nColorTemp is not None:
		raise WacValueError("colorTempLevel and mixColorTemp are mutually exclusive")

	obj = _ObjLightFields(fOn=fOn, nLevel=nLevel, fFindme=fFindme, lightmode=lightmode)

	if nColorTempLevel is not None:
		_CheckRange("colorTempLevel", nColorTempLevel, COLOR_TEMP_LEVEL_MIN, COLOR_TEMP_LEVEL_MAX)
		obj["colorTempLevel"] = nColorTempLevel

	if nColorTemp is not None:
		obj["mixColorTemp"] = nColorTemp

	return _CheckNotEmpty(obj)


def ObjStateRgbw(
	*,
	fOn: bool | None = None,
	nLevel: int | None = None,
	fFindme: bool | None = None,
	lightmode: LIGHTMODE | None = None,
	nHue: int | None = None,
	nSaturation: int | None = None,
	tplRgb: tuple[int, int, int] | None = None,
	nColorTemp: int | None = None,
) -> dict[str, Any]:
	"""Control state for an RGBW fixture (2).

	Color can be addressed three ways — HSV, RGB, or a white point — and
	they describe different modes of the same output. Sending more than one
	in a request leaves it to the firmware to decide which wins, so this
	refuses instead.

	RGB components are passed through unchecked: their range is not
	something hardware has confirmed, and inventing a bound would be worse
	than none. Hue and saturation are checked; those are known.
	"""

	cColorWay = sum(
		1 for fSet in (
			nHue is not None or nSaturation is not None,
			tplRgb is not None,
			nColorTemp is not None,
		) if fSet
	)

	if cColorWay > 1:
		raise WacValueError("set at most one of hue/saturation, rgb, or mixColorTemp")

	obj = _ObjLightFields(fOn=fOn, nLevel=nLevel, fFindme=fFindme, lightmode=lightmode)

	if nHue is not None:
		_CheckRange("hue", nHue, HUE_MIN, HUE_MAX)
		obj["hue"] = nHue

	if nSaturation is not None:
		_CheckRange("saturation", nSaturation, SATURATION_MIN, SATURATION_MAX)
		obj["saturation"] = nSaturation

	if tplRgb is not None:
		obj["red"], obj["green"], obj["blue"] = tplRgb

	if nColorTemp is not None:
		obj["mixColorTemp"] = nColorTemp

	return _CheckNotEmpty(obj)


def ObjStateFan(
	*,
	fOn: bool | None = None,
	nFanSpeed: int | None = None,
	fWind: bool | None = None,
	nWindSpeed: int | None = None,
	fDirection: bool | None = None,
	fFindme: bool | None = None,
) -> dict[str, Any]:
	"""Control state for a fan (13).

	Speed is a gear, not a percentage — a consumer mapping a 0-100 control
	onto it has to quantize, and quantizing badly is what makes a slider
	jump under the user's finger.
	"""

	obj: dict[str, Any] = {}

	if fOn is not None:
		obj["status"] = fOn

	if nFanSpeed is not None:
		_CheckRange("fanSpeed", nFanSpeed, FAN_SPEED_MIN, FAN_SPEED_MAX)
		obj["fanSpeed"] = nFanSpeed

	if fWind is not None:
		obj["wind"] = fWind

	if nWindSpeed is not None:
		_CheckRange("windSpeed", nWindSpeed, FAN_SPEED_MIN, FAN_SPEED_MAX)
		obj["windSpeed"] = nWindSpeed

	if fDirection is not None:
		obj["fanDirection"] = fDirection

	if fFindme is not None:
		obj["findme"] = fFindme

	return _CheckNotEmpty(obj)
