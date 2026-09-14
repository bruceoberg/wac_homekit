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
import os
import signal
import sys

from io import StringIO
from pathlib import Path
from typing import Any, TextIO

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.encoder import AccessoryEncoder
from pyhap.hap_handler import HAPServerHandler

from wac_iot import DISCOK, CClient, CSnapshot, CWatcher, SDevent, SDisco, WacError

from .accessory import AID_MAX, AID_MIN, CFixtureAccessory, TierTryFromFixturek
from .netiface import StrAddrResolve
from .notify import CNotifier, SStatus

g_log = logging.getLogger(__name__)

BRIDGE_NAME = "WAC Lighting"

# Where the HAP pairing state lives. One file, holding the bridge's own MAC,
# its keypair, and every paired controller — delete it and every Home app in
# the house has to pair again.
#
# Two homes for it, and which one is right depends on who is running the
# bridge. Under systemd it is the StateDirectory the unit declares; for a
# person trying the bridge out it has to be somewhere writable without root,
# because a first run that dies on mkdir is a first run that teaches nothing.
# `PathPersistResolve` picks between them.

PERSIST_DIR_SERVICE = Path("/var/lib/wac-homekit")
PERSIST_DIR_NAME = "wac-homekit"
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

# Seconds a fixture must be absent from a *reachable* device's polls before its
# accessory is taken off the bridge. Zero is off, and off is the default.
#
# Removal is destructive in a way that putting the fixture back does not undo:
# iOS drops the accessory's room, its name, and its membership in every scene
# and automation, and a fixture that returns comes back as a stranger even
# though its AID is derived and identical. Against that, the cost of never
# removing is a light stuck on "No Response" until the bridge is restarted —
# an annoyance. So the default errs where the cheap mistake is.

FORGET_MISSING_DEFAULT = 0.0


class CDevicePoll:  # tag = dpoll
	"""One device's client and the accessories built from its fixtures."""

	def __init__(self, client: CClient, *, strDeviceId: str) -> None:
		self.client = client

		# The device's own identifier, from its MAC. The bridge keys these by
		# mDNS instance name, which is what survives a DHCP lease; this
		# survives a rename as well, and is what catches one transformer
		# turning up under two names.

		self.strDeviceId = strDeviceId

		self.mpAddrFacc: dict[int, CFixtureAccessory] = {}

		# Addresses already looked at and declined — a fan, a wall station, a
		# fixture type this library does not model. Remembered so the poll can
		# spot a genuinely new fixture with a set comparison, and so the "not a
		# light" line is logged once rather than every five seconds forever.

		self.setNAddrSkip: set[int] = set()

		# Consecutive *successful* polls in which each bridged address went
		# unreported. The only evidence this bridge trusts for removal — see
		# `SetNAddrForget` for why nothing else counts.

		self.mpAddrCMiss: dict[int, int] = {}

	async def Poll(self) -> CSnapshot | None:
		"""Read the whole device once and hand each fixture to its accessory.

		Returns the snapshot so the bridge can look for fixtures that have
		appeared since the last tick; None means the device did not answer.
		"""

		# Stamped before the read rather than after it. What an accessory needs
		# to know is whether it was written while this data was in flight, and
		# reading a whole transformer takes a second or more.

		tPoll = asyncio.get_running_loop().time()

		try:
			snap = await self.client.SnapPoll()
		except WacError as exc:
			# One unreachable transformer must not take down the others, and
			# the honest thing to show for it is every one of its lights
			# unavailable rather than a stale value.

			g_log.warning("%s: poll failed: %s", self.client.strHost, exc)

			for facc in self.mpAddrFacc.values():
				facc.MarkOffline()

			return None

		for nAddr, facc in self.mpAddrFacc.items():
			facc.Reconcile(snap.mpAddrFixtureKnown.get(nAddr), tPoll=tPoll)

		return snap

	def SetNAddrUnbridged(self, snap: CSnapshot) -> set[int]:
		"""Addresses in this snapshot that have no accessory and no verdict yet.

		Pure set arithmetic on data the poll already fetched, so the common
		answer — the empty set, every tick, forever — costs nothing.
		"""

		return snap.mpAddrFixtureKnown.keys() - self.mpAddrFacc.keys() - self.setNAddrSkip

	def SetNAddrForget(self, snap: CSnapshot, *, cMissForget: int) -> set[int]:
		"""Bridged addresses this snapshot proves are gone. Removes nothing.

		Kept apart from the removal itself, the way `SetNAddrUnbridged` is
		kept apart from `_CFaccAdd`: the decision is ours and testable without
		HAP-python, the mechanics are HAP-python's.

		**Only one signal counts, and this is it:** a device answered a poll
		successfully, right now, and its fixture list no longer carries an
		address this bridge holds an accessory for. That means the fixture was
		pulled from the track or deleted in the WAC app. Everything else that
		looks like disappearance is not evidence at all — an unreachable
		device is a power cut, a reboot or a lease change caught mid-flight;
		an mDNS `Removed` is advisory; and a whole device going quiet is
		indistinguishable from someone unplugging a transformer for the
		afternoon. Which is why this is only ever reached from a poll that
		*succeeded*, and why devices are never removed at all.

		**Never called on a failed poll** — `Poll` returns None for that and
		`_PollAll` skips. The counters must not move then: a device
		unreachable for an hour has to come back to exactly the counts it
		left with.

		Hysteresis on top of that, because a bulk fixture read has been seen
		to omit fixtures it should have listed. `SnapPoll` works around that
		with an explicit address array, but the conservative reading is that
		an absence may still be transient, so an address has to be missing
		from `cMissForget` consecutive successful polls before it earns
		removal. Zero disables it outright, whatever the counts say.

		The caller is expected to act on what comes back. An address that has
		earned removal and is not removed goes on earning it every tick.
		"""

		# Addresses whose accessory has already gone — removed last tick, or
		# never built. Nothing to count for them.

		self.mpAddrCMiss = {
			nAddr: cMiss
			for nAddr, cMiss in self.mpAddrCMiss.items()
			if nAddr in self.mpAddrFacc
		}

		setNAddrForget: set[int] = set()

		for nAddr in self.mpAddrFacc:
			if nAddr in snap.mpAddrFixtureKnown:
				# Back, or never away. A run of misses that did not reach the
				# threshold buys nothing towards the next one.

				self.mpAddrCMiss.pop(nAddr, None)

				continue

			cMiss = self.mpAddrCMiss.get(nAddr, 0) + 1
			self.mpAddrCMiss[nAddr] = cMiss

			if cMissForget and cMiss >= cMissForget:
				setNAddrForget.add(nAddr)

		return setNAddrForget


