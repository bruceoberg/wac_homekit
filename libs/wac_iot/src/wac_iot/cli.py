#!/usr/bin/env python3
"""Command line interface for the device layer.

Slightly impure for a library to ship a console script, but being able to
point `dump` at real hardware without standing up the bridge is worth it.
"""

from __future__ import annotations  # Forward refs without quotes

import argparse
import asyncio
import json
import logging
import sys

from typing import Any

from pydantic import BaseModel

from . import __version__
from .client import CClient
from .control import (
	COLOR_TEMP_LEVEL_MAX,
	COLOR_TEMP_LEVEL_MIN,
	FAN_SPEED_MAX,
	FAN_SPEED_MIN,
	HUE_MAX,
	HUE_MIN,
	LEVEL_MAX,
	LEVEL_MIN,
	RGB_MAX,
	SATURATION_MAX,
	SATURATION_MIN,
	ObjStateFan,
	ObjStateLight,
	ObjStateRgbw,
	ObjStateWhite,
)
from .device import SDeviceInfo
from .discovery import LDiscoBrowse
from .errors import WacError, WacValueError
from .fixture import CFixtures
from .models import (
	LIGHTMODE,
	CFixture,
	SState,
	SStateFan,
	SStateLight,
	SStateRgbw,
	SStateWhite,
)
from .transport import PORT_HTTP, PORT_HTTPS, ProbeHost, SPortProbe


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

# Readability beats cleverness here: this output exists to be compared against
# the vendor documentation by eye.


def PrintRule(strTitle: str) -> None:
	print()
	print(f"=== {strTitle} " + "=" * max(0, 68 - len(strTitle)))
	print()


def PrintJson(obj: Any, strLabel: str = "raw") -> None:
	print(f"--- {strLabel} ---")
	print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def ObjPrune(obj: Any) -> Any:
	"""Drop None-valued fields, recursively.

	Every model field defaults to None, so an unpruned dump buries the few
	fields a fixture actually reports under dozens of nulls.
	"""

	if isinstance(obj, dict):
		return {
			strKey: ObjPrune(objValue)
			for strKey, objValue in obj.items()
			if objValue is not None
		}

	if isinstance(obj, list):
		return [ObjPrune(objValue) for objValue in obj]

	return obj


def MpModelPrune(model: BaseModel) -> Any:
	"""Dump a model without the empties.

	Field names are the device's own, so this output lines up directly with
	the raw JSON printed above it. Anything the model did not declare
	survives here too, because the models allow extra fields — which is the
	point when checking real hardware against the documentation.
	"""

	return ObjPrune(model.model_dump())


def PrintModel(model: BaseModel, strLabel: str = "parsed") -> None:
	print(f"--- {strLabel} ---")
	print(json.dumps(MpModelPrune(model), indent=2, sort_keys=True, default=str))


def PrintFixture(fixture: CFixture) -> None:
	print()
	print(f"  {fixture.StrDescribe()}")

	if not fixture.FIsKnown():
		print("  !! type not modeled by this library — raw structures follow")

	for strLabel, model in (
		("state", fixture.state),
		("tune", fixture.tune),
		("detail", fixture.detail),
	):
		print(f"    {strLabel}: {json.dumps(MpModelPrune(model), sort_keys=True, default=str)}")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


