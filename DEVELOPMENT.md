# PKGNAME

TODO: one-line description.

## Setup

```sh
# First time: initialise the devenv lock and Python venv
devenv update       # resolves devenv.yaml → devenv.lock
direnv allow        # activates the env; uv sync runs automatically
```

After that, `cd`-ing into the directory activates the shell automatically
(direnv hook required — add `eval "$(direnv hook zsh)"` to `~/.zshrc` if not
already present).

## Usage

```sh
uv run PKGNAME          # run via uv (works outside the devenv shell too)
PKGNAME                 # run directly once the devenv shell is active
```

## Development

```sh
just test               # run pytest
just check              # mypy type-check
just upgrade            # uv lock --upgrade && uv sync
just add <pkg>          # add a runtime dependency
just add-dev <pkg>      # add a dev dependency
```
