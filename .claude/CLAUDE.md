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

## The vendor spec

The WAC IoT Unified REST Interface PDF is in `private/`, which is gitignored.
Read it for protocol details.

Every page is marked CONFIDENTIAL. **Never copy its text into a committed
file** — no pasting tables into docstrings, comments, README, or markdown.
Derive code from it; do not reproduce it.

It is also not fully reliable. Verify these against a real device rather than
trusting the document:

- The overview says the HTTP server port is 80; the mDNS section says the
  advertised service port is 443, corresponding to HTTPS. Determine which
  actually answers, and expect a self-signed certificate.
- `result` is documented as a String and appears as `"0"` in the sample device
  response. Parse both string and numeric forms.
- Fixture type IDs skip 4, 5, and 7–10. Unknown types must log and be skipped,
  never raise.
- mDNS TXT keys contain literal spaces: `Firmware Ver`, `Protocol Ver`.
- Tunable white accepts `colorTempLevel` (steps 1–7) *or* `mixColorTemp`
  (Kelvin), explicitly not both. RGBW exposes both RGB and HSV. Pick one path
  per fixture type and use it consistently.

## Protocol facts that shape the design

- Every endpoint is a `POST` carrying an `action` number in the JSON body.
  There is no verb-to-operation mapping; do not design one.
- `POST /fixture` with `{"action": 3}` and `addr` omitted returns **every**
  fixture with its `state`, `tune`, and `detail`. The poll loop is one request,
  not one per fixture. `/group` action 3 behaves the same way.
- There is no push channel. Polling is the only option; 5–10 seconds is the
  starting range for these ESP32-class devices.
- `findme` in a fixture's control state maps onto HomeKit's Identify
  characteristic.
- Group address 255 is a built-in "All-Default" group containing every fixture.

## Testing

The things worth testing here are the pure functions whose arithmetic is easy
to get subtly wrong:

- unit conversion between device and HomeKit ranges
- mDNS TXT record parsing
- status code to exception mapping

The device layer is not worth a mock HTTP server. Verify it against real
hardware with the `dump` CLI instead.