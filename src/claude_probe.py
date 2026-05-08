"""
Probes `claude /usage` via PTY to read real Anthropic server-side quota data.
Returns the same percentages and reset times that the user sees in /status.

Run modes (CLI):
  --update-cache <cache_path> [timeout]
      One-shot probe; writes result to <cache_path>. Used by SessionStart hook.
  --daemon <cache_path> [interval]
      Long-running loop that refreshes <cache_path> every <interval> seconds.
      Defaults to 600s. Holds a PID file at <cache_path>'s parent dir.
  --ensure-daemon <cache_path> <src_dir> [interval]
      Idempotent: spawns the daemon if it isn't already running.
  --stop-daemon <cache_path>
      Sends SIGTERM to a running daemon (no-op if absent).
"""
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional, Tuple

# `pty` / `select` (on master fds) are POSIX-only. Defer the import so this
# module can still be loaded on Windows for cache reading; the actual probe
# will fall back to an error ProbeResult there.
_PTY_AVAILABLE = sys.platform != "win32"

DEFAULT_DAEMON_INTERVAL = 600.0  # seconds — matches PROBE_TTL in status_line.py
PID_FILE_NAME = "probe_daemon.pid"


@dataclass
class ProbeResult:
    session_pct_used: Optional[int]       # e.g. 8  (percent used)
    weekly_pct_used: Optional[int]        # e.g. 35
    sonnet_pct_used: Optional[int]        # e.g. 1
    opus_pct_used: Optional[int]          # e.g. None or 20
    session_reset_text: Optional[str]     # e.g. "11:50am (Asia/Shanghai)"
    weekly_reset_text: Optional[str]      # e.g. "May 4, 2pm (Asia/Shanghai)"
    account_tier: Optional[str]           # e.g. "Claude Max"
    error: Optional[str] = None


def _run_usage_pty(timeout: float = 20.0) -> bytes:
    """Run `claude /usage` under a PTY and return the raw terminal bytes."""
    if not _PTY_AVAILABLE:
        raise RuntimeError("pty probe is not supported on this platform")

    import pty
    import select

    env = os.environ.copy()
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        ["claude", "/usage"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)

    output = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        r, _, _ = select.select([master_fd], [], [], min(remaining, 0.5))
        if r:
            try:
                chunk = os.read(master_fd, 4096)
                output += chunk
            except OSError:
                break
        if proc.poll() is not None:
            # drain
            try:
                while True:
                    output += os.read(master_fd, 4096)
            except OSError:
                pass
            break

    try:
        os.close(master_fd)
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()

    return output


def _clean(raw: bytes) -> str:
    """Strip ANSI escape sequences and block-drawing chars; collapse whitespace."""
    text = raw.decode("utf-8", errors="replace")

    # Replace cursor-forward sequences with a space so words stay separated
    text = re.sub(r"\x1b\[(\d*)C", lambda m: " " * max(1, int(m.group(1) or "1")), text)

    # Strip remaining ANSI/VT escape sequences
    text = re.sub(r"\x1b\[[^a-zA-Z]*[a-zA-Z]", "", text)
    text = re.sub(r"\x1b[^\[]\S*", "", text)
    text = re.sub(r"\x1b.", "", text)

    # Strip block-drawing Unicode (progress bar blocks)
    text = re.sub(r"[▀-▟─-╿]", "", text)

    # Normalize line endings and collapse runs of spaces/tabs
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)

    return text


def _extract_pct(label: str, text: str) -> Optional[int]:
    """
    Find a section whose heading contains `label`, then grab the first
    "N% used" or "N% left" within the next 8 non-empty lines.
    """
    lines = text.splitlines()
    label_lower = label.lower()
    for i, line in enumerate(lines):
        if label_lower in line.lower():
            window = [l for l in lines[i : i + 8] if l.strip()]
            for candidate in window:
                m = re.search(r"(\d{1,3})\s*%\s*(used|left)", candidate, re.IGNORECASE)
                if m:
                    val = int(m.group(1))
                    if m.group(2).lower() == "left":
                        val = 100 - val  # convert "left" to "used"
                    return val
    return None