async def DiscoverRun(args: argparse.Namespace) -> int:
	lDisco = await LDiscoBrowse(args.browse)

	if not lDisco:
		print(f"no devices answered in {args.browse:g}s")

		if sys.platform == "darwin":
			# Two mechanisms gate multicast here, and they are
			# indistinguishable from the inside: macOS Local Network privacy,
			# and any outbound firewall. Either way a denied process sees its
			# sendto fail with EPIPE and simply hears nothing back, which
			# looks exactly like an empty network. Both judge the owning
			# application rather than the binary, so a shell hosted inside an
			# editor is judged as that editor.

			print()
			print("this is usually a blocked process, not an empty network. two causes:")
			print("  - macOS: System Settings > Privacy & Security > Local Network")
			print("  - an outbound firewall (Little Snitch and friends) blocking")
			print("    UDP 5353 to 224.0.0.251")
			print()
			print("both judge the *host app* — a shell inside an editor is judged as")
			print("that editor, not as your terminal, so the same command can work in")
			print("one and fail in the other.")
			print()
			print("cross-check with:  dns-sd -B _easylink._tcp local")
			print("that goes through the system mDNS daemon and is not blocked by")
			print("either. if it lists devices and this does not, the process is blocked.")

		return 1

	for disco in lDisco:
		print()
		print(f"host:         {disco.strHost}")
		print(f"ip:           {disco.strIp}")
		print(f"port:         {disco.nPort}")
		print(f"firmware ver: {disco.strFirmwareVer}")
		print(f"protocol:     {disco.strProtocol}")
		print(f"protocol ver: {disco.strProtocolVer}")
		print(f"mac:          {disco.strMac}")
		print(f"mac suffix:   {disco.strMacSuffix}")
		print(f"txt:          {json.dumps(disco.mpStrTxt, sort_keys=True)}")

	print()
	print(f"{len(lDisco)} device(s)")

	return 0


def PrintPortProbe(pprobe: SPortProbe) -> None:
	strScheme = "https" if pprobe.fTls else "http"

	print()
	print(f"port {pprobe.nPort} ({strScheme})")
	print(f"  tcp open:      {pprobe.fTcpOpen}")

	if pprobe.fTls and pprobe.fTcpOpen:
		print(f"  tls handshake: {pprobe.fTlsHandshake}")
		print(f"  cert verifies: {pprobe.fCertVerified}")

		if pprobe.strCertError:
			print(f"  cert error:    {pprobe.strCertError}")

		if pprobe.strTlsVersion:
			print(f"  tls version:   {pprobe.strTlsVersion}")

		if pprobe.strCipher:
			print(f"  cipher:        {pprobe.strCipher}")

		if pprobe.strCertSha256:
			print(f"  cert sha256:   {pprobe.strCertSha256}")

	if pprobe.nHttpStatus is not None:
		print(f"  http status:   {pprobe.nHttpStatus}")

	if pprobe.fJsonBody is not None:
		print(f"  json body:     {pprobe.fJsonBody}")

	if pprobe.strError:
		print(f"  error:         {pprobe.strError}")


async def ProbeRun(args: argparse.Namespace) -> int:
	print(f"probing {args.host} (read-only)")

	probe = await ProbeHost(args.host, dTTimeout=args.timeout)

	for pprobe in probe.lPortProbe:
		PrintPortProbe(pprobe)

	pprobeAnswering = probe.PportprobeAnswering()

	print()

	if pprobeAnswering is None:
		print("no port served the interface — neither 80 nor 443 answered a device query")

		return 1

	strScheme = "https" if pprobeAnswering.fTls else "http"
	print(f"verdict: the interface answers on {strScheme}, port {pprobeAnswering.nPort}")

	if pprobeAnswering.fTls and pprobeAnswering.fCertVerified is False:
		print("         certificate is self-signed, as expected — use --insecure (the default)")

	return 0


