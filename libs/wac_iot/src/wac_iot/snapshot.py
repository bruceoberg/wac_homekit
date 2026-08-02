#!/usr/bin/env python3
"""One poll's worth of a device: its identity plus every fixture on it.

A WAC transformer is one network endpoint carrying many fixtures, which is
the shape most consumers have to reckon with and the one with no obvious
prior art — the comparable libraries model one device as one light. Without
somewhere to put it, every consumer re-derives the same three things: keying
fixtures by address, composing an identifier stable enough to survive a
rename or a DHCP lease, and separating the transformer's own identity from
its fixtures'.

Home Assistant wants exactly that split — the transformer as a device, each
fixture as an entity registered under it — and so does the bridge, since a
HomeKit accessory needs a stable identifier too.
"""

from __future__ import annotations  # Forward refs without quotes

from .device import SDeviceInfo
from .errors import WacResponseError
from .models import CFixture


def StrNormMac(strMac: str) -> str:
	"""Fold a MAC into a form safe to build an identifier from.

	Separators vary by firmware and by which field a MAC arrived in. Since
	these strings end up inside identifiers a consumer stores permanently,
	normalizing once here beats discovering later that a firmware update
	changed every unique ID.
	"""

	return "".join(ch for ch in strMac if ch.isalnum()).lower()


class CSnapshot:  # tag = snap
	"""What a single poll saw.

	Built from a device query and a fixture read that have already happened
	— it issues nothing itself, so a caller holding the raw responses does
	not pay for them twice.
	"""

	def __init__(self, devi: SDeviceInfo, lFixture: list[CFixture]) -> None:
		self.devi = devi
		self.lFixture = lFixture  # every fixture, in the order the device listed them

		# Addressed lookup, which is what a poll loop actually wants. A
		# fixture with no address cannot be addressed or identified, so it
		# stays in lFixture and out of here rather than being dropped or
		# given a synthetic key.

		self.mpAddrFixture = {
			fixture.nAddr: fixture
			for fixture in lFixture
			if fixture.nAddr is not None
		}

		# The same, minus the types this library does not model. A consumer
		# building entities wants this one: the ColorScaping transformer
		# reports a type-4 pseudo-fixture that is addressable like any other
		# but carries empty state and tune, so registering it produces a
		# control that can never report or change anything.
		#
		# This also excludes a genuinely new fixture type from later
		# firmware. That is the intended reading — a consumer cannot decide
		# what kind of entity an unmodeled type deserves. Use mpAddrFixture
		# when you want everything, as a dump or a diagnostic does.

		self.mpAddrFixtureKnown = {
			nAddr: fixture
			for nAddr, fixture in self.mpAddrFixture.items()
			if fixture.FIsKnown()
		}

	def StrDeviceId(self) -> str:
		"""Stable identifier for the device itself.

		Raises rather than returning None: a consumer that cannot identify a
		device cannot register it either, and failing here says why. `staMac`
		is undocumented but present on every response observed.
		"""

		if not self.devi.staMac:
			raise WacResponseError("device reported no staMac, so it cannot be identified")

		return StrNormMac(self.devi.staMac)

	def StrFixtureId(self, nAddr: int) -> str:
		"""Stable identifier for one fixture.

		Scoped by the device, because a fixture address is only unique within
		the transformer that owns it. Deliberately independent of the
		fixture's name and of the device's IP, both of which change.
		"""

		return f"{self.StrDeviceId()}_{nAddr:08x}"

	def FixtureTry(self, nAddr: int) -> CFixture | None:
		"""The fixture at an address, or None if this poll did not see it."""

		return self.mpAddrFixture.get(nAddr)
