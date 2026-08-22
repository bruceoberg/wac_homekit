#!/usr/bin/env python3
"""mDNS discovery, in two independent halves.

`DiscoFromTxt` is pure: a TXT-record mapping in, a discovery result out, no
I/O and no Zeroconf anywhere in its signature. `CBrowser` owns a Zeroconf
instance and feeds it, either as a one-shot browse or — as `CWatcher` — as a
stream of change events that runs for as long as the consumer wants it to.

The split is deliberate. A Home Assistant integration receives its own
service info from HA's shared Zeroconf instance and must never start a
second one — it calls the parser directly and skips both browser and watcher
entirely.
"""

from __future__ import annotations  # Forward refs without quotes

import asyncio
import logging

from collections.abc import Coroutine, Mapping
from enum import IntEnum, auto
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .models import SWac

# Zeroconf backs `CBrowser` and `CWatcher` and nothing else, so it is an extra
# rather than a hard dependency: `pip install wac_iot[discovery]`. A consumer
# with its own mDNS stack — Home Assistant, which hands every integration a
# shared instance — installs the bare package and calls `DiscoFromTxt`
# directly. Importing it unconditionally would force a second Zeroconf into
# that process, so the imports happen inside the methods that need them.

if TYPE_CHECKING:
	from zeroconf import ServiceStateChange, Zeroconf
	from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf


def FIsZeroconfAvailable() -> bool:
	"""Whether the `discovery` extra is importable.

	Attempts the import rather than looking for the module on disk: what
	matters to `CBrowser` is whether the import will succeed, and a package
	present but broken should answer no here instead of failing later with a
	worse message.
	"""

	try:
		import zeroconf  # noqa: F401
	except ImportError:
		return False

	return True

g_log = logging.getLogger(__name__)

SERVICE_TYPE = "_easylink._tcp.local."

# Device hostnames and mDNS instance names end in the tail of the station MAC,
# after an underscore-separated prefix. The prefix varies by product line —
# WAC_CS_ on a ColorScaping transformer, WAC_WCT_ on an InvisiLED wall station
# — and the vendor documentation names a third spelling that no hardware has
# been seen to use. Matching on the trailing hex rather than a known prefix
# keeps the next product from needing a code change.

HOSTNAME_MAC_LEN = 6

# TXT keys as the device spells them, including the literal spaces. Lookup is
# normalized, so these are the canonical spelling rather than the only one
# accepted.

TXT_FIRMWARE_VER = "Firmware Ver"
TXT_PROTOCOL = "Protocol"
TXT_PROTOCOL_VER = "Protocol Ver"
TXT_MAC = "MAC"


class SDisco(SWac):  # tag = disco
	"""One discovered device."""

	strHost: str                        # mDNS instance / host name
	strIp: str | None = None
	nPort: int | None = None
	strFirmwareVer: str | None = None
	strProtocol: str | None = None
	strProtocolVer: str | None = None
	strMac: str | None = None
	strMacSuffix: str | None = None     # tail of the MAC, parsed from the host name
	mpStrTxt: dict[str, str] = Field(default_factory=dict)  # every TXT pair, decoded


class DISCOK(IntEnum):  # tag = discok — kinds of discovery change
	"""What happened to a device's advertisement.

	`Removed` is the weak one. A device that loses power never sends a
	goodbye, so its record simply expires minutes later; one that reboots may
	drop and return inside a second. Report it faithfully and treat it as
	advisory — see `CWatcher`.
	"""

	Added   = auto()
	Updated = auto()
	Removed = auto()


class SDevent(BaseModel):  # tag = devent — one change to what mDNS is advertising
	"""One discovery change: what happened, and to which device.

	Carries the whole `SDisco` rather than a delta. A consumer reacting to an
	address change wants the new address, and one reacting to an arrival
	wants everything; splitting that into "what moved" would only mean
	rejoining it at every call site.

	For a `Removed`, the `SDisco` is the last one resolved — the device is by
	definition not answering, so there is nothing fresher to carry.
	"""

	model_config = ConfigDict(frozen=True)

	discok: DISCOK
	disco: SDisco


def DiscokTryFromDisco(discoPrev: SDisco | None, discoCur: SDisco) -> DISCOK | None:
	"""What a freshly resolved advertisement means, given the last one seen.

	None means nothing changed. Devices re-announce themselves on a timer and
	after every network hiccup, so without this a consumer would see a steady
	drip of updates carrying data it already has — and the one update that
	matters, a DHCP lease moving the address, would be lost in it.
	"""

	if discoPrev is None:
		return DISCOK.Added

	if discoPrev == discoCur:
		return None

	return DISCOK.Updated


