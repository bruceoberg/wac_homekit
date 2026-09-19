"""The façade's one composed call.

`SnapPoll` is three requests behind one name, and the only judgement in it is
refusing to make the second and third for a device that was never going to
answer them.
"""

from __future__ import annotations  # Forward refs without quotes

import asyncio

from typing import Any

import pytest

from wac_iot import CClient, CFixture, SDeviceInfo, WacNoFixturesError


class CDeviceStub:  # tag = device
	"""The /device endpoint, answering one canned identity."""

	def __init__(self, devi: SDeviceInfo) -> None:
		self.devi = devi

	async def DeviQuery(self) -> SDeviceInfo:
		return self.devi


class CFixturesStub:  # tag = fixtures
	"""The /fixture endpoint, counting whether it was asked at all."""

	def __init__(self, *lObjFixture: dict[str, Any]) -> None:
		self.lObjFixture = lObjFixture
		self.cRead = 0

	async def LFixtureReadAll(self) -> list[CFixture]:
		self.cRead += 1

		return [CFixture(obj) for obj in self.lObjFixture]


def ClientStubbed(devi: SDeviceInfo, fixtures: CFixturesStub) -> CClient:
	"""A real CClient with both endpoints replaced.

	Real rather than stubbed outright because `SnapPoll` is the thing under
	test and it lives on this class; the transport underneath it is never
	reached.
	"""

	client = CClient("10.0.0.5")

	client.device = CDeviceStub(devi)  # type: ignore[assignment]
	client.fixture = fixtures  # type: ignore[assignment]

	return client


class TestSnapPoll:
	def test_a_fixture_host_is_read_normally(self) -> None:
		fixtures = CFixturesStub({"addr": 167772157, "type": 2})
		devi = SDeviceInfo.model_validate({"staMac": "AABBCC09FFFD", "systemType": "colorscaping"})

		snap = asyncio.run(ClientStubbed(devi, fixtures).SnapPoll())

		assert set(snap.mpAddrFixtureKnown) == {167772157}
		assert fixtures.cRead == 1

	def test_a_wall_station_raises_without_reading_fixtures(self) -> None:
		"""The whole point: a consumer gets a device that says what it is,
		rather than the HTTP 404 the fixture read would have produced."""

		fixtures = CFixturesStub()
		devi = SDeviceInfo.model_validate(
			{"staMac": "AABBCC09FFFE", "systemType": "invisiLED_Wall", "deviceName": "wallstation"},
		)

		with pytest.raises(WacNoFixturesError) as excinfo:
			asyncio.run(ClientStubbed(devi, fixtures).SnapPoll())

		assert not fixtures.cRead
		assert excinfo.value.strSystemType == "invisiLED_Wall"
		assert "invisiLED_Wall" in str(excinfo.value)
