# PKGNAME — Claude project notes

## Coding style

Follow the conventions in `CLAUDE-coding.md` and `CLAUDE-coding-python.md`
(Hungarian notation, type hints on all functions, tabs not spaces, etc.).

## Project layout

```
src/PKGNAME/      # main package; add sub-modules here
tests/            # pytest tests; mirror the src/ structure
```

## Toolchain quick reference

| Task | Command |
|------|---------|
| Run CLI | `uv run PKGNAME` or `just run` |
| Run tests | `just test` |
| Type-check | `just check` |
| Add dep | `just add <pkg>` |
| Upgrade deps | `just upgrade` |

## Key facts

- Python ≥ 3.13, managed by devenv/uv.
- `uv.lock` and `devenv.lock` are both committed.
- Entry point: `src/PKGNAME/main.py::main`.
- Package metadata (version, author) lives exclusively in `pyproject.toml`.
