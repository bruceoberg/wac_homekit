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

import time
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
	"""Sends a status line to systemd on change, and repeats the ones that matter.

	The poll loop calls this every five seconds for the life of the process,
	and the common case is that nothing moved, so an unchanged line is
	normally dropped for one string comparison.

	**Unchanged is not the same as delivered.** A `STATUS=` datagram is fire
	and forget: nothing acknowledges it and nothing retries it, and one going
	missing has been measured — a bridge sent its single unpaired line at
	startup, the datagram never arrived, nothing changed afterwards, and
	`systemctl status` showed a blank `Status:` field for nineteen hours. The
	next state change sent the paired line and it landed. So a caller may mark
	a status as eligible for periodic resend with `fRefresh`, and this class
	owns the interval and the clock that decide when one goes out.

	Only the unpaired line earns that: it carries the setup code, which is the
	one thing someone reads this field to get. A paired line is informational
	and a stale one costs nothing, so it still sends on change alone.

	The cost is twelve extra datagrams an hour while unpaired, which was
	weighed and accepted. Note it is not a logging-volume change — `STATUS=`
	replaces a field systemd holds and writes no journal lines — the only
	thing it makes noisier is an strace.

	`fnNotify` is the seam. Left as None it builds a `SystemdNotifier` once
	and uses its `notify`, which with no `$NOTIFY_SOCKET` in the environment
	— every dev run, every test run — swallows the failure and does nothing.
	Tests pass a list-appending fake instead and never go near a socket.

	`fnTime` is the same seam for the clock, so a test can jump an hour
	without sleeping through it.
	"""

	def __init__(
		self,
		fnNotify: Callable[[str], None] | None = None,
		dTResend: float = 60.0,
		fnTime: Callable[[], float] = time.monotonic,
	) -> None:
		self.fnNotify: Callable[[str], None] = (
			fnNotify if fnNotify is not None else sdnotify.SystemdNotifier().notify
		)

		# How long an eligible status waits before it goes out again. Held as
		# a duration rather than as a count of ticks: a tick count would
		# quietly mean something else the day the poll interval changes or a
		# third caller appears, and there are already three call sites.

		self.dTResend = dTResend

		self.fnTime = fnTime

		# The last line actually sent, so a tick that changed nothing sends
		# nothing. None means nothing has gone out yet, which is distinct
		# from having sent an empty line.

		self.strLast: str | None = None

		# When that send happened, on the monotonic clock. Paired with
		# `strLast` and updated with it, resends included.

		self.tLast: float | None = None

	def Notify(self, status: SStatus, fRefresh: bool = False) -> None:
		"""Send this status if it moved, or if it is due to be repeated.

		`fRefresh` says this status is *eligible* for the periodic resend
		above — not that it should go out now. The caller decides which
		statuses are worth repeating; the interval and the clock are ours.
		"""

		strStatus = StrStatus(status)
		tNow = self.fnTime()

		if strStatus == self.strLast:
			if not fRefresh:
				return

			if self.tLast is not None and tNow - self.tLast < self.dTResend:
				return

		self.strLast = strStatus
		self.tLast = tNow

		self.fnNotify(f"STATUS={strStatus}")
