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
  outright on every device tested. There is no TLS and no certificate to deal
  with. `wac_iot probe` re-checks this.

  **The advertised port is not a statement about any of that.** The wall
  stations advertise 443 and then refuse it; the ColorScaping transformer on
  `iotmVer 01.04.0149` advertises 80. So mDNS disagrees with itself across the
  fleet, and what it says tracks firmware generation rather than what the
  device serves. Ignore `nPort` and use 80 — and never make the port a
  discriminator between device kinds.

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
- **Fixture type 4 exists**, despite the document skipping it, and it is not a
  fixture. Named `FIXTUREK.Pseudo` — for the role the evidence carries rather
  than for the inference below. Observed on a ColorScaping transformer at
  firmware 01.04.0149: the untouched default name `New Fixture 17044171` and
  absent from the WAC app; `state` and `tune` both `{}`, so it can neither
  report nor be controlled; `detail.model` and `detail.ledDriver` both
  `gnipacsroloC`, i.e. "ColorScaping" reversed; `detail.fwVer` of `07.68`,
  exactly the device's own `scmVer`; `detail.factory` of 41, outside the
  documented 1–6; `detail.pcbVer` of `"\u0001.\u0001"`, raw bytes where a
  version string belongs. The All-Default group (address 255) omits it, and so
  does the documented bulk read — only `LFixtureReadAll`'s explicit address
  array surfaces it at all.

  **The reading, and it is a reading:** the transformer's own SCM appearing in
  the fixture table as an artifact rather than anything on the track. One unit
  on one firmware version; the reversed strings and the `scmVer` match are
  inference, not report. It carries the raw passthrough shapes for that reason
  — a real model would claim a confidence nothing here has earned — and
  `wac_iot dump` marks it as a pseudo-fixture and prints its raw structures,
  which is the recourse if the reading ever turns out wrong.

  Genuinely unknown types (anything still unnamed) must log and resolve, never
  raise.
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

- **Control works and partial writes are mostly partial.** `status` alone,
  then `red`/`green`/`blue` alone, each accepted with `result "0"`, and
  `level` held at 9981 across both. Send only what is changing — but see the
  two exceptions below: `status` and `mode` both move on their own.
- **Writing brightness or colour turns the fixture on.** Measured on a fixture
  sitting at `status: false`: a lone `level` write and a lone RGB write each
  came back with `status: true`, which neither request mentioned. Not an RGBW
  quirk — a `level` write to the ELV zone does it too.
  Colour *temperature* does not do this — `mixColorTemp` writes left an off
  fixture off. So a consumer that dims or recolours a light it believes to be
  off has just switched it on, and its own idea of on/off is now wrong until
  the next poll.
- **An explicit `status: false` loses to a colour write in the same request.**
  `{red, green, blue, status: false}` sent to a fixture that was on left it
  on — the colour write's implicit turn-on wins regardless of ordering in the
  body. Turning a light off while also setting its colour takes two requests,
  off last.

  **This library absorbs that**, the way `LFixtureReadAll` absorbs the broken
  bulk read: every typed `Control*` method routes through
  `_ObjControlOffLast`, which sends the state without `status`, then
  `{"status": false}` on its own, and returns the *second* response — the one
  describing where the fixture actually ended up. It is a firmware rule, not a
  HomeKit one, so it belongs here rather than in any one consumer; a Home
  Assistant integration hitting the same endpoint would otherwise hit the same
  bug.

  The extra round trip is spent only on a batch that carries an explicit off
  with something else. A lone `status: false` is one request, and so is an
  explicit `status: true` alongside anything — the device turns the fixture on
  for those writes regardless, so there is no ordering to enforce.

  **If the first request raises, the off is never sent and the error
  propagates unchanged.** Deliberate, and worth not re-deriving: forcing the
  off through anyway would be this library inventing an error policy for its
  consumers. A light that stays on for one poll interval and then reports
  itself honestly as on is the better failure.

  `ObjControl` is untouched and stays raw — one wire action, one response.
- **The action 4 response carries the fixture's full new `state`.** Undocumented,
  seen on both RGBW and ELV, and it agrees exactly with an immediate read —
  including across a ramped turn-on on a fixture with `onRate: 200`, so it is
  the settled target rather than an intermediate. Cheap to use as the
  confirmation of a write instead of a second request, as long as it is read
  as what the firmware *accepted*: an out-of-range value comes back clamped.
- **RGB components are 0–255**, not the 0–10000 everything else uses. 255
  was accepted and stored verbatim, and the fixture's own full-blue state
  reports `blue: 255`.
- **RGB and HSV are two views of one colour state, not independent
  fields.** Writing `red`/`green`/`blue` = 255 also moved `hue` 6666 → 0 and
  `saturation` 10000 → 0, which nothing in the request mentioned. So the
  firmware derives one from the other, and sending both in a single request
  really would be two conflicting writes. `ObjStateRgbw` refuses that.