class CBridge(Bridge):  # tag = bridge
	"""Every light on every discovered device, behind one HomeKit bridge."""

	def __init__(
		self,
		driver: AccessoryDriver,
		*,
		dTPoll: float,
		cMissForget: int = 0,
		notifier: CNotifier | None = None,
	) -> None:
		super().__init__(driver, BRIDGE_NAME)

		self.dTPoll = dTPoll

		# Consecutive missing polls before a fixture's accessory is taken off
		# the bridge; zero never removes anything. In polls rather than in
		# seconds because that is what the counters count — `CMissForget` does
		# the arithmetic once, at startup, against the interval in force.

		self.cMissForget = cMissForget

		# Where the `systemctl status` line comes from, when there is one.
		# Optional because nothing but the real run needs it: a bridge built
		# in a test has no systemd to talk to and no reason to pretend.

		self.notifier = notifier

		# Every bridged device, keyed by the mDNS instance name it was
		# discovered under. Keyed by that rather than by address because the
		# address is the thing that moves: a DHCP lease change re-announces
		# the same name somewhere else, and matching on it is how that becomes
		# a re-point instead of a second copy of every light.

		self.mpStrDpoll: dict[str, CDevicePoll] = {}

		# Whether the driver is serving yet. Before it is, there is no
		# advertisement to update and no controller to tell — see
		# `_ConfigChanged`.

		self.fServing = False

	async def FTryAddDevice(self, disco: SDisco) -> bool:
		"""Open a client for a discovered device and bridge its light fixtures.

		False means nothing was added — already bridged, unreadable, or
		carrying no fixture this phase handles. Either way it is reported and
		whatever else is being added still gets its chance.

		Callers must not run two of these at once for the same device. Nothing
		here enforces it, because nothing needs to: every call site is the
		single-threaded event handler in `WatchAsync`, which awaits one event
		to completion before taking the next. A device announcing itself three
		times while its first add is still reading the transformer therefore
		finds it already in `mpStrDpoll` by the time its second event is
		looked at.
		"""

		if disco.strHost in self.mpStrDpoll:
			# A backstop, not the real guard. `OnDeviceSeen` decides
			# add-versus-follow before getting here, and it is the one that
			# knows a re-announcement of a bridged device may carry a new
			# address. Reaching this line means a caller skipped that.

			g_log.debug("%s: already bridged", disco.strHost)

			return False

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
			strDeviceId = snap.StrDeviceId()
		except WacError as exc:
			g_log.error("%s: could not be read, skipping: %s", disco.strIp, exc)
			await client.Close()

			return False

		if any(dpoll.strDeviceId == strDeviceId for dpoll in self.mpStrDpoll.values()):
			# The same transformer answering under a second mDNS name — a
			# rename leaving the old record still cached, most plausibly.
			# Bridging it twice would mean two accessories per fixture built
			# from the same stable fixture id, so the second of each would
			# collide on AID and get shifted off it.

			g_log.info("%s: device %s is already bridged, skipping", disco.strHost, strDeviceId)
			await client.Close()

			return False

		dpoll = CDevicePoll(client, strDeviceId=strDeviceId)

		if not self._CFaccAdd(dpoll, snap):
			g_log.warning("%s: no light fixtures, skipping", disco.strIp)
			await client.Close()

			return False

		self.mpStrDpoll[disco.strHost] = dpoll

		g_log.info(
			"%s: bridged %d light fixture(s) from device %s",
			disco.strIp,
			len(dpoll.mpAddrFacc),
			strDeviceId,
		)

		self._ConfigChanged()

		return True

	def _CFaccAdd(self, dpoll: CDevicePoll, snap: CSnapshot) -> int:
		"""Build an accessory for each of this device's lights that lacks one.

		Serves both the first snapshot of a device and every one after it: a
		fixture commissioned into a running system shows up in the next poll
		and becomes an accessory by exactly this route, with no separate path
		to keep in step.

		Built from `mpAddrFixtureKnown`, not `mpAddrFixture`: the ColorScaping
		transformer reports a pseudo-fixture with empty state that would
		become an accessory unable to report or change anything.

		Returns how many were added, so the caller can tell whether the
		accessory list moved and decide once — rather than once per fixture —
		to say so.
		"""

		cFacc = 0

		for nAddr in sorted(dpoll.SetNAddrUnbridged(snap)):
			fixture = snap.mpAddrFixtureKnown[nAddr]
			tier = TierTryFromFixturek(fixture.fixturek)

			if tier is None:
				g_log.info(
					"%s: not a light, skipping — %s",
					dpoll.client.strHost,
					fixture.StrDescribe(),
				)

				dpoll.setNAddrSkip.add(nAddr)

				continue

			facc = CFixtureAccessory(
				self.driver,
				dpoll.client,
				nAddr=nAddr,
				strFixtureId=snap.StrFixtureId(nAddr),
				fixture=fixture,
				tier=tier,
			)

			facc.aid = self._NAidFree(facc.aid, facc.display_name)

			self.add_accessory(facc)
			dpoll.mpAddrFacc[nAddr] = facc
			cFacc += 1

		return cFacc

	def _CFaccForget(self, dpoll: CDevicePoll, setNAddr: set[int]) -> int:
		"""Take the accessories for these addresses off the bridge, for good.

		The mechanics half of the removal; `SetNAddrForget` is the decision
		half and is the one with the judgement in it. Returns how many went,
		so the caller batches the config change per device rather than per
		fixture.

		`Bridge.accessories` is the whole of what HAP-python consults —
		`to_HAP`, `get_accessories` and `get_characteristic` all read it and
		nothing else holds a second list. What it does *not* clean up is
		`driver.topics`, the per-(aid, iid) event subscription registry, so
		that is done here: left behind, a late reconcile would push an event
		for an accessory the controller has just been told does not exist.

		Logged at warning rather than info on purpose. This is destructive and
		not undone by putting the fixture back — iOS loses the accessory's
		room, its name, and its place in every scene and automation — so
		someone reading the journal to work out where a light went should find
		it without turning on debug.

		Deliberately *not* added to `setNAddrSkip`: a fixture that comes back
		should be picked up by the ordinary unbridged path and rebuilt.

		A control request already in flight for this accessory is left to
		finish, and that is right: the user asked for it, the device still
		answers at that address, and `_ControlAsync` holds the client rather
		than the bridge. What it does on the way back is nothing — the
		reconcile pushes values into characteristics nobody is subscribed to
		any more, because the topics went with the accessory above, so
		`driver.publish` returns before it reaches a socket.

		What HAP-python does *not* survive is a controller writing to the
		removed AID before it refetches `/accessories`.
		`AccessoryDriver.get_characteristics` checks for the missing
		accessory and skips it; `set_characteristics` does not, and raises
		`AttributeError` on `None`. asyncio turns that into a dropped HAP
		connection with a traceback in the log; iOS reconnects, refetches,
		and carries on. The window is between `config_changed` and that
		refetch, and it is narrow — but a bridge with `--forget-missing` on
		may show one, and it is HAP-python's bug rather than a sign the
		removal went wrong.
		"""

		for nAddr in sorted(setNAddr):
			facc = dpoll.mpAddrFacc.pop(nAddr)

			self.accessories.pop(facc.aid, None)
			self._TopicsForget(facc.aid)

			g_log.warning(
				"%s: removing %s — fixture %d gone from %d consecutive polls; "
				"its Home app room, name, scenes and automations go with it",
				dpoll.client.strHost,
				facc.display_name,
				nAddr,
				dpoll.mpAddrCMiss.get(nAddr, self.cMissForget),
			)

		return len(setNAddr)

	def _TopicsForget(self, nAid: int) -> None:
		"""Drop every event subscription belonging to one accessory.

		HAP-python keys `driver.topics` by `f"{aid}.{iid}"`, and an aid is an
		integer, so the prefix match is exact. Reaching into the dict rather
		than through `async_subscribe_client_topic` because the public route
		wants one call per (client, topic) pair and there is nothing to
		unsubscribe *from* any more — the accessory is gone either way.
		"""

		strPrefix = f"{nAid}."

		# Materialized before deleting, because this is the driver's own dict
		# and mutating it under iteration raises.

		lStrTopic = [
			strTopic
			for strTopic in self.driver.topics
			if strTopic.startswith(strPrefix)
		]

		for strTopic in lStrTopic:
			del self.driver.topics[strTopic]

	def _ConfigChanged(self) -> None:
		"""Tell paired controllers the accessory list moved.

		Every call rewrites the persist file and bumps the advertised config
		number, so callers batch it — once per device that contributed
		fixtures, never once per fixture.

		A no-op until the driver is serving. During the startup grace window
		there is nothing advertising and nobody paired, and calling it then
		would write pairing state for a bridge the driver has not been handed
		yet.
		"""

		if not self.fServing:
			return

		self.driver.config_changed()

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

	# -----------------------------------------------------------------------
	# Discovery, for as long as the bridge runs
	# -----------------------------------------------------------------------

	async def GraceAsync(self, watcher: CWatcher, dTGrace: float) -> None:
		"""Handle discovery events for a fixed window before first serving.

		The devices are nearly always already on the network when the bridge
		starts, and a bridge that comes up empty and fills in over the next
		few seconds shows a controller an accessory list that changes right
		after it connected. Waiting the window out costs a few seconds once
		and makes the ordinary case arrive complete.

		Not a deadline on anything: whatever has not answered by the end
		arrives through `WatchAsync` instead and is bridged at runtime.
		"""

		loop = asyncio.get_running_loop()
		tEnd = loop.time() + dTGrace

		while True:
			dTLeft = tEnd - loop.time()

			if dTLeft <= 0:
				return

			try:
				devent = await asyncio.wait_for(anext(watcher), dTLeft)
			except (TimeoutError, StopAsyncIteration):
				return

			await self.OnDevent(devent)

	async def WatchAsync(self, watcher: CWatcher) -> None:
		"""Handle discovery events for the rest of the process's life."""

		async for devent in watcher:
			try:
				await self.OnDevent(devent)
			except Exception:
				# Deliberately broad. Whatever went wrong with one device, a
				# bridge that quietly stops noticing every *other* device is a
				# far worse outcome — and the symptom, months later, is a new
				# light that never appears.

				g_log.exception("%s: error handling discovery event", devent.disco.strHost)

	async def OnDevent(self, devent: SDevent) -> None:
		"""React to one discovery change. All of the bridge's mDNS policy.

		Serialized by its callers, one event at a time — see `FTryAddDevice`.
		"""

		match devent.discok:
			case DISCOK.Added | DISCOK.Updated:
				await self.OnDeviceSeen(devent.disco)

			case DISCOK.Removed:
				# Advisory, and acted on by doing nothing. An mDNS goodbye is
				# best-effort — a device that loses power sends none at all,
				# and one that reboots can go and return inside a second — so
				# the only trustworthy liveness test is whether it answers a
				# request, which the poll loop already makes every few seconds
				# and already turns into "No Response" on every one of that
				# device's lights. A device that comes back resumes polling
				# with no ceremony.
				#
				# Still nothing, now that `--forget-missing` can remove an
				# accessory, and for a sharper reason than before: removal is
				# destructive and a missing packet is not evidence. The only
				# thing that earns it is a device *answering* and not
				# mentioning a fixture it used to have — which this event is
				# the precise opposite of. Devices are never removed at all.

				g_log.debug("%s: mDNS says gone; leaving it to the poll", devent.disco.strHost)

	async def OnDeviceSeen(self, disco: SDisco) -> None:
		"""Bridge a device, or follow one already bridged to where it now is.

		Added and Updated land here together, deliberately. Which of the two
		mDNS calls an announcement depends only on whether the watcher still
		held a cached record — and a power cycle reliably destroys that cache:
		the device goes away, its record is dropped, and it comes back as an
		*Added* even though this bridge has been holding a client for it the
		whole time. Trusting that distinction would leave a transformer that
		rebooted onto a new DHCP lease stranded on its old address forever,
		which is the exact failure this watch exists to prevent.

		So the add-versus-follow decision comes from the bridge's own state,
		which knows what it is holding, rather than from mDNS's opinion of
		what is new.
		"""

		dpoll = self.mpStrDpoll.get(disco.strHost)

		if dpoll is None:
			# Never bridged, or bridged and then dropped for having no lights.
			# Either way the announcement is its next chance — a device that
			# was simply unreachable the first time round gets picked up here.

			await self.FTryAddDevice(disco)

			return

		if not disco.strIp or disco.strIp == dpoll.client.strHost:
			# Already pointed at the right place. An announcement that moved
			# something else — a firmware version after an OTA, say — lands
			# here too, and nothing the bridge holds is built on any of it.

			return

		# Re-pointed rather than rebuilt, and the accessories are left
		# completely alone: they are what iOS paired with, and their AIDs,
		# their names and their current values all have to survive a move the
		# user never even sees.

		g_log.info("%s: moved to %s", disco.strHost, disco.strIp)

		dpoll.client.SetHost(disco.strIp)

	# -----------------------------------------------------------------------
	# Serving
	# -----------------------------------------------------------------------

	async def CloseClients(self) -> None:
		"""Close every device session this bridge opened."""

		for dpoll in self.mpStrDpoll.values():
			await dpoll.client.Close()

	def NotifyStatus(self) -> None:
		"""Tell systemd what this bridge is currently doing.

		Every count is read off state the bridge already holds, so this is
		cheap enough to call on every poll tick and let `CNotifier` decide
		whether anything is worth sending.
		"""

		if self.notifier is None:
			return

		cLight = 0
		cLightOffline = 0

		for dpoll in self.mpStrDpoll.values():
			for facc in dpoll.mpAddrFacc.values():
				cLight += 1

				if not facc.fOnline:
					cLightOffline += 1

		cClientPaired = len(self.driver.state.paired_clients)

		self.notifier.Notify(
			SStatus(
				# Withheld once paired, for the reason `PrintSetupCode`
				# spends a docstring on: the bridge would refuse it.

				strPincode=None if cClientPaired else self.driver.state.pincode.decode(),
				cClientPaired=cClientPaired,
				cDevice=len(self.mpStrDpoll),
				cLight=cLight,
				cLightOffline=cLightOffline,
			)
		)

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

		# Snapshotted, because a discovery event handled while this tick is in
		# flight may add a device — and a dict that changes size during
		# iteration raises.

		lDpoll = list(self.mpStrDpoll.values())

		lResult = await asyncio.gather(
			*(dpoll.Poll() for dpoll in lDpoll),
			return_exceptions=True,
		)

		# Poll swallows every WacError itself, so anything arriving here is a
		# bug rather than a flaky network. Letting it escape would kill the
		# poll loop silently and leave the bridge answering with stale values
		# forever.

		for dpoll, objResult in zip(lDpoll, lResult):
			if isinstance(objResult, BaseException):
				g_log.exception(
					"%s: unexpected error while polling",
					dpoll.client.strHost,
					exc_info=objResult,
				)

				continue

			if objResult is None:
				continue

			# Removal first, on the same snapshot, so a fixture that went and
			# came back inside one tick — an address reused by a replacement,
			# say — is removed and rebuilt in the right order rather than
			# added and then immediately taken away again.
			#
			# Reached only from a poll that answered. That is the whole of the
			# evidence rule: see `SetNAddrForget`.

			cFaccMoved = self._CFaccForget(
				dpoll,
				dpoll.SetNAddrForget(objResult, cMissForget=self.cMissForget),
			)

			# A fixture commissioned into a running system arrives here — the
			# poll already read it, so noticing costs a set comparison and no
			# extra request. Batched per device: one config change however
			# many fixtures a single device contributed, in either direction.
			#
			# Guarded because building an accessory needs the snapshot to
			# identify itself, which a device that answered but reported no
			# MAC cannot do. That is worth a line in the log and nothing more
			# — it must not be what ends the poll loop, and it must not
			# swallow a removal that already happened.

			try:
				cFaccMoved += self._CFaccAdd(dpoll, objResult)
			except WacError as exc:
				g_log.error("%s: could not add a new fixture: %s", dpoll.client.strHost, exc)

			if cFaccMoved:
				self._ConfigChanged()

		# Unconditional, and deliberately not guarded by any "did anything
		# change" test here. `CNotifier` already suppresses the resend, and
		# working it out twice is how the two answers drift apart.

		self.NotifyStatus()

	def setup_message(self) -> None:
		"""Nothing. `PrintSetupCode` says all of this, and says it better.

		HAP-python prints its own pairing block here — but only when the
		bridge is unpaired, and its text advises installing
		`HAP-python[QRCode]` for a feature this bridge already provides
		without it. Left in place it would contradict the QR code printed a
		moment later.
		"""

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