def _StrDecode(obj: Any) -> str | None:
	"""Decode a TXT key or value that may arrive as bytes or str."""

	if obj is None:
		return None

	if isinstance(obj, bytes):
		return obj.decode("utf-8", errors="replace")

	if isinstance(obj, str):
		return obj

	return str(obj)


def _StrNormKey(strKey: str) -> str:
	"""Fold a TXT key for lookup.

	The device uses keys with literal spaces, and firmware revisions have not
	been consistent about spacing or case. Comparing on a folded form means a
	rename to `firmwareVer` would not silently drop the field.
	"""

	return "".join(strKey.split()).lower()


def MpStrTxtNormalize(mpTxt: Mapping[Any, Any]) -> dict[str, str]:
	"""Decode a raw TXT mapping into plain str → str.

	Zeroconf hands over bytes keys and values; Home Assistant hands over
	str. Both work.
	"""

	mpStrTxt: dict[str, str] = {}

	for objKey, objValue in mpTxt.items():
		strKey = _StrDecode(objKey)

		if strKey is None:
			continue

		mpStrTxt[strKey] = _StrDecode(objValue) or ""

	return mpStrTxt


def StrTryMacSuffix(strHost: str) -> str | None:
	"""Pull the MAC tail out of a device host name.

	Takes whatever follows the last underscore, provided it looks like the
	tail of a MAC. Returns None for a name that does not follow the
	convention, rather than guessing.
	"""

	# The instance name may arrive fully qualified with the service type
	# still attached.

	strBare = strHost.split(".")[0]

	strPrefix, strSep, strSuffix = strBare.rpartition("_")

	if not strSep or not strPrefix:
		return None

	if len(strSuffix) != HOSTNAME_MAC_LEN:
		return None

	try:
		int(strSuffix, 16)
	except ValueError:
		return None

	return strSuffix


def DiscoFromTxt(
	mpTxt: Mapping[Any, Any],
	*,
	strHost: str,
	strIp: str | None = None,
	nPort: int | None = None,
) -> SDisco:
	"""Build a discovery result from a TXT-record mapping.

	Pure, and free of any Zeroconf type. Keys and values may be bytes or
	str; keys are matched ignoring case and internal spaces. Unrecognized
	pairs are preserved in `mpStrTxt` rather than dropped.
	"""

	mpStrTxt = MpStrTxtNormalize(mpTxt)
	mpStrLookup = {_StrNormKey(strKey): strValue for strKey, strValue in mpStrTxt.items()}

	def StrField(strKey: str) -> str | None:
		strValue = mpStrLookup.get(_StrNormKey(strKey))

		return strValue or None

	return SDisco(
		strHost=strHost,
		strIp=strIp,
		nPort=nPort,
		strFirmwareVer=StrField(TXT_FIRMWARE_VER),
		strProtocol=StrField(TXT_PROTOCOL),
		strProtocolVer=StrField(TXT_PROTOCOL_VER),
		strMac=StrField(TXT_MAC),
		strMacSuffix=StrTryMacSuffix(strHost),
		mpStrTxt=mpStrTxt,
	)


def DiscoFromServiceInfo(info: AsyncServiceInfo) -> SDisco:
	"""Adapt a Zeroconf service info into the pure parser's inputs."""

	lStrIp = info.parsed_scoped_addresses()

	return DiscoFromTxt(
		info.properties,
		strHost=info.name,
		strIp=lStrIp[0] if lStrIp else None,
		nPort=info.port,
	)


