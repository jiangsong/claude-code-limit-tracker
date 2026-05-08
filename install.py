#!/usr/bin/env python3
"""
Installation script for Claude Code Usage Tracker.
Integrates with Claude Code's settings and sets up the tracker.
"""

import json
import os
import sys
import subprocess
import shutil
from pathlib import Path

# Console codepage on Chinese-locale Windows is cp936; emoji + ✓/❌ glyphs
# will crash on print without an explicit UTF-8 reconfiguration.
if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _venv_python(project_dir: Path) -> Path:
    """Return the path to the venv's Python interpreter for this OS."""
    if sys.platform == "win32":
        return project_dir / ".venv" / "Scripts" / "python.exe"
    return project_dir / ".venv" / "bin" / "python"


def _have_uv() -> bool:
    return shutil.which("uv") is not None


def check_dependencies():
    """Check required dependencies. uv is optional."""
    print("Checking dependencies...")

    if sys.version_info < (3, 8):
        print("\n❌ Python 3.8+ is required.")
        return False

    if _have_uv():
        print("✓ uv detected")
    else:
        print("ℹ uv not found — falling back to stdlib venv + pip "
              "(install uv for faster setup: https://github.com/astral-sh/uv)")

    print("✓ Python version OK")
    return True


def setup_virtual_env():
    """Set up virtual environment and install packages.

    Prefers uv when available; otherwise uses `python -m venv` + pip.
    """
    print("\nSetting up Python environment...")

    project_dir = Path(__file__).parent
    venv_dir = project_dir / ".venv"
    py = _venv_python(project_dir)

    if venv_dir.exists() and py.exists():
        print(f"Reusing existing virtual environment at {venv_dir}")
    else:
        print("Creating virtual environment...")
        if _have_uv():
            cmd = ["uv", "venv"]
        else:
            cmd = [sys.executable, "-m", "venv", str(venv_dir)]
        result = subprocess.run(cmd, cwd=project_dir, capture_output=True)
        if result.returncode != 0:
            err = result.stderr.decode(errors="replace") if result.stderr else ""
            print(f"❌ Failed to create virtual environment: {err}")
            return False

    # If numpy already imports cleanly, skip the install step entirely. This
    # matters because `uv venv` creates a pip-less venv, so the stdlib pip
    # fallback fails on existing uv-created venvs.
    probe = subprocess.run(
        [str(py), "-c", "import numpy"], capture_output=True
    )
    if probe.returncode == 0:
        print("✓ numpy already installed")
        print("✓ Python environment configured")
        return True

    print("Installing numpy...")
    if _have_uv():
        cmd = ["uv", "pip", "install", "numpy", "--python", str(py)]
        cwd = project_dir
    else:
        # Bootstrap pip if the venv was created without it (e.g. `uv venv`).
        has_pip = subprocess.run(
            [str(py), "-c", "import pip"], capture_output=True
        ).returncode == 0
        if not has_pip:
            print("Bootstrapping pip via ensurepip...")
            ensure = subprocess.run(
                [str(py), "-m", "ensurepip", "--upgrade"], capture_output=True
            )
            if ensure.returncode != 0:
                err = ensure.stderr.decode(errors="replace") if ensure.stderr else ""
                print(f"❌ ensurepip failed: {err}")
                print("  Install uv (https://github.com/astral-sh/uv) or recreate "
                      ".venv with `python -m venv .venv` and retry.")
                return False
        cmd = [str(py), "-m", "pip", "install", "--disable-pip-version-check", "numpy"]
        cwd = None
    result = subprocess.run(cmd, cwd=cwd, capture_output=True)
    if result.returncode != 0:
        err = result.stderr.decode(errors="replace") if result.stderr else ""
        print(f"❌ Failed to install numpy: {err}")
        return False

    print("✓ Python environment configured")
    return True


SESSION_HOOK_TIMEOUT = "15"  # seconds; SessionStart blocks Claude startup


def _data_dir(project_dir: Path) -> Path:
    return project_dir / "data"


def _probe_cache_path(project_dir: Path) -> Path:
    return _data_dir(project_dir) / "probe_cache.json"


def _probe_script(project_dir: Path) -> Path:
    return project_dir / "src" / "claude_probe.py"


def _install_session_start_hook(settings: dict, project_dir: Path) -> None:
    """Register a SessionStart hook that synchronously refreshes the probe cache.

    This guarantees that the very first status-line render after Claude Code
    starts up shows real-time `/usage` data instead of whatever was cached
    when the previous session ended.

    The hook is idempotent across re-installs: any existing hook entry whose
    command points at our claude_probe.py is replaced rather than duplicated.
    """
    py = _venv_python(project_dir)
    cache_path = _probe_cache_path(project_dir)
    probe = _probe_script(project_dir)
    hook_command = f'"{py}" "{probe}" --update-cache "{cache_path}" {SESSION_HOOK_TIMEOUT}'

    hooks = settings.setdefault("hooks", {})
    session_hooks = hooks.setdefault("SessionStart", [])

    probe_str = str(probe)
    new_entries = []
    for entry in session_hooks:
        sub_hooks = (entry or {}).get("hooks") or []
        # Drop any prior installation of *our* probe hook; keep all others.
        keep = [h for h in sub_hooks if probe_str not in (h or {}).get("command", "")]
        if keep:
            new_entry = dict(entry)
            new_entry["hooks"] = keep
            new_entries.append(new_entry)

    new_entries.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": hook_command}],
    })
    hooks["SessionStart"] = new_entries