def PathPersistResolve(
	pathGiven: Path | None,
	*,
	pathService: Path = PERSIST_DIR_SERVICE,
) -> Path:
	"""Where the pairing state should live, given what the user asked for.

	Three cases, in order:

	1. `--persist-dir` was given. Honored exactly, including failing later on
	   a directory that cannot be created — a bridge that quietly pairs
	   somewhere other than where it was told is worse than one that refuses
	   to start.
	2. The service directory already exists and is writable. That is systemd:
	   `StateDirectory=wac-homekit` creates it before the unit runs, so its
	   presence is a reliable signal that this is the service and not a
	   person at a terminal.
	3. Otherwise the per-user state directory, created on demand. This is
	   what makes a blind first run work at all — case 2's directory needs
	   root to create, and someone trying the bridge out should not need it.

	The service check is `os.access` rather than `exists()` because a
	directory that is there but not writable is no use, and the point of
	looking is to avoid a crash rather than to detect systemd for its own
	sake. A nonexistent path answers False to the same call, so one test
	covers both.
	"""

	if pathGiven is not None:
		return pathGiven

	if os.access(pathService, os.W_OK | os.X_OK):
		return pathService

	# XDG's own rule: an unset *or empty* variable falls back to the default.

	strStateHome = os.environ.get("XDG_STATE_HOME")
	pathStateHome = Path(strStateHome) if strStateHome else Path.home() / ".local" / "state"

	return pathStateHome / PERSIST_DIR_NAME


