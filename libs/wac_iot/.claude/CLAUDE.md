# wac_iot

A client library for WAC Lighting IoT devices. Speaks the vendor's REST
interface over plain HTTP, discovers devices over mDNS, and reports device
state in device units.

It currently lives as a workspace member of the `wac_homekit` repo, but it is
written to be lifted out as a standalone package backing other consumers — a
Home Assistant integration in particular. Nothing here may assume the bridge.

## Hard rules

- **Never import `pyhap` or anything else HomeKit-related.** Anything that
  couples this library to HomeKit turns the extraction into a rewrite.
- **Async throughout** — `aiohttp`, never `requests`.
- **The public surface is whatever `src/wac_iot/__init__.py` exports.**
  Consumers import from there, not from submodules.
- **No unit conversion.** This library speaks device units exactly as the spec
  defines them, and exports the bounds (`LEVEL_MAX`, `HUE_MAX`,
  `FAN_SPEED_MAX` and friends) so a consumer can convert against them rather
  than hardcoding numbers:

  - `level` (brightness): 0–10000, in 0.01% steps
  - `hue` / `saturation`: 0–10000
  - `mixColorTemp`: degrees Kelvin
  - `fanSpeed`: gears 1–6

  The RGB triple is the exception the spec does not flag: 0–255. See below.

- **Build control state with the typed `Control*` methods on `CFixtures`**, not
  a hand-written dict. They enforce the ranges and the mutually-exclusive
  groupings (stepped white index vs. Kelvin; HSV vs. RGB vs. white point) in
  one place, and refuse before spending a request.

## The vendor spec

The WAC IoT Unified REST Interface PDF is in `private/` at the repo root,
which is gitignored. Read it for protocol details.

Every page is marked CONFIDENTIAL. **Never copy its text into a committed
file** — no pasting tables into docstrings, comments, README, or markdown.
Derive code from it; do not reproduce it.

It is also not fully reliable. **Where the document and hardware disagree, the
hardware wins** — the notes below record measurements, not readings.

### Measured against real hardware

Measured on a ColorScaping controller (`iotmVer 01.04.0149`, `restVer 1.40`)
and two InvisiLED wall stations (`iotmVer 01.00.0014`, `restVer 1.40`). The
document describes protocol 1.91, so expect further drift on newer firmware —
re-run `wac_iot dump` rather than assuming these hold.

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
instead — this library needs outbound UDP 5353 to 224.0.0.251 and outbound
TCP 80 to the transformers. A consumer that also listens (a bridge) needs
inbound TCP on its own port too.
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
- **`level` is writable and exact.** 50% → `level: 5000`, 100% →
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
and `saturation` to match, so a poll reports colour correctly. **A consumer
must therefore read HSV and write RGB**, converting on its own side; this
library does not convert, and `ObjStateRgbw` enforces that the two views are
never sent together. Round trip verified on hardware: Hue 120 → RGB (0,255,0)
→ device `hue 3333` → read back as 120°, exact.

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

The two answers lead to different consumers. If magnitude does drive output,
apparent brightness is a product of `level` and RGB magnitude, reading
`level` alone would report 99.8% on a half-lit fixture, and writing
brightness has two mechanisms that need to be chosen between. If it does
not, `level` alone is the brightness field and the magnitude is cosmetic.
Test in the dark: set `red: 255`, then `red: 64`, with `level` untouched.

**This library reports both axes and converts neither** — the choice of where
brightness comes from is the consumer's, and it is a choice that has to be
made before the dark test lands.

Colour *hue* readings are not in doubt — cyan, red and green were each set
from the app and read back correctly, and a blue-to-white change was
confirmed by eye. Only the brightness axis is unresolved.

#### One request at a time, per device

The firmware answers serially no matter how many requests are in flight, so
overlapping them gains nothing and starts costing failures. Measured on the
ColorScaping transformer, read-only throughout — `/device` with `query` and
`/fixture` action 5, neither of which carries a write. Serialized means one
`CTransport` with its lock; concurrent means one transport per caller, which
reproduces the pre-lock behaviour exactly.

| callers | endpoint | serialized | concurrent |
|---|---|---|---|
| 8  | `/fixture` a5 | 8 ok, 0.62s  | 8 ok, 1.25s |
| 16 | `/fixture` a5 | 16 ok, 1.24s | **2 failed**, 3.09s |
| 24 | `/fixture` a5 | 24 ok, 1.86s | **1 failed**, 4.46s |
| 8  | `/device`     | 8 ok, 2.56s  | 8 ok, 2.46s |
| 16 | `/device`     | 16 ok, 5.14s | **2 failed** |
| 24 | `/device`     | 24 ok, 7.82s | **7 failed** |

- **Serializing never failed** — zero errors in every row, at every load.
- **Unserialized sheds requests from 16 concurrent up**, as `WacTimeoutError`
  on `/device` and `WacTransportError` (connection reset) on `/fixture`.
- **Serializing is also faster** where it matters: 24 `/fixture` reads in
  1.86s against 4.46s. Piling requests on does not merely risk failure, it
  slows the firmware down. `/device` concurrent looks quicker at 24 callers
  only because seven of them gave up.
- **At three or four concurrent requests neither mode fails**, which is all a
  polling consumer generates on its own. The lock matters when other clients
  are also talking — a phone, a home hub, the WAC app.
- **It serializes only *our* traffic.** Other clients are separate TCP peers
  and can still overload the device. This removes our contribution to a
  pile-up; it does not immunise anything.

The lock lives on `CTransport`, which is already one per device, so a
consumer holding several transformers still talks to all of them at once. It
is held across retries and their backoff rather than one attempt: a device
that just timed out is the last thing that should get a second conversation
while the first is still backing off.

This was first seen in the field rather than in a benchmark — a live bridge
polling every 5s, with a phone, a home hub and the WAC app all on the same
transformer, produced `poll failed` and one `control failed`, and that lost
control was a real user toggle that never reached the hardware.

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
  `findme` as evidence that the write failed, and do not build anything that
  reads it back.

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

  **Consumers should build their entities from `mpAddrFixtureKnown`, not
  `mpAddrFixture`.** The type-4 pseudo-fixture is addressable like any other
  but has empty `state`, so it would become an entity that can never report or
  change anything. The known map drops it, and drops any future type this
  library does not model yet. Use the full map only for dumps and diagnostics.
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
- Group address 255 is a built-in "All-Default" group. It holds every *real*
  fixture, but not the type-4 pseudo-fixture above — do not treat its
  membership as equivalent to the action 5 address list.

## Testing

The things worth testing here are the pure functions whose parsing and
arithmetic are easy to get subtly wrong:

- mDNS TXT record parsing
- status code to exception mapping
- the control-state builders, especially the mutually-exclusive groupings
  they are there to refuse
- snapshot identity derivation

**The device layer is not worth a mock HTTP server.** Verify it against real
hardware with the `dump` CLI instead — a mock would only encode what we
already believe, and every entry in the notes above is a case where what we
believed was wrong.
