#!/usr/bin/env python3
"""CLI entry point for the HomeKit bridge."""

from __future__ import annotations  # Forward refs without quotes

import argparse
import asyncio
import io
import logging
import re
import sys

from pathlib import Path

from wac_iot import WacError

from . import __version__
from .driver import (
	FORGET_MISSING_DEFAULT,
	PERSIST_DIR_SERVICE,
	POLL_INTERVAL_DEFAULT,
	PORT_DEFAULT,
	NRun,
)
from .netiface import IFACE_AUTO, CIfaceError

# HomeKit setup codes are eight digits, and the 3-2-3 grouping is not
# cosmetic: HAP-python hands the hyphenated string to SRP as the password, so
# "426-83-591" *is* the shared secret and the hyphens are inside the hash. The
# Home app's manual entry, meanwhile, shows two groups of four — that is
# Apple's keypad, not the wire format, and iOS rebuilds the 3-2-3 string from
# the digits before hashing.
#
# So the grouping a user sees and the grouping the protocol needs genuinely
# differ. Accept the digits however they arrive and normalize.

g_rePincodeDigits = re.compile(r"^\d{8}$")

# Checking here beats letting HAP-python generate a keypair and then reject
# the code.


def StrPincode(strArg: str) -> str:
	"""argparse type for a HomeKit setup code, normalized to 3-2-3.

	Separators are stripped rather than required, so typing back the eight
	digits the Home app just asked for works.
	"""

	strDigits = re.sub(r"[\s\-]", "", strArg)

	if not g_rePincodeDigits.match(strDigits):
		raise argparse.ArgumentTypeError(f"pincode must be eight digits, got {strArg!r}")

	return f"{strDigits[:3]}-{strDigits[3:5]}-{strDigits[5:]}"


def main() -> None:
	parser = argparse.ArgumentParser(
		prog="wac_homekit",
		description="HomeKit bridge for WAC Lighting IoT devices",
	)
	parser.add_argument(
		"--version",
		action="version",
		version=f"%(prog)s {__version__}",
	)
	parser.add_argument(
		"-v",
		"--verbose",
		action="store_true",
		help="log at debug level",
	)
	parser.add_argument(
		"--browse",
		type=float,
		default=5.0,
		help=(
			"seconds to wait for devices before first serving (default: 5). "
			"discovery keeps running afterwards; this only decides how long to "
			"hold off so the usual case comes up already populated"
		),
	)
	parser.add_argument(
		"--require-devices",
		action="store_true",
		help=(
			"exit nonzero if no device answered within --browse, instead of "
			"serving an empty bridge and waiting for one to appear"
		),
	)
	parser.add_argument(
		"--poll-interval",
		type=float,
		default=POLL_INTERVAL_DEFAULT,
		help=f"seconds between device polls (default: {POLL_INTERVAL_DEFAULT:g})",
	)
	parser.add_argument(
		"--forget-missing",
		type=float,
		default=FORGET_MISSING_DEFAULT,
		metavar="SECONDS",
		help=(
			"remove a light from the bridge once its fixture has been absent "
			"from a reachable device's polls for this long (default: "
			f"{FORGET_MISSING_DEFAULT:g}, never). THIS LOSES THE ACCESSORY'S "
			"HOME APP CONFIGURATION — its room, its name, and its place in "
			"every scene and automation — and a fixture that comes back comes "
			"back as a new accessory. off by default, where a fixture that is "
			"gone shows as No Response until the bridge is restarted. a device "
			"that stops answering altogether is never removed, however long "
			"it stays away"
		),
	)
	parser.add_argument(
		"--persist-dir",
		type=Path,
		default=None,
		help=(
			"directory holding the HomeKit pairing state (default: "
			f"{PERSIST_DIR_SERVICE} when it exists and is writable, else "
			"$XDG_STATE_HOME/wac-homekit, else ~/.local/state/wac-homekit)"
		),
	)
	parser.add_argument(
		"--port",
		type=int,
		default=PORT_DEFAULT,
		help=f"port the bridge listens on (default: {PORT_DEFAULT})",
	)
	parser.add_argument(
		"--pincode",
		type=StrPincode,
		default=None,
		help=(
			"HomeKit setup code, eight digits with or without separators "
			"(default: a fresh random one each run, printed at startup)"
		),
	)
	parser.add_argument(
		"--unpair",
		action="store_true",
		help=(
			"forget every paired controller at startup and serve as an unpaired "
			"bridge, printing a setup code. for a bridge deleted from the Home "
			"app while it was not running: the removal is sent over a HAP "
			"connection, so with nothing listening it never arrives, and the "
			"bridge goes on advertising itself as paired. nothing here can "
			"notice that — iOS does not contact a bridge that says it is paired, "
			"so there is no request to see — which is why this is a flag and not "
			"automatic. the MAC and the keypair are kept, so only the "
			"controllers that no longer exist are lost"
		),
	)
	parser.add_argument(
		"--interface",
		default=IFACE_AUTO,
		metavar="IFACE",
		help=(
			"interface to browse for devices on, and whose address the bridge "
			"binds and advertises: an interface name (en0), an address "
			"(10.0.0.5), 'wifi', or 'auto' "
			f"(default: {IFACE_AUTO} — wifi if there is one, else the default route)"
		),
	)

	args = parser.parse_args()

	# Milliseconds, not just seconds. The failures worth diagnosing here are
	# requests colliding on one device, and at a 5s poll interval two of those
	# land inside the same second — a whole-second stamp cannot separate them.
	# journald stamps its own lines, so this duplicates under systemd; that is
	# cheaper than not being able to read an interactive run.

	logging.basicConfig(
		level=logging.DEBUG if args.verbose else logging.INFO,
		format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
		datefmt="%H:%M:%S",
	)

	# The setup code goes to stdout, and stdout is a pipe under systemd or
	# behind any redirect — where Python's default is to block-buffer it.
	# Measured: the whole pairing block, digits and QR, sat in the buffer while
	# the bridge ran, which is precisely as useful as not printing it. Logging
	# goes to stderr and was unaffected, so the failure looks like the code was
	# never printed at all rather than like buffering.
	#
	# Line buffering rather than a flush at each print: the reset prints a code
	# mid-run too, and anything added later should not have to remember.

	if isinstance(sys.stdout, io.TextIOWrapper):
		sys.stdout.reconfigure(line_buffering=True)

	try:
		nExit = asyncio.run(
			NRun(
				dTBrowse=args.browse,
				dTPoll=args.poll_interval,
				dTForget=args.forget_missing,
				pathPersistDir=args.persist_dir,
				nPort=args.port,
				strPincode=args.pincode,
				strIface=args.interface,
				fRequireDevices=args.require_devices,
				fUnpair=args.unpair,
			)
		)
	except CIfaceError as exc:
		# The message already lists what the machine actually has, which is
		# the only thing that makes a typo'd interface name diagnosable.

		print(f"error: {exc}", file=sys.stderr)
		nExit = 1

	except OSError as exc:
		# Overwhelmingly a persist directory the process cannot create or
		# write. The default resolution avoids that, so reaching here means
		# an explicit --persist-dir that the process has no business in.

		print(f"error: {exc}", file=sys.stderr)
		nExit = 1

	except WacError as exc:
		print(f"error: {exc}", file=sys.stderr)
		nExit = 1

	except KeyboardInterrupt:
		# NRun installs its own SIGINT handler once it is serving, so this
		# only catches a Ctrl-C during discovery or setup.

		nExit = 130

	sys.exit(nExit)


if __name__ == "__main__":
	main()
