# wac_homekit

An Apple HomeKit bridge to WAC lighting devices.

Several options exist for bringing 3rd party devices into the Apple HomeKit ecosystem.

[Homebridge](https://github.com/homebridge/homebridge) is a minimal driver for this purpose, but it is written in JavaScript and TypeScript, of which I am unfamiliar.
[Home Assistant](https://github.com/home-assistant) (aka HA) is a very mature project, written in python, of which I am very familiar. However, HA is too big of a system, effectively encompassing everything that HomeKit provides. It can be used as a bridge for 3rd party devices, but there's a lot of overhead just getting everything set up. Publishing integrations is also quite onerous (as it should be for a consumer facing project).
[HAP-python](https://github.com/ikalchev/HAP-python) is a small library that provides direct connection to HomeKit, and it is written in python.

This project uses HAP-python to connect WAC lighting devices to a HomeKit installation. It needs to run as an always on service (I use nixos for this).
