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

Pairing removal and recovery are now verified against a real Home app too:
an orphaned bridge recovered with `--unpair` and re-added with all three
lights, and a deletion made while the bridge was running handled
automatically — see the pairing section for what each measurement settled.

Still unexercised: any genuinely tunable-white fixture, there being none on
this transformer; and `_CloseOther`, since iOS had already closed its other
connections before the removal arrived.

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
  status: false}` left the light on, and `_ControlAsync` builds exactly that
  body when a batch carries On alongside Hue/Saturation — so a scene that set
  a colour and turned a light off did not turn it off. *Handled*, and handled
  **in `wac_iot`, not here**: it is firmware behaviour rather than a HomeKit
  quirk, and the library already has precedent for spending an extra request
  to absorb one. The typed `Control*` methods route through
  `_ObjControlOffLast`, which sends the colour, then the off alone, and
  returns the second response. Nothing on this side changed: `_ControlAsync`
  still awaits one call per tier and still reconciles from what comes back,
  which is now the off's echoed state.

  The second round trip is spent only on a batch that carries an explicit off
  *with* something else — which, on the evidence, is only ever a scene. An
  ordinary tap on the tile sends On by itself and costs exactly what it did.
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
- **Added and Updated are the same event to this bridge.** Which one mDNS
  calls an announcement depends only on whether the watcher still held a
  cached record, and a power cycle destroys that cache — the device goes away,
  its record is dropped, and it returns as an *Added* while the bridge has
  been holding a client for it the whole time. An earlier cut trusted that
  distinction and routed Added straight to `FTryAddDevice`, whose
  already-bridged guard returned early: a transformer rebooting onto a new
  lease would have stayed stranded on its old address forever, which is the
  exact failure the watch exists to prevent. `OnDeviceSeen` decides
  add-versus-follow from the bridge's own state instead, which knows what it
  is holding.
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
- **Removal is off by default, and what it costs is why.** Taking an
  accessory off a live bridge is not undone by putting the fixture back: iOS
  drops that accessory's room, its name, and its membership in every scene
  and automation, and the fixture that returns comes back as a stranger to be
  set up again — even though `NAidFromFixtureId` hands it the identical AID.
  Against that, never removing costs a light stuck on No Response until the
  bridge is restarted. One of those is an evening's work and the other is an
  annoyance, so the default errs towards the annoyance: `--forget-missing`
  takes seconds and defaults to 0, never. Its help text says outright what a
  non-zero value loses.

  **One signal earns a removal, and it is positive evidence.** A device
  answered a poll, successfully, right now, and its fixture list no longer
  carries an address this bridge holds an accessory for — the fixture was
  pulled from the track or deleted in the WAC app. Nothing else counts. An
  unreachable device is a power cut, a reboot, or a lease change caught
  mid-flight, and a failed poll never reaches the decision at all, so a
  transformer unplugged for an afternoon comes back to exactly the miss
  counts it left with. An mDNS `Removed` is advisory and still does nothing.
  And **devices themselves are never removed**, at any threshold: a whole
  transformer going quiet is indistinguishable from someone unplugging it.

  On top of that, hysteresis — the address has to be missing from N
  *consecutive successful* polls. Not because the evidence is weak but
  because a bulk fixture read has been seen to omit fixtures it should have
  listed (see the device layer); `SnapPoll` works around that already, and
  this is the belt to its braces.

  N is the threshold divided by the poll interval and **rounded up**, because
  a threshold is a minimum wait and not a target: `--forget-missing 59` at a
  5s interval waits twelve polls, not eleven. Truncating would have fired at
  55 seconds — before the user asked, on the one action here that cannot be
  undone. Ceiling makes the floor of 1 redundant for any positive threshold;
  it stays for the case it was written for, which is a threshold shorter than
  one interval collapsing into 0, the value that means never.

  The shape mirrors the addition path it sits next to. `SetNAddrForget` is
  the decision and removes nothing — pure enough to test against a stub
  snapshot, which is where every case above is pinned down. `_CFaccForget` is
  the mechanics, batched one `config_changed()` per device that lost
  something rather than one per fixture, logged at **warning** because
  someone reading the journal to find out where a light went should not need
  debug to find it. A removed address is *not* added to `setNAddrSkip`, so a
  fixture that comes back is rebuilt by the ordinary unbridged path.

  **`Bridge.accessories` is not quite the whole of it.** It is what
  HAP-python serves from — `to_HAP`, `get_accessories` and
  `get_characteristic` read it and nothing holds a second list — but
  `driver.topics`, the per-`aid.iid` event subscription registry, is
  untouched by any of that, so `_TopicsForget` clears it. Left behind, a
  reconcile still in flight would push an event for an accessory the
  controller has just been told does not exist; cleared, that reconcile runs
  to completion and `driver.publish` returns before it reaches a socket,
  which is exactly what should happen to a write the user asked for against a
  fixture that has since gone.

  **The one rough edge is HAP-python's.**
  `AccessoryDriver.get_characteristics` checks for a missing accessory and
  skips it; `set_characteristics` does not, and raises `AttributeError` on
  `None`. A controller writing to the removed AID between `config_changed`
  and its refetch of `/accessories` therefore gets its HAP connection dropped
  with a traceback in the log, then reconnects and carries on. Narrow, not
  worth a second monkeypatch of HAP-python, and recorded here so the
  traceback reads as known rather than as the removal having gone wrong.
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

  **Changing this directory is indistinguishable from replacing the bridge.**
  The HAP MAC lives in that file, and iOS knows the bridge by it — point the
  process at a different directory and the Home app sees a stranger while
  every paired accessory sits at No Response waiting for a bridge that never
  comes back. It bit this repo's own bridge the first time the default moved:
  paired against `private/hap`, restarted on the new default, two paired
  clients stranded. Nothing was lost — the old file was untouched and naming
  it again restored everything — but the symptom looks exactly like a broken
  bridge, so check the `pairing state in ...` line before believing anything
  else.

- **`systemctl status` answers "is it paired, what is it serving, is anything
  unreachable" without the journal.** `notify.py` sends `sd_notify`'s
  `STATUS=` — one line, three states: `unpaired — setup code 123-45-678;
  1 device, 3 lights`, `paired with 2 controllers; 1 device, 3 lights,
  2 unreachable`, and `; no devices` in place of the counts for a bridge that
  has found nothing yet. The unreachable clause is present only when it is
  non-zero, and the counts are singular and plural correctly, because "1
  lights" in that line reads as a bug in the bridge rather than in the line.

  **The setup code appears in it while unpaired, and this is the one place
  that is world-readable.** Any local unprivileged user can read a unit's
  status over D-Bus, unlike the journal. Judged acceptable and recorded here
  so it is a decision rather than an oversight: after pairing there is no code
  in it at all — the same `cClientPaired` guard `PrintSetupCode` uses, for the
  same reason — and a HAP setup code buys nothing to something not already on
  the LAN.

  **Cross-repo: the unit must set `NotifyAccess=main` or every datagram is
  dropped.** The unit lives in a separate NixOS repo. There is no error, no
  log line and nothing in `systemctl status` but a missing `Status:` field,
  which looks exactly like the bridge not running this code — so a status
  line that never appears is that setting before it is anything else.

  Duplicate sends are suppressed in `CNotifier`, not worked out at the call
  sites: the poll loop calls `NotifyStatus` every five seconds forever and the
  common case is that nothing moved, so the comparison is against the last
  formatted string and the call sites stay unconditional. Two answers to "did
  anything change" is how the two drift apart.

  **`READY=1` and the watchdog were left out deliberately.** The unit is
  `Type=simple`; readiness sent to a unit that did not ask for it is ignored
  at best, and at worst it invites a later `Type=notify` that nothing here is
  written for. Nothing is sent on shutdown either — the process is going away
  and systemd already shows the exit status.

### Pairing presentation

- **The setup code is printed on every startup**, not only the first — which
  is what makes `journalctl -u wac-homekit` sufficient to pair with under
  systemd, without a file to go and read.

  It is *not* stable across restarts, and nothing here should imply it is.
  Measured on a real state file: neither `pincode` nor `setup_id` is among
  the keys the encoder writes, so an unpaired restart without `--pincode`
  generates a fresh code — and a fresh X-HM URI and QR code with it. This is
  the same fact recorded under "What HAP-python actually requires"; it is
  repeated here because the QR makes it look like a stable artifact and it is
  not. After pairing it stops mattering, since pairing is keyed on the
  persisted keypair rather than on the code.
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
- **A paired bridge prints no setup code at all**, and says how many
  controllers it is paired with instead. A paired accessory advertises
  `sf=0`, refuses `/pair-setup`, and accepts further controllers only through
  one already paired — so a code printed then is a trap rather than a
  redundancy.

  The case that makes this worth code rather than a comment: **removing a
  bridge from the Home app while the bridge is not running leaves it paired
  forever.** iOS unpairs by sending `RemovePairing` over a HAP connection, so
  with nothing listening there is nowhere to deliver it — it drops the bridge
  from its own database and moves on. Measured here: both paired clients
  still in the persist file afterwards, the bridge still advertising `sf=0`,
  and the phone convinced it was gone and ready to re-pair.

  So **delete from the Home app with the bridge running.** That is still the
  cheapest prevention — it makes the whole thing automatic — but it is no
  longer the only way out; `--unpair` is below.

  The symptom names the wrong problem, and the *refusal is the controller's,
  not the bridge's*. Entering the setup code fails silently — straight back to
  the "Select an Accessory" screen, no error — because iOS filters on `sf` and
  never opens a socket; an earlier note here read the failure as the bridge
  refusing a pairing it had in fact never been asked for. (Scanning the QR
  reportedly gives *"Accessory Not Found"*, which is the same client-side
  refusal wearing a message.) `dns-sd -L "WAC Lighting <mac-tail>" _hap._tcp
  local` shows `sf` directly and settles it in one command.
- **iOS does not contact a bridge that says it is paired — measured, twice.**
  This is the fact the whole recovery story turns on, so it is recorded before
  the design it dictates. With a real orphaned bridge on the LAN (real state
  file, both phantom controllers, `sf=0`), typing its setup code into the Home
  app produced **no TCP connection at all** — not a refused pairing, not an
  error, just a silent return to the "Select an Accessory" screen. The add
  flow filters on `sf` client-side and never opens a socket. Nothing
  server-side can change that.

  So **a bridge that was not running when it was deleted cannot detect it.**
  There is no request to notice, no failed handshake to count, no in-band
  evidence of any kind. An earlier cut of this took a `/pair-setup` M1 at a
  paired bridge as proof the pairing was stale, cleared it, and let the same
  request carry on into a pairing that could then succeed. It works — against
  a HAP client that will talk to an `sf=0` accessory. iOS will not, which
  makes it dead code carrying a live risk (any process on the LAN could drop
  the bridge's pairings by posting one packet), so it was removed. Do not
  rebuild it without first re-measuring the silence above.
- **`--unpair` is the recovery, and it is a person deciding.** It forgets
  every paired controller at startup, before the driver starts, so the first
  advertisement goes out as `sf=1` — announcing paired and then correcting it
  would leave a controller that heard only the first announcement ignoring a
  bridge that is waiting for it. It keeps the MAC and the keypair, which is
  the whole reason it exists rather than `rm`: the bridge loses only the
  controllers that no longer exist. Verified on the real bridge: two phantom
  clients forgotten, `sf=1`, code and QR printed, and the Home app added all
  three lights on the first try.

  The paired-bridge startup message names that flag now, and says outright
  that nothing here can notice the deletion — because the previous wording
  sent someone hunting for a state file, and the one before that implied the
  bridge would work it out by itself.
- **Deleted while the bridge is *running* is fully automatic**, and that is
  the case a bridge running as a service actually hits. iOS sends
  `RemovePairing`, HAP-python clears the client and re-advertises, and
  `CPairingWatch` supplies what is missing: the setup code, which was withheld
  at startup because the bridge was paired then, so without it the bridge sits
  there pairable and mute. Verified end to end — deletion from a real Home
  app, one `Unpairing` line, the code and QR printed unprompted, `sf=1`, and
  `paired_clients {}` in the persist file.

  Note **one removal empties a bridge with several controllers**: iOS pairs
  the phone as admin and adds a home hub as a non-admin, and HAP-python's
  `remove_paired_client` clears everyone once the last admin goes. So the
  watch fires on the transition to zero, not per client — measured with an
  iPhone plus a hub pairing.
- **Sessions outlive the pairings they were made under.** The keys belong to
  the session, not to the pairing, so a controller that has just been unpaired
  could go on reading and writing until something drops the socket — the "it
  still thinks it has a connection" half of the complaint. `_CloseOther` drops
  every HAP connection but the one being answered, which is spared because it
  still has a response to send. Note this stayed *unexercised* in the live
  test: iOS had already closed its other connections by the time the removal
  arrived, so the loop found nothing to drop. Insurance, not something
  observed working.
- **The seam is one wrapped `HAPServerHandler` method**, and there is no
  other. The removal is handled inside the handler, and the driver is told
  nothing that distinguishes it from any other unpair. Wrapping
  `handle_pairings` rather than subclassing the class and rebinding it in
  `hap_protocol` keeps the patch to the one entry point that can leave the
  bridge unpaired, and survives HAP-python constructing handlers wherever it
  likes. It runs on the event loop, so the persist is safe to reach from
  there; the advertisement is HAP-python's own to update, which it does after
  the response has gone out.
- **stdout is line-buffered at startup, and that is load-bearing.** The setup
  code is printed, not logged, and print goes to a pipe under systemd or any
  redirect — where Python block-buffers it. Measured: the whole pairing block,
  digits and QR, sat in the buffer for the life of the process while logging
  (stderr) flowed normally, so the failure looks exactly like the code was
  never printed. That defeats the "`journalctl` is enough to pair with" claim
  above, and it would have swallowed the code `CPairingWatch` prints mid-run.
  Line buffering rather than a flush per call site, so nothing added later has
  to remember.
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
- **`--interface` pins four things, and the fourth took a second parameter.**
  Pinned: the address HAP-python binds and listens on, the address it puts in
  the advertised A record, the interface `wac_iot` browses for devices on, and
  the set of interfaces HAP-python multicasts that record over. The last one
  used to leak: `AccessoryDriver` builds its own Zeroconf, and unhandled it
  gets Zeroconf's default of *every* interface, so the announcement went out
  from a socket bound to `0.0.0.0` and produced a `Host is down`
  (`EHOSTDOWN`) traceback whenever a VPN `utun` was up, since those links
  carry no multicast. Never wrong, only noisy — the record's content was the
  pinned address all along — but it was the last place the choice leaked.

  `interface_choice=[strAddr]` closes it. The name suggests an
  `InterfaceChoice` enum and the docstring says so too, but the code passes
  the value straight to `AsyncZeroconf(interfaces=)`, which documents a list
  of addresses as a first-class option — the same shape `CWatcher` is handed
  for the browse side, so one resolved address pins all four.

  **`async_zeroconf_instance=` is the fix that looks tidier and is not.**
  Sharing the watcher's instance would mean one Zeroconf instead of two, but
  `AccessoryDriver.async_stop` calls `advertiser.async_close()`
  unconditionally — it does not track whether it built the thing — and that
  close removes every service listener on the instance. HAP-python would be
  tearing down the watcher's Zeroconf from inside its own shutdown. Two
  instances pinned to one interface is the cheaper answer, and it keeps the
  bridge clear of `zeroconf` entirely: a list of address strings is not an
  import.
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
- the unpair path: that `CUnpairAll` empties the bridge through the driver
  rather than editing state underneath it, that a partial removal is not
  mistaken for a bridge coming free, and that the connection being answered is
  not among the ones dropped. The driver is stubbed — the decision is ours,
  the mechanics are HAP-python's.
- the removal path, which is the one where a wrong answer costs a user their
  Home app configuration: `SetNAddrForget` against stub snapshots, for what
  earns a removal and — far more of the cases — what does not; `CMissForget`
  for the seconds-to-polls arithmetic, including the floor of 1 that keeps a
  threshold shorter than one interval from collapsing into "never"; and
  `_CFaccForget` on a real `CBridge` over a stub driver, for the accessory
  leaving both maps, its event subscriptions going with it, and one config
  change per device rather than one per fixture.

The accessory and driver layers need a real device and a real Home app; a
HAP-python test harness would only be testing HAP-python.
