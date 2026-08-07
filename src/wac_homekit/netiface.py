#!/usr/bin/env python3
"""Choosing which local interface the bridge lives on.

A laptop that moves between wifi and a dock has two addresses on the same
subnet, and which one holds the default route flips as it is plugged in and
out. Left to itself HAP-python advertises whichever one wins that race, so
the address the Home app remembers changes underneath it and discovery goes
looking on the wrong link.

Pinning one interface fixes both halves at once — the same address is handed
to HAP-python to advertise and to `wac_iot` to browse on, so they cannot
disagree.

Nothing here is HomeKit-specific, but it lives on this side of the boundary
on purpose: `wac_iot` takes plain addresses and stays free of any
platform-sniffing.
"""

from __future__ import annotations  # Forward refs without quotes

import logging
import socket
import subprocess
import sys

from pathlib import Path

import ifaddr

g_log = logging.getLogger(__name__)

# What `--interface` accepts beyond a literal interface name or address.

IFACE_AUTO = "auto"
IFACE_WIFI = "wifi"

# macOS names the wifi port one of these in `networksetup` output. "AirPort"
# is the older spelling and still appears on long-lived installs.

g_lStrPortWifi = ("Wi-Fi", "AirPort")


class CIfaceError(Exception):  # tag = ifcerr
	"""No usable address for what the user asked for."""


def StrTryIfaceWifiFromPorts(strOut: str) -> str | None:
	"""Pull the wifi device name out of `networksetup -listallhardwareports`.

	Pure, because it is the part worth testing: the output is a series of
	blank-line-separated stanzas, and the device name sits on a line *after*
	the port name that identifies it.
	"""

	fWifi = False

	for strLine in strOut.splitlines():
		strLine = strLine.strip()

		if strLine.startswith("Hardware Port:"):
			fWifi = any(strPort in strLine for strPort in g_lStrPortWifi)

		elif fWifi and strLine.startswith("Device:"):
			return strLine.split(":", 1)[1].strip()

	return None


def StrTryIfaceWifi() -> str | None:
	"""Name of this machine's wifi interface, or None if it has none.

	There is no portable answer, and neither platform's is guessable from an
	address: en0 is wifi on a laptop and ethernet on a Mac mini.
	"""

	if sys.platform == "darwin":
		# The only supported mapping from hardware port to BSD device name.

		try:
			strOut = subprocess.run(
				["networksetup", "-listallhardwareports"],
				capture_output=True,
				text=True,
				timeout=5.0,
				check=True,
			).stdout
		except (OSError, subprocess.SubprocessError) as exc:
			g_log.debug("networksetup failed: %s", exc)

			return None

		return StrTryIfaceWifiFromPorts(strOut)

	# Linux exposes a `wireless` subdirectory only for 802.11 devices. Sorted
	# so a machine with two radios picks the same one every run.

	try:
		for path in sorted(Path("/sys/class/net").glob("*")):
			if (path / "wireless").is_dir():
				return path.name
	except OSError as exc:
		g_log.debug("scanning /sys/class/net failed: %s", exc)

	return None


def MpStrAddrByIface() -> dict[str, str]:
	"""Every interface holding an IPv4 address, mapped to the first one.

	Loopback is dropped: advertising on it produces a bridge only this
	machine can see, which is a confusing way to fail.
	"""

	mpStrAddr: dict[str, str] = {}

	for adapter in ifaddr.get_adapters():
		for ip in adapter.ips:
			if not ip.is_IPv4:
				continue

			strAddr = str(ip.ip)

			if strAddr.startswith("127."):
				continue

			mpStrAddr.setdefault(adapter.nice_name, strAddr)

	return mpStrAddr


def StrTryAddrDefaultRoute() -> str | None:
	"""The address the default route would source from.

	This is what HAP-python picks when left alone. Connecting a UDP socket
	assigns a local address without sending anything, so it costs no traffic
	and works with no route to the far end.
	"""

	sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

	try:
		sock.connect(("10.255.255.255", 1))

		return str(sock.getsockname()[0])
	except OSError:
		return None
	finally:
		sock.close()


def StrAddrResolve(strIface: str) -> str:
	"""Turn a `--interface` value into the IPv4 address to bind and advertise.

	Accepts an interface name, a literal address, `wifi`, or `auto`. Raises
	`CIfaceError` with the available choices rather than falling back
	silently — a bridge on the wrong interface looks like a bridge that
	works until the machine moves.
	"""

	mpStrAddr = MpStrAddrByIface()

	def StrDescribe() -> str:
		if not mpStrAddr:
			return "no interface has an IPv4 address"

		return "available: " + ", ".join(
			f"{strIface}={strAddr}" for strIface, strAddr in sorted(mpStrAddr.items())
		)

	# An explicit address, used as given. Checked against the live interfaces
	# so a stale address from a config file fails now rather than at bind.

	if strIface in mpStrAddr.values():
		return strIface

	if strIface not in (IFACE_AUTO, IFACE_WIFI) and strIface in mpStrAddr:
		return mpStrAddr[strIface]

	if strIface in (IFACE_AUTO, IFACE_WIFI):
		strWifi = StrTryIfaceWifi()

		if strWifi is not None and strWifi in mpStrAddr:
			g_log.info("using wifi interface %s (%s)", strWifi, mpStrAddr[strWifi])

			return mpStrAddr[strWifi]

		if strIface == IFACE_WIFI:
			strWhy = (
				f"wifi interface {strWifi} has no IPv4 address"
				if strWifi is not None
				else "no wifi interface found"
			)

			raise CIfaceError(f"--interface wifi: {strWhy} — {StrDescribe()}")

		# auto: no wifi, so whatever the machine would route out of. Warned
		# about because it is the address that moves when a dock appears.

		strAddr = StrTryAddrDefaultRoute()

		if strAddr is not None:
			g_log.warning(
				"no wifi interface; using default route address %s. "
				"Pass --interface to pin one if this machine has more than one link.",
				strAddr,
			)

			return strAddr

		raise CIfaceError(f"could not determine any local address — {StrDescribe()}")

	raise CIfaceError(f"unknown interface {strIface!r} — {StrDescribe()}")
