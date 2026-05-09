#!/usr/bin/env python3
"""
Status line generator for Claude Code integration.
Reads JSON input from stdin and outputs formatted status line.

Usage data priority:
  1. Native rate_limits from Claude Code stdin (most accurate, updates on each API request)
  2. Real Anthropic data from `claude /usage` probe cache (refreshes every 10 min)
  3. Local JSONL token counts as fallback (approximate)

Cache freshness is maintained by two cooperating mechanisms:
  - A SessionStart hook (configured in ~/.claude/settings.json by install.py)
    runs the probe synchronously when a Claude Code session begins, so the
    very first status-line render uses fresh data.
  - A long-running background daemon (started lazily here via ensure_daemon)
    refreshes the cache every PROBE_TTL seconds and keeps running even after
    Claude Code exits, so cached values stay current between sessions.

Sonnet/Opus split is only available from the probe.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Claude Code pipes our stdout through a shell; on Windows that defaults to
# the console's OEM codepage (e.g. cp936) which mangles emoji + ANSI. Force
# UTF-8 on stdin/stdout/stderr so the status line renders correctly.
if sys.platform == "win32":
    for stream_name in ("stdout", "stderr", "stdin"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

from tracker import UsageTracker
from config import Config
from git_info import GitInfo
import claude_probe as probe_mod

PROBE_TTL = 3600  # seconds — daemon refreshes /usage at most once per hour.
                  # `claude /usage` is genuinely expensive (spins up a full
                  # Claude Code subprocess) and the same value also appears
                  # nowhere on screen until the next render, so polling it
                  # often costs CPU without a visible benefit.


def _probe_cache_path() -> str:
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    return str(data_dir / "probe_cache.json")


def _src_dir() -> str:
    return str(Path(__file__).parent)


def _pct_color(config: "Config", pct_used: int):
    """Map 0-100 used% to an RGB color tuple."""
    if pct_used <= 50:
        return (0, 255, 0)      # green
    elif pct_used <= 80:
        return (255, 215, 0)    # yellow
    else:
        return (255, 80, 80)    # red


_MONTH_INDEX = {
    name: idx + 1 for idx, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}


def _parse_reset_text_to_epoch(reset_text: str,
                               max_horizon_secs: float) -> Optional[float]:
    """Parse a probe-cache reset string into an epoch timestamp.

    Two forms are produced by `claude /usage`:
      * "Resets 11:50am (Asia/Shanghai)" — clock time only, used for the
        5-hour cycle (always within ~5h, so rolls to tomorrow if the time
        has already passed today).
      * "Resets May 4, 2pm (Asia/Shanghai)" — date + time, used for the
        weekly window (rolls year forward if the date is already past).

    The CLI emits these in the user's local time zone, so a naive
    `datetime.now()` parse matches the wall-clock the user reads on screen
    without needing tzdata or zoneinfo. Returns None on any failure.

    `max_horizon_secs` is a sanity bound on the time-only form; if the
    inferred target is further out than the bound (with 20% slack) we
    treat the parse as ambiguous rather than guess.
    """
    if not reset_text:
        return None
    import re
    from datetime import datetime, timedelta

    body = re.sub(r"^Resets?\s+", "", reset_text, flags=re.IGNORECASE).strip()
    body = re.sub(r"\s*\([^)]*\)\s*$", "", body).strip()
    now = datetime.now()

    # Day-and-time separator varies between Claude Code versions:
    #   "May 4, 2pm"       — comma
    #   "May 15 at 7pm"    — the word "at"
    #   "May 15 7pm"       — bare whitespace
    m = re.match(
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))[a-z]*"
        r"\s+(\d{1,2})"
        r"(?:\s*,\s*|\s+at\s+|\s+)"
        r"(\d{1,2})(?::(\d{1,2}))?\s*([ap]m)",
        body, re.IGNORECASE,
    )
    if m:
        month = _MONTH_INDEX.get(m.group(1).lower()[:3])
        if month is None:
            return None
        day = int(m.group(2))
        hour = int(m.group(3))
        minute = int(m.group(4) or 0)
        ampm = m.group(5).lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        try:
            target = now.replace(month=month, day=day, hour=hour, minute=minute,
                                 second=0, microsecond=0)
        except ValueError:
            return None
        if target < now:
            try:
                target = target.replace(year=now.year + 1)
            except ValueError:
                return None
        return target.timestamp()

    m = re.match(r"(\d{1,2}):(\d{2})\s*([ap]m)", body, re.IGNORECASE)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        ampm = m.group(3).lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        if (target - now).total_seconds() > max_horizon_secs * 1.2:
            return None
        return target.timestamp()

    return None


def _resolve_reset_secs(stdin_resets_at: Optional[float],
                        probe_reset_text: Optional[str],
                        max_horizon_secs: float) -> Optional[float]:
    """Return seconds-until-reset using the most authoritative source.

    Priority: stdin epoch (already a number) → probe text parsed to epoch.
    Returns None when neither source yields a usable value, so the caller
    can decide whether to fall back to a local estimate or hide the field.
    """
    if stdin_resets_at:
        return stdin_resets_at - time.time()
    if probe_reset_text:
        epoch = _parse_reset_text_to_epoch(probe_reset_text, max_horizon_secs)
        if epoch is not None:
            return epoch - time.time()
    return None


def _format_countdown(secs: float) -> str:
    """Compact countdown for the weekly reset.

    >= 1 day -> '1d20h' (no minutes — keeps the status line short)
    <  1 day -> '4h28m' or '15m'
    <= 0     -> 'now'
    """
    if secs <= 0:
        return "now"
    if secs >= 86400:
        d = int(secs // 86400)
        h = int((secs % 86400) // 3600)
        return f"{d}d{h}h"
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _read_stdin_payload() -> dict:
    """Read and parse Claude Code's full stdin JSON payload.

    Claude Code pipes a single JSON object to the statusLine command on
    every refresh. Returns the parsed dict (rate_limits, workspace, cwd,
    model, ...) or {} if unavailable. stdin can only be read once, so
    callers must extract every field they need from the returned dict.
    """
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                return json.loads(raw)
    except Exception:
        pass
    return {}


def _resolve_current_model(payload: dict, usage) -> str:
    """Pick the active model name to display.

    Priority:
      1. Claude Code stdin payload `model.display_name` / `model.id` — refreshed
         on every status-line render and reflects the user's /model selection,
         including "Default" (which `~/.claude/settings.json` does NOT record).
      2. CLAUDE_MODEL env var (legacy / non-CC contexts).
      3. `~/.claude/settings.json` `model` field (only set on explicit override).
      4. Most-recent session's response counts as a last resort.
    """
    model_payload = payload.get("model") or {}
    display_name = (model_payload.get("display_name") or "").strip()
    model_id = (model_payload.get("id") or "").strip().lower()

    if display_name:
        # Strip parenthetical qualifiers like "(1M context)" — they bloat the
        # status line and push the weekly reset countdown off-screen.
        import re
        return re.sub(r"\s*\([^)]*\)\s*", "", display_name).strip() or display_name
    if model_id:
        if "opus" in model_id:
            return "Opus"
        if "sonnet" in model_id:
            return "Sonnet"
        if "haiku" in model_id:
            return "Haiku"

    claude_model = os.environ.get("CLAUDE_MODEL", "").lower()
    if "opus" in claude_model:
        return "Opus 4"
    if "sonnet" in claude_model:
        return "Sonnet 4"

    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        if settings_path.exists():
            with open(settings_path, encoding="utf-8") as f:
                s = json.load(f)
            model_setting = (s.get("model") or "").lower()
            if "opus" in model_setting:
                return "Opus 4"
            if "sonnet" in model_setting:
                return "Sonnet 4"
    except Exception:
        pass

    if usage and usage.sessions:
        recent = usage.sessions[-1]
        if recent.opus_responses > recent.sonnet_responses:
            return "Opus 4"

    return "Sonnet 4"


def _resolve_project_path(payload: dict) -> str:
    """Pick the session's current directory from the stdin payload.

    Claude Code's main process keeps its original cwd even when the user
    cd's inside a session, so os.getcwd() is unreliable. The session's
    actual directory is delivered via stdin under workspace.current_dir
    (with cwd / projectPath as compatibility fallbacks).
    """
    workspace = payload.get("workspace") or {}
    return (
        workspace.get("current_dir")
        or payload.get("cwd")
        or payload.get("projectPath")
        or os.getcwd()
    )


def generate_status_line():
    """Generate status line output for Claude Code."""

    # --- Read Claude Code's stdin payload once (rate_limits + workspace) ---
    payload = _read_stdin_payload()
    rate_limits = payload.get("rate_limits") or {}
    stdin_5h = rate_limits.get("five_hour")
    stdin_7d = rate_limits.get("seven_day")

    # --- Project / git ---
    project_path = _resolve_project_path(payload)
    try:
        project_name = Path(project_path).name or "unknown"
        if project_name == "claude-code-usage-tracking":
            project_name = "usage-tracker"
    except Exception:
        project_name = "unknown"

    config = Config()
    tracker = UsageTracker()
    git_info = GitInfo(cache_duration=config.git_cache_duration)

    usage = tracker.update()

    # --- Probe cache (supplementary: sonnet/opus split, 10-min auto-refresh) ---
    # The cache is kept fresh by a long-running background daemon that polls
    # `claude /usage` every PROBE_TTL seconds even when no Claude session is
    # active. ensure_daemon() is idempotent — it costs a stat + tiny read when
    # the daemon is already running. This replaces the previous one-shot spawn
    # which only ran when a status line was being rendered.
    cache_path = _probe_cache_path()
    probe_mod.set_cache_path(cache_path)
    probe_mod.ensure_daemon(cache_path, _src_dir(), PROBE_TTL)
    cached, cached_at = probe_mod.read_cache()

    now = time.time()
    # The daemon polls `claude /usage` every PROBE_TTL seconds and is the sole
    # cache writer. Earlier versions also fired a one-shot `spawn_background_refresh`
    # whenever the cache looked stale, but that piled up multiple `claude /usage`
    # processes per status-line refresh (each one quite CPU-heavy) without
    # actually helping when the underlying probe was failing — the daemon's own
    # next tick covers the same case.

    # --- Fallback reset time from rolling window ---
    cycle_end = usage.current_5h_start + 5 * 3600
    time_remaining = cycle_end - now

    # --- Build status line ---
    parts = []

    # Project
    parts.append(f"📁 {project_name}")

    # Git
    if config.show_git_info:
        git_status = git_info.get_git_status(project_path)
        git_display = git_info.format_git_info(git_status)
        if git_display:
            parts.append(git_display)

    # Model — Claude Code sends the active model in stdin on every refresh;
    # this reflects the user's /model selection (including "Default") accurately.
    # Env var and settings.json are legacy fallbacks for non-CC contexts.
    current_model = _resolve_current_model(payload, usage)
    parts.append(f"🤖 {current_model}")

    # --- 5-hour session usage ---
    # Priority: stdin rate_limits → probe cache → local estimate
    if stdin_5h is not None and stdin_5h.get("used_percentage") is not None:
        pct = int(stdin_5h["used_percentage"])
        color = _pct_color(config, pct)
        prompt_count = usage.current_5h_prompts
        parts.append(
            f"\033[38;2;{color[0]};{color[1]};{color[2]}m"
            f"⚡{prompt_count}p·{pct}%"
            f"\033[0m"
        )
        # Reset countdown — prefer stdin's epoch, then parse probe text,
        # finally fall back to the local rolling-window estimate. Always
        # rendered as a countdown so the format stays the same whether
        # we're at session startup (probe cache) or steady-state (stdin).
        secs = _resolve_reset_secs(
            stdin_5h.get("resets_at"),
            cached.session_reset_text if cached else None,
            5 * 3600,
        )
        if secs is None:
            secs = time_remaining
        parts.append(f"🔄 {_format_countdown(secs)}")
    elif cached and cached.session_pct_used is not None:
        pct = cached.session_pct_used
        color = _pct_color(config, pct)
        prompt_count = usage.current_5h_prompts
        parts.append(
            f"\033[38;2;{color[0]};{color[1]};{color[2]}m"
            f"⚡{prompt_count}p·{pct}%"
            f"\033[0m"
        )
        secs = _resolve_reset_secs(None, cached.session_reset_text, 5 * 3600)
        if secs is None:
            secs = time_remaining
        parts.append(f"🔄 {_format_countdown(secs)}")
    else:
        # Fallback: local rolling-window estimate
        limits = config.get_tier_limits()
        pct_est = int((usage.current_5h_prompts / limits.cycle_5h_max) * 100) if limits.cycle_5h_max else 0
        color = config.get_usage_color(usage.current_5h_prompts, limits.cycle_5h_max)
        parts.append(
            f"\033[38;2;{color[0]};{color[1]};{color[2]}m"
            f"⚡{usage.current_5h_prompts}/{limits.cycle_5h_max}p({pct_est}%~)"
            f"\033[0m"
        )
        parts.append(f"🔄 {_format_countdown(time_remaining)}")

    # --- Weekly usage ---
    # Priority: stdin rate_limits (overall%) + probe cache (sonnet/opus split) → local estimate
    stdin_w_pct = int(stdin_7d["used_percentage"]) if stdin_7d and stdin_7d.get("used_percentage") is not None else None
    probe_w_pct = cached.weekly_pct_used if cached else None

    w_pct = stdin_w_pct if stdin_w_pct is not None else probe_w_pct

    if w_pct is not None:
        w_color = _pct_color(config, w_pct)
        weekly_str = (
            f"\033[38;2;{w_color[0]};{w_color[1]};{w_color[2]}m"
            f"📅 W:{w_pct}%"
            f"\033[0m"
        )
        # Sonnet/Opus split only from probe cache
        if cached and cached.sonnet_pct_used is not None:
            s_color = _pct_color(config, cached.sonnet_pct_used)
            weekly_str += (
                f" \033[38;2;{s_color[0]};{s_color[1]};{s_color[2]}m"
                f"S:{cached.sonnet_pct_used}%"
                f"\033[0m"
            )
        if cached and cached.opus_pct_used is not None:
            o_color = _pct_color(config, cached.opus_pct_used)
            weekly_str += (
                f" \033[38;2;{o_color[0]};{o_color[1]};{o_color[2]}m"
                f"O:{cached.opus_pct_used}%"
                f"\033[0m"
            )
        # Weekly reset countdown — stdin's numeric resets_at is preferred; if
        # missing (e.g. early in a session before any API call has populated
        # rate_limits), derive from the probe cache's "Resets May 4, 2pm" form
        # so the indicator stays consistent across data sources.
        weekly_secs = _resolve_reset_secs(
            stdin_7d.get("resets_at") if stdin_7d else None,
            cached.weekly_reset_text if cached else None,
            7 * 86400,
        )
        if weekly_secs is not None:
            weekly_str += f" ↻{_format_countdown(weekly_secs)}"
        parts.append(weekly_str)
    else:
        # Fallback: local session-hour estimate (labelled as approximate)
        limits = config.get_tier_limits()
        if config.tier in ["max_5x", "max_20x"]:
            s_color = config.get_usage_color(usage.weekly_sonnet_hours, limits.weekly_sonnet_max)
            o_color = config.get_usage_color(usage.weekly_opus_hours, limits.weekly_opus_max or 0)
            parts.append(
                f"\033[38;2;{s_color[0]};{s_color[1]};{s_color[2]}m"
                f"📅 S4:{usage.weekly_sonnet_hours:.1f}h~"
                f"\033[0m"
                f" \033[38;2;{o_color[0]};{o_color[1]};{o_color[2]}m"
                f"O4:{usage.weekly_opus_hours:.1f}h~"
                f"\033[0m"
            )
        else:
            color = config.get_usage_color(usage.weekly_sonnet_hours, limits.weekly_sonnet_max)
            parts.append(
                f"\033[38;2;{color[0]};{color[1]};{color[2]}m"
                f"📅 {usage.weekly_sonnet_hours:.1f}h/{limits.weekly_sonnet_max}h~"
                f"\033[0m"
            )

    print(" | ".join(parts))


if __name__ == "__main__":
    generate_status_line()
