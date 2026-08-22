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
- `libs/wac_iot/src/wac_iot/` — the device library. Owns everything WAC, and
  carries its own rules in `libs/wac_iot/.claude/rules/`.

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

## The device layer

Everything about the WAC protocol — the confidential vendor spec, the hardware
measurements that contradict it, and `wac_iot`'s own API contract — lives in
**`libs/wac_iot/.claude/rules/wac-iot.md`**. It is a nested rule, so it loads
by itself whenever anything under `libs/wac_iot/` is read; record new hardware
findings *there* rather than here, so they travel with the library when it is
extracted. Nothing in that subtree should need a rule at this level.

The two facts from it most likely to bite on the HomeKit side:

- **Colour is read as HSV and written as RGB.** Writing `hue` / `saturation`
  is either refused outright or, after a `mode` write has been attempted,
  accepted and silently discarded. There is no combination of HSV fields this
  firmware honors, which is the whole reason `TplRgbFromHueSat` exists.
- **`level` is the brightness axis; RGB magnitude barely renders at all.**
  Measured after dark against a side-by-side control. So brightness has one
  field and the triple is chromaticity, which is what the bridge already
  assumed.

## The HomeKit side

Built on HAP-python 5.0. `convert.py` owns the units, `accessory.py` turns one
`CFixture` into one Lightbulb, `driver.py` owns the `Bridge`, the poll loop,
and the discovery watch.
Lights only — `TierTryFromFixturek` returning None is the filter, and adding a
fixture type to `g_mpFixturekTier` is the whole change needed to bridge it.

`findme` in a fixture's control state maps onto HomeKit's Identify
characteristic. It is write-only — it never appears in a read-back `state` —
so nothing may confirm an Identify by reading it back.

Measured end to end, driven from Eve: `findme` flashes the fixture one second
on, one second off, for 30 blinks — about a minute — then stops by itself,
leaving the stored state untouched. **The Home app gives no way to press it**,
though: iOS surfaces Identify during the add-accessory flow and, for
accessories behind a bridge, not afterwards. Deleting and re-pairing to hunt
for the button is not worth it — it would cost every room assignment, name,
scene and automation, and probably would not produce a persistent button
anyway. Use Eve or another third-party client. HAP-python has no Identify
dispatch of its own, so the `configure_char` setter is the only route, and it
runs through the same `client_update_value` path as every other write.

### Verified on hardware

Run against the ColorScaping transformer at protocol 1.40 — three light
fixtures (`hub`, an ELV; `water` and `sky`, both RGBW) plus the type-4
pseudo-fixture. What the live run established:

- Discovery, accessory construction, tier selection and characteristic sets
  are all correct. The ELV got On + Brightness; the RGBW pair got On +
  Brightness + Hue + Saturation, and later ColorTemperature as well (see the
  white-point decision below). The type-4 pseudo-fixture was excluded.
- Writes land exactly. HomeKit Hue 120 → device `hue 3333` → reads back 120°;
  Brightness 50 → `level 5000` → reads back 50. No drift on any axis, so no
  tile flicker.
- **The reconcile-corrects-a-failed-write path is confirmed, not theoretical.**
  The first colour write was refused by the firmware (see the HSV notes in the
  device layer). HomeKit had already optimistically shown the new value; the
  next poll pulled it back to what the device actually holds. That is the
  designed behaviour and it works — but note the corollary: a failed write
  returns success to the controller, and the only correction is the next poll.
- Three polls over ~22s produced **zero** characteristic updates once the
  fixtures were idle, so the "only notify if the value moved" guard holds.
- Clean SIGTERM shutdown, exit 0.

Pairing from a real Home app is done: the bridge appears in Home with its
three accessories, and they persist across a bridge restart — which is the
`NAidFromFixtureId` stability claim above holding in practice, not just in
tests.

ColorTemperature is exercised too, on RGBW rather than on a tunable-white
fixture, which does not exist on this transformer: the Home app's temperature
control drives `mixColorTemp` and produces a white the colour wheel cannot.
Switching back and forth between the colour and temperature tabs behaves.

