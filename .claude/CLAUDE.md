# wac_homekit

A HomeKit bridge for WAC Lighting IoT devices, built on HAP-python.

## Commands

- `just test` — pytest
- `just check` — mypy (strict)
- `just run <args>` — run an entry point
- `just add <pkg>` / `just add-dev <pkg>` — add dependencies
- `just upgrade` — relock and sync

Add dependencies with `just add`, never by hand-editing `pyproject.toml`.

The environment is devenv + uv; `direnv allow` activates it. Do not create or
activate virtualenvs by hand — `UV_PROJECT_ENVIRONMENT` already points at
devenv's managed venv, and a stray `.venv/` will silently shadow it.

## Layout

Two packages in one uv workspace:

- `src/wac_homekit/` — the bridge. Owns everything HomeKit.
- `libs/wac_iot/src/wac_iot/` — the device library. Owns everything WAC.

## Hard rules

**The package boundary is the point of this repo.**

- `wac_iot` must never import `pyhap` or anything else HomeKit-related.
- `wac_homekit` must never import `aiohttp` or `zeroconf` directly. It reaches
  the devices only through `wac_iot`.
- `wac_iot` is async throughout — `aiohttp`, never `requests`.
- `wac_iot`'s public surface is whatever `wac_iot/__init__.py` exports.
  Consumers import from there, not from submodules.

`wac_iot` is meant to be extracted later as a standalone package backing a Home
Assistant integration. Anything that couples it to HomeKit turns that extraction
into a rewrite.

**Unit conversion lives in `wac_homekit`, never in `wac_iot`.**

`wac_iot` speaks device units exactly as the spec defines them:

- `level` (brightness): 0–10000, in 0.01% steps
- `hue` / `saturation`: 0–10000
- `mixColorTemp`: degrees Kelvin
- `fanSpeed`: gears 1–6

`wac_homekit` converts to HomeKit units (Brightness 0–100, Hue 0–360,
Saturation 0–100, ColorTemperature in mireds, RotationSpeed 0–100) in one
place. Careless round-tripping between these ranges makes Home app tiles
visibly flicker, so keep the conversions together and test them.

Convert *into* the bounds `wac_iot` exports — `LEVEL_MAX`, `HUE_MAX`,
`FAN_SPEED_MAX` and friends — rather than repeating the numbers above in
conversion code. Build the resulting state with the typed `Control*` methods
on `CFixtures`, not a hand-written dict: they enforce the ranges and the
mutually-exclusive groupings (stepped white index vs. Kelvin; HSV vs. RGB
vs. white point) in one place, and refuse before spending a request.

## The vendor spec

The WAC IoT Unified REST Interface PDF is in `private/`, which is gitignored.
Read it for protocol details.

Every page is marked CONFIDENTIAL. **Never copy its text into a committed
file** — no pasting tables into docstrings, comments, README, or markdown.
Derive code from it; do not reproduce it.

It is also not fully reliable. **Where the document and hardware disagree, the
hardware wins** — the notes below record measurements, not readings.

### Measured against real hardware

Measured on a ColorScaping controller (`iotmVer 01.04.0149`, `restVer 1.40`)
and two InvisiLED wall stations (`iotmVer 01.00.0014`, `restVer 1.40`). The
document describes protocol 1.91, so expect further drift on newer firmware —
re-run `just dump` rather than assuming these hold.

- **The interface is plain HTTP on port 80.** Port 443 refuses the connection
  outright on every device tested, even though mDNS advertises it. There is no
  TLS and no certificate to deal with. `wac_iot probe` re-checks this.
- **`query` must be boolean `true`.** The document's example shows `"query": 1`;
  that is rejected with an undocumented result code `-100` and a `status`
  string explaining it. Status codes outside Appendix 2 exist — never assume
  the appendix is exhaustive.
- **mDNS instance names do not use the documented `STRUT_` prefix.** Observed:
  `WAC_WCT_xxxxxx` (wall station) and `WAC_CS_xxxxxx` (ColorScaping). The
  stable part is the trailing six hex digits of the station MAC; parse that,
  do not match a prefix.
- **Fixture type 4 exists**, despite the document skipping it. Its `detail` is
  corrupt — model and driver strings arrive byte-reversed (`gnipacsroloC`),
  with a nonsense date code and control characters in `pcbVer`. It has empty
  `state` and `tune` and is excluded from the All-Default group, so it is
  likely the controller appearing as a pseudo-fixture. Unknown types must log
  and resolve, never raise.
- `result` is documented as a String and observed as `"0"`. Parse both string
  and numeric forms.
- mDNS TXT keys do contain literal spaces, as documented: `Firmware Ver`,
  `Protocol Ver`.
- Every response carries an undocumented `staMac`.
- Fixture addresses are large 32-bit values, not small indices, and a fixture's
  default name embeds its own address in hex (`Zone 2 09FFFFFD` at
  `167772157`).
