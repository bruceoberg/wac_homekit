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

#### When discovery finds nothing

Almost always a blocked *process*, not an empty network. Two candidates, and
the CLI text naming only the first is incomplete:

- macOS Local Network privacy (System Settings > Privacy & Security).
- An outbound firewall. Little Snitch was the actual culprit on this machine,
  and its denial is indistinguishable from the macOS one: `sendto` to
  224.0.0.251 fails with `BrokenPipeError` (EPIPE) and the browse just goes
  quiet.

**The identity being judged is the host application, not the terminal.** Both
mechanisms attribute a subprocess to the app that owns it, so a shell inside
VS Code is judged as `com.microsoft.VSCode` — Little Snitch spells this out in
its own log as `LSSocketFlow /Applications/Visual Studio Code.app/... via
.../python3.14`. The same command therefore works from Terminal and fails from
an editor-hosted shell, which reads as flakiness until you know to look.

**A non-empty result does not mean discovery works.** A blocked process still
receives loopback self-answers, so it sees this machine's own services and
nothing else. Compare against `dns-sd -B _easylink._tcp local`, which goes
through the system mDNSResponder and is not subject to either mechanism: if
`dns-sd` reports a service on a real interface and the library reports nothing,
the process is blocked. Watch the `if` column — interface 1 is loopback.

Little Snitch rules should be keyed to the host app, not to the interpreter:
a nix-store python path carries a content hash that changes on every rebuild,
and the binary is `adhoc, linker-signed`, so its "Code ID" is a build hash
with no developer identity behind it. Constrain by destination and port
instead — the bridge needs outbound UDP 5353 to 224.0.0.251, outbound TCP 80
to the transformers, and inbound TCP on the bridge's own port.
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

#### Writing state (action 4)

Measured on an RGBW fixture on the ColorScaping transformer. These are the
only writes ever made to this hardware.

- **Control works and partial writes are partial.** `status` alone, then
  `red`/`green`/`blue` alone, each accepted with `result "0"`. Fields not
  named in the body stayed exactly where they were — `level` held at 9981
  and `mode` at 2 across both writes. Send only what is changing.
- **RGB components are 0–255**, not the 0–10000 everything else uses. 255
  was accepted and stored verbatim, and the fixture's own full-blue state
  reports `blue: 255`.
- **RGB and HSV are two views of one colour state, not independent
  fields.** Writing `red`/`green`/`blue` = 255 also moved `hue` 6666 → 0 and
  `saturation` 10000 → 0, which nothing in the request mentioned. So the
  firmware derives one from the other, and sending both in a single request
  really would be two conflicting writes. `ObjStateRgbw` refuses that.
- **RGB (255,255,255) produces visible white** on an RGBW fixture, confirmed
  by eye. The firmware agrees, reporting `saturation: 0` afterwards. Whether
  this lights a dedicated white LED or just all three colour channels is
  unknown — the `mixColorTemp` path has never been written.
- **`level` is writable and exact.** HomeKit 50% → `level: 5000`, 100% →
  `level: 10000`, each accepted and stored verbatim with the colour fields
  untouched. Note a fixture idling at 9981 reads as 100% and gets snapped to
  10000 by the first brightness write; that is stable, not oscillating.

##### Colour is read as HSV and written as RGB

The single most surprising thing this hardware does, and the one a consumer
will get wrong by reading the document. **Writing `hue` / `saturation` never
changes the light.** Measured, in this order, on an RGBW fixture that was on
and blue:

- `{hue, saturation}` → refused, `MissingRequiredParam (-44)`, *"incorrect
  set of HSV attributes in command, no HSV action taken"*.
- `{hue, saturation, level}` → refused, same error.
- `{hue, saturation, mode: 3}` → refused, same error.
- `{hue, saturation, level, mode: 3}` → refused, same error.
- `{red, green, blue}` → **accepted, and the light changed.**

So there is no combination of HSV fields this firmware honors. Worse, after a
`mode` write is attempted the failure mode *changes*: `{mode: 3}` is accepted
while `mode` stays 2, and subsequent `{hue, saturation}` writes then return
`result "0"` and are silently discarded. An accepted-and-ignored write is far
more dangerous than a refused one — do not read a zero result on an HSV write
as evidence that anything happened.

Reads are unaffected and stay on HSV: writing `red/green/blue` moves `hue`
and `saturation` to match, so a poll reports colour correctly. The bridge
therefore reads HSV and writes RGB, converting in `TplRgbFromHueSat`. Round
trip verified on hardware: HomeKit Hue 120 → RGB (0,255,0) → device `hue
3333` → read back as 120°, exact.

`mode` appears to be read-only in practice. Nothing has ever moved it.

#### Where brightness lives — partly measured, partly open

Setting "dark red" from the WAC app produced `red: 128` alongside `hue: 0`,
`saturation: 10000`, `level: 9977`. Writing `red: 255` back moved nothing
else. What that establishes on the wire:

- **`level` and the RGB triple are independent fields.** Writing the RGB
  triple alone left `level` at 9977 exactly. Neither is derived from the
  other.