async def DumpRun(args: argparse.Namespace) -> int:
	"""Read every endpoint, reporting rather than aborting on failure.

	Firmware in the field does not implement all of these — an InvisiLED
	Wall on protocol 1.40 answers /device and 404s on /fixture. One dead
	endpoint must not cost you the others.
	"""

	cFailed = 0

	async with CClient(
		args.host,
		fTls=args.https,
		nPort=args.port,
		dTTimeout=args.timeout,
		fVerifyTls=args.verify_tls,
	) as client:
		print(f"dumping {client.strBaseUrl}")

		PrintRule("/device query")

		try:
			obj = await client.device.ObjQuery()
			PrintJson(obj)
			PrintModel(SDeviceInfo.model_validate(obj))
		except WacError as exc:
			cFailed += 1
			print(f"!! failed: {exc}")

		PrintRule("/fixture action 3, addr omitted (documented bulk read)")

		lAddrSummary: list[int] = []

		try:
			obj = await client.fixture.ObjRead()
			PrintJson(obj)

			lAddrSummary = [
				fixture.nAddr
				for fixture in CFixtures.LFixtureFromRead(obj, client.strHost)
				if fixture.nAddr is not None
			]
		except WacError as exc:
			cFailed += 1
			print(f"!! failed: {exc}")

		PrintRule("/fixture action 5 (list addresses)")

		lAddr: list[int] = []

		try:
			lAddr = await client.fixture.LAddrList()
			print(f"--- {len(lAddr)} address(es) ---")
			print(json.dumps(lAddr))

			# The two forms are supposed to agree. Where they do not, the
			# bulk read is the one that lies.

			setMissing = set(lAddr) - set(lAddrSummary)
			if setMissing:
				print()
				print(f"!! {len(setMissing)} address(es) listed but absent from the bulk read: "
					f"{sorted(setMissing)}")
		except WacError as exc:
			cFailed += 1
			print(f"!! failed: {exc}")

		PrintRule("/fixture action 3, explicit addr array (full structures)")

		try:
			lFixture = await client.fixture.LFixtureReadAll()
			print(f"--- parsed: {len(lFixture)} fixture(s) ---")

			for fixture in lFixture:
				PrintFixture(fixture)

			cUnknown = sum(1 for fixture in lFixture if not fixture.FIsKnown())
			if cUnknown:
				print()
				print(f"!! {cUnknown} fixture(s) of a type this library does not model")
		except WacError as exc:
			cFailed += 1
			print(f"!! failed: {exc}")

		PrintRule("/group action 3 (all groups)")

		try:
			PrintJson(await client.ObjGroupRead())
		except WacError as exc:
			cFailed += 1
			print(f"!! failed: {exc}")

		PrintRule("/automation action 5 (list)")

		try:
			PrintJson(await client.ObjAutomationList())
		except WacError as exc:
			cFailed += 1
			print(f"!! failed: {exc}")

	if cFailed:
		print()
		print(f"{cFailed} of 6 endpoint reads failed")

	return 1 if cFailed else 0


# ---------------------------------------------------------------------------
# set — the only subcommand that writes
# ---------------------------------------------------------------------------

# Every measurement recorded in this package's rules has the same shape: what
# the fixture held, what went out on the wire, what came back, and — the part
# that keeps being the surprise — which *other* fields moved. So this prints
# all four rather than just firing the request.


def TplRgbParse(strArg: str) -> tuple[int, int, int]:
	"""argparse type for an R,G,B triple.

	Components are 0-255, unlike everything else here. Range checking is left
	to the builder, which words the complaint better.
	"""

	lStr = strArg.replace(",", " ").split()

	if len(lStr) != 3:
		raise argparse.ArgumentTypeError(f"rgb takes three components, got {strArg!r}")

	try:
		nRed, nGreen, nBlue = (int(strOne) for strOne in lStr)
	except ValueError:
		raise argparse.ArgumentTypeError(f"rgb components must be integers, got {strArg!r}") from None

	return (nRed, nGreen, nBlue)


def LightmodeParse(strArg: str) -> LIGHTMODE:
	"""argparse type for a light mode, by name."""

	mpStrLightmode = {
		lightmode.name.lower(): lightmode
		for lightmode in LIGHTMODE
		if lightmode is not LIGHTMODE.Unknown
	}

	lightmode = mpStrLightmode.get(strArg.lower())

	if lightmode is None:
		raise argparse.ArgumentTypeError(
			f"unknown mode {strArg!r} — one of {', '.join(sorted(mpStrLightmode))}"
		)

	return lightmode


# Which builder takes which flag. A field the fixture's own builder does not
# accept has to be refused rather than dropped: a hardware test that reports
# success while changing nothing is worse than one that fails outright.

g_setStrArgLight = frozenset({"fOn", "nLevel", "fFindme", "lightmode"})
g_setStrArgWhite = g_setStrArgLight | {"nColorTempLevel", "nColorTemp"}
g_setStrArgRgbw  = g_setStrArgLight | {"nHue", "nSaturation", "tplRgb", "nColorTemp"}
g_setStrArgFan   = frozenset({"fOn", "fFindme", "nFanSpeed", "fWind", "nWindSpeed", "fDirection"})

# Builder keyword back to the flag that set it, so a refusal names what the
# user typed rather than what the library calls it.

