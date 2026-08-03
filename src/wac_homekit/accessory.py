#!/usr/bin/env python3
"""One HomeKit accessory per WAC light fixture.

A transformer carries many fixtures and HomeKit wants one accessory per
controllable thing, so this is where a `CFixture` becomes a Lightbulb. The
three capability tiers below share almost everything — the On/Brightness
plumbing, the setter, the reconcile — so they are one class with conditional
characteristic setup rather than three subclasses that would differ by a
handful of lines each.

This holds a `CClient` and an address, never a `CSnapshot`: a snapshot is
what one poll saw and goes stale the moment the next one starts.
"""

from __future__ import annotations  # Forward refs without quotes

import hashlib
import logging
import re

from enum import IntEnum, auto
from typing import TYPE_CHECKING, Any

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_LIGHTBULB

from wac_iot import (
	FIXTUREK,
	CClient,
	CFixture,
	SDetail,
	SStateLight,
	SStateRgbw,
	SStateWhite,
	WacError,
)

from .convert import (
	CColorTempRange,
	NBrightnessFromLevel,
	NDegFromHue,
	NLevelFromBrightness,
	NPctFromSaturation,
	TplRgbFromHueSat,
)

if TYPE_CHECKING:
	from pyhap.accessory_driver import AccessoryDriver

g_log = logging.getLogger(__name__)

# HAP service and characteristic display names, as HAP-python's loader spells
# them. Named rather than inlined because the service-level setter callback
# identifies characteristics by exactly these strings.

SERV_ACCESSORY_INFORMATION = "AccessoryInformation"
SERV_LIGHTBULB = "Lightbulb"

CHAR_BRIGHTNESS = "Brightness"
CHAR_COLOR_TEMPERATURE = "ColorTemperature"
CHAR_HUE = "Hue"
CHAR_IDENTIFY = "Identify"
CHAR_ON = "On"
CHAR_SATURATION = "Saturation"

MANUFACTURER = "WAC Lighting"


class LIGHTTIER(IntEnum):  # tag = tier — what a fixture can be asked to do
	"""How much of HomeKit's Lightbulb a fixture can actually support.

	Ten fixture types collapse into three sets of characteristics, the same
	way `wac_iot` collapses them into six wire shapes.
	"""

	Dimmable = auto()   # On, Brightness
	White    = auto()   # ... plus ColorTemperature
	Rgbw     = auto()   # ... plus Hue and Saturation


# Lights only, deliberately. Motorized trackheads, fans, and wall stations are
# not lights and get no entry here; neither does a type this library does not
# model. `TierTryFromFixturek` returning None is the filter the driver uses,
# so adding a type here is the only change needed to bridge it.

g_mpFixturekTier: dict[FIXTUREK, LIGHTTIER] = {
	FIXTUREK.SingleColor:    LIGHTTIER.Dimmable,
	FIXTUREK.Elv:            LIGHTTIER.Dimmable,

	FIXTUREK.TunableWhite:   LIGHTTIER.White,
	FIXTUREK.Controller24V:  LIGHTTIER.White,
	FIXTUREK.DecorativeLow:  LIGHTTIER.White,
	FIXTUREK.DecorativeHigh: LIGHTTIER.White,

	FIXTUREK.Rgbw:           LIGHTTIER.Rgbw,
}


def TierTryFromFixturek(fixturek: FIXTUREK) -> LIGHTTIER | None:
	"""The tier a fixture type belongs to, or None if it is not a light."""

	return g_mpFixturekTier.get(fixturek)


# AIDs must be stable across restarts — iOS remembers which accessory in a
# bridge it paired with by AID, and a shuffle turns every light in the Home app
# into a stranger. They must also be integers, while the only stable identifier
# a fixture has is a string. SHA-256 truncated to six bytes is the bridge: a
# cryptographic digest so nearby addresses do not cluster, truncated because
# HAP AIDs are better kept small, and folded above 7 because AID 1 is the
# bridge itself and HAP-python documents 7 as unusable (its issue #61).
#
# Six bytes gives a collision chance under 1 in 10^10 for any plausible number
# of fixtures. The driver still checks, because "essentially never" is not
# "never" and the failure mode is a silently missing light.

AID_HASH_BYTES = 6
AID_MIN = 8
AID_MAX = 1 << 32

# HomeKit expects a dotted numeric firmware revision. WAC firmware strings have
# not all been seen, and iOS logs a complaint about anything else, so an
# unrecognized one is simply left unset.

g_reFirmware = re.compile(r"^\d+(\.\d+){0,2}$")


def NAidFromFixtureId(strFixtureId: str) -> int:
	"""A stable HAP accessory ID derived from a fixture's stable identifier."""

	bDigest = hashlib.sha256(strFixtureId.encode()).digest()[:AID_HASH_BYTES]

	return AID_MIN + int.from_bytes(bDigest, "big") % (AID_MAX - AID_MIN)


