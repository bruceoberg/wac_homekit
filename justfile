# List available recipes
default:
    @just --list

# Run the CLI entry point
run *args:
    uv run --project . {{args}}

# Dump a device's /device, /fixture, /group and /automation state
dump *args:
    uv run wac_iot dump {{args}}

# Run tests
test *args:
    uv run pytest {{args}}

# Type-check with mypy (files come from [tool.mypy] — src/ and libs/)
check:
    uv run mypy

# Add a runtime dependency (e.g. `just add requests`)
add *pkgs:
    uv add {{pkgs}}

# Add a dev-only dependency (e.g. `just add-dev pytest`)
add-dev *pkgs:
    uv add --dev {{pkgs}}

# Upgrade all locked dependencies to their latest allowed versions
upgrade:
    uv lock --upgrade
    uv sync
