#!/usr/bin/env python3
"""Interface selection.

The `networksetup` parser is the piece worth testing: it reads a stanza
format where the identifying line comes *before* the value wanted, so an
off-by-one stanza silently returns the wrong interface — and picking the
wrong interface produces a bridge that works until the machine is moved.

`StrAddrResolve` is tested against an injected address map rather than the
live machine, so the results do not depend on what is plugged in.
"""

from __future__ import annotations  # Forward refs without quotes

import pytest

from wac_homekit import netiface
from wac_homekit.netiface import CIfaceError, StrAddrResolve, StrTryIfaceWifiFromPorts

# Shape of real `networksetup -listallhardwareports` output: a leading blank
# line, then stanzas separated by blank lines, wifi not first.

STR_PORTS = """
Hardware Port: Thunderbolt Ethernet Slot 1
Device: en6
Ethernet Address: 64:4b:f0:70:58:77

Hardware Port: Thunderbolt Bridge
Device: bridge0
Ethernet Address: 36:8b:a0:c0:ed:00

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 68:5e:dd:1d:84:7a
"""


def test_WifiFoundAfterOtherPorts() -> None:
	assert StrTryIfaceWifiFromPorts(STR_PORTS) == "en0"


def test_WifiIsNotJustTheFirstDevice() -> None:
	# The bug this guards: returning en6 because it appeared first.

	assert StrTryIfaceWifiFromPorts(STR_PORTS) != "en6"


def test_AirportSpellingStillWorks() -> None:
	strOut = "Hardware Port: AirPort\nDevice: en1\n"

	assert StrTryIfaceWifiFromPorts(strOut) == "en1"


def test_NoWifiPort() -> None:
	strOut = "Hardware Port: Ethernet\nDevice: en0\n"

	assert StrTryIfaceWifiFromPorts(strOut) is None


def test_PortWithoutDevice() -> None:
	# A stanza that names wifi but never gives a device must not fall through
	# and claim the next stanza's device.

	strOut = "Hardware Port: Wi-Fi\n\nHardware Port: Ethernet\nDevice: en3\n"

	assert StrTryIfaceWifiFromPorts(strOut) != "en3"


def test_Empty() -> None:
	assert StrTryIfaceWifiFromPorts("") is None


@pytest.fixture
def mpStrAddr(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
	"""A machine on wifi and a dock at once — the case that motivated this."""

	mpStrAddr = {"en0": "10.10.10.142", "en6": "10.10.10.159"}

	monkeypatch.setattr(netiface, "MpStrAddrByIface", lambda: mpStrAddr)
	monkeypatch.setattr(netiface, "StrTryIfaceWifi", lambda: "en0")

	return mpStrAddr


def test_ResolveByIfaceName(mpStrAddr: dict[str, str]) -> None:
	assert StrAddrResolve("en6") == "10.10.10.159"


def test_ResolveByAddress(mpStrAddr: dict[str, str]) -> None:
	assert StrAddrResolve("10.10.10.159") == "10.10.10.159"


def test_ResolveWifiPrefersWifiOverDefaultRoute(mpStrAddr: dict[str, str]) -> None:
	# The whole point: en6 holds the default route, wifi must still win.

	assert StrAddrResolve("wifi") == "10.10.10.142"
	assert StrAddrResolve("auto") == "10.10.10.142"


def test_ResolveUnknownIface(mpStrAddr: dict[str, str]) -> None:
	with pytest.raises(CIfaceError) as exc:
		StrAddrResolve("en99")

	# The message has to name the real options or it is not actionable.

	assert "en0=10.10.10.142" in str(exc.value)


def test_ResolveStaleAddress(mpStrAddr: dict[str, str]) -> None:
	# An address that was right yesterday fails now, rather than at bind time.

	with pytest.raises(CIfaceError):
		StrAddrResolve("10.10.10.99")


def test_ResolveWifiWhenThereIsNone(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(netiface, "MpStrAddrByIface", lambda: {"en6": "10.10.10.159"})
	monkeypatch.setattr(netiface, "StrTryIfaceWifi", lambda: None)

	with pytest.raises(CIfaceError):
		StrAddrResolve("wifi")


def test_ResolveAutoFallsBackToDefaultRoute(monkeypatch: pytest.MonkeyPatch) -> None:
	# auto degrades where wifi refuses, because a headless server has no wifi
	# and still has to run.

	monkeypatch.setattr(netiface, "MpStrAddrByIface", lambda: {"en6": "10.10.10.159"})
	monkeypatch.setattr(netiface, "StrTryIfaceWifi", lambda: None)
	monkeypatch.setattr(netiface, "StrTryAddrDefaultRoute", lambda: "10.10.10.159")

	assert StrAddrResolve("auto") == "10.10.10.159"
