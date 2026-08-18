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

import asyncio
import hashlib
import logging
import re

from enum import IntEnum, auto
from typing import TYPE_CHECKING, Any

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_LIGHTBULB

from wac_iot import (
	FIXTUREK,
	LIGHTMODE,
	CClient,
	CFixture,
	CFixtures,
	SDetail,
	SState,
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


# The state shape a tier's fixtures report. A poll gets its shape from the
# fixture's own type, but the state echoed back by a control response carries
# no type at all — and does not need to, because the accessory already knows
# what it is talking to.

g_mpTierClsState: dict[LIGHTTIER, type[SStateLight]] = {
	LIGHTTIER.Dimmable: SStateLight,
	LIGHTTIER.White:    SStateWhite,
	LIGHTTIER.Rgbw:     SStateRgbw,
}


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
		self.clsState = g_mpTierClsState[tier]

		# A drag on a Home app slider is a burst of writes — a dozen in three
		# seconds, measured — and one device request each would queue behind
		# the last at ~1.4s apiece. The light and the tile would still be
		# working through the burst long after the finger lifted.
		#
		# So a write records only *which* characteristics were touched. Their
		# values are read at send time, straight off the characteristics,
		# where HAP-python has already stored the newest. Latest wins, and a
		# drag costs two or three requests instead of twelve.

		self.setStrCharPending: set[str] = set()
		self.taskControl: asyncio.Task[None] | None = None

		# Loop time of the most recent write to this fixture, against which a
		# poll's data is judged fresh or stale. See `_FIsPollStale`.

		self.tControlLast = 0.0

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

		# Straight to the state, not through `Reconcile`: this fixture was
		# just read, and there is no write history for it to be stale against.

		self.ReconcileState(fixture.state)

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
		done. This only notes what was touched; the sending is the worker's
		job, so a burst of writes collapses instead of queueing.
		"""

		self.setStrCharPending |= set(mpStrValue)

		if self.taskControl is None or self.taskControl.done():
			self.taskControl = self.driver.async_add_job(self._ControlPendingAsync())

	async def _ControlPendingAsync(self) -> None:
		"""Send what is pending until nothing is, one request at a time.

		Looping rather than sending once is the whole point: writes that
		arrive while a request is in flight land in the same set and go out
		together in the next one.
		"""

		while self.setStrCharPending:
			setStrChar = self.setStrCharPending
			self.setStrCharPending = set()

			await self._ControlAsync(setStrChar)

	async def _ControlAsync(self, setStrChar: set[str]) -> None:
		"""Send the named characteristics at their current values, in device units.

		Values come off the characteristics rather than out of the batch that
		named them, because by the time this runs several batches may have
		been folded together and only the newest value is wanted.
		"""

		fOn: bool | None = None
		if CHAR_ON in setStrChar:
			fOn = bool(self.charOn.value)

		nLevel: int | None = None
		if CHAR_BRIGHTNESS in setStrChar:
			nLevel = NLevelFromBrightness(int(self.charBrightness.value))

		nColorTemp: int | None = None
		if CHAR_COLOR_TEMPERATURE in setStrChar and self.charColorTemp is not None:
			nColorTemp = self.ctrange.NKelvinFromMired(int(self.charColorTemp.value))

		# Colour is written as RGB, never as hue/saturation — the firmware
		# refuses or silently discards HSV writes. See TplRgbFromHueSat.
		#
		# The Home app can move one of the pair without the other, but RGB
		# needs both. HAP-python has already stored the incoming value on each
		# characteristic by the time this runs, so reading them back gives the
		# intended combination rather than a half-applied one.

		tplRgb: tuple[int, int, int] | None = None
		fColorSet = CHAR_HUE in setStrChar or CHAR_SATURATION in setStrChar

		if fColorSet and self.charHue is not None and self.charSaturation is not None:
			tplRgb = TplRgbFromHueSat(
				float(self.charHue.value),
				float(self.charSaturation.value),
			)

		# The batch can contain characteristics this bridge does not map — a
		# Name write, say. Sending nothing beats spending a request, and the
		# builders in wac_iot would refuse an empty state anyway.

		if all(obj is None for obj in (fOn, nLevel, nColorTemp, tplRgb)):
			g_log.debug("%s: nothing to control in %s", self.display_name, sorted(setStrChar))

			return

		objControl: dict[str, Any] | None = None

		try:
			match self.tier:
				case LIGHTTIER.Dimmable:
					objControl = await self.client.fixture.ControlLight(
						self.nAddr,
						fOn=fOn,
						nLevel=nLevel,
					)

				case LIGHTTIER.White:
					objControl = await self.client.fixture.ControlWhite(
						self.nAddr,
						fOn=fOn,
						nLevel=nLevel,
						nColorTemp=nColorTemp,
					)

				case LIGHTTIER.Rgbw:
					# BB(bruce) an RGBW fixture will also honor mixColorTemp
					# — measured — so HomeKit's white point could be driven
					# through it. Offering both axes means picking one per
					# write, since they are mutually exclusive on the wire and
					# each drags `mode` along behind it. That is a phase of
					# its own; this one offers Hue/Saturation.

					objControl = await self.client.fixture.ControlRgbw(
						self.nAddr,
						fOn=fOn,
						nLevel=nLevel,
						tplRgb=tplRgb,
					)

		except WacError as exc:
			g_log.error("%s: control failed: %s", self.display_name, exc)

		# Stamped even when the write failed, because a request that timed out
		# may still have reached the fixture — a poll read before it is no
		# more trustworthy than one read before a success.

		self.tControlLast = asyncio.get_running_loop().time()

		# A response describes the request it answered, which more input has
		# already overtaken. Folding it back would push a value the user has
		# dragged past — the slider marching back through where it has been.
		# The write that drains the rest is the one that gets to reconcile.

		if objControl is not None and not self.setStrCharPending:
			self._ReconcileControl(objControl)

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

	def _ReconcileControl(self, objControl: dict[str, Any]) -> None:
		"""Fold a write's own response back in, rather than waiting for a poll.

		Action 4 echoes the fixture's whole post-write state, so what the
		firmware actually did lands in HomeKit as the write completes. That
		matters because the firmware routinely does something other than what
		was asked: writing `level` or a colour also switches the fixture on,
		and an out-of-range colour temperature comes back clamped. Without
		this the Home app shows the requested value, and the wrong one, until
		the next poll.

		The poll remains the correction for everything else — changes made
		from a wall station or the WAC app, and writes that failed outright,
		which never get here.
		"""

		objState = CFixtures.ObjTryStateFromControl(objControl)

		if objState is None:
			# Not seen on real firmware, but a response without a state is
			# well-formed enough for `ObjAction` to have accepted it, and
			# there is nothing to fold in.

			return

		self.ReconcileState(self.clsState.model_validate(objState))

	def _FIsPollStale(self, tPoll: float) -> bool:
		"""Whether a poll's data predates what HomeKit has already been told.

		A read that started before the newest local write describes the
		fixture as it was *before* that write, so folding it in shoves the
		Home app back to the value the user just moved away from. Measured:
		a tap at 30%, another at 100% six seconds later, and the slider
		snapping back to 30 forty milliseconds after the second tap — far too
		quick to be a device round trip, because it was the poll.

		A write still in flight counts as newer than any poll, since the read
		cannot have seen it yet.
		"""

		if self.taskControl is not None and not self.taskControl.done():
			return True

		return self.tControlLast > tPoll

	def Reconcile(self, fixture: CFixture | None, *, tPoll: float) -> None:
		"""Fold a fresh poll's view of this fixture back into HomeKit.

		`tPoll` is the loop time the read began, which is what decides whether
		this view is still current — see `_FIsPollStale`.

		`None` means this poll did not see the fixture at all, which is not
		the same as seeing it report itself offline — but it looks identical
		from the Home app, so both land on unavailable.
		"""

		if self._FIsPollStale(tPoll):
			g_log.debug("%s: poll predates the last write, dropping", self.display_name)

			return

		if fixture is None:
			self.MarkOffline()

			return

		self.ReconcileState(fixture.state)

	def ReconcileState(self, state: SState) -> None:
		"""Fold one reading of this fixture's state into HomeKit.

		Shared by the poll and by the state a control response echoes back —
		the same thing arriving by two routes.
		"""

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
			if state.mode is LIGHTMODE.TunableWhite:
				# The fixture is showing its white point, and the hue and
				# saturation it still reports are leftovers from the last
				# colour it held. Reporting them paints the Home app tile deep
				# blue for a light that is plainly white, so report white
				# instead and leave the stale hue alone behind it.
				#
				# `colormode` says the same thing in words ("CCT" / "RGB").

				self._SetCharTry(self.charSaturation, 0)

			else:
				# Never treat a falsy hue as "no colour reported" — fully
				# saturated red reports hue 0, and only saturation tells it
				# apart from white.

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
