"""The bridge's pure decisions.

Only the ones that are pure. The poll loop, the accessory building and the
discovery watch all need a real device and a real Home app; a HAP-python test
harness would only be testing HAP-python.
"""

from __future__ import annotations  # Forward refs without quotes

import asyncio

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from pyhap.hap_handler import HAPServerHandler
from pyhap.loader import Loader
from pyhap.state import State

from wac_iot import WacTransportError

from wac_homekit.driver import (
	PERSIST_DIR_NAME,
	XHM_PREFIX,
	CBridge,
	CDevicePoll,
	CMissForget,
	CPairingWatch,
	CUnpairAll,
	PathPersistResolve,
	StrBase36,
	StrXhmUri,
)

# Category 2 is HomeKit's bridge category, which is what this bridge is.

CATEGORY_BRIDGE = 2


class TestPathPersistResolve:
	"""Where the pairing state lands, which decides whether a blind run works.

	The whole point is that no case needs root. `pathService` is injected so
	the suite never depends on whether this machine happens to have
	/var/lib/wac-homekit — which is exactly the ambiguity the resolution
	exists to remove.
	"""

	def test_explicit_wins_over_everything(self, tmp_path: Path) -> None:
		"""--persist-dir is a hard requirement, not a preference."""

		pathGiven = tmp_path / "somewhere"
		pathService = tmp_path / "service"
		pathService.mkdir()

		assert PathPersistResolve(pathGiven, pathService=pathService) == pathGiven

	def test_explicit_is_not_checked_for_usability(self, tmp_path: Path) -> None:
		"""A bridge quietly pairing somewhere other than where it was told is
		worse than one that fails on mkdir later."""

		pathGiven = tmp_path / "does" / "not" / "exist"

		assert PathPersistResolve(pathGiven) == pathGiven

	def test_service_dir_used_when_writable(self, tmp_path: Path) -> None:
		"""systemd's StateDirectory exists before the unit runs, which is the
		signal that this is the service rather than a person at a terminal."""

		pathService = tmp_path / "service"
		pathService.mkdir()

		assert PathPersistResolve(None, pathService=pathService) == pathService

	def test_missing_service_dir_falls_back(
		self,
		tmp_path: Path,
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		pathState = tmp_path / "state"
		monkeypatch.setenv("XDG_STATE_HOME", str(pathState))

		pathResolved = PathPersistResolve(None, pathService=tmp_path / "absent")

		assert pathResolved == pathState / PERSIST_DIR_NAME

	def test_unwritable_service_dir_falls_back(
		self,
		tmp_path: Path,
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		"""Present but not writable is the unprivileged case on a machine that
		once ran the service as root — no more use than absent."""

		pathService = tmp_path / "service"
		pathService.mkdir(mode=0o500)
		monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

		pathResolved = PathPersistResolve(None, pathService=pathService)

		assert pathResolved == tmp_path / "state" / PERSIST_DIR_NAME

	def test_unset_xdg_state_home_uses_the_documented_default(
		self,
		tmp_path: Path,
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		monkeypatch.delenv("XDG_STATE_HOME", raising=False)
		monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

		pathResolved = PathPersistResolve(None, pathService=tmp_path / "absent")

		assert pathResolved == tmp_path / ".local" / "state" / PERSIST_DIR_NAME

	def test_empty_xdg_state_home_is_treated_as_unset(
		self,
		tmp_path: Path,
		monkeypatch: pytest.MonkeyPatch,
	) -> None:
		"""XDG's own rule, and the difference between a home-relative path and
		a state directory at the filesystem root."""

		monkeypatch.setenv("XDG_STATE_HOME", "")
		monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

		pathResolved = PathPersistResolve(None, pathService=tmp_path / "absent")

		assert pathResolved == tmp_path / ".local" / "state" / PERSIST_DIR_NAME


class TestStrBase36:
	"""Base 36 is only here because HAP writes the setup payload in it.

	Round-tripped through `int(s, 36)` rather than against a table of
	expected strings: the standard library already knows how to read base 36,
	so it makes a better oracle than a second hand-written encoder would.
	"""

	@pytest.mark.parametrize("nValue", [0, 1, 9, 10, 35, 36, 1295, 1296, 4615176725])
	def test_round_trips_through_int(self, nValue: int) -> None:
		assert int(StrBase36(nValue), 36) == nValue

	def test_zero_is_a_digit_not_an_empty_string(self) -> None:
		assert StrBase36(0) == "0"

	def test_digits_are_uppercase(self) -> None:
		"""Lowercase decodes fine but does not match what a controller
		expects to see printed on a label."""

		assert StrBase36(35) == "Z"


class TestStrXhmUri:
	"""The setup payload behind the pairing QR code.

	Unpacked field by field rather than compared against a fixed string,
	because a fixed string would pass just as happily with two fields
	transposed.
	"""

	PINCODE = "517-73-973"
	SETUP_ID = "ABCD"

	def NPayload(self, strUri: str) -> int:
		strPayload = strUri[len(XHM_PREFIX):-len(self.SETUP_ID)]

		return int(strPayload, 36)

	def StrUri(self) -> str:
		return StrXhmUri(CATEGORY_BRIDGE, self.PINCODE, self.SETUP_ID)

	def test_shape(self) -> None:
		strUri = self.StrUri()

		assert strUri.startswith(XHM_PREFIX)
		assert strUri.endswith(self.SETUP_ID)
		assert len(strUri) == len(XHM_PREFIX) + 9 + len(self.SETUP_ID)

	def test_carries_the_setup_code_in_the_low_27_bits(self) -> None:
		"""Eight digits top out at 99,999,999, which is inside 27 bits — so a
		setup code never runs into the flags nibble above it."""

		assert self.NPayload(self.StrUri()) & 0x7FFFFFF == 51773973

	def test_carries_the_flags_and_category(self) -> None:
		nPayload = self.NPayload(self.StrUri())

		assert (nPayload >> 27) & 0xF == 2                  # reachable over IP
		assert (nPayload >> 31) & 0xFF == CATEGORY_BRIDGE

	def test_separators_in_the_pincode_are_ignored(self) -> None:
		"""The 3-2-3 grouping is what SRP hashes; the payload wants the digits."""

		assert StrXhmUri(CATEGORY_BRIDGE, "517-73-973", "AB") == StrXhmUri(
			CATEGORY_BRIDGE, "51773973", "AB"
		)

	def test_payload_is_padded_to_nine_digits(self) -> None:
		"""The padding is never decorative: the smallest payload a real bridge
		can produce is six base-36 digits, so the width comes from the zeros."""

		strUri = StrXhmUri(0, "00000001", "ZZZZ")

		assert strUri == f"{XHM_PREFIX}0004FTI4HZZZZ"


class CProtoStub:  # tag = proto
	"""One HAP connection, which remembers only whether it was closed."""

	def __init__(self) -> None:
		self.fClosed = False

	def close(self) -> None:
		self.fClosed = True


class CDriverStub:  # tag = driver
	"""AccessoryDriver as far as the pairing code reaches into it.

	`unpair` is HAP-python's own minus the persist, which needs a running
	loop and a file; the count stands in for it, since what matters here is
	that removal went through the driver rather than at the state directly.
	"""

	def __init__(self, state: State) -> None:
		self.state = state
		self.cUnpair = 0
		self.http_server: Any = SimpleNamespace(connections={})

	def unpair(self, uuidClient: UUID) -> None:
		self.state.remove_paired_client(uuidClient)
		self.cUnpair += 1


class CBridgeStub:  # tag = bridge
	"""The two attributes StrTryXhmUri and the watch actually use."""

	def __init__(self, driver: CDriverStub) -> None:
		self.driver = driver
		self.category = CATEGORY_BRIDGE

	def NotifyStatus(self) -> None:
		"""Nothing. The unpair path calls it; what it sends is tested in
		`test_notify.py`, against numbers rather than against a live bridge."""


class CHandlerStub:  # tag = handler
	"""A HAPServerHandler mid-request."""

	def __init__(self, state: State) -> None:
		self.state = state
		self.client_address = ("10.0.0.9", 50000)
		self.response: Any = SimpleNamespace(pairing_changed=False)


def StatePaired(cClient: int = 2) -> State:
	state = State(address="127.0.0.1", mac="AA:BB:CC:DD:EE:FF", pincode=b"111-22-333")

	for _ in range(cClient):
		# Admin permissions, which is what iOS pairs with and what makes
		# removing the last one cascade.

		state.add_paired_client(str(uuid4()).encode(), b"\x00" * 32, b"\x01")

	return state


class TestCUnpairAll:
	"""`--unpair`, which is the only recovery a deleted-while-down bridge has.

	Measured, and the reason this is a flag rather than something automatic:
	iOS never contacts an accessory advertising `sf=0`, so a bridge holding
	controllers that no longer exist gets no request to notice — not even a
	refused one.
	"""

	def test_forgets_everyone_and_says_how_many(self) -> None:
		state = StatePaired(cClient=2)
		driver = CDriverStub(state)

		assert CUnpairAll(driver) == 2  # type: ignore[arg-type]
		assert not state.paired
		assert not state.paired_clients

	def test_goes_through_the_driver(self) -> None:
		"""So the persist file is rewritten by the path an ordinary removal
		takes, rather than the state being edited underneath it."""

		driver = CDriverStub(StatePaired(cClient=1))

		CUnpairAll(driver)  # type: ignore[arg-type]

		assert driver.cUnpair == 1

	def test_an_unpaired_bridge_is_a_no_op(self) -> None:
		driver = CDriverStub(StatePaired(cClient=0))

		assert CUnpairAll(driver) == 0  # type: ignore[arg-type]
		assert not driver.cUnpair


class TestCPairingWatch:
	"""What a *running* bridge does when its last controller removes it.

	Stubbed rather than driven through a real driver: what is worth testing
	is the decision — when the bridge counts as free, and what is left
	running afterwards — and none of that is HAP-python's.
	"""

	def PwatBuild(self, state: State, tmp_path: Path) -> tuple[CPairingWatch, CDriverStub]:
		driver = CDriverStub(state)
		bridge = CBridgeStub(driver)

		return CPairingWatch(bridge, pathPersist=tmp_path / "state.json"), driver  # type: ignore[arg-type]

	def test_a_removal_that_empties_the_bridge_announces_it(
		self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
	) -> None:
		"""It is pairable again, and the code was withheld at startup because
		it was paired then — so without this it sits there mute."""

		state = StatePaired(cClient=1)
		pwat, _ = self.PwatBuild(state, tmp_path)

		def HandlePairings(handler: Any) -> None:
			state.remove_paired_client(next(iter(state.paired_clients)))

		monkeypatch.setattr(HAPServerHandler, "handle_pairings", HandlePairings)

		pwat.Install()
		HAPServerHandler.handle_pairings(CHandlerStub(state))

		assert "1112 2333" in capsys.readouterr().out

	def test_a_removal_leaving_a_controller_says_nothing(
		self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
	) -> None:
		"""Dropping one of several controllers is not a bridge coming free."""

		state = StatePaired(cClient=2)
		pwat, _ = self.PwatBuild(state, tmp_path)

		def HandlePairings(handler: Any) -> None:
			# Straight off the state, so the last-admin cascade in
			# `remove_paired_client` does not take the other one with it.

			state.paired_clients.pop(next(iter(state.paired_clients)))

		monkeypatch.setattr(HAPServerHandler, "handle_pairings", HandlePairings)

		pwat.Install()
		HAPServerHandler.handle_pairings(CHandlerStub(state))

		assert state.paired
		assert not capsys.readouterr().out

	def test_other_connections_are_dropped_but_not_the_asker(
		self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
	) -> None:
		"""A session outlives the pairing it was made under, so a controller
		that was just unpaired would go on talking. The one being answered is
		spared — it still has a response to send."""

		state = StatePaired(cClient=1)
		pwat, driver = self.PwatBuild(state, tmp_path)
		handler = CHandlerStub(state)

		protoAsker = CProtoStub()
		protoOther = CProtoStub()

		driver.http_server.connections = {
			handler.client_address: protoAsker,
			("10.0.0.8", 50001): protoOther,
		}

		monkeypatch.setattr(
			HAPServerHandler,
			"handle_pairings",
			lambda h: state.remove_paired_client(next(iter(state.paired_clients))),
		)

		pwat.Install()
		HAPServerHandler.handle_pairings(handler)

		assert not protoAsker.fClosed
		assert protoOther.fClosed


class CSnapStub:  # tag = snap
	"""A CSnapshot as far as the removal decision reaches into it.

	Only the known-fixture map matters here — the decision is set arithmetic
	on addresses, and what hangs off each one is the accessory's business.
	"""

	def __init__(self, *lAddr: int) -> None:
		self.mpAddrFixtureKnown: dict[int, Any] = {nAddr: object() for nAddr in lAddr}


class CFaccStub:  # tag = facc
	"""One bridged light, as the poll and the removal reach into it."""

	def __init__(self, nAid: int, strName: str) -> None:
		self.aid = nAid
		self.display_name = strName
		self.cOffline = 0

	def Reconcile(self, fixture: Any, *, tPoll: float) -> None:
		"""Nothing. What a fixture's state does to its characteristics is
		`test_accessory.py`'s business, and needs a real HAP service."""

	def MarkOffline(self) -> None:
		self.cOffline += 1


class CClientStub:  # tag = client
	"""A CClient that answers one canned snapshot, or refuses to."""

	def __init__(self, *lAddr: int, fFail: bool = False) -> None:
		self.strHost = "10.0.0.5"
		self.lAddr = lAddr
		self.fFail = fFail

	async def SnapPoll(self) -> CSnapStub:
		if self.fFail:
			raise WacTransportError("connection reset")

		return CSnapStub(*self.lAddr)


class TestCMissForget:
	"""Seconds in, consecutive missing polls out.

	It rounds up, because a threshold is a minimum wait rather than a target
	and removal is the one thing here that cannot be undone. The floor of 1
	then only matters at the bottom: a threshold shorter than one interval
	must not collapse into 0, which is the value that disables removal
	entirely.
	"""

	def test_a_whole_number_of_intervals(self) -> None:
		assert CMissForget(60.0, 5.0) == 12

	def test_a_partial_interval_rounds_up(self) -> None:
		"""Eleven polls is 55s, which is short of the threshold; the twelfth
		is the first that has genuinely waited long enough."""

		assert CMissForget(59.0, 5.0) == 12

	def test_shorter_than_one_interval_is_one_not_zero(self) -> None:
		assert CMissForget(1.0, 5.0) == 1

	def test_zero_is_off(self) -> None:
		assert CMissForget(0.0, 5.0) == 0

	def test_negative_is_off(self) -> None:
		assert CMissForget(-1.0, 5.0) == 0

	def test_a_nonsense_interval_does_not_divide_by_zero(self) -> None:
		"""Not reachable from the CLI, and a traceback at startup would be a
		worse answer than the only sensible reading of it."""

		assert CMissForget(60.0, 0.0) == 1


class TestSetNAddrForget:
	"""When a fixture has earned removal — which is destructive and final.

	iOS loses the accessory's room, its name, and its place in every scene
	and automation, and a fixture that returns comes back as a stranger
	despite a derived, identical AID. So the bar is positive evidence only: a
	device answered, right now, and did not mention a fixture this bridge
	holds an accessory for.
	"""

	ADDR = 167772157
	ADDR_OTHER = 167772158

	def DpollBuild(self, *lAddr: int) -> CDevicePoll:
		# Never polled through — every test here calls the decision directly,
		# with the snapshot it wants to try.

		dpoll = CDevicePoll(CClientStub(), strDeviceId="dev")  # type: ignore[arg-type]

		for iAddr, nAddr in enumerate(lAddr):
			dpoll.mpAddrFacc[nAddr] = CFaccStub(100 + iAddr, f"Light {iAddr}")  # type: ignore[assignment]

		return dpoll

	def test_an_address_that_keeps_reporting_is_never_returned(self) -> None:
		dpoll = self.DpollBuild(self.ADDR)

		for _ in range(20):
			assert not dpoll.SetNAddrForget(CSnapStub(self.ADDR), cMissForget=3)  # type: ignore[arg-type]

	def test_fewer_than_the_threshold_is_not_enough(self) -> None:
		dpoll = self.DpollBuild(self.ADDR)

		for _ in range(2):
			assert not dpoll.SetNAddrForget(CSnapStub(), cMissForget=3)  # type: ignore[arg-type]

	def test_exactly_the_threshold_earns_it(self) -> None:
		dpoll = self.DpollBuild(self.ADDR)

		for _ in range(2):
			dpoll.SetNAddrForget(CSnapStub(), cMissForget=3)  # type: ignore[arg-type]

		assert dpoll.SetNAddrForget(CSnapStub(), cMissForget=3) == {self.ADDR}  # type: ignore[arg-type]

	def test_a_failed_poll_between_two_misses_does_not_advance_it(self) -> None:
		"""A failed poll never reaches here at all — `Poll` returns None and
		`_PollAll` skips — so a device unreachable for an hour comes back to
		exactly the counts it left with. This is that invariant stated as the
		absence it is."""

		dpoll = self.DpollBuild(self.ADDR)

		dpoll.SetNAddrForget(CSnapStub(), cMissForget=2)  # type: ignore[arg-type]

		# The hour of failed polls. Nothing is called, so nothing moves.

		assert dpoll.mpAddrCMiss == {self.ADDR: 1}

		assert dpoll.SetNAddrForget(CSnapStub(), cMissForget=2) == {self.ADDR}  # type: ignore[arg-type]

	def test_reappearing_resets_the_count(self) -> None:
		"""A run of misses that did not reach the threshold buys nothing
		towards the next run."""

		dpoll = self.DpollBuild(self.ADDR)

		for _ in range(2):
			dpoll.SetNAddrForget(CSnapStub(), cMissForget=3)  # type: ignore[arg-type]

		assert not dpoll.SetNAddrForget(CSnapStub(self.ADDR), cMissForget=3)  # type: ignore[arg-type]
		assert not dpoll.mpAddrCMiss

		for _ in range(2):
			assert not dpoll.SetNAddrForget(CSnapStub(), cMissForget=3)  # type: ignore[arg-type]

		assert dpoll.SetNAddrForget(CSnapStub(), cMissForget=3) == {self.ADDR}  # type: ignore[arg-type]

	def test_a_threshold_of_zero_never_returns_anything(self) -> None:
		"""The default, and the counts are kept regardless — what zero
		disables is the answer, not the arithmetic."""

		dpoll = self.DpollBuild(self.ADDR)

		for _ in range(50):
			assert not dpoll.SetNAddrForget(CSnapStub(), cMissForget=0)  # type: ignore[arg-type]

		assert dpoll.mpAddrCMiss == {self.ADDR: 50}

	def test_only_the_missing_address_is_returned(self) -> None:
		dpoll = self.DpollBuild(self.ADDR, self.ADDR_OTHER)

		snap = CSnapStub(self.ADDR_OTHER)

		assert dpoll.SetNAddrForget(snap, cMissForget=1) == {self.ADDR}  # type: ignore[arg-type]

	def test_counts_are_dropped_with_the_accessory(self) -> None:
		"""Otherwise a fixture rebuilt at the same address would inherit the
		misses that got its predecessor removed."""

		dpoll = self.DpollBuild(self.ADDR)

		dpoll.SetNAddrForget(CSnapStub(), cMissForget=3)  # type: ignore[arg-type]
		dpoll.mpAddrFacc.clear()

		assert not dpoll.SetNAddrForget(CSnapStub(), cMissForget=3)  # type: ignore[arg-type]
		assert not dpoll.mpAddrCMiss


class CDriverForgetStub:  # tag = driver
	"""AccessoryDriver as far as building a bridge and removing from it reach.

	`loader` is HAP-python's real one, because the bridge's own constructor
	wants an AccessoryInformation service and a stub of that would be a stub
	of the thing under test's foundations. Everything else is a counter.
	"""

	def __init__(self) -> None:
		self.loader = Loader()
		self.topics: dict[str, set[tuple[str, int]]] = {}
		self.cConfigChanged = 0

	def config_changed(self) -> None:
		self.cConfigChanged += 1


class TestCFaccForget:
	"""The mechanics of taking a light off a live bridge.

	The decision is tested above and is the half with judgement in it. This
	is the half that has to leave nothing behind: `Bridge.accessories` is
	what HAP-python serves from, and `driver.topics` is where a late event
	would otherwise still find a subscriber for an accessory the controller
	has just been told does not exist.
	"""

	ADDR = 167772157
	ADDR_OTHER = 167772158

	def BridgeBuild(self) -> tuple[CBridge, CDriverForgetStub]:
		driver = CDriverForgetStub()
		bridge = CBridge(driver, dTPoll=5.0, cMissForget=1)  # type: ignore[arg-type]
		bridge.fServing = True

		return bridge, driver

	def DpollBuild(self, bridge: CBridge, *lAddr: int, client: Any = None) -> CDevicePoll:
		dpoll = CDevicePoll(client or CClientStub(), strDeviceId="dev")  # type: ignore[arg-type]

		for iAddr, nAddr in enumerate(lAddr):
			facc = CFaccStub(100 + iAddr, f"Light {iAddr}")

			dpoll.mpAddrFacc[nAddr] = facc  # type: ignore[assignment]
			bridge.accessories[facc.aid] = facc  # type: ignore[assignment]

		bridge.mpStrDpoll["WAC_CS_abc123"] = dpoll

		return dpoll

	def test_the_accessory_leaves_both_the_bridge_and_the_device(self) -> None:
		bridge, _ = self.BridgeBuild()
		dpoll = self.DpollBuild(bridge, self.ADDR, self.ADDR_OTHER)

		assert bridge._CFaccForget(dpoll, {self.ADDR}) == 1

		assert 100 not in bridge.accessories
		assert self.ADDR not in dpoll.mpAddrFacc

		# The one that stayed, which is what makes the assertion above mean
		# something other than "the dicts were emptied".

		assert 101 in bridge.accessories
		assert self.ADDR_OTHER in dpoll.mpAddrFacc

	def test_event_subscriptions_go_with_it(self) -> None:
		"""Left behind, a reconcile still in flight would push an event for an
		accessory the controller no longer knows about."""

		bridge, driver = self.BridgeBuild()
		dpoll = self.DpollBuild(bridge, self.ADDR, self.ADDR_OTHER)

		driver.topics = {
			"100.9": {("10.0.0.9", 50000)},
			"100.11": {("10.0.0.9", 50000)},
			"101.9": {("10.0.0.9", 50000)},
		}

		bridge._CFaccForget(dpoll, {self.ADDR})

		assert set(driver.topics) == {"101.9"}

	def test_a_removed_address_is_not_skipped_afterwards(self) -> None:
		"""A fixture that comes back should be rebuilt by the ordinary
		unbridged path, not declined forever."""

		bridge, _ = self.BridgeBuild()
		dpoll = self.DpollBuild(bridge, self.ADDR)

		bridge._CFaccForget(dpoll, {self.ADDR})

		assert not dpoll.setNAddrSkip
		assert dpoll.SetNAddrUnbridged(CSnapStub(self.ADDR)) == {self.ADDR}  # type: ignore[arg-type]

	def test_one_config_change_per_device_not_per_fixture(self) -> None:
		"""Every call rewrites the persist file and bumps the advertised
		config number, so two fixtures leaving one device is one event."""

		bridge, driver = self.BridgeBuild()

		# The device answers, and mentions neither fixture. That is the one
		# signal removal is allowed to act on.

		self.DpollBuild(bridge, self.ADDR, self.ADDR_OTHER, client=CClientStub())

		asyncio.run(bridge._PollAll())

		assert not bridge.accessories
		assert driver.cConfigChanged == 1

	def test_a_device_that_did_not_answer_loses_nothing(self) -> None:
		"""A failed poll is a power cut, a reboot, or a lease change caught
		mid-flight. There is no evidence in it at all."""

		bridge, driver = self.BridgeBuild()
		dpoll = self.DpollBuild(bridge, self.ADDR, client=CClientStub(fFail=True))

		for _ in range(10):
			asyncio.run(bridge._PollAll())

		assert self.ADDR in dpoll.mpAddrFacc
		assert not dpoll.mpAddrCMiss
		assert not driver.cConfigChanged
