# wac_iot

An async client library for [WAC Lighting](https://www.waclighting.com/) IoT
devices — the transformers and controllers that speak the vendor's local REST
interface over your own network.

No cloud, no account, no vendor app in the middle: point it at a device on the
LAN and talk to it directly.

## What it does

- **Discovery** over mDNS, either as a one-shot browse or as a long-lived
  watch that reports devices appearing, moving between DHCP leases, and going
  away.
- **Reads** a whole transformer — its own identity plus every fixture hanging
  off it — in one call.
- **Writes** fixture state through typed builders that refuse an invalid
  combination before spending a request.

## Install

```sh
pip install wac_iot            # the library
pip install wac_iot[discovery] # ... plus mDNS discovery
```

Discovery is an extra because it is the only part that needs `zeroconf`. A
consumer that already owns an mDNS stack — Home Assistant hands every
integration a shared instance — installs the bare package and calls
`DiscoFromTxt` on the service info it already has.

## Use

```python
import asyncio

from wac_iot import CClient, LDiscoBrowse


async def main() -> None:
	for disco in await LDiscoBrowse():
		if not disco.strIp:
			continue

		async with CClient(disco.strIp) as client:
			snap = await client.SnapPoll()

			print(f"{disco.strIp}: device {snap.StrDeviceId()}")

			for nAddr, fixture in snap.mpAddrFixtureKnown.items():
				print(f"  {nAddr}: {fixture.StrDescribe()}")

			# Turn the first fixture on at half brightness. `level` is in
			# device units, 0-10000 — see below.

			nAddr = next(iter(snap.mpAddrFixtureKnown))

			await client.fixture.ControlLight(nAddr, fOn=True, nLevel=5000)


asyncio.run(main())
```

One transformer carries many fixtures, so a fixture is addressed within its
device rather than reached on its own. `CSnapshot` gives you the split, along
with identifiers stable across a rename or a DHCP lease.

`mpAddrFixtureKnown` rather than `mpAddrFixture`: some hardware reports a
pseudo-fixture with empty state, and this library would rather hide it than
hand you an entity that can never report or change anything.

## Values are in device units, deliberately

`level` runs 0–10000 in hundredths of a percent, hue and saturation run
0–10000, colour temperature is degrees Kelvin, fan speed is a gear number.
Nothing here converts to anyone's preferred scale — but the bounds are
exported (`LEVEL_MAX`, `HUE_MAX`, `FAN_SPEED_MAX` and friends) so a consumer
can convert against them instead of hardcoding numbers that a firmware
revision may move.

Doing the conversion in one place, on the consumer's side, is the point: a
value that gets rescaled and rounded on both sides of a round trip is what
makes a UI slider visibly flicker.

## Supported surface

Whatever `wac_iot/__init__.py` exports. Import from the package, not from its
submodules.

## Status

Written against a vendor specification and corrected against real hardware,
which disagree in several places. Where they disagree, the hardware wins and
this library follows the hardware. Fixture types this author has no example
of are implemented from the specification alone and have never been exercised.

`wac_iot` also ships a CLI — `wac_iot discover`, `dump`, `probe`, `set` — which
is how the hardware gets interrogated in the first place. Point it at a device
before assuming anything.

## Licence

MIT. See [LICENSE](LICENSE).