def StrTryFirmware(strVer: str | None) -> str | None:
	"""A firmware string HomeKit will accept, or None."""

	if strVer and g_reFirmware.match(strVer):
		return strVer

	return None


class CFixtureAccessory(Accessory):  # tag = facc
	"""One light fixture, as HomeKit sees it."""

	category = CATEGORY_LIGHTBULB

	def __init__(
		self,
		driver: AccessoryDriver,
		client: CClient,
		*,
		nAddr: int,
		strFixtureId: str,
		fixture: CFixture,
		tier: LIGHTTIER,
	) -> None:
		super().__init__(
			driver,
			fixture.strName or f"Fixture {nAddr}",
			aid=NAidFromFixtureId(strFixtureId),
		)

		self.client = client
		self.nAddr = nAddr
		self.tier = tier

		# Reported by the last poll that saw this fixture. False also covers a
		# poll that failed outright, which is how a whole unplugged
		# transformer shows up in the Home app as "No Response".

		self.fOnline = True

		self._SetInfo(fixture.detail, strFixtureId)

		# Only the characteristics this tier can honor. A ColorTemperature on
		# a single-color fixture would be a control that silently does
		# nothing, which is worse than not offering it.

		lStrChar = [CHAR_BRIGHTNESS]

		if tier is LIGHTTIER.White:
			lStrChar.append(CHAR_COLOR_TEMPERATURE)
		elif tier is LIGHTTIER.Rgbw:
			lStrChar += [CHAR_HUE, CHAR_SATURATION]

		self.servLight = self.add_preload_service(SERV_LIGHTBULB, chars=lStrChar)

		# One setter for the whole service rather than one per characteristic.
		# The Home app writes On and Brightness — or On, Hue and Saturation —
		# in a single request, and HAP-python hands the whole batch over here.
		# Per-characteristic setters would turn that into two or three device
		# requests racing each other, and the RGBW builder would see hue and
		# saturation as separate writes to the same colour state.

		self.servLight.setter_callback = self._OnSetService

		self.charOn         = self.servLight.get_characteristic(CHAR_ON)
		self.charBrightness = self.servLight.get_characteristic(CHAR_BRIGHTNESS)

		self.charColorTemp = (
			self.servLight.get_characteristic(CHAR_COLOR_TEMPERATURE)
			if tier is LIGHTTIER.White else None
		)
		self.charHue = (
			self.servLight.get_characteristic(CHAR_HUE)
			if tier is LIGHTTIER.Rgbw else None
		)
		self.charSaturation = (
			self.servLight.get_characteristic(CHAR_SATURATION)
			if tier is LIGHTTIER.Rgbw else None
		)

		# The span this fixture actually covers, which is also what the Home
		# app's temperature slider should stop at.

		self.ctrange = CColorTempRange(fixture.detail)

		if self.charColorTemp is not None:
			self.charColorTemp.override_properties(properties={
				"minValue": self.ctrange.nMiredMin,
				"maxValue": self.ctrange.nMiredMax,
			})

		self.get_service(SERV_ACCESSORY_INFORMATION).configure_char(
			CHAR_IDENTIFY,
			setter_callback=self._OnIdentify,
		)

		self.Reconcile(fixture)

	@property
	def available(self) -> bool:
		"""False makes the Home app show this accessory as unresponsive."""

		return self.fOnline

	def _SetInfo(self, detail: SDetail, strFixtureId: str) -> None:
		"""Fill in the AccessoryInformation service from the fixture's detail."""

		self.set_info_service(
			manufacturer=MANUFACTURER,
			model=detail.model or "unknown",
			serial_number=strFixtureId,
			firmware_revision=StrTryFirmware(detail.fwVer),
		)

	# -----------------------------------------------------------------------
	# HomeKit → device
	# -----------------------------------------------------------------------

	def _OnSetService(self, mpStrValue: dict[str, Any]) -> None:
		"""HAP service-level setter. Runs on the driver's event loop.

		HAP-python has already stored the new values on the characteristics
		by the time this runs, so the optimistic local update the user sees is
		done and the only thing left is to tell the fixture. A failed write
		therefore leaves HomeKit briefly ahead of the hardware; the next poll
		reconciles it, which is the same correction path a change made from
		the WAC app takes.
		"""

		self.driver.async_add_job(self._ControlAsync(mpStrValue))

	async def _ControlAsync(self, mpStrValue: dict[str, Any]) -> None:
		"""Send exactly the fields that changed, in device units."""

		fOn: bool | None = None
		if CHAR_ON in mpStrValue:
			fOn = bool(mpStrValue[CHAR_ON])

		nLevel: int | None = None
		if CHAR_BRIGHTNESS in mpStrValue:
			nLevel = NLevelFromBrightness(int(mpStrValue[CHAR_BRIGHTNESS]))

		nColorTemp: int | None = None
		if CHAR_COLOR_TEMPERATURE in mpStrValue:
			nColorTemp = self.ctrange.NKelvinFromMired(int(mpStrValue[CHAR_COLOR_TEMPERATURE]))

		# Colour is written as RGB, never as hue/saturation — the firmware
		# refuses or silently discards HSV writes. See TplRgbFromHueSat.
		#
		# The Home app can move one of the pair without the other, but RGB
		# needs both. HAP-python has already stored the incoming value on each
		# characteristic by the time this runs, so reading them back gives the
		# intended combination rather than a half-applied one.

		tplRgb: tuple[int, int, int] | None = None
		fColorSet = CHAR_HUE in mpStrValue or CHAR_SATURATION in mpStrValue

		if fColorSet and self.charHue is not None and self.charSaturation is not None:
			tplRgb = TplRgbFromHueSat(
				float(self.charHue.value),
				float(self.charSaturation.value),
			)

		# The batch can contain characteristics this bridge does not map — a
		# Name write, say. Sending nothing beats spending a request, and the
		# builders in wac_iot would refuse an empty state anyway.

		if all(obj is None for obj in (fOn, nLevel, nColorTemp, tplRgb)):
			g_log.debug("%s: nothing to control in %s", self.display_name, sorted(mpStrValue))

			return

		try:
			match self.tier:
				case LIGHTTIER.Dimmable:
					await self.client.fixture.ControlLight(
						self.nAddr,
						fOn=fOn,
						nLevel=nLevel,
					)

				case LIGHTTIER.White:
					await self.client.fixture.ControlWhite(
						self.nAddr,
						fOn=fOn,
						nLevel=nLevel,
						nColorTemp=nColorTemp,
					)

				case LIGHTTIER.Rgbw:
					# BB(bruce) an RGBW fixture also reports a colour
					# temperature range, so HomeKit's white point could be
					# driven through mixColorTemp. That path has never been
					# written to real hardware and RGB and mixColorTemp are
					# mutually exclusive on the wire, so this phase offers
					# Hue/Saturation only and leaves the white point alone.

					await self.client.fixture.ControlRgbw(
						self.nAddr,
						fOn=fOn,
						nLevel=nLevel,
						tplRgb=tplRgb,
					)

		except WacError as exc:
			g_log.error("%s: control failed: %s", self.display_name, exc)

	def _OnIdentify(self, objValue: Any) -> None:
		"""HomeKit Identify, which is the device's `findme`.

		Write-only: `findme` has never been observed in a fixture's read-back
		state, so there is nothing to confirm and nothing to reconcile. This
		fires and forgets on purpose.
		"""

		self.driver.async_add_job(self._IdentifyAsync())

	async def _IdentifyAsync(self) -> None:
		try:
			await self.client.fixture.Identify(self.nAddr)
		except WacError as exc:
			g_log.error("%s: identify failed: %s", self.display_name, exc)

	# -----------------------------------------------------------------------
	# Device → HomeKit
	# -----------------------------------------------------------------------

	def Reconcile(self, fixture: CFixture | None) -> None:
		"""Fold a fresh poll's view of this fixture back into HomeKit.

		`None` means this poll did not see the fixture at all, which is not
		the same as seeing it report itself offline — but it looks identical
		from the Home app, so both land on unavailable.
		"""

		if fixture is None:
			self.MarkOffline()

			return

		state = fixture.state

		if not isinstance(state, SStateLight):
			# The fixture at this address answered with a shape that is not a
			# light's. A retyped or replaced fixture is the plausible cause,
			# and rebuilding accessories mid-run is a phase of its own.

			g_log.warning("%s: fixture at %d is no longer a light", self.display_name, self.nAddr)
			self.MarkOffline()

			return

		self.fOnline = state.online is not False

		self._SetCharTry(self.charOn, state.status)

		if state.level is not None:
			self._SetCharTry(self.charBrightness, NBrightnessFromLevel(state.level))

		if self.charColorTemp is not None and isinstance(state, SStateWhite):
			if state.mixColorTemp is not None:
				self._SetCharTry(self.charColorTemp, self.ctrange.NMiredFromKelvin(state.mixColorTemp))

		if isinstance(state, SStateRgbw):
			# Never treat a falsy hue as "no colour reported" — fully
			# saturated red reports hue 0, and only saturation tells it apart
			# from white.

			if state.hue is not None:
				self._SetCharTry(self.charHue, NDegFromHue(state.hue))

			if state.saturation is not None:
				self._SetCharTry(self.charSaturation, NPctFromSaturation(state.saturation))

	def MarkOffline(self) -> None:
		"""Report this fixture as unreachable without touching its values."""

		self.fOnline = False

	@staticmethod
	def _SetCharTry(char: Any, objValue: Any) -> None:
		"""Push a value into a characteristic, but only if it actually moved.

		Every write that changes a value notifies every subscribed client, so
		an unguarded reconcile would spray a HomeKit event per characteristic
		per poll for a system that is sitting still.
		"""

		if char is None or objValue is None or char.value == objValue:
			return

		char.set_value(objValue)