def CMissForget(dTForget: float, dTPoll: float) -> int:
	"""Consecutive missing polls that `--forget-missing SECONDS` comes to.

	Seconds are what a user can reason about; polls are what the counters
	count. Converting once here rather than comparing elapsed time per
	fixture keeps the decision a pure integer comparison, which is also what
	makes it testable.

	The floor is 1, not 0: a threshold shorter than one poll interval means
	"as soon as possible", and rounding it down to zero would silently mean
	"never" — the same value that disables the feature. Zero is reserved for
	the user actually asking for off.
	"""

	if dTForget <= 0:
		return 0

	if dTPoll <= 0:
		# Not reachable from the CLI as it stands, but dividing by it would
		# turn a nonsense interval into a traceback at startup rather than
		# into the only sensible reading of it, which is "every poll".

		return 1

	return max(1, int(dTForget / dTPoll))


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

	It is given *twice*, and the second one is the point. `address=` decides
	what HAP-python binds and what goes in the advertised A record;
	`interface_choice=` decides which interfaces its Zeroconf actually
	multicasts that record over. Left unset, HAP-python builds an
	`AsyncZeroconf()` with Zeroconf's own default of every interface — which
	on a machine with a VPN up means announcing from a socket bound to
	`0.0.0.0` onto a `utun` that carries no multicast, and an `EHOSTDOWN`
	traceback for every announcement. Harmless, because the record's
	*content* was always the pinned address, but it was the last place the
	interface choice leaked.

	The list is a plain list of addresses, which is exactly what `CWatcher`
	is handed for the browse side — so the same value pins all three things
	and they cannot drift.

	Deliberately not `async_zeroconf_instance=`, which would share the
	watcher's own instance and looks like the tidier fix. `async_stop` calls
	`advertiser.async_close()` unconditionally, on an instance it was given
	just as readily as on one it built — so sharing would have HAP-python
	closing the watcher's Zeroconf out from under it, listeners and all.
	Two instances on one pinned interface is the cheaper answer.
	"""

	pathPersistDir.mkdir(parents=True, exist_ok=True)

	return AccessoryDriver(
		address=strAddr,
		port=nPort,
		persist_file=str(pathPersistDir / PERSIST_FILE),
		pincode=strPincode.encode() if strPincode else None,
		encoder=CEncoderPretty(),
		loop=loop,
		interface_choice=[strAddr],
	)


def PrintNoDevices(dTBrowse: float, strAddr: str) -> None:
	"""Say why an empty network is probably not an empty network.

	The same guidance `wac_iot discover` prints, because the failure looks
	identical and is nearly always the same cause: a blocked process rather
	than an absent device.

	Printed and then carried on from, since the bridge now serves an empty
	bridge and waits. Worth printing anyway: a user who is looking at this
	because their lights never appeared needs the diagnosis, and by the time
	they notice, the startup window is long past.
	"""

	print(f"no WAC devices answered in {dTBrowse:g}s on {strAddr}")
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


def PrintQrXhm(strXhmUri: str) -> None:
	"""Render an X-HM setup URI as a QR code on the terminal.

	`invert=True` because the quiet zone has to read as the *light* side and
	the modules as the dark one: inverted, the border is drawn with block
	characters, which on the dark terminal this normally runs in — a shell,
	or `journalctl` — is exactly right. On a light background it comes out
	reversed, which most scanners still read.
	"""

	import qrcode

	qr = qrcode.QRCode(border=2)

	qr.add_data(strXhmUri)
	qr.print_ascii(invert=True)


def PrintSetupCode(
	strPincode: str,
	strXhmUri: str | None,
	*,
	cClientPaired: int,
	pathPersist: Path,
) -> None:
	"""Show how to pair with this bridge, or why there is nothing to show.

	**A paired accessory cannot be paired again from its setup code.** It
	advertises `sf=0`, refuses `/pair-setup`, and takes further controllers
	only through one already paired — which is what the Home app does when
	it adds a hub or a family member. So once `paired_clients` is non-empty
	the code and the QR are not merely redundant, they are a trap: a
	controller pointed at them fails with a message ("Accessory Not Found")
	that names the wrong problem entirely.

	This is not hypothetical, and the orphan case is the nasty one. Deleting
	a bridge in the Home app removes it from *iOS's* database and does not
	reliably tell the accessory — measured here, with both paired clients
	still in the persist file afterwards. From the phone the bridge looks
	gone and ready to re-pair; from the bridge it is still paired and
	refusing. The line below is what makes those two views comparable
	without a packet capture, and the `--unpair` line below is what gets
	someone out of it without going near the state file.

	When there is a code worth showing, the digits come first and grouped
	twice: HAP-python prints them 3-2-3, which is the format the protocol
	hashes, while the Home app's manual entry offers two groups of four.
	Printing both saves regrouping eight digits by eye at the exact moment a
	mistyped one reads as a pairing failure. The QR follows, and is additive
	— the digits are on screen before anything about rendering it can go
	wrong.

	Note the code is *not* stable across restarts: HAP-python persists the
	keypair and the paired clients but neither the pincode nor the setup id,
	so an unpaired restart without `--pincode` prints a fresh code and a
	fresh QR. Which is exactly why this prints on every startup — under
	systemd it makes `journalctl -u wac-homekit` enough to pair with, with no
	file to go and read.
	"""

	if cClientPaired:
		# The recovery path names the file outright rather than pointing at
		# the log line above it. Someone reading this is looking at a bridge
		# that will not pair and has just been told why; making them go and
		# find the path is the last thing that should stand in their way.

		print(f"paired with {cClientPaired} controller(s) — no setup code applies")
		print("to pair another, add it from a controller already paired with this bridge.")
		print()
		print("if no controller has it any more — deleted from the Home app while this")
		print("bridge was not running, so the removal never reached it — nothing here can")
		print("notice: iOS does not contact a bridge that says it is paired. restart with")
		print("--unpair to forget them, and this prints a setup code instead of this.")

		return

	strDigits = strPincode.replace("-", "")

	print(f"setup code: {strDigits[:4]} {strDigits[4:]}   (entered as {strPincode})")

	if strXhmUri is None:
		return

	print()

	try:
		PrintQrXhm(strXhmUri)
	except Exception as exc:
		# Deliberately broad, and deliberately quiet. Nothing about rendering
		# a QR code is worth failing a startup over, and the digits above are
		# already on screen.

		g_log.debug("could not render the setup QR code: %s", exc)
		print(f"setup URI: {strXhmUri}")


# The HAP setup payload, which is what an X-HM URI carries: a version, some
# reserved bits, the accessory category, a flags nibble, and the eight-digit
# setup code, packed into 47 bits and written in base 36.
#
# Built here rather than through `Accessory.xhm_uri`, which does exactly this
# and would be the obvious thing to call. It is unusable without the
# `HAP-python[QRCode]` extra: `base36` is imported only under HAP-python's own
# `SUPPORT_QR_CODE` flag, so calling it in a plain install raises NameError
# from inside the method. Installing that extra to reach it would also drag in
# `pyqrcode`, which would then print a second QR code of its own next to ours.
#
# The layout is HAP's, not HAP-python's, and has been stable across the
# protocol's life — the encoding below is not tracking an implementation
# detail that can move under it.

XHM_PREFIX = "X-HM://"
XHM_PAYLOAD_LEN = 9        # base-36 digits, zero-padded
XHM_FLAG_IP = 2            # this bridge is reachable over IP

XHM_BITS_RESERVED = 4
XHM_BITS_CATEGORY = 8
XHM_BITS_FLAGS = 4
XHM_BITS_PINCODE = 27

g_strBase36Digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def StrBase36(nValue: int) -> str:
	"""A non-negative integer in base 36, digits 0-9 then A-Z."""

	if not nValue:
		return g_strBase36Digits[0]

	lStrDigit: list[str] = []

	while nValue:
		nValue, iDigit = divmod(nValue, 36)
		lStrDigit.append(g_strBase36Digits[iDigit])

	return "".join(reversed(lStrDigit))


def StrXhmUri(nCategory: int, strPincode: str, strSetupId: str) -> str:
	"""The setup URI a HomeKit controller expects behind a pairing QR code."""

	nPayload = 0                                    # version, always zero so far

	nPayload = (nPayload << XHM_BITS_RESERVED)
	nPayload = (nPayload << XHM_BITS_CATEGORY) | (nCategory & 0xFF)
	nPayload = (nPayload << XHM_BITS_FLAGS) | XHM_FLAG_IP
	nPayload = (nPayload << XHM_BITS_PINCODE) | (int(strPincode.replace("-", "")) & 0x7FFFFFFF)

	return XHM_PREFIX + StrBase36(nPayload).rjust(XHM_PAYLOAD_LEN, "0") + strSetupId


def StrTryXhmUri(bridge: CBridge) -> str | None:
	"""The bridge's X-HM setup URI, or None if it could not be built.

	Guarded because it is decoration: it reaches into driver state for the
	pincode and the setup id, and a shape this has not seen must not be what
	stops a bridge from starting.
	"""

	try:
		return StrXhmUri(
			bridge.category,
			bridge.driver.state.pincode.decode(),
			bridge.driver.state.setup_id,
		)
	except Exception as exc:
		g_log.debug("could not build the setup URI: %s", exc)

		return None


def CUnpairAll(driver: AccessoryDriver) -> int:
	"""Forget every paired controller. Returns how many there were.

	Through `driver.unpair` rather than at the state directly, so the persist
	file is rewritten by the same path an ordinary removal takes. One at a
	time and re-read each round, because removing the last admin clears the
	rest from under the loop.

	The MAC and the keypair are left alone, which is the whole reason this
	exists rather than `rm`: the bridge keeps its identity and loses only the
	controllers that no longer exist.
	"""

	cClient = len(driver.state.paired_clients)

	while driver.state.paired_clients:
		driver.unpair(next(iter(driver.state.paired_clients)))

	return cClient


class CPairingWatch:  # tag = pwat
	"""Makes a bridge that has just become unpaired say so, and mean it.

	iOS unpairs by sending `RemovePairing` over a HAP connection, and
	HAP-python handles that correctly — clears the client, re-advertises
	`sf=1`. What it does not do is leave the bridge usable: the setup code was
	withheld at startup because the bridge was paired at the time, so it now
	sits there pairable and mute, and the sessions opened under the pairing
	that just vanished are still open. This closes both gaps, and it is the
	whole of what a *running* bridge can do about being deleted.

	**A bridge that was not running when it was deleted cannot be helped from
	here, and that is measured, not assumed.** iOS never contacts an accessory
	advertising `sf=0` — not to pair with it, not to be refused by it. Typing
	its setup code into the Home app produced no TCP connection at all, twice,
	against a real orphaned bridge on the same LAN. So there is no request to
	notice, and no in-band evidence that a pairing has gone stale. Recovery is
	`--unpair`, which is a person deciding — see `CUnpairAll`.

	Installed by wrapping `HAPServerHandler.handle_pairings`, which is the
	only seam available: the removal is handled inside the handler and the
	driver is told nothing that distinguishes it from any other unpair.
	Wrapping the method rather than subclassing the class keeps this to the
	one entry point that can leave the bridge unpaired, and survives
	HAP-python building handlers wherever it likes. It runs on the event loop,
	so the persist and the advertisement update are safe to reach from here.
	"""

	def __init__(self, bridge: CBridge, *, pathPersist: Path) -> None:
		self.bridge = bridge

		# Named only to be printed in the recovery advice, but printed at the
		# one moment someone needs it.

		self.pathPersist = pathPersist

	def Install(self) -> None:
		"""Wrap the handler method. Called once, for the life of the process."""

		fnPairings = HAPServerHandler.handle_pairings

		def HandlePairings(handler: Any) -> None:
			fPaired = handler.state.paired

			fnPairings(handler)

			# Only the transition, and only the last one out. Dropping one
			# controller of several is not a bridge coming free, and
			# HAP-python re-advertises for that case by itself.

			if fPaired and not handler.state.paired:
				self._OnUnpaired(handler.client_address)

		HAPServerHandler.handle_pairings = HandlePairings

	def _OnUnpaired(self, tplPeerKeep: tuple[str, int]) -> None:
		"""Announce that this bridge is pairable again, and say how."""

		g_log.warning("no longer paired with any controller — pairable again")

		self._CloseOther(tplPeerKeep)

		PrintSetupCode(
			self.bridge.driver.state.pincode.decode(),
			StrTryXhmUri(self.bridge),
			cClientPaired=0,
			pathPersist=self.pathPersist,
		)

		# The case the status line earns its keep on. The bridge has just
		# become pairable with nobody watching the journal, and this puts
		# the new setup code one `systemctl status` away.

		self.bridge.NotifyStatus()

	def _CloseOther(self, tplPeerKeep: tuple[str, int]) -> None:
		"""Close every HAP connection except the one being answered.

		A session outlives the pairing it was established under — its keys are
		the session's, not the pairing's — so a controller that has just been
		unpaired goes on reading and writing until something happens to drop
		the socket. That is the "it still thinks it has a connection" half of
		this: the pairing is gone but the conversation is not.

		The connection being answered is spared. It still has a response to
		send, and closing it would abort the very remove-pairing that got us
		here. Closing is clean — HAP-python's `connection_lost` unsubscribes
		the peer from its event topics on the way out.
		"""

		mpPeerProto = self.bridge.driver.http_server.connections

		for tplPeer, proto in list(mpPeerProto.items()):
			if tplPeer == tplPeerKeep:
				continue

			g_log.info("dropping the HAP connection from %s", tplPeer)

			proto.close()


async def NRun(
	*,
	dTBrowse: float,
	dTPoll: float,
	dTForget: float = FORGET_MISSING_DEFAULT,
	pathPersistDir: Path | None,
	nPort: int,
	strPincode: str | None,
	strIface: str,
	fRequireDevices: bool,
	fUnpair: bool = False,
) -> int:
	"""Discover, bridge, serve, and shut down cleanly. Returns an exit code.

	Written to be started blind, on a network with nothing on it yet. An
	empty result is a log line rather than an exit: HomeKit is perfectly
	happy with an empty bridge, pairing works, and the watch bridges devices
	as they appear. `--require-devices` restores the old behaviour for a
	script that wants an exit code out of "is anything there".
	"""

	# Resolved once and used for both halves, so the interface we browse on
	# and the address we advertise can never drift apart.

	strAddr = StrAddrResolve(strIface)
	pathPersistDir = PathPersistResolve(pathPersistDir)

	g_log.info("bridging on %s", strAddr)
	g_log.info("pairing state in %s", pathPersistDir)

	loop = asyncio.get_running_loop()
	driver = DriverBuild(
		pathPersistDir=pathPersistDir,
		nPort=nPort,
		strPincode=strPincode,
		strAddr=strAddr,
		loop=loop,
	)

	# Built before the bridge, because the bridge takes it. One notifier for
	# the life of the process — it is what remembers the last line sent.

	bridge = CBridge(
		driver,
		dTPoll=dTPoll,
		cMissForget=CMissForget(dTForget, dTPoll),
		notifier=CNotifier(),
	)

	# One watch for the whole run, opened before anything is bridged. The
	# startup window is not a separate browse — it is the first `dTBrowse`
	# seconds of this same stream, which is what keeps a device announcing
	# itself right on the boundary from falling between two browsers.

	async with CWatcher([strAddr]) as watcher:
		await bridge.GraceAsync(watcher, dTBrowse)

		if not bridge.accessories:
			# Two different failures that used to read the same. An empty
			# network is nearly always a blocked process and gets the whole
			# diagnosis; devices that answered but carry nothing bridgeable is
			# a different situation entirely, and printing the mDNS
			# troubleshooting for it would send someone hunting a firewall
			# rule that is not there.

			if not watcher.mpStrDisco:
				PrintNoDevices(dTBrowse, strAddr)
			else:
				g_log.warning(
					"%d device(s) answered, none with a light fixture to bridge",
					len(watcher.mpStrDisco),
				)

			if fRequireDevices:
				await bridge.CloseClients()

				return 1

			g_log.warning("serving an empty bridge; devices will be added as they appear")

		driver.add_accessory(bridge)

		# Everything after this point may change the accessory list on a live
		# bridge, which is what `config_changed` exists to announce.

		bridge.fServing = True

		evStop = asyncio.Event()

		for sig in (signal.SIGINT, signal.SIGTERM):
			loop.add_signal_handler(sig, evStop.set)

		taskWatch = asyncio.create_task(bridge.WatchAsync(watcher))

		pwat = CPairingWatch(bridge, pathPersist=pathPersistDir / PERSIST_FILE)

		# Installed before the server accepts anything, so the first request of
		# the run is already covered.

		pwat.Install()

		# Cleared before the driver starts, so the very first advertisement
		# goes out as `sf=1`. Doing it after would announce the bridge as
		# paired and then correct it, and a controller that heard only the
		# first announcement would go on ignoring a bridge that is waiting for
		# it.

		if fUnpair:
			cClientForgotten = CUnpairAll(driver)

			g_log.warning("--unpair: forgot %d paired controller(s)", cClientForgotten)

		await driver.async_start()

		PrintSetupCode(
			driver.state.pincode.decode(),
			StrTryXhmUri(bridge),
			cClientPaired=len(driver.state.paired_clients),
			pathPersist=pathPersistDir / PERSIST_FILE,
		)

		# Straight after the printing, so the first status line carries the
		# same facts that just went to the terminal rather than waiting a
		# poll interval to agree with them.

		bridge.NotifyStatus()

		try:
			await evStop.wait()
		finally:
			for sig in (signal.SIGINT, signal.SIGTERM):
				loop.remove_signal_handler(sig)

			g_log.info("shutting down")

			# Cancelled explicitly rather than left to the watcher's own
			# end-of-stream sentinel: `async_stop` waits on the poll loop, and
			# a discovery event arriving mid-shutdown would be adding
			# accessories to a bridge on its way out.

			taskWatch.cancel()

			await driver.async_stop()

	return 0
