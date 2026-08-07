#!/usr/bin/env python3
"""The bridge itself: discovery, accessory construction, and the poll loop.

Not an `Accessory` — this is the layer that owns one, plus the
`AccessoryDriver` that serves it and the `CClient` per device it polls.

There is no push channel on these devices, so polling is the whole story on
the read side. It happens once per device per tick, on the bridge, because
`SnapPoll` reads an entire transformer in three requests no matter how many
fixtures hang off it. Polling per accessory instead would multiply that by the
fixture count and learn nothing extra.
"""

from __future__ import annotations  # Forward refs without quotes

import asyncio
import json
import logging
import signal
import sys

from io import StringIO
from pathlib import Path
from typing import Any, TextIO

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.encoder import AccessoryEncoder

from wac_iot import CClient, CSnapshot, LDiscoBrowse, SDisco, WacError

from .accessory import AID_MAX, AID_MIN, CFixtureAccessory, TierTryFromFixturek
from .netiface import StrAddrResolve

g_log = logging.getLogger(__name__)

BRIDGE_NAME = "WAC Lighting"

# Where the HAP pairing state lives. One file, holding the bridge's own MAC,
# its keypair, and every paired controller — delete it and every Home app in
# the house has to pair again. The default matches the StateDirectory a
# systemd unit would hand this service.

PERSIST_DIR_DEFAULT = Path("/var/lib/wac-homekit")
PERSIST_FILE = "wac_homekit.state"

# 51826 is the port Homebridge made conventional for a HomeKit bridge.
# HAP-python's own default is 51234, which collides with nothing in
# particular but says nothing either.

PORT_DEFAULT = 51826

# Seconds between polls. The vendor offers no push channel, so this is the
# only thing standing between a wall-station press and the Home app noticing:
# a change made outside HomeKit is invisible for up to one interval. Five
# seconds is the responsive end of the range these ESP32-class transformers
# tolerate, and costs three requests per device per tick regardless of how
# many fixtures are on it. Raise it if a transformer starts refusing polls.

POLL_INTERVAL_DEFAULT = 5.0

# Per-request timeout and retry for the poll path, tighter than the library
# defaults. A poll that retries with backoff for half a minute is a poll that
# is still running when the next one is due; one quick retry rides out a
# dropped packet, and anything worse is better reported as unavailable and
# picked up on the next tick.

POLL_TIMEOUT = 5.0
POLL_RETRY = 1


class CDevicePoll:  # tag = dpoll
	"""One device's client and the accessories built from its fixtures."""

	def __init__(self, client: CClient) -> None:
		self.client = client
		self.mpAddrFacc: dict[int, CFixtureAccessory] = {}

	async def Poll(self) -> None:
		"""Read the whole device once and hand each fixture to its accessory."""

		try:
			snap = await self.client.SnapPoll()
		except WacError as exc:
			# One unreachable transformer must not take down the others, and
			# the honest thing to show for it is every one of its lights
			# unavailable rather than a stale value.

			g_log.warning("%s: poll failed: %s", self.client.strHost, exc)

			for facc in self.mpAddrFacc.values():
				facc.MarkOffline()

			return

		for nAddr, facc in self.mpAddrFacc.items():
			facc.Reconcile(snap.mpAddrFixtureKnown.get(nAddr))