Identify works from a real controller — Eve, since the Home app offers no
button for a bridged accessory. That covers the last write path: every
characteristic this bridge offers has now been driven from a HomeKit
controller against real hardware.

Still unexercised: any genuinely tunable-white fixture, there being none on
this transformer.

### What the device does that the bridge has to answer for

Measured after the bridge was written, so these are open against the current
code rather than settled by it:

- **Writing brightness or colour turns the fixture on.** A lone `level` or
  RGB write to a fixture at `status: false` comes back `status: true`,
  measured on both RGBW and ELV. *Handled*, and handled by following the
  device rather than fighting it — the control response says `status: true`
  and `_ReconcileControl` folds that straight into the On characteristic.
  The rejected alternative was attaching HomeKit's current On value to every
  write, which would have let a stale belief turn off a light someone had
  just switched on at the wall.
- **An explicit off loses to a colour write in the same request.** `{rgb...,
  status: false}` left the light on. `_ControlAsync` builds exactly that body
  when a batch carries On alongside Hue/Saturation, so turning a light off
  from a scene that also sets its colour does not turn it off. **Still open.**
  The fix is two requests with off last, which costs a second round trip on
  every batch that carries On — worth doing only once the Home app is
  observed sending that combination. The tile is at least honest in the
  meantime, since the echoed state reports the light as on.
- **`mixColorTemp` works on RGBW fixtures and does *not* turn them on.**
  Handled — it is what the RGBW white point is now driven through. The
  firmware treats the two colour axes as mutually exclusive per request and
  flips `mode` to follow whichever was written, so only one may be sent;
  `_OnSetService` keeps the pending set down to one accordingly.

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
  There is nothing to set optimistically. What corrects it is the action 4
  response, which echoes the fixture's whole post-write state — see
  `_ReconcileControl`. Only a write that never reached the device at all is
  left for the poll, along with changes made from a wall station or the WAC
  app.
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

### Running as a service

The bridge is written to be started blind — on a network with nothing on it
yet, by someone who has not read any of this.

- **An empty network is not an error.** Nothing answering the startup browse
  logs the `PrintNoDevices` diagnosis and then serves an empty bridge anyway.
  HomeKit is fine with one: pairing works, and accessories arriving later show
  up. `--require-devices` restores the old exit-1, for a script that wants an
  answer to "is anything there". Note the two failures are told apart —
  nothing answered mDNS at all gets the blocked-process diagnosis, devices
  that answered but carry no light gets a plain warning, because sending
  someone hunting a firewall rule that is not there is worse than saying
  less.
- **`--browse` is a grace window, not a browse.** It is the first N seconds of
  the same `CWatcher` stream that then runs for the life of the process, so a
  device announcing itself right on the boundary cannot fall between two
  browsers. Its only job is to let the ordinary case — devices already
  present — come up populated rather than popping in one at a time after a
  controller has already connected.
- **Runtime additions go through one path, whatever noticed them.** A device
  the watch finds and a fixture the poll finds both end at `_CFaccAdd`, which
  builds accessories for whatever the device has and this bridge does not.
  `driver.config_changed()` rewrites the persist file on every call, so it is
  called once per device that contributed, never once per fixture — and not
  at all until `fServing`, since during the grace window there is nothing
  advertising and nobody paired.
- **A fixture appearing on a running device costs nothing to notice.** The
  poll already read the whole transformer, so `SetNAddrUnbridged` is set
  arithmetic on data in hand. Addresses already declined are remembered in
  `setNAddrSkip`, which is what keeps the "not a light" line to one per
  fixture instead of one every five seconds forever.
- **A device's IP is followed, never rebuilt around.** `mpStrDpoll` is keyed
  by mDNS instance name precisely because that is the part that does not move;
  a lease change re-announces the same name at a new address and
  `CClient.SetHost` re-points the transport under the accessories. The
  accessories themselves are untouched — they are what iOS paired with, and
  their AIDs, names and values all have to survive a move the user never sees.
  Before this, a lease change stranded the poll loop on a dead address until
  the bridge was restarted.
