#!/usr/bin/env python3
"""The /fixture endpoint, all eight actions."""

from __future__ import annotations  # Forward refs without quotes

import logging

from enum import IntEnum
from typing import Any

from .models import CFixture
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

	async def ObjList(self) -> dict[str, Any]:
		"""Action 5 — list fixture addresses."""

		return await self.trans.ObjAction(URI, self.ACTION.List)

	async def ObjConfigure(self, nAddr: int, objTune: dict[str, Any]) -> dict[str, Any]:
		"""Action 6 — set a fixture's fine-tuning values."""

		return await self.trans.ObjAction(URI, self.ACTION.Configure, addr=nAddr, tune=objTune)

	async def ObjSearch(self) -> dict[str, Any]:
		"""Action 7 — start looking for newly installed fixtures."""

		return await self.trans.ObjAction(URI, self.ACTION.Search)

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
