#!/usr/bin/env python3
"""CLI entry point for the HomeKit bridge."""

from __future__ import annotations  # Forward refs without quotes

import argparse

from . import __version__


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

	parser.parse_args()

	# BB(bruce) the bridge itself lands in a later phase; see .claude/CLAUDE.md.

	print("hello from wac_homekit")


if __name__ == "__main__":
	main()
