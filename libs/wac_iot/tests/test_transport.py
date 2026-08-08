"""Transport behaviour that does not need a device: request serialization."""

from __future__ import annotations  # Forward refs without quotes

import asyncio

from typing import Any

from wac_iot.transport import CTransport


class CTransportCounting(CTransport):  # tag = trans
	"""A transport that records how many posts are in flight at once.

	The retry layer is replaced rather than mocked at the HTTP level: what is
	under test is the lock in `ObjPost`, and going anywhere near aiohttp would
	only test aiohttp.
	"""

	def __init__(self, strHost: str = "10.0.0.1") -> None:
		super().__init__(strHost)

		self.cInFlight = 0
		self.cInFlightMax = 0
		self.cPost = 0

	async def _ObjPostRetry(self, strUri: str, obj: dict[str, Any]) -> dict[str, Any]:
		self.cInFlight += 1
		self.cInFlightMax = max(self.cInFlightMax, self.cInFlight)
		self.cPost += 1

		# Long enough that overlap would be certain without the lock: every
		# coroutine reaches this await before any of them resumes.

		await asyncio.sleep(0.01)

		self.cInFlight -= 1

		return {"result": "0"}


def test_posts_to_one_device_never_overlap() -> None:
	"""Concurrent posts to a single device are serialized.

	The hardware has few connection slots, and an overlapping poll and control
	write produced timeouts and connection resets against a real transformer.
	"""

	async def Go() -> CTransportCounting:
		trans = CTransportCounting()

		await trans.Open()

		try:
			await asyncio.gather(*(trans.ObjPost("/fixture", {}) for _ in range(6)))
		finally:
			await trans.Close()

		return trans

	trans = asyncio.run(Go())

	assert trans.cPost == 6          # every request still happened
	assert trans.cInFlightMax == 1   # but never two at once


def test_separate_devices_are_not_serialized_against_each_other() -> None:
	"""The lock is per device. Two transformers still talk at the same time."""

	async def Go() -> tuple[CTransportCounting, CTransportCounting]:
		transA = CTransportCounting("10.0.0.1")
		transB = CTransportCounting("10.0.0.2")

		await transA.Open()
		await transB.Open()

		try:
			await asyncio.gather(
				transA.ObjPost("/fixture", {}),
				transB.ObjPost("/fixture", {}),
			)
		finally:
			await transA.Close()
			await transB.Close()

		return transA, transB

	transA, transB = asyncio.run(Go())

	assert transA.cPost == 1
	assert transB.cPost == 1