g_mpStrArgStrFlag = {
	"fOn":             "--on/--off",
	"nLevel":          "--level",
	"fFindme":         "--findme",
	"lightmode":       "--mode",
	"nHue":            "--hue",
	"nSaturation":     "--saturation",
	"tplRgb":          "--rgb",
	"nColorTemp":      "--color-temp",
	"nColorTempLevel": "--color-temp-level",
	"nFanSpeed":       "--fan-speed",
	"fWind":           "--wind/--no-wind",
	"nWindSpeed":      "--wind-speed",
	"fDirection":      "--fan-direction",
}


def MpStrArgControl(args: argparse.Namespace) -> dict[str, Any]:
	"""The builder keywords the user actually asked for.

	Unset options are dropped rather than passed as None, so the request
	carries only what is changing — which is what the firmware wants, and
	what makes a read-back diff mean anything.
	"""

	mpStrArg: dict[str, Any] = {
		"fOn":             args.fOn,
		"nLevel":          args.level,
		"fFindme":         args.findme,
		"lightmode":       args.mode,
		"nHue":            args.hue,
		"nSaturation":     args.saturation,
		"tplRgb":          args.rgb,
		"nColorTemp":      args.color_temp,
		"nColorTempLevel": args.color_temp_level,
		"nFanSpeed":       args.fan_speed,
		"fWind":           args.fWind,
		"nWindSpeed":      args.wind_speed,
		"fDirection":      None if args.fan_direction is None else bool(args.fan_direction),
	}

	return {strArg: obj for strArg, obj in mpStrArg.items() if obj is not None}


def CheckArgsSupported(mpStrArg: dict[str, Any], setStrOk: frozenset[str], strShape: str) -> None:
	lStrFlag = sorted(g_mpStrArgStrFlag[strArg] for strArg in mpStrArg if strArg not in setStrOk)

	if lStrFlag:
		raise WacValueError(f"a {strShape} fixture does not take {', '.join(lStrFlag)}")


def ObjStateByShape(state: SState, mpStrArg: dict[str, Any]) -> dict[str, Any]:
	"""Build control state with the builder matching the fixture's wire shape.

	Dispatch is on the state class `wac_iot` already picked for this fixture
	type, not on a second type-to-capability table that could drift from it.
	Order matters: white, RGBW and motor all derive from the plain light
	shape.

	The state is built here and sent by the caller, rather than going through
	the `Control*` convenience methods, only so the exact body can be printed
	before it goes out. Same builders, same range and exclusion checks.
	"""

	if isinstance(state, SStateRgbw):
		CheckArgsSupported(mpStrArg, g_setStrArgRgbw, "RGBW")

		return ObjStateRgbw(**mpStrArg)

	if isinstance(state, SStateWhite):
		CheckArgsSupported(mpStrArg, g_setStrArgWhite, "tunable white")

		return ObjStateWhite(**mpStrArg)

	if isinstance(state, SStateFan):
		CheckArgsSupported(mpStrArg, g_setStrArgFan, "fan")

		return ObjStateFan(**mpStrArg)

	if isinstance(state, SStateLight):
		# A motorized trackhead lands here too. It dims like any other light,
		# and aim and zoom have no builder yet.

		CheckArgsSupported(mpStrArg, g_setStrArgLight, "single color")

		return ObjStateLight(**mpStrArg)

	raise WacValueError(f"nothing controllable on a {type(state).__name__} fixture")


def PrintStateMoved(mpStrBefore: dict[str, Any], mpStrAfter: dict[str, Any]) -> None:
	"""Every field that moved, whether the request mentioned it or not.

	Writing the RGB triple also moves hue and saturation, which no request
	ever asks for. Catching that is most of why this subcommand exists.
	"""

	lStrLine = [
		f"  {strKey}: {mpStrBefore.get(strKey)!r} -> {mpStrAfter.get(strKey)!r}"
		for strKey in sorted(set(mpStrBefore) | set(mpStrAfter))
		if mpStrBefore.get(strKey) != mpStrAfter.get(strKey)
	]

	print("--- moved ---")
	print("\n".join(lStrLine) if lStrLine else "  nothing")


async def FixtureTryRead(client: CClient, nAddr: int) -> CFixture | None:
	lFixture = await client.fixture.LFixtureRead(nAddr)

	return lFixture[0] if lFixture else None