class CBrowser:  # tag = browser
	"""Owns a Zeroconf instance and browses for devices.

	Only for standalone use. A Home Assistant integration gets service info
	from HA's own Zeroconf and should call `DiscoFromTxt` directly.
	"""

	g_dTResolve = 3.0  # seconds to wait for a service's details

	def __init__(self, lStrAddr: list[str] | None = None) -> None:
		if not FIsZeroconfAvailable():
			# Named after whatever was actually constructed, so a CWatcher
			# does not report a problem with CBrowser.

			raise RuntimeError(
				f"{type(self).__name__} needs the zeroconf package: install "
				"wac_iot[discovery]. To parse service info from an mDNS stack you "
				"already have, call DiscoFromTxt instead."
			)

		self.azc: AsyncZeroconf | None = None
		self.mpStrDisco: dict[str, SDisco] = {}
		self.lTask: list[asyncio.Task[None]] = []

		# Local IPv4 addresses to browse from, or None for every interface.
		# A machine with two links onto the same subnet — a laptop on wifi and
		# a dock at once — otherwise browses on whichever one Zeroconf
		# happens to pick. Plain addresses, so nothing about how the caller
		# chose them leaks in here.

		self.lStrAddr = lStrAddr

	async def __aenter__(self) -> CBrowser:
		from zeroconf.asyncio import AsyncZeroconf

		# Zeroconf's own default is every interface, and it is not the same
		# thing as passing a list of all of them — so branch rather than
		# compute a list to hand over.

		if self.lStrAddr:
			self.azc = AsyncZeroconf(interfaces=self.lStrAddr)
		else:
			self.azc = AsyncZeroconf()

		return self

	async def __aexit__(self, *args: object) -> None:
		for task in self.lTask:
			task.cancel()

		if self.azc is not None:
			await self.azc.async_close()
			self.azc = None

	async def LDiscoBrowse(self, dTBrowse: float = 5.0) -> list[SDisco]:
		"""Browse for the device service and return what answered.

		Blocks for the full browse window; there is no way to know an
		enumeration is complete on an unmanaged network.
		"""

		from zeroconf.asyncio import AsyncServiceBrowser

		if self.azc is None:
			raise RuntimeError("CBrowser must be used as an async context manager")

		self.mpStrDisco.clear()

		browser = AsyncServiceBrowser(
			self.azc.zeroconf,
			[SERVICE_TYPE],
			handlers=[self._OnServiceStateChange],
		)

		try:
			await asyncio.sleep(dTBrowse)

			if self.lTask:
				await asyncio.gather(*self.lTask, return_exceptions=True)
		finally:
			await browser.async_cancel()
			self.lTask.clear()

		return sorted(self.mpStrDisco.values(), key=lambda disco: disco.strHost)

	def _OnServiceStateChange(
		self,
		zeroconf: Zeroconf,
		service_type: str,
		name: str,
		state_change: ServiceStateChange,
	) -> None:
		"""Zeroconf callback. Runs on Zeroconf's thread, so it only schedules."""

		from zeroconf import ServiceStateChange

		if state_change is ServiceStateChange.Removed:
			self.mpStrDisco.pop(name, None)

			return

		self.lTask.append(asyncio.ensure_future(self._ResolveService(service_type, name)))

	async def _ResolveService(self, strServiceType: str, strName: str) -> None:
		from zeroconf.asyncio import AsyncServiceInfo

		if self.azc is None:
			return

		info = AsyncServiceInfo(strServiceType, strName)

		if not await info.async_request(self.azc.zeroconf, int(self.g_dTResolve * 1000)):
			g_log.debug("no details resolved for %s", strName)

			return

		self.mpStrDisco[strName] = DiscoFromServiceInfo(info)