- **mDNS removals are ignored on purpose.** They are advisory (see the device
  layer), and the poll loop already turns an unreachable device into No
  Response on every one of its lights — which is the correct HomeKit
  presentation and is based on a real request rather than on a missing packet.
  A device that returns resumes polling with no ceremony.
- **Nothing is ever removed from the bridge.** A fixture or device that is
  genuinely gone stays on show as No Response until a restart. Removing an
  accessory from a live bridge has pairing-state consequences that deserve
  their own phase; there is a `BB(bruce)` at the natural place.
- **Discovery events are handled one at a time, and that is the whole of the
  race protection.** `WatchAsync` awaits each `OnDevent` to completion, so a
  device that announces itself three times during its own first add finds
  itself already in `mpStrDpoll` by the second event. Nothing needs a lock
  because nothing is concurrent.
- **The persist directory resolves rather than defaulting.** `--persist-dir`
  if given, else `/var/lib/wac-homekit` when it already exists and is writable
  (systemd's `StateDirectory` creates it before the unit runs, which makes its
  presence a reliable signal), else `$XDG_STATE_HOME/wac-homekit` or
  `~/.local/state/wac-homekit`. The old unconditional `/var/lib` default made
  a blind first run die on mkdir. Whichever is chosen is logged at info,
  because pairing state is the one file a user may need to go and find.

### Pairing presentation

- **The setup code is printed on every startup**, not only the first. It is
  stable once generated, and under systemd this is what makes
  `journalctl -u wac-homekit` sufficient to pair with.
- **The QR code is rendered here, not by HAP-python.** `Accessory.xhm_uri()`
  looks like the thing to call and is unusable without the
  `HAP-python[QRCode]` extra: `base36` is imported only under HAP-python's own
  `SUPPORT_QR_CODE` flag, so in a plain install the method raises `NameError`
  from inside itself. Installing that extra to reach it would also pull in
  `pyqrcode`, which then prints a second QR code next to ours. So `StrXhmUri`
  packs the payload itself — the layout is HAP's and has been stable across
  the protocol's life — and `qrcode` renders it. `CBridge.setup_message` is
  overridden to nothing for the same reason: HAP-python's own block advises
  installing an extra for a feature this bridge already has.
- **The digits print before the QR can fail.** The QR is additive; a rendering
  failure logs at debug and falls back to printing the URI as text.
- `invert=True` on `print_ascii`, because the quiet zone has to read as the
  light side — correct on the dark terminal a shell or `journalctl` normally
  is.

### Decisions worth not relitigating

- **AIDs must be stable across restarts** — iOS remembers which accessory in a
  bridge it paired with by AID, and a reshuffle turns every light in the Home
  app into a stranger. `NAidFromFixtureId` is SHA-256 of `StrFixtureId`
  truncated to six bytes and folded above 7 (1 is the bridge itself;
  HAP-python documents 7 as unusable). `CBridge._NAidFree` still checks,
  because the failure mode of a collision is a light that silently never
  appears.
- **The RGB triple carries chromaticity only; `level` carries brightness.**
  `TplRgbFromHueSat` pins value at full, so half-saturated red is
  `(255, 128, 128)`, never a dimmed `(128, 0, 0)`. This was a hedge against
  an unmeasured question; the dark test has since settled it in the choice's
  favour — a 4× cut in RGB magnitude is barely visible and a further 4× is
  invisible, while the same ratio on `level` is obvious. See the device-layer
  notes. Nothing to revisit.
- **An RGBW fixture needs a white point, not just a colour wheel.** The RGB
  triple drives its colour channels and never its white LED — measured, and
  the reason a HomeKit "white" came out visibly blue: the Home app's white
  swatch is Hue 251°, Saturation 5%, which converts faithfully to a slightly
  blue RGB, and three coloured LEDs mixed to white are cool before that tint
  is added. So RGBW carries ColorTemperature as well as Hue/Saturation, and
  the two displace each other rather than racing.