def _extract_reset(label: str, text: str) -> Optional[str]:
    """Find reset text near a section heading.

    The Claude CLI sometimes renders "Resets" as "Rese s" due to cursor-movement
    overwrite artefacts in the PTY output. We match both forms.
    """
    # Pattern for a reset line: "Resets HH:MMam" or "Resets Month D, H[H]am"
    # Also catches the garbled "Rese s ..." form.
    _RESET_RE = re.compile(
        r"(?:Resets?|Rese\s+s)\s+(.+)",
        re.IGNORECASE,
    )

    lines = text.splitlines()
    label_lower = label.lower()
    for i, line in enumerate(lines):
        if label_lower in line.lower():
            window = lines[i : i + 10]
            for candidate in window:
                m = _RESET_RE.search(candidate)
                if m:
                    reset_body = m.group(1).strip()
                    # Handle duplicate: "11:50am (TZ)11:50am (TZ)" → take last occurrence
                    parts = re.split(r"\d{1,2}:\d{2}[ap]m", reset_body, flags=re.IGNORECASE)
                    times = re.findall(r"\d{1,2}:\d{2}[ap]m", reset_body, flags=re.IGNORECASE)
                    if times:
                        reset_body = times[-1] + (parts[-1] if parts else "")
                    return f"Resets {reset_body}".strip()
    return None


def _extract_tier(text: str) -> Optional[str]:
    """Detect Claude Max / Claude Pro from the header line."""
    lower = text.lower()
    if "claude max" in lower:
        return "Claude Max"
    if "claude pro" in lower:
        return "Claude Pro"
    return None


def probe(timeout: float = 20.0) -> ProbeResult:
    """Run `claude /usage` and return parsed quota data."""
    try:
        raw = _run_usage_pty(timeout=timeout)
    except Exception as e:
        return ProbeResult(None, None, None, None, None, None, None, error=str(e))

    if not raw:
        return ProbeResult(None, None, None, None, None, None, None, error="empty output")

    text = _clean(raw)

    session_pct = _extract_pct("Current session", text)
    weekly_pct = _extract_pct("Current week (all models)", text)
    sonnet_pct = _extract_pct("Current week (Sonnet", text)
    opus_pct = _extract_pct("Current week (Opus", text)
    session_reset = _extract_reset("Current session", text)
    weekly_reset = _extract_reset("Current week", text)
    tier = _extract_tier(text)

    if session_pct is None:
        return ProbeResult(None, None, None, None, None, None, tier,
                           error="could not find 'Current session' in output")

    return ProbeResult(
        session_pct_used=session_pct,
        weekly_pct_used=weekly_pct,
        sonnet_pct_used=sonnet_pct,
        opus_pct_used=opus_pct,
        session_reset_text=session_reset,
        weekly_reset_text=weekly_reset,
        account_tier=tier,
    )


CACHE_FILE = None  # set at import time in status_line via set_cache_path()


def set_cache_path(path: str) -> None:
    global CACHE_FILE
    CACHE_FILE = path


def read_cache() -> Optional["ProbeResult"]:
    """Return cached ProbeResult if cache file exists, else None."""
    import json
    if not CACHE_FILE:
        return None
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return ProbeResult(
            session_pct_used=d.get("session_pct_used"),
            weekly_pct_used=d.get("weekly_pct_used"),
            sonnet_pct_used=d.get("sonnet_pct_used"),
            opus_pct_used=d.get("opus_pct_used"),
            session_reset_text=d.get("session_reset_text"),
            weekly_reset_text=d.get("weekly_reset_text"),
            account_tier=d.get("account_tier"),
            error=d.get("error"),
        ), d.get("cached_at", 0)
    except Exception:
        return None, 0


def write_cache(result: "ProbeResult") -> None:
    """Write probe result to cache file atomically."""
    import json
    if not CACHE_FILE:
        return
    data = {
        "session_pct_used": result.session_pct_used,
        "weekly_pct_used": result.weekly_pct_used,
        "sonnet_pct_used": result.sonnet_pct_used,
        "opus_pct_used": result.opus_pct_used,
        "session_reset_text": result.session_reset_text,
        "weekly_reset_text": result.weekly_reset_text,
        "account_tier": result.account_tier,
        "error": result.error,
        "cached_at": time.time(),
    }
    tmp = CACHE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def spawn_background_refresh(cache_path: str, src_dir: str) -> None:
    """Launch a background process to refresh the probe cache (one-shot).

    Kept for backwards compatibility; new code should prefer `ensure_daemon`
    which keeps the cache fresh even when Claude Code is idle.
    """
    if not _PTY_AVAILABLE:
        return  # PTY probe not supported on this platform
    script = os.path.join(src_dir, "claude_probe.py")
    popen_kwargs = dict(
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if sys.platform == "win32":
        # Detach from the parent's console so no window flashes during the
        # status-line refresh, and so the parent can exit independently.
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        )
    else:
        popen_kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, script, "--update-cache", cache_path],
        **popen_kwargs,
    )


# ---------------------------------------------------------------------------
# Background daemon: keeps the probe cache fresh even when Claude Code is idle
# ---------------------------------------------------------------------------