async def SetRun(args: argparse.Namespace) -> int:
	"""Read one fixture, write it, read it back, report what moved."""

	mpStrArg = MpStrArgControl(args)

	if not mpStrArg:
		print("nothing to set — pass --on/--off, --level, --rgb, or another field", file=sys.stderr)

		return 2

	async with CClient(
		args.host,
		fTls=args.https,
		nPort=args.port,
		dTTimeout=args.timeout,
		fVerifyTls=args.verify_tls,
	) as client:
		print(f"setting {client.strBaseUrl} fixture {args.addr}")

		fixture = await FixtureTryRead(client, args.addr)

		if fixture is None:
			print(f"!! no fixture at address {args.addr}", file=sys.stderr)

			return 1

		print(f"  {fixture.StrDescribe()}")

		mpStrBefore = MpModelPrune(fixture.state)
		PrintJson(mpStrBefore, "state before")

		objState = ObjStateByShape(fixture.state, mpStrArg)
		PrintJson(objState, "state sent")

		PrintJson(await client.fixture.ObjControl(args.addr, objState), "response")

		# The firmware answers before the fixture has necessarily settled, and
		# an immediate read-back has been seen to report the old value.

		if args.settle:
			await asyncio.sleep(args.settle)

		fixtureAfter = await FixtureTryRead(client, args.addr)

		if fixtureAfter is None:
			print(f"!! fixture {args.addr} vanished between write and read-back", file=sys.stderr)

			return 1

		mpStrAfter = MpModelPrune(fixtureAfter.state)
		PrintJson(mpStrAfter, "state after")
		PrintStateMoved(mpStrBefore, mpStrAfter)

	return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def ParserCommon() -> argparse.ArgumentParser:
	"""Options every subcommand takes.

	Carried by a parent parser rather than the top-level one so they are
	typed after the subcommand, alongside --host, instead of before it.
	"""

	parser = argparse.ArgumentParser(add_help=False)
	parser.add_argument(
		"-v",
		"--verbose",
		action="store_true",
		help="log at debug level",
	)
	parser.add_argument(
		"--timeout",
		type=float,
		default=10.0,
		help="per-request timeout in seconds (default: 10)",
	)

	return parser


