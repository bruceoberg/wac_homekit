"""The /fixture endpoint's own decisions.

Two kinds, and neither needs hardware. The parsing helpers see the shapes
real firmware has been seen to return, including the ones the vendor
documentation does not mention. The control path is driven through a stub
transport that records request bodies in order, because the one firmware
quirk `CFixtures` works around is a rule about how many requests a write
takes and what is in each.
"""

from __future__ import annotations  # Forward refs without quotes

import asyncio
import logging

from collections.abc import Coroutine
from typing import Any

import pytest

from wac_iot import CFixtures, WacTransportError
from wac_iot import models


class TestObjTryStateFromControl:
	def test_returns_the_echoed_state(self) -> None:
		"""An action 4 response carries the fixture's whole post-write state."""

		obj: dict[str, Any] = {
			"action": 4,
			"result": "0",
			"staMac": "A0:B7:65:1A:7D:C0",
			"state": {"level": 2000, "online": True, "status": True},
		}

		assert CFixtures.ObjTryStateFromControl(obj) == {
			"level": 2000, "online": True, "status": True,
		}

	def test_reports_what_the_firmware_accepted(self) -> None:
		"""Out-of-range values come back clamped, not as sent — that is the point."""

		obj: dict[str, Any] = {"result": "0", "state": {"mixColorTemp": 6500}}

		objState = CFixtures.ObjTryStateFromControl(obj)

		assert objState is not None
		assert objState["mixColorTemp"] == 6500

	@pytest.mark.parametrize(
		"obj",
		[
			{"action": 4, "result": "0"},
			{"state": None},
			{"state": "ok"},
			{"state": []},
			{},
		],
	)
	def test_returns_none_without_a_usable_state(self, obj: dict[str, Any]) -> None:
		assert CFixtures.ObjTryStateFromControl(obj) is None


class CTransStub:  # tag = trans
	"""A CTransport that records what was asked of it and answers canned.

	`lObjState` is the point: the firmware quirk this exercises is about
	*how many* requests carry *which* fields, so the assertion has to be able
	to read the bodies in order.
	"""

	def __init__(
		self,
		lObjResponse: list[dict[str, Any]] | None = None,
		*,
		excRaise: Exception | None = None,
	) -> None:
		self.lObjState: list[dict[str, Any]] = []
		self.lObjResponse = lObjResponse or []

		# Raised by the *first* request, which is the only failure ordering
		# that says anything: it is what decides whether the off still goes.

		self.excRaise = excRaise

	async def ObjAction(self, strUri: str, nAction: int, **kwargs: Any) -> dict[str, Any]:
		iRequest = len(self.lObjState)

		self.lObjState.append(kwargs["state"])

		if self.excRaise is not None and not iRequest:
			raise self.excRaise

		if iRequest < len(self.lObjResponse):
			return self.lObjResponse[iRequest]

		return {"result": "0"}


def ObjRun(coro: Coroutine[Any, Any, dict[str, Any]]) -> dict[str, Any]:
	"""Drive one control coroutine to completion.

	`asyncio.run` rather than a pytest async plugin: nothing here touches a
	socket, a clock or a task, so a loop per test costs nothing and the suite
	stays free of a plugin it would otherwise need for two files.
	"""

	return asyncio.run(coro)


