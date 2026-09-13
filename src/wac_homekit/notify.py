#!/usr/bin/env python3
"""The one line `systemctl status wac-homekit` shows under `Status:`.

`sd_notify(3)`'s `STATUS=` field, and nothing else from that protocol. It
answers the three questions someone asks before they start reading a journal:
is the bridge paired, what is it serving, and is any of it unreachable.

Deliberately not sent from here: `READY=1`, because the unit is `Type=simple`
and readiness sent to a unit that did not ask for it is at best ignored; and
`WATCHDOG=1`, because nothing here is written to keep a watchdog fed. Neither
belongs in a file whose job is a status string.

**The status line is world-readable on the machine.** Any local unprivileged
user can read it over D-Bus, unlike the journal, and while the bridge is
unpaired it carries the setup code. Judged acceptable: after pairing there is
no code in it at all, and a HAP setup code is worth nothing to something that
is not already on the LAN with the bridge.

**Cross-repo dependency: the unit must set `NotifyAccess=main`.** Without it
systemd drops every datagram this sends — no error, no log line, nothing in
`systemctl status` but an absent `Status:` field, which looks exactly like
this code not running at all. The unit lives in a separate NixOS repo, so
that line and this file have to be changed together.
"""

from __future__ import annotations  # Forward refs without quotes

from dataclasses import dataclass
from typing import Callable

import sdnotify


@dataclass(frozen=True)
class SStatus:  # tag = status
	"""Everything the status line says, as numbers rather than prose."""

	# The setup code, or None once the bridge is paired. A paired bridge
	# refuses `/pair-setup`, so a code shown then is a trap rather than a
	# convenience — see `PrintSetupCode`, which makes the same distinction
	# for the same reason.

	strPincode: str | None

	cClientPaired: int
	cDevice: int
	cLight: int

	# Lights currently answering nothing — an unplugged transformer, or a
	# fixture that has stopped appearing in its device's snapshot. Both end
	# at `CFixtureAccessory.MarkOffline`, so this needs no state of its own.

	cLightOffline: int


def StrCount(cItem: int, strNoun: str) -> str:
	"""`1 light` / `3 lights`.

	Worth the three lines: "1 lights" in a status line reads as a bug in the
	bridge rather than as a bug in the status line.
	"""

	return f"{cItem} {strNoun}" if cItem == 1 else f"{cItem} {strNoun}s"


def StrStatus(status: SStatus) -> str:
	"""One line, never more.

	A newline in a `STATUS=` datagram truncates what systemd shows at the
	first one, so the result is squeezed flat at the end rather than trusted
	to be single-line — the pincode arrives from HAP-python's state file and
	the device counts from the network.
	"""

	# Pairing is decided by the client count, not by whether a pincode
	# happens to be present: the count is the fact, and keying on it means a
	# stray code can never be printed for a bridge that would refuse it.

	if status.cClientPaired:
		strPair = f"paired with {StrCount(status.cClientPaired, 'controller')}"
	elif status.strPincode:
		strPair = f"unpaired — setup code {status.strPincode}"
	else:
		strPair = "unpaired"

	if status.cDevice:
		strServe = f"{StrCount(status.cDevice, 'device')}, {StrCount(status.cLight, 'light')}"

		# Only when there is something to report. A bridge with everything
		# answering should not have to say "0 unreachable" to say so.

		if status.cLightOffline:
			strServe += f", {status.cLightOffline} unreachable"
	else:
		strServe = "no devices"

	return " ".join(f"{strPair}; {strServe}".split())


class CNotifier:  # tag = notif
	"""Sends a status line to systemd, and not the same one twice in a row.

	The poll loop calls this every five seconds for the life of the process,
	and the common case is that nothing moved. Suppressing the resend keeps
	an strace of this bridge down to the ticks where something actually
	changed, and costs one string comparison to do it.

	`fnNotify` is the seam. Left as None it builds a `SystemdNotifier` once
	and uses its `notify`, which with no `$NOTIFY_SOCKET` in the environment
	— every dev run, every test run — swallows the failure and does nothing.
	Tests pass a list-appending fake instead and never go near a socket.
	"""

	def __init__(self, fnNotify: Callable[[str], None] | None = None) -> None:
		self.fnNotify: Callable[[str], None] = (
			fnNotify if fnNotify is not None else sdnotify.SystemdNotifier().notify
		)

		# The last line actually sent, so a tick that changed nothing sends
		# nothing. None means nothing has gone out yet, which is distinct
		# from having sent an empty line.

		self.strLast: str | None = None

	def Notify(self, status: SStatus) -> None:
		"""Send this status, unless it is the one already showing."""

		strStatus = StrStatus(status)

		if strStatus == self.strLast:
			return

		self.strLast = strStatus

		self.fnNotify(f"STATUS={strStatus}")
