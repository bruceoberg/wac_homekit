#!/usr/bin/env python3
"""Fixture types and the state / tune / detail structures they carry.

Field names here are the device's own JSON names, verbatim — `mixColorTemp`,
`DTWLevel`, `UDEndPointFlag`, odd capitalisation and all. That is deliberate:
what you read off a model is spelled exactly like what came off the wire, so
a dump can be diffed against a raw response without a translation step.

Values are in device units, deliberately untranslated — see the unit
conversion rules in the repo's CLAUDE.md. Every model allows unknown keys
and defaults every field to None: the vendor documentation is known to
disagree with real hardware, and silently dropping a field we were not
told about would defeat the whole point of the `dump` CLI.
"""

from __future__ import annotations  # Forward refs without quotes

import logging

from enum import IntEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

g_log = logging.getLogger(__name__)


class FIXTUREK(IntEnum):  # tag = fixturek — kinds of fixture
	"""Fixture type identifiers.

	The documented IDs are sparse. Anything unrecognized resolves to
	`Unknown` instead of raising, so one unfamiliar fixture on a track
	cannot take down a poll of the whole system.
	"""

	Unknown            = -1
	SingleColor        = 0
	TunableWhite       = 1
	Rgbw               = 2
	MotorizedTrackhead = 3
	Elv                = 6
	WallStation        = 11
	Controller24V      = 12
	Fan                = 13
	DecorativeLow      = 14
	DecorativeHigh     = 15

	@classmethod
	def _missing_(cls, value: object) -> FIXTUREK:
		g_log.warning("unrecognized fixture type %r", value)

		return cls.Unknown


class LIGHTMODE(IntEnum):  # tag = lightmode
	"""Control modes a light can be asked to operate in."""

	Unknown      = -1
	Dimmable     = 0
	TunableWhite = 1
	Rgb          = 2
	Hsv          = 3
	DimToWarm    = 4
	Natural      = 5
	Dynamic      = 6

	@classmethod
	def _missing_(cls, value: object) -> LIGHTMODE:
		g_log.debug("unrecognized light mode %r", value)

		return cls.Unknown


