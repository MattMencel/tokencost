# v1.0.3 — Windows support

Full Windows 10/11 port alongside the existing macOS build.

## Added

- **`onbording.ps1`** — PowerShell setup/start/stop script (Windows equivalent of `onbording.sh`): creates the venv, installs deps, imports history, sets `ANTHROPIC_BASE_URL` as a User env var, registers Scheduled Tasks for proxy autostart + 5-min sync, starts the proxy, opens the dashboard
- **`tokencost.bat`** — double-click launcher for `onbording.ps1`
- **`requirements.txt`** — pinned dependency list (`fastapi`, `uvicorn`, `httpx`)

## Fixes (cross-platform)

- **UTF-8 console**: `proxy.py` and `import_history.py` now force UTF-8 on stdout/stderr — Windows' default cp1252 console crashed on box-drawing / emoji output
- **Dashboard read**: `/dashboard` opened `dashboard.html` without an encoding — crashed with `UnicodeDecodeError` on Windows; now reads as UTF-8
- **Local log paths**: history import now searches Windows locations (`%APPDATA%\Claude`, `%APPDATA%\Code`) in addition to macOS `~/Library/...` and Linux `~/.config/...` for Claude CLI, Claude Desktop, VS Code extensions, and Copilot logs
- **Update command**: `/version` `update_cmd` and the version-cache writer now emit a PowerShell command on Windows instead of `bash onbording.sh`
- **Path/timezone**: `projects.py` uses `os.sep` for home-prefix abbreviation and a DST-safe `astimezone()` for the "today" cutoff (replaces the fragile `time.timezone` math)

## Result

Proxy, dashboard, history import, and live sync all verified working on Windows 11 (Python 3.14).
