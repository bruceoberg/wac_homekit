#!/usr/bin/env python3
"""The /fixture endpoint, all eight actions."""

from __future__ import annotations  # Forward refs without quotes

import logging

from enum import IntEnum
from typing import Any

from .control import ObjStateFan, ObjStateLight, ObjStateRgbw, ObjStateWhite
from .models import CFixture, LIGHTMODE
from .transport import CTransport

g_log = logging.getLogger(__name__)

URI = "/fixture"


class CFixtures:  # tag = fixs
	"""The /fixture endpoint for one transport.

	Methods returning `Obj` hand back the raw response object; the parsed
	helpers build on them without issuing a second request.
	"""

	class ACTION(IntEnum):
		Create    = 0
		Modify    = 1
		Delete    = 2
		Read      = 3
		Control   = 4
		List      = 5
		Configure = 6
		Search    = 7

	def __init__(self, trans: CTransport) -> None:
		self.trans = trans

	async def ObjCreate(self) -> dict[str, Any]:
		"""Action 0.

		Fixtures are created by being physically installed and identifying
		themselves; this action exists for completeness and does nothing.
		"""

		return await self.trans.ObjAction(URI, self.ACTION.Create)

	async def ObjModify(self, nAddr: int, strName: str) -> dict[str, Any]:
		"""Action 1 — rename a fixture."""

		return await self.trans.ObjAction(URI, self.ACTION.Modify, addr=nAddr, name=strName)

	async def ObjDelete(self, nAddr: int) -> dict[str, Any]:
		"""Action 2 — remove a fixture from the system."""

		return await self.trans.ObjAction(URI, self.ACTION.Delete, addr=nAddr)

	async def ObjRead(self, addr: int | list[int] | None = None) -> dict[str, Any]:
		"""Action 3 — read one fixture, several, or every one.

		Omitting `addr` does NOT do what the documentation claims. Measured
		against ColorScaping firmware 01.04.0149, it returns a summary of
		each fixture (addr, name, type, model, online) with no state, tune,
		or detail — and it omitted a fixture that action 5 lists.

		Passing an explicit address array returns the full structures. Use
		`LFixtureReadAll` unless you specifically want the summary form.
		"""

		return await self.trans.ObjAction(URI, self.ACTION.Read, addr=addr)

	async def ObjControl(self, nAddr: int, objState: dict[str, Any]) -> dict[str, Any]:
		"""Action 4 — set a fixture's state.

		`objState` is in device units and uses the device's own field
		names. Unit conversion belongs to the consumer, not here.
		"""

		return await self.trans.ObjAction(URI, self.ACTION.Control, addr=nAddr, state=objState)

	@staticmethod
	def ObjTryStateFromControl(obj: dict[str, Any]) -> dict[str, Any] | None:
		"""The fixture's post-write state, if the control response carried one.

		Undocumented, and measured on both RGBW and ELV fixtures: an action 4
		response echoes the fixture's whole state after the write. It agrees
		exactly with an immediate read, ramp times included, and it reports
		what the firmware *accepted* — an out-of-range color temperature
		comes back already clamped to the fixture's own bound.

		That makes it the cheapest correction a consumer has. A write that
		was silently adjusted, or that moved a field nobody asked for —
		writing `level` also turns the fixture on — shows up here rather than
		at the next poll.
		"""

		objState = obj.get("state")

		return objState if isinstance(objState, dict) else None

	async def ObjList(self) -> dict[str, Any]:
		"""Action 5 — list fixture addresses."""

		return await self.trans.ObjAction(URI, self.ACTION.List)

	async def ObjConfigure(self, nAddr: int, objTune: dict[str, Any]) -> dict[str, Any]:
		"""Action 6 — set a fixture's fine-tuning values."""

		return await self.trans.ObjAction(URI, self.ACTION.Configure, addr=nAddr, tune=objTune)

	async def ObjSearch(self) -> dict[str, Any]:
		"""Action 7 — start looking for newly installed fixtures."""

		return await self.trans.ObjAction(URI, self.ACTION.Search)

	# Typed control. Action 4 with the state built by `control`, so the rules
	# about which fields may travel together live in one place instead of at
	# every call site. Pick the method matching the fixture's type; anything
	# not covered here still goes through `ObjControl` with a hand-built dict.
	#
	# They route through `_ObjControlOffLast` rather than `ObjControl` because
	# one firmware quirk cannot be expressed in a single request at all — see
	# its docstring. `ObjControl` stays what it says it is: one wire action,
	# raw response.

	async def _ObjControlOffLast(self, nAddr: int, objState: dict[str, Any]) -> dict[str, Any]:
		"""Action 4, split in two when an explicit off travels with anything else.

		Measured: `{red, green, blue, status: false}` sent to a fixture that
		was on left it **on**. Writing colour or `level` turns a fixture on as
		a side effect, and that implicit turn-on beats the explicit
		`status: false` in the same body regardless of ordering. So a request
		carrying an off alongside another field cannot be trusted to turn the
		fixture off, and the only way to express it is two requests with the
		off last.

		The second response is the one returned: it describes the fixture's
		final state, which is what a consumer folds back in.

		Only that combination pays for the extra round trip. A lone
		`status: false`, and anything not turning the fixture off, is one
		request exactly as before. An explicit `status: true` alongside other
		fields is also left alone — the device turns the fixture on for those
		writes anyway, so there is no ordering to enforce.

		**Errors propagate.** If the first request raises, the off is never
		sent and the exception reaches the caller unchanged. Trying the off
		anyway is the obvious thing to add here and is deliberately not done:
		inventing an error policy inside this library is worse than a light
		that stays on for one poll interval and then honestly reports itself
		as on.
		"""

		if objState.get("status") is not False or len(objState) < 2:
			return await self.ObjControl(nAddr, objState)

		objRest = {
			strField: objValue
			for strField, objValue in objState.items()
			if strField != "status"
		}

		await self.ObjControl(nAddr, objRest)

		return await self.ObjControl(nAddr, {"status": False})

	async def ControlLight(
		self,
		nAddr: int,
		*,
		fOn: bool | None = None,
		nLevel: int | None = None,
		fFindme: bool | None = None,
		lightmode: LIGHTMODE | None = None,
	) -> dict[str, Any]:
		"""Control a single color or ELV fixture (0, 6)."""

		return await self._ObjControlOffLast(
			nAddr,
			ObjStateLight(fOn=fOn, nLevel=nLevel, fFindme=fFindme, lightmode=lightmode),
		)

	async def ControlWhite(
		self,
		nAddr: int,
		*,
		fOn: bool | None = None,
		nLevel: int | None = None,
		fFindme: bool | None = None,
		lightmode: LIGHTMODE | None = None,
		nColorTempLevel: int | None = None,
		nColorTemp: int | None = None,
	) -> dict[str, Any]:
		"""Control a tunable white fixture (1, 12, 14, 15)."""

		return await self._ObjControlOffLast(
			nAddr,
			ObjStateWhite(
				fOn=fOn,
				nLevel=nLevel,
				fFindme=fFindme,
				lightmode=lightmode,
				nColorTempLevel=nColorTempLevel,
				nColorTemp=nColorTemp,
			),
		)

	async def ControlRgbw(
		self,
		nAddr: int,
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
		"""Control an RGBW fixture (2)."""

		return await self._ObjControlOffLast(
			nAddr,
			ObjStateRgbw(
				fOn=fOn,
				nLevel=nLevel,
				fFindme=fFindme,
				lightmode=lightmode,
				nHue=nHue,
				nSaturation=nSaturation,
				tplRgb=tplRgb,
				nColorTemp=nColorTemp,
			),
		)

	async def ControlFan(
		self,
		nAddr: int,
		*,
		fOn: bool | None = None,
		nFanSpeed: int | None = None,
		fWind: bool | None = None,
		nWindSpeed: int | None = None,
		fDirection: bool | None = None,
		fFindme: bool | None = None,
	) -> dict[str, Any]:
		"""Control a fan (13)."""

		return await self._ObjControlOffLast(
			nAddr,
			ObjStateFan(
				fOn=fOn,
				nFanSpeed=nFanSpeed,
				fWind=fWind,
				nWindSpeed=nWindSpeed,
				fDirection=fDirection,
				fFindme=fFindme,
			),
		)

	async def Identify(self, nAddr: int, *, fOn: bool = True) -> dict[str, Any]:
		"""Make a fixture announce itself.

		`findme` is shared by every fixture type that has it, so this needs
		no per-type variant. It is what HomeKit's Identify characteristic and
		Home Assistant's button entity both land on.
		"""

		return await self.ObjControl(nAddr, {"findme": fOn})

	async def LFixtureRead(self, addr: int | list[int] | None = None) -> list[CFixture]:
		"""Action 3, parsed into fixtures.

		Unknown fixture types are kept, not dropped — they log a warning at
		construction and resolve to FIXTUREK.Unknown. Filter on
		`FIsKnown()` if you need only the ones this library models.
		"""

		return self.LFixtureFromRead(await self.ObjRead(addr))

	@staticmethod
	def LFixtureFromRead(obj: dict[str, Any]) -> list[CFixture]:
		"""Build fixtures from a response already in hand.

		Separate from `LFixtureRead` so a caller that wants both the raw
		object and the parsed fixtures does not pay for two requests.
		"""

		objFixtures = obj.get("fixture")

		if isinstance(objFixtures, list):
			return [CFixture(objOne) for objOne in objFixtures if isinstance(objOne, dict)]

		# A single-address read returns the fixture's fields inline rather
		# than wrapped in an array.

		if "type" in obj:
			return [CFixture(obj)]

		return []

	async def LFixtureReadAll(self) -> list[CFixture]:
		"""Every fixture, with its full state, tune, and detail.

		Two requests, not one per fixture: action 5 for the addresses, then
		action 3 with all of them at once. The documented one-request form
		(action 3 with `addr` omitted) returns summaries only and has been
		observed to miss fixtures that action 5 lists, so it is not usable
		as a poll.
		"""

		lAddr = await self.LAddrList()

		if not lAddr:
			return []

		return self.LFixtureFromRead(await self.ObjRead(lAddr))

	async def LAddrList(self) -> list[int]:
		"""Action 5, parsed to a list of addresses."""

		obj = await self.ObjList()
		objAddr = obj.get("addr")

		if not isinstance(objAddr, list):
			return []

		return [nAddr for nAddr in objAddr if isinstance(nAddr, int)]