class CWatcher(CBrowser):  # tag = watcher
	"""A browse that never ends, reported as a stream of change events.

		async with CWatcher([strAddr]) as watcher:
			async for devent in watcher:
				...

	An async iterator rather than a set of callbacks, deliberately. A
	consumer's reaction to a device appearing is usually itself async — open
	a session, read the device — and an `async for` lets that happen in the
	consumer's own task with no re-entrancy to reason about. Wrapping this in
	callbacks is a three-line loop; going the other way is not.

	Zeroconf's notifications may arrive on a thread that is not the consumer's
	loop, so the handoff happens here: the callback does nothing but
	`call_soon_threadsafe` onto the loop this watcher was entered on. Nothing
	a consumer touches is ever reached from another thread.

	**`Removed` is advisory.** mDNS goodbyes are best-effort — a device that
	loses power sends none, and its record lingers until it expires — while a
	device that reboots can be removed and re-added inside a second. This
	reports what mDNS said and nothing more; deciding what "gone" means is
	consumer policy, and the honest test is whether the device answers a
	request. Debouncing it here would only hide the raw signal from a consumer
	that has a better test available.
	"""

	def __init__(self, lStrAddr: list[str] | None = None) -> None:
		super().__init__(lStrAddr)

		self.browser: AsyncServiceBrowser | None = None

		# The loop the watcher was entered on, which is the only one events are
		# ever delivered to. Captured in `__aenter__` rather than at
		# construction, so building one outside a running loop is fine.

		self.loop: asyncio.AbstractEventLoop | None = None

		# Unbounded. The alternative is dropping events, and the event most
		# worth having — a device's address moving — is exactly the one a
		# consumer cannot recover from having missed. mDNS traffic for a
		# handful of lighting controllers is nowhere near a volume worth
		# bounding, and a consumer that stops iterating is a bug rather than a
		# case to survive.
		#
		# None is the end-of-stream sentinel `__aexit__` pushes.

		self.queueDevent: asyncio.Queue[SDevent | None] = asyncio.Queue()

	async def __aenter__(self) -> CWatcher:
		from zeroconf.asyncio import AsyncServiceBrowser

		await super().__aenter__()

		assert self.azc is not None  # narrowed for mypy; the base guarantees it

		self.loop = asyncio.get_running_loop()

		self.browser = AsyncServiceBrowser(
			self.azc.zeroconf,
			[SERVICE_TYPE],
			handlers=[self._OnServiceStateChange],
		)

		return self

	async def __aexit__(self, *args: object) -> None:
		if self.browser is not None:
			await self.browser.async_cancel()
			self.browser = None

		# Ends any `async for` parked on the queue, rather than leaving it
		# waiting on a watcher that has stopped watching.

		self.queueDevent.put_nowait(None)

		await super().__aexit__(*args)

	def __aiter__(self) -> CWatcher:
		return self

	async def __anext__(self) -> SDevent:
		devent = await self.queueDevent.get()

		if devent is None:
			raise StopAsyncIteration

		return devent

	def _OnServiceStateChange(
		self,
		zeroconf: Zeroconf,
		service_type: str,
		name: str,
		state_change: ServiceStateChange,
	) -> None:
		"""Zeroconf's callback, which may run on any thread. Only hands off.

		`call_soon_threadsafe` is correct from the loop's own thread too, and
		Zeroconf does not promise which thread this arrives on — so it is used
		unconditionally rather than after a check that could go stale.
		"""

		if self.loop is None:
			return

		self.loop.call_soon_threadsafe(self._Dispatch, service_type, name, state_change)

	def _Dispatch(self, strServiceType: str, strName: str, state_change: ServiceStateChange) -> None:
		"""Turn one notification into an event. Runs on the consumer's loop."""

		from zeroconf import ServiceStateChange

		if state_change is ServiceStateChange.Removed:
			self._Remove(strName)

			return

		# Both Added and Updated need the service resolved before there is
		# anything worth reporting, and which of the two it is depends on what
		# comes back — a re-announcement arrives as Added.

		self._TaskTrack(self._ResolveAndReport(strServiceType, strName))

	def _TaskTrack(self, cr: Coroutine[Any, Any, None]) -> None:
		"""Run a resolve, holding a reference to it until it finishes.

		The list is the base class's, so `__aexit__` cancels whatever is still
		in flight. Removing on completion matters here in a way it does not
		for a one-shot browse: a watcher lives as long as the process, and a
		device re-announcing itself every few minutes would otherwise leave a
		finished task behind every time.
		"""

		task = asyncio.ensure_future(cr)

		self.lTask.append(task)
		task.add_done_callback(self.lTask.remove)

	async def _ResolveAndReport(self, strServiceType: str, strName: str) -> None:
		"""Resolve a service and queue an event, if anything actually changed."""

		from zeroconf.asyncio import AsyncServiceInfo

		if self.azc is None:
			return

		info = AsyncServiceInfo(strServiceType, strName)

		if not await info.async_request(self.azc.zeroconf, int(self.g_dTResolve * 1000)):
			g_log.debug("no details resolved for %s", strName)

			return

		disco = DiscoFromServiceInfo(info)
		discok = DiscokTryFromDisco(self.mpStrDisco.get(strName), disco)

		self.mpStrDisco[strName] = disco

		if discok is None:
			return

		g_log.debug("%s: %s", strName, discok.name.lower())

		self.queueDevent.put_nowait(SDevent(discok=discok, disco=disco))

	def _Remove(self, strName: str) -> None:
		"""Report a service going away, using the last details resolved for it."""

		disco = self.mpStrDisco.pop(strName, None)

		if disco is None:
			# Never resolved in the first place, so a consumer was never told
			# it existed and has nothing to undo.

			return

		g_log.debug("%s: removed", strName)

		self.queueDevent.put_nowait(SDevent(discok=DISCOK.Removed, disco=disco))


async def LDiscoBrowse(
	dTBrowse: float = 5.0,
	lStrAddr: list[str] | None = None,
) -> list[SDisco]:
	"""Convenience one-shot browse, optionally pinned to local addresses."""

	async with CBrowser(lStrAddr) as browser:
		return await browser.LDiscoBrowse(dTBrowse)