- **RGBW fixtures also report a color temperature range and step table** in
  `detail` (`minColorTemp` / `maxColorTemp` / `colorTempStepsTable`), so RGBW
  shares the tunable-white detail shape rather than the plain one. Within that
  table the firmware names the value `colorStepsValue`, not the documented
  `mixColorTemp`; both are accepted.
- A transformer's own output zone appears as an ordinary **type 6 (ELV)**
  fixture, which the document does describe as virtual. It is not a distinct
  hub or controller type.

### Still unverified

- Tunable white accepts `colorTempLevel` (steps 1–7) *or* `mixColorTemp`
  (Kelvin), explicitly not both. RGBW exposes both RGB and HSV. Pick one path
  per fixture type and use it consistently.
- No tunable white, fan, motorized trackhead, or wall-station *fixture* (type
  11) has been seen on real hardware yet. Those models are written from the
  document alone. Single color (0), RGBW (2), and ELV (6) have been seen.
- Configure (action 6) is still unexercised. Control (action 4) has been
  written exactly once, on the ColorScaping transformer: `findme` true then
  false against an RGBW fixture that was off. Both were accepted with
  `result "0"`, and no other state field moved. Nothing else — no level,
  status, or color — has ever been written.
- **`findme` never appears in a fixture's read-back `state`.** It stayed
  absent before, during, and after the write above, so it looks write-only.
  Whether the fixture physically responded is unconfirmed: the fixture was
  off at the time and nobody was watching it. Do not treat a missing
  `findme` as evidence that Identify failed, and do not build a HomeKit
  Identify round trip that reads it back.

## Protocol facts that shape the design

- Every endpoint is a `POST` carrying an `action` number in the JSON body.
  There is no verb-to-operation mapping; do not design one. `/device` is the
  exception: it carries no action and dispatches on which fields are present.
- **The documented one-request bulk read does not work.** `POST /fixture` with
  `{"action": 3}` and `addr` omitted is documented to return every fixture with
  its `state`, `tune`, and `detail`. It does neither: it returns summaries only
  (`addr`, `name`, `type`, `model`, `online`) *and* silently omits fixtures
  that action 5 lists.

  Poll with **action 5 for the addresses, then action 3 with the full address
  array** — that returns complete structures and is still two requests total,
  not one per fixture. `CFixtures.LFixtureReadAll` does exactly this; use it
  rather than `ObjRead()`.

  Higher still, `CClient.SnapPoll` pairs that with a device query and returns
  a `CSnapshot` — fixtures keyed by address, plus `StrDeviceId` /
  `StrFixtureId` for identifiers stable across renames and DHCP leases. One
  transformer carries many fixtures, so a consumer needs that split; poll
  through `SnapPoll` rather than rebuilding it.

  **Build entities from `mpAddrFixtureKnown`, not `mpAddrFixture`.** The
  type-4 pseudo-fixture is addressable like any other but has empty `state`,
  so it would become an accessory that can never report or change anything.
  The known map drops it, and drops any future type this library does not
  model yet. Use the full map only for dumps and diagnostics.
- Not every device implements every endpoint. The wall stations answer only
  `/device`, `/network`, `/ota`, and `/fs`, and return HTTP 404 with a plain
  text body for `/fixture`, `/group`, and `/automation`. Tools must degrade
  per-endpoint instead of aborting the run.
- **Wall stations are not reachable as fixtures over REST.** Even fully
  commissioned through the WAC app, an InvisiLED wall station exposes no
  fixture, group, remote, or input endpoint — only its own identity. It does
  not appear in the transformer's fixture list either, and the transformer's
  `/remote` list is empty. Devices are associated only by a shared
  `locationId`, and the transformer advertises a `wsMcast` feature, so button
  presses almost certainly travel over the UDP multicast channel rather than
  REST. **Do not plan on reading wall-station buttons through this
  interface** — treat each device as its own independent REST endpoint,
  grouped by `locationId`.
- The transformer is the only device worth polling for light state. Address it
  directly; do not try to reach its fixtures through a wall station.
- There is no push channel. Polling is the only option; 5–10 seconds is the
  starting range for these ESP32-class devices.
- `findme` in a fixture's control state maps onto HomeKit's Identify
  characteristic.
- Group address 255 is a built-in "All-Default" group. It holds every *real*
  fixture, but not the type-4 pseudo-fixture above — do not treat its
  membership as equivalent to the action 5 address list.

## Testing

The things worth testing here are the pure functions whose arithmetic is easy
to get subtly wrong:

- unit conversion between device and HomeKit ranges
- mDNS TXT record parsing
- status code to exception mapping

The device layer is not worth a mock HTTP server. Verify it against real
hardware with the `dump` CLI instead.