class TestControlOffLast:
	"""An explicit off has to be a request of its own, or it loses.

	Measured on real hardware: `{red, green, blue, status: false}` left the
	fixture on, because writing colour turns a fixture on as a side effect
	and that implicit turn-on beats the explicit off in the same body. Same
	class of thing for `level`. So the typed `Control*` methods spend a
	second round trip — but only on the batch that actually needs it.
	"""

	ADDR = 167772157

	@staticmethod
	def FixsBuild(trans: CTransStub) -> CFixtures:
		return CFixtures(trans)  # type: ignore[arg-type]

	def test_off_with_colour_is_two_requests_off_last(self) -> None:
		trans = CTransStub()

		ObjRun(self.FixsBuild(trans).ControlRgbw(self.ADDR, fOn=False, tplRgb=(255, 0, 0)))

		assert trans.lObjState == [
			{"red": 255, "green": 0, "blue": 0},
			{"status": False},
		]

	def test_off_with_level_is_two_requests_off_last(self) -> None:
		trans = CTransStub()

		ObjRun(self.FixsBuild(trans).ControlLight(self.ADDR, fOn=False, nLevel=5000))

		assert trans.lObjState == [
			{"level": 5000},
			{"status": False},
		]

	def test_a_lone_off_stays_one_request(self) -> None:
		"""Nothing for it to lose to, so nothing to pay for."""

		trans = CTransStub()

		ObjRun(self.FixsBuild(trans).ControlLight(self.ADDR, fOn=False))

		assert trans.lObjState == [{"status": False}]

	def test_on_with_colour_stays_one_request(self) -> None:
		"""The device turns the fixture on for a colour write anyway, so there
		is no ordering to enforce and no round trip worth spending."""

		trans = CTransStub()

		ObjRun(self.FixsBuild(trans).ControlRgbw(self.ADDR, fOn=True, tplRgb=(0, 255, 0)))

		assert trans.lObjState == [{"status": True, "red": 0, "green": 255, "blue": 0}]

	def test_no_status_at_all_stays_one_request(self) -> None:
		trans = CTransStub()

		ObjRun(self.FixsBuild(trans).ControlWhite(self.ADDR, nLevel=2000, nColorTemp=4000))

		assert trans.lObjState == [{"level": 2000, "mixColorTemp": 4000}]

	def test_the_second_response_is_the_one_returned(self) -> None:
		"""It describes the fixture's final state, which is what a consumer
		folds back in — the first one still describes a fixture that is on."""

		trans = CTransStub([
			{"result": "0", "state": {"status": True, "level": 5000}},
			{"result": "0", "state": {"status": False, "level": 5000}},
		])

		obj = ObjRun(self.FixsBuild(trans).ControlLight(self.ADDR, fOn=False, nLevel=5000))

		assert CFixtures.ObjTryStateFromControl(obj) == {"status": False, "level": 5000}

	def test_a_failed_first_request_propagates_and_sends_no_off(self) -> None:
		"""Deliberate: forcing the off through would be this library inventing
		an error policy, and a light that stays on for one poll interval and
		then reports itself honestly is the better failure."""

		trans = CTransStub(excRaise=WacTransportError("connection reset"))

		with pytest.raises(WacTransportError):
			ObjRun(self.FixsBuild(trans).ControlLight(self.ADDR, fOn=False, nLevel=5000))

		assert trans.lObjState == [{"level": 5000}]


class TestReadNamesTheDevice:
	"""An unknown type has to arrive with the host it was read from.

	The plumbing is what this pins, not the message: `LFixtureFromRead`
	defaults its host to None, so a caller that stops passing one loses the
	address silently rather than failing.
	"""

	class CTransRead:  # tag = trans
		"""Answers one action 3 read, and knows where it is dialling."""

		def __init__(self, strHost: str) -> None:
			self.strHost = strHost

		async def ObjAction(self, strUri: str, nAction: int, **kwargs: Any) -> dict[str, Any]:
			return {"result": "0", "fixture": [{"addr": 1, "type": 4}]}

	def test_an_unknown_type_read_from_a_device_names_it(
		self, caplog: pytest.LogCaptureFixture
	) -> None:
		# Type 4 is the ColorScaping pseudo-fixture, which is exactly the one
		# that warns on a live bridge.

		models.g_setUnknownSeen.clear()

		fixs = CFixtures(self.CTransRead("192.0.2.10"))  # type: ignore[arg-type]

		with caplog.at_level(logging.WARNING, logger="wac_iot.models"):
			lFixture = asyncio.run(fixs.LFixtureRead(1))

		assert len(lFixture) == 1
		assert "192.0.2.10" in caplog.records[0].getMessage()
