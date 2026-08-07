#!/usr/bin/env python3
"""CLI entry point for the HomeKit bridge."""

from __future__ import annotations  # Forward refs without quotes

import argparse
import asyncio
import logging
import re
import sys

from pathlib import Path

from wac_iot import WacError

from . import __version__
from .driver import PERSIST_DIR_DEFAULT, POLL_INTERVAL_DEFAULT, PORT_DEFAULT, NRun
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
		help="seconds to browse for devices at startup (default: 5)",
	)
	parser.add_argument(
		"--poll-interval",
		type=float,
		default=POLL_INTERVAL_DEFAULT,
		help=f"seconds between device polls (default: {POLL_INTERVAL_DEFAULT:g})",
	)
	parser.add_argument(
		"--persist-dir",
		type=Path,
		default=PERSIST_DIR_DEFAULT,
		help=f"directory holding the HomeKit pairing state (default: {PERSIST_DIR_DEFAULT})",
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
		"--interface",
		default=IFACE_AUTO,
		metavar="IFACE",
		help=(
			"interface to browse and advertise on: an interface name (en0), "
			"an address (10.0.0.5), 'wifi', or 'auto' "
			f"(default: {IFACE_AUTO} — wifi if there is one, else the default route)"
		),
	)

	args = parser.parse_args()

	logging.basicConfig(
		level=logging.DEBUG if args.verbose else logging.INFO,
		format="%(levelname)s %(name)s: %(message)s",
	)

	try:
		nExit = asyncio.run(
			NRun(
				dTBrowse=args.browse,
				dTPoll=args.poll_interval,
				pathPersistDir=args.persist_dir,
				nPort=args.port,
				strPincode=args.pincode,
				strIface=args.interface,
			)
		)
	except CIfaceError as exc:
		# The message already lists what the machine actually has, which is
		# the only thing that makes a typo'd interface name diagnosable.

		print(f"error: {exc}", file=sys.stderr)
		nExit = 1

	except OSError as exc:
		# Overwhelmingly a persist directory the process cannot create or
		# write — /var/lib/wac-homekit needs either root or --persist-dir.

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