- **HSV carries no value component.** Taking red from 128 to 255 left `hue`
  at 0 and `saturation` at 10000, unmoved. Value is therefore in the RGB
  magnitude and nowhere in H/S — so `hue` + `saturation` + `level` is *not*
  a complete description of the colour state.
- **`hue: 0` is ambiguous on its own.** Fully saturated red and fully
  desaturated white both report it; only `saturation` separates them. Never
  treat a falsy hue as "no colour reported".

**Open: whether RGB magnitude actually changes light output.** Every
brightness reading so far was taken in daylight, where neither the app's
own "red" / "dark red" presets nor our 128 → 255 write produced a
discernible difference. So `red: 128` may mean half output, or may be a
stored value the fixture does not render. Until that is settled in darkness,
do not build brightness conversion on either assumption.

The two answers lead to different bridges. If magnitude does drive output,
apparent brightness is a product of `level` and RGB magnitude, reading
`level` alone would report 99.8% on a half-lit fixture, and writing
brightness has two mechanisms that need to be chosen between. If it does
not, `level` alone is the brightness field and the magnitude is cosmetic.
Test in the dark: set `red: 255`, then `red: 64`, with `level` untouched.

**What the bridge does meanwhile.** `TplRgbFromHueSat` pins value at full, so
the RGB triple carries chromaticity only and `level` carries brightness
alone. Half-saturated red is `(255, 128, 128)`, never a dimmed `(128, 0, 0)`.
That is the choice that stays correct under either answer: if magnitude is
cosmetic it is obviously right, and if magnitude does drive output it still
keeps the two axes from fighting. Revisit it only once the dark test lands.

Colour *hue* readings are not in doubt — cyan, red and green were each set
from the app and read back correctly, and a blue-to-white change was
confirmed by eye. Only the brightness axis is unresolved.

### Still unverified

- Tunable white accepts `colorTempLevel` (steps 1–7) *or* `mixColorTemp`
  (Kelvin), explicitly not both — document only, and no tunable white fixture
  has been seen. The comparable RGBW rule *is* now measured; see above.
- No tunable white, fan, motorized trackhead, or wall-station *fixture* (type
  11) has been seen on real hardware yet. Those models are written from the
  document alone. Single color (0), RGBW (2), and ELV (6) have been seen.
- Configure (action 6) is still unexercised. Of the action 4 fields,
  `status`, `findme`, the RGB triple and `level` are now measured working;
  `hue`, `saturation` and `mode` are measured *not* working (see above);
  `mixColorTemp` has still never been written to any fixture.
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

## The HomeKit side

Built on HAP-python 5.0. `convert.py` owns the units, `accessory.py` turns one
`CFixture` into one Lightbulb, `driver.py` owns the `Bridge` and the poll loop.
Lights only — `TierTryFromFixturek` returning None is the filter, and adding a
fixture type to `g_mpFixturekTier` is the whole change needed to bridge it.

### Verified on hardware

Run against the ColorScaping transformer at protocol 1.40 — three light
fixtures (`hub`, an ELV; `water` and `sky`, both RGBW) plus the type-4
pseudo-fixture. What the live run established:

- Discovery, accessory construction, tier selection and characteristic sets
  are all correct. The ELV got On + Brightness; the RGBW pair got On +
  Brightness + Hue + Saturation; nothing got ColorTemperature, since no
  tunable-white fixture exists on this transformer. The type-4 pseudo-fixture
  was excluded.
- Writes land exactly. HomeKit Hue 120 → device `hue 3333` → reads back 120°;
  Brightness 50 → `level 5000` → reads back 50. No drift on any axis, so no
  tile flicker.
- **The reconcile-corrects-a-failed-write path is confirmed, not theoretical.**
  The first colour write was refused by the firmware (see the HSV section
  above). HomeKit had already optimistically shown the new value; the next
  poll pulled it back to what the device actually holds. That is the designed
  behaviour and it works — but note the corollary: a failed write returns
  success to the controller, and the only correction is the next poll.
- Three polls over ~22s produced **zero** characteristic updates once the
  fixtures were idle, so the "only notify if the value moved" guard holds.
- Clean SIGTERM shutdown, exit 0.

Still unexercised on hardware: pairing from a real Home app, ColorTemperature
(no tunable-white fixture exists here), and Identify.

### What HAP-python actually requires

- **`AccessoryDriver.start()` does not work on Python 3.14.** It installs an
  `asyncio.SafeChildWatcher`, which 3.14 removed, and dies with an
  `AttributeError` before the loop ever runs. Pass `loop=` and drive
  `async_start()` / `async_stop()` yourself. That is the right shape anyway —
  the `aiohttp` sessions inside `wac_iot` have to live on the same loop.
- **The pincode is not persisted.** The encoder stores the MAC, the keypair,
  the paired clients, the config version and the accessories hash — not the
  setup code. So an unpaired restart without `--pincode` prints a different
  code every time. After pairing it stops mattering.