- **Only the colour axis the fixture is rendering gets reported.** HomeKit
  treats ColorTemperature and Hue/Saturation as two views of one state, so
  publishing both at once is a contradiction and the Home app renders the
  blend — a saturated red plus a 5208K white point painted the tile
  flesh-coloured for a plainly red light. `mode` says which axis is live; the
  other keeps whatever it last held, which is where the user would resume on
  that tab. Do not "fix" a stale-looking ColorTemperature by reporting it
  unconditionally.
- **A colour the fixture got from us is not re-derived from it.** RGB is
  8 bits per channel and hue is recomputed from it, so a round trip loses
  several degrees at low saturation — HomeKit asked for 251°, the fixture
  answered 253.8, and the swatch moved under the user a second later. While
  the fixture holds exactly the triple we sent, what the user picked is the
  better record; a colour set at the wall or in the WAC app fails that test
  and reconciles normally. `_FIsRgbOurs` is the whole of it.
- **Colour temperature endpoints snap rather than convert.** The reciprocal of
  370 mireds is 2703K — three Kelvin inside a 2700K fixture's limit, and a
  value that does not survive the round trip. `CColorTempRange` returns the
  fixture's own bound at each end. When a fixture reports no span,
  2700–6500K is the documented fallback; widen it only by reading a real
  fixture's `detail`.
- **Poll interval defaults to 5s**, the responsive end of the range these
  transformers tolerate. It is *not* what decides how quickly a change made
  through a group — a wall-station scene, or any scene at all — reaches
  HomeKit: the transformer reports group writes into its per-fixture state
  tens of seconds late, and polling faster does nothing about it. See the
  device-layer notes. Choose this interval for how hard it leans on the
  hardware, not for a responsiveness it cannot buy.
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
  `AccessoryDriver(address=)` and `LDiscoBrowse`.
- **`--interface` pins three things, and there is a fourth it does not.**
  Pinned: the address HAP-python binds and listens on, the address it puts in
  the advertised A record, and the interface `wac_iot` browses for devices on.
  *Not* pinned: the set of interfaces HAP-python multicasts its own mDNS
  announcement over. `AccessoryDriver` builds its own Zeroconf and we never
  hand it one, so the announcement goes out everywhere — visible as a
  `Host is down` (`EHOSTDOWN`) traceback from a socket bound to `0.0.0.0`
  whenever a `utun` from a VPN is up, since those interfaces do not carry
  multicast. Harmless, because the record *content* is still the pinned
  address, so a controller that hears the announcement on any link connects
  to the right one. Fixable by passing `zeroconf_instance=` — until then, do
  not read the earlier phrasing "advertising and discovery cannot drift
  apart" as covering the interface set. It covers the address only.
- **`--interface` keeps its generic name on purpose.** It takes an interface
  name, an address, `wifi`, or `auto`, and every explicit value is a hard
  requirement — `CIfaceError` rather than a silent fallback, because a bridge
  on the wrong interface looks like one that works right up until the machine
  moves. Only `auto`, the default, is a preference: wifi if the machine has a
  radio, else the default route with a warning, so a headless box with no
  radio still runs. A `--prefer-*` name would advertise a fallback that only
  `auto` has.

## Testing

`just test` runs both packages' suites; `wac_iot` keeps its own testing notes
alongside its own rules. On the bridge side the things worth testing are the
pure functions whose arithmetic is easy to get subtly wrong:

- unit conversion between device and HomeKit ranges, including round trips
  across the whole range — an asymmetric conversion is what makes a Home app
  tile flicker
- tier selection, AID derivation, and the firmware-string guard
- interface resolution, against an injected address map rather than the live
  machine. The `networksetup` stanza parser is the one with a real trap: the
  device name arrives on a line *after* the port name identifying it, so a
  naive parse returns whichever device it happened to see first.
- persist-directory resolution, with the service directory injected so the
  suite never depends on whether this particular machine happens to have
  `/var/lib/wac-homekit` — which is exactly the ambiguity the resolution
  exists to remove.
- the X-HM setup payload, unpacked field by field rather than compared against
  a fixed string, which would pass just as happily with two fields transposed.

The accessory and driver layers need a real device and a real Home app; a
HAP-python test harness would only be testing HAP-python.
