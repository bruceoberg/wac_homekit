# wac_homekit

An Apple HomeKit bridge to WAC lighting devices.

Several options exist for bringing 3rd party devices into the Apple HomeKit ecosystem.

[Homebridge](https://github.com/homebridge/homebridge) is a minimal driver for this purpose, but it is written in JavaScript and TypeScript, of which I am unfamiliar.
[Home Assistant](https://github.com/home-assistant) (aka HA) is a very mature project, written in python, of which I am very familiar. However, HA is too big of a system, effectively encompassing everything that HomeKit provides. It can be used as a bridge for 3rd party devices, but there's a lot of overhead just getting everything set up. Publishing integrations is also quite onerous (as it should be for a consumer facing project).
[HAP-python](https://github.com/ikalchev/HAP-python) is a small library that provides direct connection to HomeKit, and it is written in python.

This project uses HAP-python to connect WAC lighting devices to a HomeKit installation. It needs to run as an always on service (I use nixos for this).

## Running as a service

The bridge is meant to run always-on under systemd, with its flags in
`ExecStart` and its pairing state in the unit's `StateDirectory` —
`/var/lib/wac-homekit`. Started that way it prints a short pairing line at
startup rather than the terminal's block, and `systemctl status` answers
"is it paired, what is it serving, is anything unreachable" without the
journal.

### Unpairing a service bridge

Deleting the bridge in the Home app while the bridge is *running* needs
nothing from you — iOS sends the removal over a HAP connection, the bridge
handles it, and a fresh setup code appears in the journal straight away.

Deleting it while the bridge is **not** running leaves it paired forever.
The removal is only ever sent over a HAP connection, so with nothing
listening it is never delivered: iOS drops the bridge from its own database
while the bridge goes on advertising itself as paired. Nothing on this side
can notice, either — iOS does not contact an accessory that says it is
paired, so there is no request to see and no failed pairing to count.
Entering the setup code then fails silently, straight back to the "Select an
Accessory" screen, which looks like the bridge is broken when in fact it is
the phone declining to talk to it.

The way out is to clear the state and start over:

```sh
sudo systemctl stop wac-homekit
sudo systemctl clean wac-homekit --what=state
sudo systemctl start wac-homekit
```

- `--what=` is required. Left off, `systemctl clean` defaults to `cache` and
  `runtime` and will cheerfully report success having touched no pairing
  state at all.
- Stop first. `pyhap` holds the state in memory and rewrites the file, so a
  clean underneath a running bridge is undone by the next write.
- This deletes the bridge's HAP identity — its MAC and its keypair — along
  with its pairings. Every controller loses the bridge, and re-pairing starts
  from a fresh setup code, printed at the next startup.
- `systemctl clean` rather than `rm -rf`, because under `DynamicUser` the
  state directory is a symlink into `/var/lib/private` and an `rm -rf` of the
  path itself removes the link while leaving the state behind — an unpair
  that reports success and did nothing.

Running interactively, `--unpair` does the narrower thing: it forgets the
paired controllers at startup and keeps the MAC and keypair, so the bridge
loses only the controllers that no longer exist.
