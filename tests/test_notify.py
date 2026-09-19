"""The `systemctl status` line, and the resend suppression behind it.

All pure. Nothing here constructs a `SystemdNotifier`, opens a socket, or
sets `$NOTIFY_SOCKET` — `CNotifier`'s whole injection seam exists so that the
formatting and the don't-repeat-yourself rule can be tested without systemd
anywhere in the room.

The counts and the plurals are the point. A status line is read at a glance
by someone who has not looked at this bridge in a month, and "1 lights" in it
reads as a broken bridge rather than as a broken f-string.
"""

from __future__ import annotations  # Forward refs without quotes

from wac_homekit.notify import CNotifier, SStatus, StrStatus


def StatusMake(
	*,
	strPincode: str | None = None,
	cClientPaired: int = 0,
	cDevice: int = 1,
	cLight: int = 3,
	cLightOffline: int = 0,
) -> SStatus:
	"""A populated, paired, everything-answering bridge unless told otherwise."""

	return SStatus(
		strPincode=strPincode,
		cClientPaired=cClientPaired,
		cDevice=cDevice,
		cLight=cLight,
		cLightOffline=cLightOffline,
	)


# ---------------------------------------------------------------------------
# StrStatus — pairing half
# ---------------------------------------------------------------------------


def test_status_unpaired_shows_the_code() -> None:
	strStatus = StrStatus(StatusMake(strPincode="123-45-678"))

	assert "unpaired" in strStatus
	assert "123-45-678" in strStatus


def test_status_paired_shows_neither_code_nor_unpaired() -> None:
	# The paired form is what a bridge shows for most of its life, and a
	# setup code in it would be one a controller could only fail with.

	strStatus = StrStatus(StatusMake(strPincode="123-45-678", cClientPaired=2))

	assert "123-45-678" not in strStatus
	assert "unpaired" not in strStatus
	assert "paired with 2 controllers" in strStatus


def test_status_unpaired_without_a_code_still_says_unpaired() -> None:
	assert "unpaired" in StrStatus(StatusMake(strPincode=None))


def test_status_controller_count_is_singular_and_plural() -> None:
	assert "1 controller;" in StrStatus(StatusMake(cClientPaired=1))
	assert "2 controllers" in StrStatus(StatusMake(cClientPaired=2))


# ---------------------------------------------------------------------------
# StrStatus — serving half
# ---------------------------------------------------------------------------


def test_status_device_and_light_counts_are_singular_and_plural() -> None:
	assert "1 device, 1 light" in StrStatus(StatusMake(cDevice=1, cLight=1))
	assert "2 devices, 3 lights" in StrStatus(StatusMake(cDevice=2, cLight=3))


def test_status_unreachable_clause_appears_only_when_non_zero() -> None:
	assert "unreachable" not in StrStatus(StatusMake(cLightOffline=0))
	assert "2 unreachable" in StrStatus(StatusMake(cLightOffline=2))


def test_status_no_devices_replaces_the_counts() -> None:
	# A blind first run on an empty network sits here, and "0 devices, 0
	# lights" says the same thing in a way that reads like a failure.

	strStatus = StrStatus(StatusMake(cDevice=0, cLight=0))

	assert "no devices" in strStatus
	assert "0 device" not in strStatus


# ---------------------------------------------------------------------------
# StrStatus — exact forms
# ---------------------------------------------------------------------------


def test_status_exact_forms() -> None:
	# Pinned whole, because these are what `systemctl status` puts in front
	# of someone and the punctuation is part of being readable.

	assert StrStatus(
		StatusMake(strPincode="123-45-678", cDevice=1, cLight=3)
	) == "unpaired — setup code 123-45-678; 1 device, 3 lights"

	assert StrStatus(
		StatusMake(cClientPaired=2, cDevice=1, cLight=3, cLightOffline=2)
	) == "paired with 2 controllers; 1 device, 3 lights, 2 unreachable"

	assert StrStatus(
		StatusMake(strPincode="123-45-678", cDevice=0, cLight=0)
	) == "unpaired — setup code 123-45-678; no devices"


# ---------------------------------------------------------------------------
# StrStatus — single line
# ---------------------------------------------------------------------------


def test_status_never_contains_a_newline() -> None:
	# A newline truncates a `STATUS=` datagram at systemd's end, so the
	# failure is an unexplained half-line rather than an error anywhere.

	for status in (
		StatusMake(strPincode="123-45-678"),
		StatusMake(cClientPaired=1, cLightOffline=1),
		StatusMake(cDevice=0, cLight=0),
		StatusMake(strPincode="123-45-678\nSTATUS=nonsense"),
		StatusMake(strPincode="123\r\n45\t678"),
	):
		strStatus = StrStatus(status)

		assert "\n" not in strStatus
		assert "\r" not in strStatus
		assert "\t" not in strStatus


