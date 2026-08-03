#!/usr/bin/env python3
"""Device units in, HomeKit units out — and back again.

The only place in the bridge that knows both sides of the boundary. `wac_iot`
speaks the device's ranges and refuses to guess what a consumer's platform
wants; HomeKit has its own ranges for the same four quantities. Keeping every
conversion here means a rounding decision gets made once, and can be tested
without hardware.

That testing is the point. A conversion that is not its own inverse makes a
Home app tile flicker: the app writes 50%, the next poll reads back 49%, the
tile snaps, the app writes again. So every pair below round-trips exactly for
values that started on the HomeKit side, which is the direction a user's own
action travels.

Device-side bounds are imported from `wac_iot` rather than written out, which
is why that package exports them.
"""

from __future__ import annotations  # Forward refs without quotes

from wac_iot import (
	HUE_MAX,
	LEVEL_MAX,
	RGB_MAX,
	SATURATION_MAX,
	SDetail,
	SDetailWhite,
)

# HomeKit's own ranges, from the characteristic definitions HAP-python ships.
# Brightness is an integer characteristic; Hue and Saturation are floats with a
# minStep of 1. That asymmetry is Apple's, not ours — it is why the HomeKit
# side of the signatures below is `float` for two of the three and `int` for
# the other.

BRIGHTNESS_MIN = 0
BRIGHTNESS_MAX = 100

HUE_DEG_MIN = 0
HUE_DEG_MAX = 360

SATURATION_PCT_MIN = 0
SATURATION_PCT_MAX = 100

# A mired is a reciprocal megakelvin. HomeKit's ColorTemperature is in mireds;
# the device's mixColorTemp is in Kelvin.

MIRED_SCALE = 1_000_000


def _NClamp(n: int, nMin: int, nMax: int) -> int:
	return max(nMin, min(nMax, n))


def _NRound(g: float) -> int:
	"""Round half away from zero, for non-negative input.

	`round()` rounds halves to even, which breaks the round-trip guarantee
	asymmetrically — 0.5 lands on 0 but 1.5 lands on 2. Every quantity here
	is non-negative, so adding a half and flooring is both correct and
	obvious.
	"""

	return int(g + 0.5)