class CBridge(Bridge):  # tag = bridge
	"""Every light on every discovered device, behind one HomeKit bridge."""

	def __init__(self, driver: AccessoryDriver, *, dTPoll: float) -> None:
		super().__init__(driver, BRIDGE_NAME)

		self.dTPoll = dTPoll
		self.lDpoll: list[CDevicePoll] = []

	async def FTryAddDevice(self, disco: SDisco) -> bool:
		"""Open a client for a discovered device and bridge its light fixtures.

		False means nothing was added — the device could not be read, or it
		had no fixture this phase handles. Either way it is reported and the
		remaining devices still get their chance.
		"""

		if not disco.strIp:
			g_log.error("%s: advertised no address, skipping", disco.strHost)

			return False

		# Deliberately not disco.nPort. mDNS advertises 443 on every device
		# measured, and 443 refuses the connection on every device measured;
		# the library's own default of plain HTTP on 80 is the one that works.

		client = CClient(disco.strIp, dTTimeout=POLL_TIMEOUT, cRetry=POLL_RETRY)

		try:
			await client.Open()
			snap = await client.SnapPoll()
		except WacError as exc:
			g_log.error("%s: could not be read, skipping: %s", disco.strIp, exc)
			await client.Close()

			return False

		dpoll = self._DpollFromSnap(client, snap)

		if not dpoll.mpAddrFacc:
			g_log.warning("%s: no light fixtures, skipping", disco.strIp)
			await client.Close()

			return False

		self.lDpoll.append(dpoll)

		g_log.info(
			"%s: bridged %d light fixture(s) from device %s",
			disco.strIp,
			len(dpoll.mpAddrFacc),
			snap.StrDeviceId(),
		)

		return True

	def _DpollFromSnap(self, client: CClient, snap: CSnapshot) -> CDevicePoll:
		"""Build an accessory for every light fixture the snapshot knows about.

		Built from `mpAddrFixtureKnown`, not `mpAddrFixture`: the ColorScaping
		transformer reports a pseudo-fixture with empty state that would
		become an accessory unable to report or change anything.
		"""

		dpoll = CDevicePoll(client)

		for nAddr, fixture in snap.mpAddrFixtureKnown.items():
			tier = TierTryFromFixturek(fixture.fixturek)

			if tier is None:
				g_log.info("%s: not a light, skipping — %s", client.strHost, fixture.StrDescribe())

				continue

			facc = CFixtureAccessory(
				self.driver,
				client,
				nAddr=nAddr,
				strFixtureId=snap.StrFixtureId(nAddr),
				fixture=fixture,
				tier=tier,
			)

			facc.aid = self._NAidFree(facc.aid, facc.display_name)

			self.add_accessory(facc)
			dpoll.mpAddrFacc[nAddr] = facc

		return dpoll

	def _NAidFree(self, nAid: int, strName: str) -> int:
		"""The given AID, or the next free one if something already holds it.

		A six-byte digest makes this essentially unreachable, but "essentially"
		is doing real work in that sentence and the alternative failure is a
		light that silently never appears. Probing shifts only the colliding
		accessory, and only for as long as the collision exists — which is a
		worse stability guarantee than the hash gives, hence the warning.
		"""

		if nAid not in self.accessories:
			return nAid

		nAidNext = nAid

		while nAidNext in self.accessories:
			nAidNext = AID_MIN + (nAidNext + 1 - AID_MIN) % (AID_MAX - AID_MIN)

		g_log.warning("%s: AID %d already taken, using %d instead", strName, nAid, nAidNext)

		return nAidNext

	async def CloseClients(self) -> None:
		"""Close every device session this bridge opened."""

		for dpoll in self.lDpoll:
			await dpoll.client.Close()

	async def run(self) -> None:
		"""Poll every device, forever.

		`Bridge.run` normally schedules each contained accessory's own `run`;
		none of ours has one, so overriding it outright loses nothing.

		`Accessory.run_at_interval` is a decorator that takes a literal, and
		the interval is a command-line argument — so it gets applied here
		rather than at class definition. Same loop either way, including the
		part that matters: it waits on the driver's stop event, so shutdown
		does not have to sit out a full interval.
		"""

		await Accessory.run_at_interval(self.dTPoll)(CBridge._PollAll)(self)

	async def _PollAll(self) -> None:
		"""One tick: every device, concurrently."""

		lResult = await asyncio.gather(
			*(dpoll.Poll() for dpoll in self.lDpoll),
			return_exceptions=True,
		)

		# Poll swallows every WacError itself, so anything arriving here is a
		# bug rather than a flaky network. Letting it escape would kill the
		# poll loop silently and leave the bridge answering with stale values
		# forever.

		for dpoll, objResult in zip(self.lDpoll, lResult):
			if isinstance(objResult, BaseException):
				g_log.exception(
					"%s: unexpected error while polling",
					dpoll.client.strHost,
					exc_info=objResult,
				)

	async def stop(self) -> None:
		await super().stop()
		await self.CloseClients()


class CEncoderPretty:  # tag = encp
	"""HAP-python's state encoder, writing JSON a human can read.

	The stock encoder emits the whole file as one long line. That file is the
	only place the bridge's identity lives — its MAC, its keypair, its config
	version, and which controllers are paired with it — so reading it by eye
	is how you answer "is anything actually paired?" without a running
	bridge. Cheap whitespace for a file written once per config change.

	Delegates rather than subclasses, for two reasons. The field list is
	HAP-python's to own, so round-tripping through its encoder means a field
	added upstream shows up here for free instead of silently going missing.
	And `AccessoryDriver` only duck-types this, so nothing has to inherit an
	`Any` base — which keeps the subclassing exemption limited to the two
	modules that genuinely need it.

	`persist` and `load_into` are named by HAP-python's interface, not by our
	conventions.
	"""

	@staticmethod
	def persist(fp: TextIO, state: Any) -> None:
		fpBuf = StringIO()

		AccessoryEncoder.persist(fpBuf, state)

		# Insertion order is kept rather than sorted: HAP-python emits
		# identity first and key material last, which reads better than
		# alphabetical would.

		json.dump(json.loads(fpBuf.getvalue()), fp, indent="\t")

	@staticmethod
	def load_into(fp: TextIO, state: Any) -> None:
		# Unchanged — `json.load` neither knows nor cares about the whitespace,
		# so a file written by either encoder loads under either.

		AccessoryEncoder.load_into(fp, state)