# ---------------------------------------------------------------------------
# CNotifier
# ---------------------------------------------------------------------------


def test_notifier_sends_the_first_status() -> None:
	lStrSent: list[str] = []
	notif = CNotifier(lStrSent.append)

	notif.Notify(StatusMake(strPincode="123-45-678"))

	assert len(lStrSent) == 1


def test_notifier_suppresses_an_unchanged_resend() -> None:
	# The poll loop calls this every five seconds forever, and nothing moving
	# is the common case.

	lStrSent: list[str] = []
	notif = CNotifier(lStrSent.append)

	status = StatusMake(strPincode="123-45-678")

	notif.Notify(status)
	notif.Notify(status)
	notif.Notify(StatusMake(strPincode="123-45-678"))

	assert len(lStrSent) == 1


def test_notifier_sends_again_once_something_moves() -> None:
	lStrSent: list[str] = []
	notif = CNotifier(lStrSent.append)

	notif.Notify(StatusMake(strPincode="123-45-678"))
	notif.Notify(StatusMake(cClientPaired=1))
	notif.Notify(StatusMake(cClientPaired=1, cLightOffline=1))

	assert len(lStrSent) == 3


def test_notifier_sends_the_formatted_status() -> None:
	# Compared against `StrStatus` rather than a literal: the exact forms are
	# pinned above, and duplicating them here would only mean two places to
	# edit when the wording changes.

	lStrSent: list[str] = []
	notif = CNotifier(lStrSent.append)

	status = StatusMake(strPincode="123-45-678", cLightOffline=1)

	notif.Notify(status)

	assert lStrSent == [f"STATUS={StrStatus(status)}"]


# ---------------------------------------------------------------------------
# CNotifier — periodic resend
# ---------------------------------------------------------------------------


class CClockFake:  # tag = clock
	"""A monotonic clock a test can jump forward in, instead of sleeping."""

	def __init__(self) -> None:
		self.t = 1000.0

	def __call__(self) -> float:
		return self.t


def test_notifier_resends_an_unchanged_refreshable_status_once_the_interval_passes() -> None:
	# The nineteen-hour blank `Status:` field this exists for: the startup
	# datagram was lost, and with nothing changing afterwards nothing was
	# ever sent again.

	lStrSent: list[str] = []
	clock = CClockFake()
	notif = CNotifier(lStrSent.append, dTResend=60.0, fnTime=clock)

	status = StatusMake(strPincode="123-45-678")

	notif.Notify(status, fRefresh=True)

	clock.t += 60.0

	notif.Notify(status, fRefresh=True)

	assert len(lStrSent) == 2


def test_notifier_does_not_resend_before_the_interval_has_passed() -> None:
	# Twelve an hour is the accepted cost; one per poll tick is not.

	lStrSent: list[str] = []
	clock = CClockFake()
	notif = CNotifier(lStrSent.append, dTResend=60.0, fnTime=clock)

	status = StatusMake(strPincode="123-45-678")

	notif.Notify(status, fRefresh=True)

	for _ in range(11):
		clock.t += 5.0

		notif.Notify(status, fRefresh=True)

	assert len(lStrSent) == 1


def test_notifier_never_resends_an_unchanged_paired_status() -> None:
	# A paired line is informational and carries no setup code, so a stale
	# one costs nothing and is not worth a datagram.

	lStrSent: list[str] = []
	clock = CClockFake()
	notif = CNotifier(lStrSent.append, dTResend=60.0, fnTime=clock)

	status = StatusMake(cClientPaired=1)

	notif.Notify(status)

	clock.t += 3600.0

	notif.Notify(status)

	assert len(lStrSent) == 1


def test_notifier_resends_the_formatted_status() -> None:
	# A resend is the same line, not a bare marker — it is replacing the one
	# systemd is showing.

	lStrSent: list[str] = []
	clock = CClockFake()
	notif = CNotifier(lStrSent.append, dTResend=60.0, fnTime=clock)

	status = StatusMake(strPincode="123-45-678", cLightOffline=1)

	notif.Notify(status, fRefresh=True)

	clock.t += 60.0

	notif.Notify(status, fRefresh=True)

	assert lStrSent == [f"STATUS={StrStatus(status)}"] * 2


def test_notifier_sends_a_change_without_waiting_for_the_interval() -> None:
	# The resend is additive. A status that moved still goes out on the tick
	# it moved, whatever the clock says.

	lStrSent: list[str] = []
	clock = CClockFake()
	notif = CNotifier(lStrSent.append, dTResend=60.0, fnTime=clock)

	notif.Notify(StatusMake(strPincode="123-45-678"), fRefresh=True)
	notif.Notify(StatusMake(strPincode="123-45-678", cLightOffline=1), fRefresh=True)

	assert len(lStrSent) == 2