- **Use `Service.setter_callback`, not per-characteristic setters.** The Home
  app writes On + Brightness, or On + Hue + Saturation, in a single request,
  and the service-level callback receives the whole batch keyed by
  characteristic display name. Splitting it per characteristic would mean two
  or three device requests racing, and for RGBW it would send hue and
  saturation as separate writes to one colour state — which `ObjStateRgbw`
  is right to treat as conflicting.
- **HAP-python has already done the optimistic update** by the time either
  setter runs: `client_update_value` stores the value and notifies first.
  There is nothing to set optimistically. A failed device write therefore
  leaves HomeKit briefly ahead of the hardware, and the next poll corrects it
  — the same path a change made from the WAC app takes.
- **`Accessory.run_at_interval` takes a literal**, so a configurable interval
  means applying the decorator at call time rather than at class definition:
  `await Accessory.run_at_interval(dT)(CBridge._PollAll)(self)`. Worth keeping
  over a hand-rolled loop, because the decorator waits on
  `driver.aio_stop_event` and shutdown does not have to sit out a full
  interval.
- **Overriding `run` on a `Bridge` drops what `Bridge.run` does** — scheduling
  each contained accessory's own `run`. Fine only while no fixture accessory
  has one.
- **`add_accessory` writes the persist file immediately.** Do not call it for
  a bridge that turned out to have nothing to serve.
- **`ColorTemperature` is not in HAP-python's Lightbulb optional list**, but
  adding it works — the loader does not validate. Its default range is
  140–500 mireds, so override `minValue`/`maxValue` per fixture from that
  fixture's own `detail` or the Home app's slider will run past the hardware.
- **HAP-python ships no `py.typed`.** Two mypy overrides in `pyproject.toml`
  cover it: `ignore_missing_imports` for `pyhap.*`, and
  `disallow_subclassing_any = false` for the two modules that subclass
  `Accessory` / `Bridge`. Nothing else in the tree may subclass an untyped
  base.
- QR-code pairing needs the `HAP-python[QRCode]` extra, which is not
  installed; startup prints the numeric code only.

### Decisions worth not relitigating

- **AIDs must be stable across restarts** — iOS remembers which accessory in a
  bridge it paired with by AID, and a reshuffle turns every light in the Home
  app into a stranger. `NAidFromFixtureId` is SHA-256 of `StrFixtureId`
  truncated to six bytes and folded above 7 (1 is the bridge itself;
  HAP-python documents 7 as unusable). `CBridge._NAidFree` still checks,
  because the failure mode of a collision is a light that silently never
  appears.
- **Colour temperature endpoints snap rather than convert.** The reciprocal of
  370 mireds is 2703K — three Kelvin inside a 2700K fixture's limit, and a
  value that does not survive the round trip. `CColorTempRange` returns the
  fixture's own bound at each end. When a fixture reports no span,
  2700–6500K is the documented fallback; widen it only by reading a real
  fixture's `detail`.
- **Poll interval defaults to 5s**, the responsive end of the range these
  transformers tolerate. It is also the upper bound on how long a
  wall-station press stays invisible to HomeKit, since there is no push
  channel.
- A failed poll marks every accessory on that device unavailable rather than
  leaving stale values on show, so an unplugged transformer reads as "No
  Response" in the Home app.
- **The bridge pins one interface; the default route does not get to choose.**
  Left alone, HAP-python derives its advertised address from the default route
  and Zeroconf browses every interface. On a machine that is on wifi and
  ethernet at once, the advertised address then moves when a dock appears and
  the browse answers on whichever link Zeroconf preferred — so the Home app
  follows the bridge onto a link that disappears at the next unplug. The HAP
  MAC is *not* the moving part: it is synthetic, generated once into the
  persist file, and stable across interfaces. Only the address moves.
  `StrAddrResolve` resolves one address and `NRun` hands the same one to both
  `AccessoryDriver(address=)` and `LDiscoBrowse`, so advertising and discovery
  cannot drift apart.
- **`--interface` keeps its generic name on purpose.** It takes an interface
  name, an address, `wifi`, or `auto`, and every explicit value is a hard
  requirement — `CIfaceError` rather than a silent fallback, because a bridge
  on the wrong interface looks like one that works right up until the machine
  moves. Only `auto`, the default, is a preference: wifi if the machine has a
  radio, else the default route with a warning, so a headless box with no
  radio still runs. A `--prefer-*` name would advertise a fallback that only
  `auto` has.

## Testing

The things worth testing here are the pure functions whose arithmetic is easy
to get subtly wrong:

- unit conversion between device and HomeKit ranges, including round trips
  across the whole range — an asymmetric conversion is what makes a Home app
  tile flicker
- tier selection, AID derivation, and the firmware-string guard
- mDNS TXT record parsing
- status code to exception mapping
- interface resolution, against an injected address map rather than the live
  machine. The `networksetup` stanza parser is the one with a real trap: the
  device name arrives on a line *after* the port name identifying it, so a
  naive parse returns whichever device it happened to see first.

The device layer is not worth a mock HTTP server. Verify it against real
hardware with the `dump` CLI instead. The accessory and driver layers likewise
need a real device and a real Home app; a HAP-python test harness would only
be testing HAP-python.