def DriverBuild(
	*,
	pathPersistDir: Path,
	nPort: int,
	strPincode: str | None,
	strAddr: str,
	loop: asyncio.AbstractEventLoop,
) -> AccessoryDriver:
	"""An AccessoryDriver bound to a loop we already own.

	The loop is passed in rather than left to HAP-python because
	`AccessoryDriver.start` installs an `asyncio.SafeChildWatcher`, which
	Python 3.14 removed. Handing it a running loop takes that path out of
	play — `async_start` / `async_stop` do everything `start` does apart from
	owning the loop, which is ours to own anyway: the device sessions live on
	it too.

	`strAddr` is given rather than left to HAP-python for the same class of
	reason: its own choice follows the default route, which moves when a
	laptop is docked, and the advertised address moving is what the Home app
	sees as the bridge disappearing.
	"""

	pathPersistDir.mkdir(parents=True, exist_ok=True)

	return AccessoryDriver(
		address=strAddr,
		port=nPort,
		persist_file=str(pathPersistDir / PERSIST_FILE),
		pincode=strPincode.encode() if strPincode else None,
		encoder=CEncoderPretty(),
		loop=loop,
	)


def PrintNoDevices(dTBrowse: float, strAddr: str) -> None:
	"""Say why an empty network is probably not an empty network.

	The same guidance `wac_iot discover` prints, because the failure looks
	identical and is nearly always the same cause: a blocked process rather
	than an absent device.
	"""

	print(f"no WAC devices answered in {dTBrowse:g}s on {strAddr} — nothing to bridge")
	print()
	print("if the device is on a link this address cannot reach, name the right")
	print("one with --interface (an interface name, an address, or 'wifi').")

	if sys.platform == "darwin":
		print()
		print("this is usually a blocked process, not an empty network. two causes:")
		print("  - macOS: System Settings > Privacy & Security > Local Network")
		print("  - an outbound firewall (Little Snitch and friends) blocking")
		print("    UDP 5353 to 224.0.0.251")
		print()
		print("both judge the *host app* — a shell inside an editor is judged as")
		print("that editor, not as your terminal, so the same command can work in")
		print("one and fail in the other.")
		print()
		print("cross-check with:  dns-sd -B _easylink._tcp local")
		print("that goes through the system mDNS daemon and is not blocked by")
		print("either. if it lists devices and this does not, the process is blocked.")


async def NRun(
	*,
	dTBrowse: float,
	dTPoll: float,
	pathPersistDir: Path,
	nPort: int,
	strPincode: str | None,
	strIface: str,
) -> int:
	"""Discover, bridge, serve, and shut down cleanly. Returns an exit code."""

	# Resolved once and used for both halves, so the interface we browse on
	# and the address we advertise can never drift apart.

	strAddr = StrAddrResolve(strIface)

	g_log.info("bridging on %s", strAddr)

	lDisco = await LDiscoBrowse(dTBrowse, [strAddr])

	if not lDisco:
		PrintNoDevices(dTBrowse, strAddr)

		return 1

	g_log.info("discovered %d device(s)", len(lDisco))

	loop = asyncio.get_running_loop()
	driver = DriverBuild(
		pathPersistDir=pathPersistDir,
		nPort=nPort,
		strPincode=strPincode,
		strAddr=strAddr,
		loop=loop,
	)

	bridge = CBridge(driver, dTPoll=dTPoll)

	for disco in lDisco:
		await bridge.FTryAddDevice(disco)

	if not bridge.accessories:
		g_log.error("no light fixtures found on any discovered device")
		await bridge.CloseClients()

		return 1

	# Only now, because add_accessory writes the persist file and would leave
	# pairing state behind for a bridge that never served anything.

	driver.add_accessory(bridge)

	evStop = asyncio.Event()

	for sig in (signal.SIGINT, signal.SIGTERM):
		loop.add_signal_handler(sig, evStop.set)

	await driver.async_start()

	try:
		await evStop.wait()
	finally:
		for sig in (signal.SIGINT, signal.SIGTERM):
			loop.remove_signal_handler(sig)

		g_log.info("shutting down")
		await driver.async_stop()

	return 0
