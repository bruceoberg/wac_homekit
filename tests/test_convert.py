"""Unit conversion between device and HomeKit ranges.

The round-trip tests are the point of the file. A conversion that is not its
own inverse makes a Home app tile flicker: the app writes 50%, the next poll
reads back 49%, the tile snaps back, the app writes again. So every pair is
checked to survive a trip out to device units and home again, across the
whole range rather than at a few sampled values.
"""

from __future__ import annotations  # Forward refs without quotes

import pytest

from wac_iot import HUE_MAX, LEVEL_MAX, SATURATION_MAX, SDetail, SDetailWhite

from wac_homekit.convert import (
	BRIGHTNESS_MAX,
	HUE_DEG_MAX,
	SATURATION_PCT_MAX,
	CColorTempRange,
	NBrightnessFromLevel,
	NDegFromHue,
	NHueFromDeg,
	NKelvinFromMired,
	NLevelFromBrightness,
	NMiredFromKelvin,
	NPctFromSaturation,
	NSaturationFromPct,
	TplRgbFromHueSat,
)


# ---------------------------------------------------------------------------
# Brightness
# ---------------------------------------------------------------------------


def test_brightness_endpoints() -> None:
	assert NLevelFromBrightness(0) == 0
	assert NLevelFromBrightness(BRIGHTNESS_MAX) == LEVEL_MAX

	assert NBrightnessFromLevel(0) == 0
	assert NBrightnessFromLevel(LEVEL_MAX) == BRIGHTNESS_MAX


def test_brightness_round_trips_everywhere() -> None:
	for nBrightness in range(BRIGHTNESS_MAX + 1):
		assert NBrightnessFromLevel(NLevelFromBrightness(nBrightness)) == nBrightness


def test_brightness_is_percent_of_level() -> None:
	# level is in 0.01% steps, so 1% of HomeKit is exactly 100 of them.

	assert NLevelFromBrightness(1) == 100
	assert NLevelFromBrightness(50) == 5000


def test_brightness_clamps_out_of_range() -> None:
	assert NLevelFromBrightness(-5) == 0
	assert NLevelFromBrightness(150) == LEVEL_MAX

	assert NBrightnessFromLevel(-1) == 0
	assert NBrightnessFromLevel(LEVEL_MAX * 2) == BRIGHTNESS_MAX


def test_brightness_from_measured_level() -> None:
	# 9981 and 9977 are levels a real fixture actually reported.

	assert NBrightnessFromLevel(9981) == 100
	assert NBrightnessFromLevel(9977) == 100


# ---------------------------------------------------------------------------
# Hue
# ---------------------------------------------------------------------------


def test_hue_endpoints() -> None:
	assert NHueFromDeg(0) == 0
	assert NHueFromDeg(HUE_DEG_MAX) == HUE_MAX

	assert NDegFromHue(0) == 0
	assert NDegFromHue(HUE_MAX) == HUE_DEG_MAX


def test_hue_round_trips_everywhere() -> None:
	for nDeg in range(HUE_DEG_MAX + 1):
		assert NDegFromHue(NHueFromDeg(nDeg)) == nDeg


def test_hue_zero_is_a_real_colour() -> None:
	# Fully saturated red reports hue 0, so nothing may treat a falsy hue as
	# "no colour reported".

	assert NDegFromHue(0) == 0
	assert NHueFromDeg(0.0) == 0


def test_hue_from_measured_value() -> None:
	# 6666 is what a real fixture reported for cyan-ish blue.

	assert NDegFromHue(6666) == 240


def test_hue_accepts_float_input() -> None:
	# HomeKit's Hue characteristic is a float with a minStep of 1.

	assert NHueFromDeg(179.6) == NHueFromDeg(180)
	assert NHueFromDeg(180.4) == NHueFromDeg(180)


def test_hue_clamps_out_of_range() -> None:
	assert NHueFromDeg(-1) == 0
	assert NHueFromDeg(720) == HUE_MAX
	assert NDegFromHue(HUE_MAX * 2) == HUE_DEG_MAX


# ---------------------------------------------------------------------------
# Saturation
# ---------------------------------------------------------------------------


def test_saturation_endpoints() -> None:
	assert NSaturationFromPct(0) == 0
	assert NSaturationFromPct(SATURATION_PCT_MAX) == SATURATION_MAX

	assert NPctFromSaturation(0) == 0
	assert NPctFromSaturation(SATURATION_MAX) == SATURATION_PCT_MAX