- **The RGB triple does not reach the white LED.** RGB (255,255,255) is
  accepted, and the firmware agrees it is neutral — `saturation: 0`,
  `hue: 0` — but by eye the fixture is visibly blue-tinted, the way three
  coloured LEDs mixed to "white" always are. So the triple drives the colour
  channels only. **The fixture's real white is `mixColorTemp`**, and a
  consumer offering only an RGB colour wheel cannot produce a white on this
  hardware at all. Earlier notes called this white "confirmed by eye"; that
  was daylight and a comparison against nothing.
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

`mode` cannot be written directly — but it is not read-only. It moves as a
*side effect* of which colour axis a request writes: an RGB write takes it to
2 (Rgb), a `mixColorTemp` write takes it to 1 (TunableWhite). So the way to
put a fixture into a colour mode is to write that mode's fields and let the
firmware follow.

Alongside it is an undocumented string field, **`colormode`**, with `"RGB"`
and `"CCT"` observed. It is *not* a rendering of `mode`, though it looks like
one until a third value turns up: `colormode` names the colour family, while
`mode` distinguishes representations within it.

| colormode | mode | meaning |
|---|---|---|
| `CCT` | 1 (TunableWhite) | the white point drives output |
| `RGB` | 2 (Rgb) | the triple carries the colour |
| `RGB` | 3 (Hsv) | hue/saturation carry it, `level` separate |

A fixture in mode 2 moves to mode 3 when `level` is written — measured, and
consistent with what the brightness section below concludes: mode 3 is the
representation where brightness is separable from chroma, so asking for a
level pushes the firmware into it. Neither field is writable.

**Test for CCT, not for RGB.** The chromatic family has at least two `mode`
values and may gain more, so a consumer wanting "is this fixture showing
white" should ask `mode == 1` and treat everything else as colour.

##### Colour temperature works, and RGBW fixtures honor it

Measured on an RGBW fixture (`915CS-CTR-WT`) that the WAC app had left in
`colormode: "CCT"`. `mixColorTemp` had never been written to any fixture
before this; it behaves far better than the HSV fields do.

- **`mixColorTemp` is writable and exact.** 2700, 6500 and 4000 each accepted
  with `result "0"` and stored verbatim.
- **It moves nothing else** — not `level`, not the RGB triple, not `hue` or
  `saturation`, and not `status`. The only accompanying change is `mode` /
  `colormode` going to CCT when the fixture was in RGB.
- **Out-of-range Kelvin is clamped, not refused.** 7000 on a 2700–6500
  fixture came back `6500`, and 2000 came back `2700`, both with `result "0"`.
  A consumer must therefore not read a zero result as "the value you sent is
  the value it holds"; clamp against the fixture's own `minColorTemp` /
  `maxColorTemp` and expect the firmware to clamp again anyway.
- So an RGBW fixture genuinely has **two** colour axes it will honor — RGB and
  Kelvin — mutually exclusive per request, which is what `ObjStateRgbw`
  already enforces. Switching between them is just writing the other one.

Still unmeasured: `colorTempLevel`, the stepped index. No fixture has been
asked for it.

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

**Settled: `level` is the brightness axis, and RGB magnitude very nearly is
not.** Measured after dark, two RGBW fixtures side by side at identical
`level`, one held at `red: 255` as the control:

| change | ratio | seen |
|---|---|---|
| `red` 255 → 64 | 4× | slightly dimmer |
| `red` 64 → 16 | 4× | no observable change |
| `level` 9981 → 2500 | 4× | obviously dimmer |

The last row is the control that makes the other two mean something: the same
4× ratio on `level` is unmistakable, so the null result on magnitude is the
hardware, not the observer. Magnitude has a small effect near the top of the
range and none below it — nothing like proportional.

So apparent brightness is `level`, and a consumer should carry chromaticity
in the triple and brightness in `level` rather than trying to split
brightness across both. Note this is a statement about *rendering*: the
firmware still stores the magnitude faithfully and reports it back, so a
consumer reading `red: 64` must not infer a quarter-lit fixture.

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

- `colorTempLevel` (steps 1–7) has never been written. `mixColorTemp` now
  has, on RGBW — see above — but no tunable white *fixture* has been seen at
  all, so the document's rule that the two are mutually exclusive is still
  document only.
- No tunable white, fan, motorized trackhead, or wall-station *fixture* (type
  11) has been seen on real hardware yet. Those models are written from the
  document alone. Single color (0), RGBW (2), and ELV (6) have been seen.
- Configure (action 6) is still unexercised. Of the action 4 fields,
  `status`, `findme`, the RGB triple, `level` and `mixColorTemp` are now
  measured working; `hue`, `saturation` and `mode` are measured *not*
  writable (see above), though `mode` does move on its own.
