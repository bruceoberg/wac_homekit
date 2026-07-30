#!/usr/bin/env python3
"""The façade a consumer holds onto.

`CClient` owns a transport and hands out the per-endpoint objects. This is
the seam a Home Assistant integration would build against, so it is the one
type here whose shape is worth guarding.
"""

from __future__ import annotations  # Forward refs without quotes

from typing import Any

from .device import CDevice
from .fixture import CFixtures
from .transport import CTransport

# Endpoints modeled only well enough to talk to. Reachable through
# `ObjAction`; they get real modules once there is a reason to shape them.

URI_GROUP = "/group"
URI_AUTOMATION = "/automation"

# Actions on those endpoints that behave like /fixture action 3 — omitting an
# address returns everything in one response.

ACTION_GROUP_READ = 3
ACTION_AUTOMATION_LIST = 5

# Every fixture belongs to this built-in group.

GROUP_ADDR_ALL = 255


class CClient:  # tag = client
	"""One device, one session.

	Use as an async context manager:

		async with CClient("10.0.0.8") as client:
			devi = await client.device.DeviQuery()
	"""

	def __init__(
		self,
		strHost: str,
		*,
		fTls: bool = False,
		nPort: int | None = None,
		dTTimeout: float = 10.0,
		fVerifyTls: bool = False,
		cRetry: int | None = None,
	) -> None:
		self.trans = CTransport(
			strHost,
			fTls=fTls,
			nPort=nPort,
			dTTimeout=dTTimeout,
			fVerifyTls=fVerifyTls,
			cRetry=cRetry,
		)

		self.device = CDevice(self.trans)
		self.fixture = CFixtures(self.trans)

	@property
	def strHost(self) -> str:
		return self.trans.strHost

	@property
	def strBaseUrl(self) -> str:
		return self.trans.strBaseUrl

	async def __aenter__(self) -> CClient:
		await self.trans.Open()

		return self

	async def __aexit__(self, *args: object) -> None:
		await self.trans.Close()

	async def Open(self) -> None:
		await self.trans.Open()

	async def Close(self) -> None:
		await self.trans.Close()

	async def ObjAction(self, strUri: str, nAction: int, **kwargs: Any) -> dict[str, Any]:
		"""Escape hatch for endpoints without a module yet."""

		return await self.trans.ObjAction(strUri, nAction, **kwargs)

	async def ObjGroupRead(self, addr: int | list[int] | None = None) -> dict[str, Any]:
		"""Read groups. Omitting `addr` returns every group in one response."""

		return await self.ObjAction(URI_GROUP, ACTION_GROUP_READ, addr=addr)

	async def ObjAutomationList(self) -> dict[str, Any]:
		"""List automations (scenes)."""

		return await self.ObjAction(URI_AUTOMATION, ACTION_AUTOMATION_LIST)