def test_saturation_round_trips_everywhere() -> None:
	for nPct in range(SATURATION_PCT_MAX + 1):
		assert NPctFromSaturation(NSaturationFromPct(nPct)) == nPct


def test_saturation_clamps_out_of_range() -> None:
	assert NSaturationFromPct(-10) == 0
	assert NSaturationFromPct(101) == SATURATION_MAX
	assert NPctFromSaturation(-1) == 0


# ---------------------------------------------------------------------------
# Hue/saturation to RGB — the only colour write the firmware honors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
	"gDeg,tplRgb",
	[
		(0,   (255, 0, 0)),      # red
		(60,  (255, 255, 0)),    # yellow
		(120, (0, 255, 0)),      # green
		(180, (0, 255, 255)),    # cyan
		(240, (0, 0, 255)),      # blue
		(300, (255, 0, 255)),    # magenta
		(360, (255, 0, 0)),      # wraps to red
	],
)
def test_full_saturation_gives_the_pure_hues(gDeg: float, tplRgb: tuple[int, int, int]) -> None:
	assert TplRgbFromHueSat(gDeg, 100) == tplRgb


def test_zero_saturation_is_white_at_every_hue() -> None:
	# Measured: RGB (255,255,255) produces visible white on an RGBW fixture,
	# and the firmware reports saturation 0 afterwards.

	for gDeg in range(0, 361, 15):
		assert TplRgbFromHueSat(gDeg, 0) == (255, 255, 255)


def test_red_matches_what_the_fixture_stores() -> None:
	# The device holds fully saturated red as red 255 / hue 0 / saturation
	# 10000 — this is the write that produced it.

	assert TplRgbFromHueSat(0, 100) == (255, 0, 0)


def test_value_is_always_full() -> None:
	# Brightness rides on `level`, never on RGB magnitude, so the brightest
	# component is pinned at 255 for every hue and saturation.

	for gDeg in range(0, 360, 7):
		for gPct in range(0, 101, 5):
			assert max(TplRgbFromHueSat(gDeg, gPct)) == 255


def test_partial_saturation_lifts_the_floor_not_the_peak() -> None:
	# Half-saturated red is 255 with the other channels raised halfway, not
	# a dimmer red — that is what keeps colour and brightness independent.

	nR, nG, nB = TplRgbFromHueSat(0, 50)

	assert nR == 255
	assert nG == nB
	assert 120 <= nG <= 135


def test_rgb_stays_in_device_range() -> None:
	for gDeg in range(-30, 400, 11):
		for gPct in (-10, 0, 33, 100, 140):
			for n in TplRgbFromHueSat(gDeg, gPct):
				assert 0 <= n <= 255


def test_hue_survives_the_round_trip_through_rgb() -> None:
	# The firmware derives hue/saturation from the RGB it is given, and the
	# next poll reads those back. A write that lands on a different hue than
	# HomeKit asked for is what makes a colour tile drift.

	for gDeg in range(0, 360, 15):
		nR, nG, nB = TplRgbFromHueSat(gDeg, 100)
		nMax, nMin = max(nR, nG, nB), min(nR, nG, nB)
		dN = nMax - nMin

		if nMax == nR:
			gBack = 60 * (((nG - nB) / dN) % 6)
		elif nMax == nG:
			gBack = 60 * ((nB - nR) / dN + 2)
		else:
			gBack = 60 * ((nR - nG) / dN + 4)

		assert abs(gBack - gDeg) < 1.0 or abs(gBack - gDeg) > 359.0


# ---------------------------------------------------------------------------
# Color temperature, unclamped
# ---------------------------------------------------------------------------


def test_mired_kelvin_are_reciprocal() -> None:
	assert NKelvinFromMired(200) == 5000
	assert NMiredFromKelvin(5000) == 200

	assert NKelvinFromMired(370) == 2703
	assert NMiredFromKelvin(2700) == 370


def test_mired_kelvin_reject_nonpositive() -> None:
	with pytest.raises(ValueError):
		NKelvinFromMired(0)

	with pytest.raises(ValueError):
		NMiredFromKelvin(-1)