def _NScale(n: int, nSrcMax: int, nDstMax: int) -> int:
	"""Rescale 0..nSrcMax onto 0..nDstMax, rounding half up.

	Integer arithmetic throughout: the whole reason this file exists is that
	float drift between two ranges is what makes tiles flicker.
	"""

	n = _NClamp(n, 0, nSrcMax)

	return (n * nDstMax + nSrcMax // 2) // nSrcMax


# ---------------------------------------------------------------------------
# Brightness — HomeKit 0..100 against device level 0..10000
# ---------------------------------------------------------------------------


def NLevelFromBrightness(nBrightness: int) -> int:
	"""Device `level` from a HomeKit Brightness percentage."""

	return _NScale(nBrightness, BRIGHTNESS_MAX, LEVEL_MAX)


def NBrightnessFromLevel(nLevel: int) -> int:
	"""HomeKit Brightness percentage from a device `level`."""

	return _NScale(nLevel, LEVEL_MAX, BRIGHTNESS_MAX)


# ---------------------------------------------------------------------------
# Hue — HomeKit 0..360 degrees against device hue 0..10000
# ---------------------------------------------------------------------------

# NOTE(bruce) NHueFromDeg and NSaturationFromPct are not currently called by
# the bridge, and that is not an oversight. Colour reads come back as HSV and
# use the ...FromHue / ...FromSaturation direction; colour writes go out as RGB
# because this firmware refuses HSV. Keep the inbound halves: they are the
# documented meaning of the device's own units, they are what a future firmware
# accepting HSV would need, and deleting half of a symmetric pair invites
# someone to re-derive it wrong later.


def NHueFromDeg(gDeg: float) -> int:
	"""Device `hue` from HomeKit degrees.

	Unused by the bridge today — see the note above.
	"""

	return _NScale(_NRound(gDeg), HUE_DEG_MAX, HUE_MAX)


def NDegFromHue(nHue: int) -> int:
	"""HomeKit degrees from a device `hue`.

	Returns an int even though the characteristic is a float: its minStep is
	1, so a fractional value would only ever be rounded away by the Home app
	— and comparing floats is how a reconcile ends up notifying on every
	poll.
	"""

	return _NScale(nHue, HUE_MAX, HUE_DEG_MAX)


# ---------------------------------------------------------------------------
# Saturation — HomeKit 0..100 against device saturation 0..10000
# ---------------------------------------------------------------------------


def NSaturationFromPct(gPct: float) -> int:
	"""Device `saturation` from a HomeKit Saturation percentage.

	Unused by the bridge today — see the note under Hue.
	"""

	return _NScale(_NRound(gPct), SATURATION_PCT_MAX, SATURATION_MAX)


def NPctFromSaturation(nSaturation: int) -> int:
	"""HomeKit Saturation percentage from a device `saturation`."""

	return _NScale(nSaturation, SATURATION_MAX, SATURATION_PCT_MAX)


def TplRgbFromHueSat(gDeg: float, gPct: float) -> tuple[int, int, int]:
	"""HomeKit hue and saturation as an RGB triple the device will accept.

	Colour on an RGBW fixture is read as HSV and written as RGB — measured,
	not assumed. Writing `hue`/`saturation` is refused outright with
	MissingRequiredParam, or, once a `mode` write has been attempted,
	accepted with result 0 and then silently discarded. Writing the RGB
	triple works and moves `hue`/`saturation` to match, which is what makes
	the asymmetry survivable: reads stay on HSV.

	Value is fixed at full. `level` is the brightness axis and the RGB
	magnitude carries chromaticity only, so a colour change never disturbs
	brightness. That split is deliberate and provisional — whether RGB
	magnitude drives light output at all is still unmeasured (it needs a
	dark room), and this is the choice that stays correct either way.
	"""

	gHue = _NRound(gDeg) % HUE_DEG_MAX / 60.0
	uSat = _NClamp(_NRound(gPct), SATURATION_PCT_MIN, SATURATION_PCT_MAX) / SATURATION_PCT_MAX

	# Standard HSV->RGB with V = 1, which collapses chroma to the saturation
	# and the offset to its complement.
	uChroma = uSat
	uSecond = uChroma * (1.0 - abs(gHue % 2.0 - 1.0))
	uMin = 1.0 - uChroma

	match int(gHue):
		case 0: tplU = (uChroma, uSecond, 0.0)
		case 1: tplU = (uSecond, uChroma, 0.0)
		case 2: tplU = (0.0, uChroma, uSecond)
		case 3: tplU = (0.0, uSecond, uChroma)
		case 4: tplU = (uSecond, 0.0, uChroma)
		case _: tplU = (uChroma, 0.0, uSecond)

	nR, nG, nB = (_NRound((u + uMin) * RGB_MAX) for u in tplU)

	return (nR, nG, nB)


# ---------------------------------------------------------------------------
# Color temperature — HomeKit mireds against device Kelvin
# ---------------------------------------------------------------------------


def NKelvinFromMired(nMired: int) -> int:
	"""Kelvin from mireds. Unclamped — see CColorTempRange."""

	if nMired <= 0:
		raise ValueError(f"mireds must be positive, got {nMired}")

	return _NRound(MIRED_SCALE / nMired)


def NMiredFromKelvin(nKelvin: int) -> int:
	"""Mireds from Kelvin. Unclamped — see CColorTempRange."""

	if nKelvin <= 0:
		raise ValueError(f"kelvin must be positive, got {nKelvin}")

	return _NRound(MIRED_SCALE / nKelvin)


class CColorTempRange:  # tag = ctrange
	"""One fixture's usable color temperature span, in both units.

	Built from that fixture's own `detail`, because the span is per-fixture
	and `wac_iot` deliberately leaves Kelvin unchecked for exactly this
	reason. It also supplies the min/max the ColorTemperature characteristic
	is overridden with, so the Home app's slider stops where the hardware
	does instead of at HAP-python's generic 140–500.

	Mireds run the other way from Kelvin, so the ends swap: the coolest
	Kelvin is the smallest mired value.
	"""

	# What to use when a fixture reports no span of its own. A plain SDetail
	# — single color, ELV — carries no minColorTemp/maxColorTemp at all, and
	# a tunable-white fixture on firmware that omits them would land here
	# too. 2700–6500K is what the one measured fixture reported and is the
	# span WAC's tunable-white line is sold as; it is a documented fallback,
	# not a discovered fact, and a fixture that disagrees will simply refuse
	# the out-of-span Kelvin we send. Prefer fixing this by reading the
	# fixture's detail over widening the default.

	g_nKelvinMinDefault = 2700
	g_nKelvinMaxDefault = 6500

	def __init__(self, detail: SDetail) -> None:
		nKelvinMin: int | None = None
		nKelvinMax: int | None = None

		if isinstance(detail, SDetailWhite):
			nKelvinMin = detail.minColorTemp
			nKelvinMax = detail.maxColorTemp

		# Both ends, positive, and the right way round — anything else is a
		# fixture that did not really report a span.

		self.fReported = (
			nKelvinMin is not None
			and nKelvinMax is not None
			and 0 < nKelvinMin < nKelvinMax
		)

		if not self.fReported or nKelvinMin is None or nKelvinMax is None:
			nKelvinMin = self.g_nKelvinMinDefault
			nKelvinMax = self.g_nKelvinMaxDefault

		self.nKelvinMin = nKelvinMin
		self.nKelvinMax = nKelvinMax

		self.nMiredMin = NMiredFromKelvin(nKelvinMax)
		self.nMiredMax = NMiredFromKelvin(nKelvinMin)

	def NKelvinFromMired(self, nMired: int) -> int:
		"""Device Kelvin from a HomeKit mired value, clamped to this fixture.

		The ends snap rather than convert. A slider dragged all the way warm
		should ask the fixture for its own minimum; running 370 mireds
		through the reciprocal instead gives 2703K, three Kelvin shy of a
		2700K fixture's actual limit and no longer a value that survives the
		trip home.
		"""

		if nMired >= self.nMiredMax:
			return self.nKelvinMin

		if nMired <= self.nMiredMin:
			return self.nKelvinMax

		return _NClamp(NKelvinFromMired(nMired), self.nKelvinMin, self.nKelvinMax)

	def NMiredFromKelvin(self, nKelvin: int) -> int:
		"""HomeKit mireds from a device Kelvin value, clamped to this fixture."""

		if nKelvin <= self.nKelvinMin:
			return self.nMiredMax

		if nKelvin >= self.nKelvinMax:
			return self.nMiredMin

		return _NClamp(NMiredFromKelvin(nKelvin), self.nMiredMin, self.nMiredMax)