- **`findme` works, and never appears in a fixture's read-back `state`.**
  Confirmed by eye on an RGBW fixture that was on and being watched: the
  write is accepted with `result "0"`, and the fixture flashes one second on,
  one second off, for **30 blinks — about a minute — then stops by itself**.
  It leaves the stored state exactly as it was: the fixture returns to the
  colour and level it held, and no field moves at any point.

  The field itself stayed absent before, during and after, in both the
  echoed state and a read-back, so it is genuinely write-only. Do not treat
  a missing `findme` as evidence that the write failed, do not build
  anything that reads it back, and do not expect to observe the flashing
  through this interface — only a person in the room can confirm it.

  Whether `findme: false` cancels an in-progress flash is untested.

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

  **Two predicates, deliberately.** `CFixture.FIsKnown()` is the shape
  question — is there a model for this type. `FIsUsable()` is the surfacing
  question, and is false for both an unmodeled type and `FIXTUREK.Pseudo`,
  which is known precisely well enough to say it should not become an entity.
  `mpAddrFixtureKnown` filters on the second. A diagnostic wants the first,
  because a dump of the pseudo-fixture is the only way to find out the reading
  above was wrong.
- Not every device implements every endpoint. The wall stations answer only
  `/device`, `/network`, `/ota`, and `/fs`, and return HTTP 404 with a plain
  text body for `/fixture`, `/group`, and `/automation`. Tools must degrade
  per-endpoint instead of aborting the run.

  **`/device` says which kind it is, and that is the only thing that does.** A
  wall station advertises `_easylink._tcp` with `Protocol:
  com.waclighting.strut` and `Protocol Ver: 1.40` — byte for byte what a
  ColorScaping transformer advertises — so discovery cannot tell them apart
  and a consumer that assumes otherwise gets a 404 on every announcement,
  forever. Its `/device` body carries `"systemType": "invisiLED_Wall"` and
  `"deviceName": "wallstation"`. `SDeviceInfo.FIsFixtureHost` reads the first
  of those and `SnapPoll` raises `WacNoFixturesError` rather than making a
  request that was always going to 404.

  That predicate is a **denylist**, deliberately. The document enumerates
  `systemType` as strut / colorscaping / gen3fan, and this device reports none
  of the three — so the documented enumeration is already incomplete, and an
  allowlist would silently drop the next product WAC ships. Unknown, absent,
  or not even a string all answer "yes, it hosts fixtures".
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

  That multicast hop is wall-station *to transformer*, and no further: a
  button runs an automation stored on the transformer, which writes a group.
  Fixtures have no address of their own on the network and are never reached
  directly. Confirmed by the owner of this installation, and consistent with
  the six group-writing automations the transformer lists. A press is
  therefore a group write by another name — see the lag it comes with,
  above.
- The transformer is the only device worth polling for light state. Address it
  directly; do not try to reach its fixtures through a wall station.
- There is no push channel. Polling is the only option; 5–10 seconds is the
  starting range for these ESP32-class devices.
- **Discovery has to keep running, and its removals are advisory.** A device
  is on DHCP, so its address moves without anything else about it changing;
  `CWatcher` reports that as an `Updated` event and `CClient.SetHost` follows
  it without disturbing anything a consumer built on that client. Removals
  are the weak half: a device that loses power sends no mDNS goodbye and its
  record just expires, while one that reboots can go and come back inside a
  second. So the library reports what mDNS said and refuses to debounce it —
  the only honest liveness test is whether the device answers a request, and
  a polling consumer already has one. `DiscokTryFromDisco` is the pure diff
  behind the add/update decision, and it stays silent when a re-announcement
  carries nothing new.
- **A group write reaches the fixtures promptly, but `/fixture` reports it
  tens of seconds late, per fixture.** Measured at 1 Hz against group 255:
  the write returns `result "0"` at once and the lights change within about a
  second, while the per-fixture `status` read back through action 3 moved
  after ~8s for one fixture and ~56s for the other. Two fixtures switched by
  one request, their reported states nearly a minute apart.

  **This is about group writes, not about who made them.** It was first seen
  after a wall-station press and looked like a wall-station problem; issuing
  the identical group write over REST reproduces it exactly. Per-fixture
  writes have no such lag — action 4 on one fixture reads back immediately
  and exactly, which is what makes the contrast meaningful.

  Wall stations matter only because every scene on this transformer writes
  group 255, so a button press is a group write by another name.

  Consequences for a consumer: **polling faster buys nothing** for a change
  made through a group, so do not present a poll interval as the latency a
  user will see, and do not shorten it hoping to improve that.

  **Reading the group instead does not help**, which was the obvious idea and
  is worth not re-deriving: `/group` action 3 returns membership only — the
  fixture addresses, the name, the address — and carries no `state` at all. A
  group is a target for writes, not a holder of state. There is nothing
  fresher to read, so the per-fixture lag is simply the latency this
  interface offers for group-originated change.
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
