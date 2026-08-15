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
- **Whether RGB magnitude drives light output is still unmeasured.** So the
  brightness axis has one settled field (`level`) and one open question. See
  the decision below.

## The HomeKit side

Built on HAP-python 5.0. `convert.py` owns the units, `accessory.py` turns one
`CFixture` into one Lightbulb, `driver.py` owns the `Bridge` and the poll loop.
Lights only — `TierTryFromFixturek` returning None is the filter, and adding a
fixture type to `g_mpFixturekTier` is the whole change needed to bridge it.

`findme` in a fixture's control state maps onto HomeKit's Identify
characteristic. It is write-only — it never appears in a read-back `state` —
so nothing may confirm an Identify by reading it back.

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
  The first colour write was refused by the firmware (see the HSV notes in the
  device layer). HomeKit had already optimistically shown the new value; the
  next poll pulled it back to what the device actually holds. That is the
  designed behaviour and it works — but note the corollary: a failed write
  returns success to the controller, and the only correction is the next poll.
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
- **The RGB triple carries chromaticity only; `level` carries brightness.**
  `TplRgbFromHueSat` pins value at full, so half-saturated red is
  `(255, 128, 128)`, never a dimmed `(128, 0, 0)`. Whether RGB magnitude
  actually drives light output is still unmeasured — see the device-layer
  notes — and this is the choice that stays correct under either answer: if
  magnitude is cosmetic it is obviously right, and if magnitude does drive
  output it still keeps the two axes from fighting. Revisit it only once the
  dark test lands.
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

The accessory and driver layers need a real device and a real Home app; a
HAP-python test harness would only be testing HAP-python.