def AddHostArgs(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--host", required=True, help="device address or hostname")
	parser.add_argument(
		"--port",
		type=int,
		default=None,
		help=f"override the port (default {PORT_HTTP} for http, {PORT_HTTPS} for https)",
	)
	parser.add_argument(
		"--https",
		action="store_true",
		help="use https (hardware measured so far serves plain http on 80)",
	)
	parser.add_argument(
		"--verify-tls",
		action="store_true",
		help="verify the TLS certificate (these devices are self-signed, so this will fail)",
	)


def main() -> None:
	parser = argparse.ArgumentParser(
		prog="wac_iot",
		description="Talk to WAC Lighting IoT devices directly",
	)
	parser.add_argument(
		"--version",
		action="version",
		version=f"%(prog)s {__version__}",
	)
	parserCommon = ParserCommon()
	subparsers = parser.add_subparsers(dest="command", required=True)

	parserDiscover = subparsers.add_parser(
		"discover",
		parents=[parserCommon],
		help="browse mDNS for devices",
		description="Browse the local network for devices and print their advertised details.",
	)
	parserDiscover.add_argument(
		"--browse",
		type=float,
		default=5.0,
		help="seconds to browse (default: 5)",
	)
	parserDiscover.set_defaults(run=DiscoverRun)

	parserProbe = subparsers.add_parser(
		"probe",
		parents=[parserCommon],
		help="find out which port and scheme a device serves",
		description=(
			"Open both candidate ports and report which one answers, including "
			"certificate behavior. Read-only."
		),
	)
	parserProbe.add_argument("--host", required=True, help="device address or hostname")
	parserProbe.set_defaults(run=ProbeRun)

	parserDump = subparsers.add_parser(
		"dump",
		parents=[parserCommon],
		help="print a device's fixtures, groups, and automations",
		description=(
			"Query the device, then read all fixtures, groups, and automations "
			"— one request each. Prints raw JSON alongside anything parsed."
		),
	)
	AddHostArgs(parserDump)
	parserDump.set_defaults(run=DumpRun)

	parserSet = subparsers.add_parser(
		"set",
		parents=[parserCommon],
		help="write one fixture's control state (the only subcommand that writes)",
		description=(
			"Read one fixture, send it a control state, then read it back and "
			"report every field that moved — including the ones the request "
			"never mentioned. Values are device units exactly as the fixture "
			"reports them; nothing is converted here."
		),
	)
	AddHostArgs(parserSet)
	parserSet.add_argument(
		"--addr",
		type=int,
		required=True,
		help="fixture address, as listed by `dump`",
	)
	parserSet.add_argument(
		"--on",
		dest="fOn",
		action="store_true",
		default=None,
		help="turn the fixture on",
	)
	parserSet.add_argument(
		"--off",
		dest="fOn",
		action="store_false",
		help="turn the fixture off",
	)
	parserSet.add_argument(
		"--level",
		type=int,
		default=None,
		metavar="N",
		help=f"brightness, {LEVEL_MIN}-{LEVEL_MAX} in 0.01%% steps",
	)
	parserSet.add_argument(
		"--hue",
		type=int,
		default=None,
		metavar="N",
		help=f"hue, {HUE_MIN}-{HUE_MAX} (measured not to work on real firmware — use --rgb)",
	)
	parserSet.add_argument(
		"--saturation",
		type=int,
		default=None,
		metavar="N",
		help=f"saturation, {SATURATION_MIN}-{SATURATION_MAX} (see --hue)",
	)
	parserSet.add_argument(
		"--rgb",
		type=TplRgbParse,
		default=None,
		metavar="R,G,B",
		help=f"color as an RGB triple, each component 0-{RGB_MAX} — the way color is written",
	)
	parserSet.add_argument(
		"--color-temp",
		type=int,
		default=None,
		metavar="K",
		help="color temperature in kelvin (mixColorTemp); bounds are per-fixture, in its detail",
	)
	parserSet.add_argument(
		"--color-temp-level",
		type=int,
		default=None,
		metavar="N",
		help=f"stepped white index, {COLOR_TEMP_LEVEL_MIN}-{COLOR_TEMP_LEVEL_MAX}",
	)
	parserSet.add_argument(
		"--mode",
		type=LightmodeParse,
		default=None,
		help="light mode by name (nothing has ever moved this on real firmware)",
	)
	parserSet.add_argument(
		"--findme",
		action="store_true",
		default=None,
		help="make the fixture announce itself; write-only, so it never reads back",
	)
	parserSet.add_argument(
		"--fan-speed",
		type=int,
		default=None,
		metavar="N",
		help=f"fan gear, {FAN_SPEED_MIN}-{FAN_SPEED_MAX} (not a percentage)",
	)
	parserSet.add_argument(
		"--wind",
		dest="fWind",
		action="store_true",
		default=None,
		help="enable the fan's wind model",
	)
	parserSet.add_argument(
		"--no-wind",
		dest="fWind",
		action="store_false",
		help="disable the fan's wind model",
	)
	parserSet.add_argument(
		"--wind-speed",
		type=int,
		default=None,
		metavar="N",
		help=f"wind gear, {FAN_SPEED_MIN}-{FAN_SPEED_MAX}",
	)
	parserSet.add_argument(
		"--fan-direction",
		type=int,
		choices=(0, 1),
		default=None,
		help="fan direction; which way each value turns is undocumented",
	)
	parserSet.add_argument(
		"--settle",
		type=float,
		default=0.5,
		metavar="SEC",
		help="seconds to wait before reading the state back (default: 0.5)",
	)
	parserSet.set_defaults(run=SetRun)

	args = parser.parse_args()

	logging.basicConfig(
		level=logging.DEBUG if args.verbose else logging.INFO,
		format="%(levelname)s %(name)s: %(message)s",
	)

	try:
		nExit = asyncio.run(args.run(args))
	except WacError as exc:
		print(f"error: {exc}", file=sys.stderr)
		nExit = 1
	except KeyboardInterrupt:
		nExit = 130

	sys.exit(nExit)


if __name__ == "__main__":
	main()
