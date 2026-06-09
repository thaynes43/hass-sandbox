# AppDaemon development environment

> **Applies to:** `appdaemon/**/*.py`, `appdaemon/tests/**`

How to run tests, use venvs, and execute Python commands when iterating on `appdaemon/` code. For coding standards and architecture, see `appdaemon-coding-guidelines.md` and `appdaemon-architecture.md`.

## Detect the current OS first

Before running commands, check whether the environment is Linux (native or WSL) or Windows:

```bash
uname -s   # "Linux" → use bash commands directly
           # If this fails or returns MINGW/MSYS → Windows/PowerShell
```

The instructions below are organized by OS. **Linux is the primary dev environment** (VSCode Server in WSL or native WSL CLI). Windows/PowerShell is the fallback.

## Virtual environments (repo root)

| Environment | Venv path | When to use |
|-------------|-----------|-------------|
| Linux (native or WSL-only) | `.venv/` | Single venv — always use this |
| Windows (with WSL available) | `.venv-wsl/` | WSL/Linux venv (preferred for pytest) |
| Windows (PowerShell only) | `.venv/` | Windows venv (fallback) |

**How to tell which layout you have:**
- If only `.venv/` exists → you're on Linux or a single-venv Windows setup
- If both `.venv/` and `.venv-wsl/` exist → you're on Windows with dual venvs

All venvs live at the **repo root** (e.g. `/home/thaynes/workspace/hass-sandbox/` on Linux, `D:\labspace\hass-sandbox\` on Windows).

## Running tests

### Linux (primary)

Run from the repo root. Activate the venv, cd to `appdaemon/`, then run pytest:

```bash
source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short
```

Single test file:

```bash
source .venv/bin/activate && cd appdaemon && python -m pytest tests/test_door_notify.py -v --tb=short
```

### Windows with WSL (preferred on Windows)

Wrap the full command in `wsl bash -c`:

```bash
wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"
```

### Windows PowerShell fallback

Use the Windows venv python directly (run from repo root):

```powershell
.\.venv\Scripts\python.exe -m pytest appdaemon/tests/ -v
```

**PowerShell pitfalls:**
- `&&` is not a valid statement separator in older PowerShell; use `;` or run via `wsl bash -c "..."`.
- Avoid complex chaining; prefer a single `wsl bash -c "..."` with the full sequence inside.

### Path translation (Windows ↔ WSL)

| Windows              | WSL                          |
|----------------------|------------------------------|
| `D:\labspace\hass-sandbox` | `/mnt/d/labspace/hass-sandbox` |

### If tests fail

- Ensure the venv exists and is up to date: `pip install -r appdaemon/requirements.txt` inside the venv.
- Run from repo root, then `cd appdaemon` before pytest (tests use `Path(__file__).resolve().parent.parent` to find `appdaemon/`).
- For failures, paste the pytest output (especially `short test summary` and tracebacks) so fixes can be applied.

## Other Python commands (lint, scripts, local AppDaemon)

All commands assume you've activated the appropriate venv first.

- **Linting**: `python -m pylint appdaemon/...` or `python -m ruff check appdaemon/`
- **Local AppDaemon run**: `appdaemon -c appdaemon` — run from repo root; reads `appdaemon/appdaemon.yaml` and `apps-dev.yaml`.
- **Install deps**: `pip install -r appdaemon/requirements.txt`
