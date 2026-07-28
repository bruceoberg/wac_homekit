# wac_homekit

A HomeKit bridge for WAC Lighting IoT devices.

## Layout

Two packages in one uv workspace:

```
pyproject.toml              # root project + [tool.uv.workspace]
src/wac_homekit/            # the bridge — owns everything HomeKit
tests/                      # bridge tests
libs/wac_iot/
  pyproject.toml            # workspace member
  src/wac_iot/              # the device library — owns everything WAC
  tests/                    # library tests
```

`wac_homekit` depends on `wac_iot` through `[tool.uv.sources]` with
`{ workspace = true }`, so it resolves to the local checkout rather than an
index. The two are kept separate on purpose: `wac_iot` is meant to be lifted
out later as a standalone package. See `.claude/CLAUDE.md` for the rules that
keeps the boundary honest — in short, `wac_iot` never imports anything
HomeKit-related, and `wac_homekit` never talks HTTP or mDNS directly.

Both packages ship `py.typed`; mypy runs strict over `src/` and `libs/` in one
pass.

## Setup

```sh
# First time: initialise the devenv lock and Python venv
devenv update       # resolves devenv.yaml → devenv.lock
direnv allow        # activates the env; uv sync runs automatically
```

After that, `cd`-ing into the directory activates the shell automatically
(direnv hook required — add `eval "$(direnv hook zsh)"` to `~/.zshrc` if not
already present).

Do not create a virtualenv by hand. `UV_PROJECT_ENVIRONMENT` already points at
devenv's managed venv, and a stray `.venv/` silently shadows it.

## Usage

```sh
uv run wac_homekit          # the bridge
uv run wac_iot --help       # the device-layer CLI (discover / probe / dump)
```

## Development

```sh
just test               # pytest across both packages
just check              # mypy strict, src/ and libs/
just dump --host <addr> # dump a real device's state
just upgrade            # uv lock --upgrade && uv sync
just add <pkg>          # add a runtime dependency
just add-dev <pkg>      # add a dev dependency
```

Add dependencies with `just add`, never by hand-editing `pyproject.toml`. To
add one to `wac_iot` rather than the bridge, use
`uv add --package wac_iot <pkg>`.

## Testing

Tests cover the pure functions whose arithmetic is easy to get subtly wrong —
unit conversion between device and HomeKit ranges, mDNS TXT parsing, status
code mapping. The device layer is verified against real hardware with
`just dump`, not against a mock HTTP server.

Pytest runs in `--import-mode=importlib` with an explicit `testpaths`. Both
packages have a `tests/test_main.py`; under the older `prepend` import mode
those two basenames collide and collection fails outright.

CI runs `uv sync`, `uv run mypy`, and `uv build --all-packages` only — tests
stay local.
