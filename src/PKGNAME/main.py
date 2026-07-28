#!/usr/bin/env python3
"""TODO: describe what this module does."""

from __future__ import annotations  # Forward refs without quotes

import argparse
import sys

from . import __version__


def main() -> None:
	parser = argparse.ArgumentParser(
		prog="PKGNAME",
		description="TODO: one-line description",
	)
	parser.add_argument(
		"--version",
		action="version",
		version=f"%(prog)s {__version__}",
	)
	# parser.add_argument("input", type=str, help="...")

	args = parser.parse_args()

	# TODO: implement
	print("hello from PKGNAME")


if __name__ == "__main__":
	main()