def _pid_file_path(cache_path: str) -> str:
    """PID file lives next to the cache so install/uninstall are co-located."""
    return os.path.join(os.path.dirname(cache_path), PID_FILE_NAME)


def _pid_alive(pid: int) -> bool:
    """POSIX-style liveness probe via signal 0. Tolerates PermissionError
    (process exists, owned by another user)."""
    if pid <= 0 or pid == os.getpid():
        return pid == os.getpid()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_pid_file(pid_file: str) -> int:
    try:
        with open(pid_file, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return -1


def daemon_loop(cache_path: str, interval: float = DEFAULT_DAEMON_INTERVAL) -> None:
    """Refresh the cache every `interval` seconds until SIGTERM/SIGINT.

    Uses an O_EXCL claim on the PID file so two daemons can't coexist; if a
    stale (dead-PID) file is found the new daemon takes over. Cleans up the
    PID file on exit, but only when it still owns it.
    """
    set_cache_path(cache_path)
    pid_file = _pid_file_path(cache_path)
    my_pid = os.getpid()

    # Atomic claim. If another live daemon already holds the file, exit.
    try:
        fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(my_pid).encode())
        os.close(fd)
    except FileExistsError:
        existing = _read_pid_file(pid_file)
        if _pid_alive(existing) and existing != my_pid:
            return
        # Stale → take over
        try:
            with open(pid_file, "w", encoding="utf-8") as f:
                f.write(str(my_pid))
        except OSError:
            return

    import signal
    stop_flag = {"v": False}

    def _on_signal(signum, frame):  # noqa: ARG001
        stop_flag["v"] = True

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass  # not main thread / unsupported

    try:
        while not stop_flag["v"]:
            try:
                write_cache(probe())
            except Exception:
                # Never crash the daemon on a single failed probe.
                pass
            # Sleep in short slices so SIGTERM is responsive.
            slept = 0.0
            slice_s = 2.0
            while slept < interval and not stop_flag["v"]:
                time.sleep(min(slice_s, interval - slept))
                slept += slice_s
    finally:
        # Only remove the PID file if we still own it (avoid clobbering a
        # successor daemon that took over after a SIGKILL).
        if _read_pid_file(pid_file) == my_pid:
            try:
                os.remove(pid_file)
            except OSError:
                pass


def ensure_daemon(
    cache_path: str,
    src_dir: str,
    interval: float = DEFAULT_DAEMON_INTERVAL,
) -> bool:
    """Spawn the probe daemon if not already running.

    Returns True if a new daemon was spawned, False otherwise. Idempotent and
    fast: a stat + small read when the daemon is healthy.
    """
    if not _PTY_AVAILABLE:
        return False  # PTY probe not supported on Windows

    pid_file = _pid_file_path(cache_path)
    if os.path.exists(pid_file):
        existing = _read_pid_file(pid_file)
        if _pid_alive(existing):
            return False
        # Stale — clean up so the new daemon can claim it cleanly.
        try:
            os.remove(pid_file)
        except OSError:
            pass

    script = os.path.join(src_dir, "claude_probe.py")
    subprocess.Popen(
        [sys.executable, script, "--daemon", cache_path, str(interval)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return True


def stop_daemon(cache_path: str) -> bool:
    """Send SIGTERM to a running daemon. Returns True if a signal was sent."""
    pid_file = _pid_file_path(cache_path)
    if not os.path.exists(pid_file):
        return False
    pid = _read_pid_file(pid_file)
    if not _pid_alive(pid):
        try:
            os.remove(pid_file)
        except OSError:
            pass
        return False
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    import json as _json

    args = sys.argv[1:]
    if args and args[0] == "--update-cache" and len(args) >= 2:
        cache_path = args[1]
        timeout = float(args[2]) if len(args) >= 3 else 20.0
        set_cache_path(cache_path)
        result = probe(timeout=timeout)
        write_cache(result)
    elif args and args[0] == "--daemon" and len(args) >= 2:
        cache_path = args[1]
        interval = float(args[2]) if len(args) >= 3 else DEFAULT_DAEMON_INTERVAL
        daemon_loop(cache_path, interval)
    elif args and args[0] == "--ensure-daemon" and len(args) >= 3:
        cache_path = args[1]
        src_dir = args[2]
        interval = float(args[3]) if len(args) >= 4 else DEFAULT_DAEMON_INTERVAL
        ensure_daemon(cache_path, src_dir, interval)
    elif args and args[0] == "--stop-daemon" and len(args) >= 2:
        stop_daemon(args[1])
    else:
        result = probe()
        print(result)
