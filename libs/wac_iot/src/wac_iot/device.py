#!/usr/bin/env python3
"""The /device endpoint.

This endpoint is the odd one out: it carries no action number and decides
what to do from which fields are present, so it does not fit the action
dispatch every other endpoint uses.

As in `models`, field names are the device's own JSON names verbatim.
"""

from __future__ import annotations  # Forward refs without quotes

from typing import Any

from .models import SWac
from .transport import CTransport

URI = "/device"


class SNwkState(SWac):  # tag = nwks
	"""Network status, as reported inside a device query."""

	provisioned: bool | None   = None
	commissioned: bool | None  = None
	ssid: str | None           = None
	ipAddr: str | None         = None
	netmask: str | None        = None
	connectMethod: str | None  = None
	ethIpAddr: str | None      = None
	staIpAddr: str | None      = None
	bssid: str | None          = None
	auth: str | None           = None
	channel: int | None        = None

	# rssi is documented as a number but observed as a string, so it stays
	# untyped rather than failing a poll over a formatting choice.

	rssi: Any = None


class SDeviceInfo(SWac):  # tag = devi
	"""Everything a device query reports about the device itself."""

	deviceName: str | None    = None
	owner: str | None         = None
	staMac: str | None        = None
	apMac: str | None         = None
	bleMac: str | None        = None
	dateCode: str | None      = None
	iotmVer: str | None       = None
	restVer: str | None       = None
	scmVer: str | None        = None
	time: str | None          = None
	timeZone: str | None      = None
	locationId: str | None    = None
	builtFor: str | None      = None
	bootCount: int | None     = None
	uptimeSeconds: int | None = None
	accessoryType: int | None = None
	networkState: int | None  = None
	features: list[str] | None = None
	nwkState: SNwkState | None = None

	# systemType is documented as a number but observed as a string, and
	# tzoffset as a number but observed as "none". Both stay untyped.

	systemType: Any = None
	tzoffset: Any   = None

	# Contents vary by system type; not worth modeling until a real device
	# says otherwise.

	systemSpecificParams: Any = None


class CDevice:  # tag = device
	"""The /device endpoint for one transport."""

	def __init__(self, trans: CTransport) -> None:
		self.trans = trans

	async def ObjQuery(self) -> dict[str, Any]:
		"""Read the device information, raw.

		Read-only — a query carries no mutating field.

		The value must be boolean true. ColorScaping firmware rejects the
		numeric 1 the documentation shows in its example.
		"""

		return await self.trans.ObjPost(URI, {"query": True})

	async def DeviQuery(self) -> SDeviceInfo:
		"""Read the device information, parsed."""

		return SDeviceInfo.model_validate(await self.ObjQuery())