# ---------------------------------------------------------------------------
# Color temperature, clamped to one fixture
# ---------------------------------------------------------------------------


def DetailWhite(nMin: int | None, nMax: int | None) -> SDetailWhite:
	return SDetailWhite(minColorTemp=nMin, maxColorTemp=nMax)


def test_ctrange_uses_the_fixtures_own_span() -> None:
	ctrange = CColorTempRange(DetailWhite(2700, 6500))

	assert ctrange.fReported
	assert ctrange.nKelvinMin == 2700
	assert ctrange.nKelvinMax == 6500

	# Mireds run the other way, so the ends swap.

	assert ctrange.nMiredMin == NMiredFromKelvin(6500)
	assert ctrange.nMiredMax == NMiredFromKelvin(2700)
	assert ctrange.nMiredMin < ctrange.nMiredMax


def test_ctrange_falls_back_when_detail_reports_nothing() -> None:
	# A plain SDetail — single color, ELV — carries no span at all.

	ctrange = CColorTempRange(SDetail())

	assert not ctrange.fReported
	assert ctrange.nKelvinMin == CColorTempRange.g_nKelvinMinDefault
	assert ctrange.nKelvinMax == CColorTempRange.g_nKelvinMaxDefault


@pytest.mark.parametrize(
	"nMin,nMax",
	[
		(None, 6500),   # half reported
		(2700, None),
		(None, None),
		(6500, 2700),   # backwards
		(4000, 4000),   # no span at all
		(0, 6500),      # nonsense low end
	],
)
def test_ctrange_falls_back_on_an_unusable_span(nMin: int | None, nMax: int | None) -> None:
	ctrange = CColorTempRange(DetailWhite(nMin, nMax))

	assert not ctrange.fReported
	assert ctrange.nKelvinMin == CColorTempRange.g_nKelvinMinDefault
	assert ctrange.nKelvinMax == CColorTempRange.g_nKelvinMaxDefault


def test_ctrange_clamps_to_the_fixture() -> None:
	ctrange = CColorTempRange(DetailWhite(2700, 6500))

	# Warmer than the fixture goes, and cooler than it goes.

	assert ctrange.NKelvinFromMired(ctrange.nMiredMax + 100) == 2700
	assert ctrange.NKelvinFromMired(ctrange.nMiredMin - 100) == 6500

	assert ctrange.NMiredFromKelvin(2000) == ctrange.nMiredMax
	assert ctrange.NMiredFromKelvin(9000) == ctrange.nMiredMin


def test_ctrange_ends_are_exact() -> None:
	# Dragging the slider fully warm or fully cool must ask for the fixture's
	# own limit, not a reciprocal rounding a few Kelvin inside it.

	ctrange = CColorTempRange(DetailWhite(2700, 6500))

	assert ctrange.NKelvinFromMired(ctrange.nMiredMax) == 2700
	assert ctrange.NKelvinFromMired(ctrange.nMiredMin) == 6500

	assert ctrange.NMiredFromKelvin(2700) == ctrange.nMiredMax
	assert ctrange.NMiredFromKelvin(6500) == ctrange.nMiredMin


def test_ctrange_never_leaves_the_kelvin_span() -> None:
	# Reciprocal rounding at an endpoint is exactly where a value slips
	# outside the range the device will accept.

	ctrange = CColorTempRange(DetailWhite(2700, 6500))

	for nMired in range(ctrange.nMiredMin, ctrange.nMiredMax + 1):
		nKelvin = ctrange.NKelvinFromMired(nMired)

		assert ctrange.nKelvinMin <= nKelvin <= ctrange.nKelvinMax


def test_ctrange_round_trips_across_its_span() -> None:
	ctrange = CColorTempRange(DetailWhite(2700, 6500))

	for nMired in range(ctrange.nMiredMin, ctrange.nMiredMax + 1):
		assert ctrange.NMiredFromKelvin(ctrange.NKelvinFromMired(nMired)) == nMired


def test_ctrange_round_trips_on_a_narrow_span() -> None:
	# A tight span is where reciprocal rounding has the least room.

	ctrange = CColorTempRange(DetailWhite(3000, 3200))

	for nMired in range(ctrange.nMiredMin, ctrange.nMiredMax + 1):
		assert ctrange.NMiredFromKelvin(ctrange.NKelvinFromMired(nMired)) == nMired