class SWac(BaseModel):  # tag = wac — shared base for every wire structure
	"""Base config for anything parsed straight off the wire.

	`extra="allow"` keeps fields the vendor documentation omits; they show
	up alongside the declared ones, spelled the same way.
	"""

	model_config = ConfigDict(frozen=True, extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# Control state — the `state` object
# ---------------------------------------------------------------------------


class SState(SWac):  # tag = state
	"""Fields common to every fixture's control state."""

	online: bool | None = None


class SStateLight(SState):  # tag = lstate — single color (types 0, 6)
	"""A light that dims but does not change color."""

	status: bool | None           = None
	level: int | None             = None
	mode: LIGHTMODE | None        = None
	colormode: str | None         = None
	findme: bool | None           = None
	requestedMode: LIGHTMODE | None = None
	funEnable: bool | None        = None


class SStateWhite(SStateLight):  # tag = wstate — tunable white (1, 12, 14, 15)
	"""Adds color temperature.

	The device accepts a stepped index or an absolute Kelvin value, never
	both in one request — `fixture.ObjControl` is where that gets enforced.
	"""

	colorTempLevel: int | None = None
	mixColorTemp: int | None   = None
	DTWLevel: int | None       = None


class SStateRgbw(SStateLight):  # tag = rstate — RGBW (2)
	"""Adds full color, addressable as either RGB or HSV."""

	red: int | None          = None
	green: int | None        = None
	blue: int | None         = None
	hue: int | None          = None
	saturation: int | None   = None
	mixColorTemp: int | None = None
	DTWLevel: int | None     = None
	DHModel: int | None      = None
	marqueeType: int | None  = None


class SStateMotor(SStateLight):  # tag = mstate — motorized trackhead (3)
	"""Adds aim and zoom.

	Absolute aim (tilt / pan) and relative aim (axis + durations) are
	mutually exclusive on the wire; sending both makes the device ignore
	the absolute values.
	"""

	zoom: int | None            = None
	axis: int | None            = None
	timeLr: int | None          = None
	timeUd: int | None          = None
	tilt: int | None            = None
	pan: int | None             = None
	preset: int | None          = None
	UDEndPointFlag: int | None  = None
	LREndPointFlag: int | None  = None
	motorRun: bool | None       = None


class SStateWall(SState):  # tag = wlstate — wall station (11)
	"""A wall station reports reachability and nothing else."""


class SStateFan(SState):  # tag = fstate — fan (13)
	"""Fan speed, direction, and the separate wind model."""

	status: bool | None       = None
	fanSpeed: int | None      = None
	wind: bool | None         = None
	windSpeed: int | None     = None
	fanDirection: bool | None = None
	findme: bool | None       = None


# ---------------------------------------------------------------------------
# Configuration — the `tune` object
# ---------------------------------------------------------------------------


class STune(SWac):  # tag = tune
	"""Fine-tuning values common to most fixtures."""

	dimmingCurve: int | None = None
	onRate: int | None       = None
	offRate: int | None      = None


class STuneWhite(STune):  # tag = wtune
	"""Tunable white and 24V controller tuning."""

	dimToWarm: bool | None     = None
	dimMode: int | None        = None
	colorTempCurve: int | None = None


class STuneRgbw(STune):  # tag = rtune
	"""RGBW tuning."""

	dimToWarm: bool | None = None


class SPresetInfo(SWac):  # tag = presi
	"""One stored aim position for a motorized trackhead."""

	position: int | None = None
	level: int | None    = None
	tilt: int | None     = None
	pan: int | None      = None
	zoom: int | None     = None
	status: bool | None  = None


class STuneMotor(STune):  # tag = mtune
	"""Motorized trackhead tuning, including its stored positions."""

	presetInfo: list[SPresetInfo] | None = None
	setPreset: int | None                = None


# ---------------------------------------------------------------------------
# Hardware identity — the `detail` object
# ---------------------------------------------------------------------------


class SDetail(SWac):  # tag = detail
	"""Manufacturing and firmware identity reported by a fixture."""

	dateCode: str | None      = None
	factory: int | None       = None
	model: str | None         = None
	ledDriver: str | None     = None
	currentOutput: int | None = None
	currentLevel: int | None  = None
	schemVer: str | None      = None
	pcbVer: str | None        = None
	fwVer: str | None         = None
	busVer: str | None        = None

	# Capability bitfield. Arrives as an array; contents are not consistent
	# enough across firmware versions to decode here.

	devAbility: list[int] | None = None


class SColorTempStep(SWac):  # tag = ctstep
	"""One entry in a fixture's stepped color-temperature table."""

	colorStepsIndex: int | None = None

	# The one place a wire name is genuinely ambiguous: ColorScaping firmware
	# sends colorStepsValue, the document calls the same thing mixColorTemp.
	# The field takes the observed spelling and accepts the documented one.

	colorStepsValue: int | None = Field(
		default=None,
		validation_alias=AliasChoices("colorStepsValue", "mixColorTemp"),
	)


class SDetailWhite(SDetail):  # tag = wdetail
	"""Adds the color temperature range and step table."""

	colorTempStepsTable: list[SColorTempStep] | None = None
	minColorTemp: int | None                         = None
	maxColorTemp: int | None                         = None


class SDetailMotor(SDetail):  # tag = mdetail
	"""Adds motor traversal timings."""

	upDownCircleTime: int | None    = None
	leftRightCircleTime: int | None = None


# ---------------------------------------------------------------------------
# Type → structure dispatch
# ---------------------------------------------------------------------------

# Several documented types are defined only by reference to another type, so
# ten IDs collapse into six real shapes.

type TShapes = tuple[type[SState], type[STune], type[SDetail]]  # tag = shapes

g_mpFixturekShapes: dict[FIXTUREK, TShapes] = {
	FIXTUREK.SingleColor:        (SStateLight, STune,      SDetail),
	FIXTUREK.Elv:                (SStateLight, STune,      SDetail),
	FIXTUREK.TunableWhite:       (SStateWhite, STuneWhite, SDetailWhite),
	FIXTUREK.Controller24V:      (SStateWhite, STuneWhite, SDetailWhite),
	FIXTUREK.DecorativeLow:      (SStateWhite, STuneWhite, SDetailWhite),
	FIXTUREK.DecorativeHigh:     (SStateWhite, STuneWhite, SDetailWhite),

	# RGBW carries the color temperature range and step table too — measured
	# on ColorScaping hardware, which reports 2700–6500K on an RGBW fixture.

	FIXTUREK.Rgbw:               (SStateRgbw,  STuneRgbw,  SDetailWhite),
	FIXTUREK.MotorizedTrackhead: (SStateMotor, STuneMotor, SDetailMotor),
	FIXTUREK.WallStation:        (SStateWall,  STune,      SDetail),
	FIXTUREK.Fan:                (SStateFan,   STune,      SDetail),
}

# An unrecognized type still parses — into the permissive bases, which with
# extra="allow" means nothing is lost.

g_shapesUnknown: TShapes = (SState, STune, SDetail)


class CFixture:  # tag = fixture
	"""One fixture, built from the object the device returns for it.

	Construction never raises on unfamiliar input. An unknown type logs and
	resolves to FIXTUREK.Unknown with its structures parsed into the
	permissive bases; the raw object stays on `obj` either way so callers
	can see exactly what arrived.
	"""

	def __init__(self, obj: dict[str, Any]) -> None:
		self.obj = obj  # raw fixture object, exactly as the device sent it

		objType = obj.get("type")
		self.nType = objType if isinstance(objType, int) else -1  # raw ID, unmapped
		self.fixturek = FIXTUREK(self.nType)

		objAddr = obj.get("addr")
		self.nAddr = objAddr if isinstance(objAddr, int) else None

		objName = obj.get("name")
		self.strName = objName if isinstance(objName, str) else None

		clsState, clsTune, clsDetail = g_mpFixturekShapes.get(self.fixturek, g_shapesUnknown)

		self.state  = clsState.model_validate(obj.get("state") or {})
		self.tune   = clsTune.model_validate(obj.get("tune") or {})
		self.detail = clsDetail.model_validate(obj.get("detail") or {})

	def FIsKnown(self) -> bool:
		"""False for a fixture type this library does not model."""

		return self.fixturek is not FIXTUREK.Unknown

	def StrDescribe(self) -> str:
		"""One-line human summary, for CLI output and logs."""

		strType = self.fixturek.name if self.FIsKnown() else f"Unknown({self.nType})"

		return f"addr={self.nAddr} type={strType} name={self.strName!r}"
