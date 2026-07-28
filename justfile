# List available recipes
default:
    @just --list

# Run the CLI entry point
run *args:
    uv run --project . {{args}}

# Run tests
test *args:
    uv run pytest {{args}}

# Type-check with mypy
check:
    uv run mypy src/

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
