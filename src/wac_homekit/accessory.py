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

		# The last RGB triple this bridge sent, if the fixture still holds it.
		# See `_FIsRgbOurs`.

		self.tplRgbLast: tuple[int, int, int] | None = None

		# Reported by the last poll that saw this fixture. False also covers a
		# poll that failed outright, which is how a whole unplugged
		# transformer shows up in the Home app as "No Response".

		self.fOnline = True

		self._SetInfo(fixture.detail, strFixtureId)

		# Only the characteristics this tier can honor. A ColorTemperature on
		# a single-color fixture would be a control that silently does
		# nothing, which is worse than not offering it.
		#
		# An RGBW fixture gets one anyway, because the RGB triple drives its
		# colour channels only and never its white LED — measured, and the
		# reason a HomeKit "white" comes out visibly blue. Without a white
		# point there is no way to ask this hardware for a real white.

		lStrChar = [CHAR_BRIGHTNESS]

		if tier is LIGHTTIER.White:
			lStrChar.append(CHAR_COLOR_TEMPERATURE)
		elif tier is LIGHTTIER.Rgbw:
			lStrChar += [CHAR_COLOR_TEMPERATURE, CHAR_HUE, CHAR_SATURATION]

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
			if tier is not LIGHTTIER.Dimmable else None
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

		setStrChar = set(mpStrValue)

		# RGB and the white point are two views of one colour state on the
		# wire, and the device refuses a request carrying both. The pending
		# set must not accumulate both either — so the newer of the two
		# displaces the older, which is exactly what switching between the
		# Home app's colour and temperature tabs means.

		if CHAR_COLOR_TEMPERATURE in setStrChar:
			self.setStrCharPending -= {CHAR_HUE, CHAR_SATURATION}
		elif setStrChar & {CHAR_HUE, CHAR_SATURATION}:
			self.setStrCharPending -= {CHAR_COLOR_TEMPERATURE}

		self.setStrCharPending |= setStrChar

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
					# Only one of the two colour axes is ever set here — see
					# the pending-set handling in `_OnSetService` — because
					# the builder refuses both, and rightly: each one drags
					# the fixture's `mode` along behind it.

					objControl = await self.client.fixture.ControlRgbw(
						self.nAddr,
						fOn=fOn,
						nLevel=nLevel,
						tplRgb=tplRgb,
						nColorTemp=nColorTemp,
					)

		except WacError as exc:
			g_log.error("%s: control failed: %s", self.display_name, exc)

		# Stamped even when the write failed, because a request that timed out
		# may still have reached the fixture — a poll read before it is no
		# more trustworthy than one read before a success.

		self.tControlLast = asyncio.get_running_loop().time()

		if objControl is not None and tplRgb is not None:
			self.tplRgbLast = tplRgb

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

	def _FIsRgbOurs(self, state: SStateRgbw) -> bool:
		"""Whether the fixture is holding exactly the colour this bridge sent.

		When it is, the hue and saturation it reports are our own values
		round-tripped through an 8-bit triple and back, and the trip is lossy
		— worst at low saturation, where the triple spans a dozen levels out
		of 255. Measured: HomeKit asked for hue 251, the fixture recomputed
		253.8, and the user's chosen swatch moved under them a second later.

		While the device agrees with us, what the user picked is the better
		record of it. A colour set anywhere else — the wall, the WAC app —
		fails this test and reconciles normally.
		"""

		if self.tplRgbLast is None:
			return False

		return (state.red, state.green, state.blue) == self.tplRgbLast

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

		# A tunable white fixture has only the one colour axis, so its white
		# point is always the truth.

		if isinstance(state, SStateWhite):
			self._SetCharTryColorTemp(state.mixColorTemp)

		if isinstance(state, SStateRgbw):
			# An RGBW fixture has two, and HomeKit treats ColorTemperature and
			# Hue/Saturation as two views of one state rather than as
			# independent controls. Reporting both at once is a contradiction,
			# and the Home app renders the blend — a fully saturated red plus
			# a 5208K white point painted the tile flesh-coloured for a light
			# that was plainly red.
			#
			# So only the axis the fixture is actually rendering gets
			# reported, and `mode` is what says which. The other is left
			# holding whatever it last had, which is also what the user would
			# return to on that tab.

			if state.mode is LIGHTMODE.TunableWhite:
				# Showing its white point. The hue and saturation it still
				# reports are leftovers from the last colour it held, and
				# reporting them paints the tile that colour for a white
				# light, so say white and leave the stale hue behind it.
				#
				# `colormode` says the same thing in words ("CCT" / "RGB"),
				# but the test is deliberately "is it CCT" rather than "is it
				# RGB": the chromatic family has more than one `mode` value.

				self._SetCharTryColorTemp(state.mixColorTemp)
				self._SetCharTry(self.charSaturation, 0)

			elif not self._FIsRgbOurs(state):
				# Never treat a falsy hue as "no colour reported" — fully
				# saturated red reports hue 0, and only saturation tells it
				# apart from white.

				if state.hue is not None:
					self._SetCharTry(self.charHue, NDegFromHue(state.hue))

				if state.saturation is not None:
					self._SetCharTry(self.charSaturation, NPctFromSaturation(state.saturation))

	def _SetCharTryColorTemp(self, nKelvin: int | None) -> None:
		"""Report a white point, in the mireds HomeKit wants."""

		if self.charColorTemp is None or nKelvin is None:
			return

		self._SetCharTry(self.charColorTemp, self.ctrange.NMiredFromKelvin(nKelvin))

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