def integrate_with_claude():
    """Integrate tracker with Claude Code settings."""
    print("\nIntegrating with Claude Code...")

    claude_settings = Path.home() / ".claude" / "settings.json"
    project_dir = Path(__file__).parent.resolve()

    if claude_settings.exists():
        backup_path = claude_settings.with_suffix('.json.backup')
        print(f"Creating backup: {backup_path}")
        shutil.copy2(claude_settings, backup_path)

        with open(claude_settings, 'r', encoding='utf-8') as f:
            settings = json.load(f)
    else:
        settings = {}

    # Run the status line via the venv's Python directly. This is
    # self-contained (no uv dependency at status-line execution time) and
    # avoids any shell-specific `cd` / chaining issues across cmd.exe,
    # PowerShell, and POSIX shells.
    py = _venv_python(project_dir)
    status_script = project_dir / "status_line.py"
    settings['statusLine'] = {
        'type': 'command',
        'command': f'"{py}" "{status_script}"',
    }

    _install_session_start_hook(settings, project_dir)

    claude_settings.parent.mkdir(exist_ok=True)
    with open(claude_settings, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)

    print("✓ Claude Code settings updated (statusLine + SessionStart probe hook)")
    return True


def start_probe_daemon():
    """Spawn the background probe daemon if not already running.

    The daemon polls `claude /usage` every 10 minutes, so cached quota data
    stays current even when no Claude Code session is active. On macOS/Linux
    only — PTY probe is not supported on Windows.
    """
    if sys.platform == "win32":
        print("ℹ Skipping probe daemon (PTY probe not supported on Windows)")
        return True

    print("\nStarting background probe daemon...")
    project_dir = Path(__file__).parent.resolve()
    py = _venv_python(project_dir)
    cache_path = _probe_cache_path(project_dir)
    src_dir = project_dir / "src"

    _data_dir(project_dir).mkdir(exist_ok=True)

    result = subprocess.run(
        [str(py), str(_probe_script(project_dir)),
         "--ensure-daemon", str(cache_path), str(src_dir)],
        capture_output=True,
    )
    if result.returncode == 0:
        print("✓ Probe daemon ensured (refreshes /usage every 10 min)")
        return True
    err = result.stderr.decode(errors="replace") if result.stderr else ""
    print(f"⚠️  Failed to start probe daemon: {err}")
    return False


def _is_already_configured(project_dir: Path) -> bool:
    cfg = project_dir / "config" / "user_config.json"
    if not cfg.exists():
        return False
    try:
        with open(cfg, encoding="utf-8") as f:
            return bool(json.load(f).get("configured"))
    except Exception:
        return False


def configure_subscription():
    """Configure subscription tier (interactive)."""
    print("\nConfiguring subscription tier...")

    project_dir = Path(__file__).parent
    (project_dir / "config").mkdir(exist_ok=True)

    if _is_already_configured(project_dir):
        try:
            with open(project_dir / "config" / "user_config.json", encoding="utf-8") as f:
                tier = json.load(f).get("subscription_tier", "?")
        except Exception:
            tier = "?"
        print(f"✓ Already configured (tier: {tier}). Run `python configure.py` to change.")
        return True

    py = _venv_python(project_dir)
    config_script = (
        "import sys\n"
        f"sys.path.insert(0, r'{project_dir / 'src'}')\n"
        "from config import Config\n"
        "Config().interactive_setup()\n"
    )
    result = subprocess.run([str(py), "-c", config_script])

    if result.returncode == 0:
        print("✓ Subscription tier configured")
        return True
    print("❌ Configuration failed")
    return False


def test_installation():
    """Smoke-test the installed status line."""
    print("\nTesting installation...")

    project_dir = Path(__file__).parent
    py = _venv_python(project_dir)

    test_input = json.dumps({"projectPath": str(project_dir)})
    result = subprocess.run(
        [str(py), str(project_dir / 'status_line.py')],
        input=test_input,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
    )

    if result.returncode == 0 and result.stdout:
        print("✓ Status line test successful")
        print(f"Sample output: {result.stdout.strip()}")
        return True

    print("❌ Status line test failed")
    if result.stderr:
        print(f"Error: {result.stderr}")
    return False


def main():
    print("=" * 60)
    print("Claude Code Usage Tracker - Python Installation")
    print("=" * 60)

    test_mode = '--test' in sys.argv
    skip_configure = '--skip-configure' in sys.argv

    if test_mode:
        print("\n🔍 Running in TEST MODE - no changes will be made")

    if not check_dependencies():
        sys.exit(1)

    if not test_mode:
        if not setup_virtual_env():
            sys.exit(1)

        if skip_configure:
            print("\nℹ Skipping subscription configuration (--skip-configure).")
        elif not configure_subscription():
            print("\n⚠️  Subscription configuration skipped")

        if not integrate_with_claude():
            sys.exit(1)

        start_probe_daemon()

        if not test_installation():
            print("\n⚠️  Test failed but installation may still work")

    print("\n" + "=" * 60)
    print("✅ Installation complete!")
    print("\nThe tracker is now integrated with Claude Code.")
    print("Your usage will be displayed in the status line.")
    print("\nTo reconfigure your subscription tier, run:")
    print("  python configure.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
