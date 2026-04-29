#!/usr/bin/env python3
"""
Status line generator for Claude Code integration.
Reads JSON input from stdin and outputs formatted status line.

Usage data priority:
  1. Native rate_limits from Claude Code stdin (most accurate, updates on each API request)
  2. Real Anthropic data from `claude /usage` probe cache (refreshes every 10 min)
  3. Local JSONL token counts as fallback (approximate)

The probe runs in the background every 10 minutes to keep data fresh even
during idle periods. Sonnet/Opus split is only available from the probe.
"""

import json
import os
import sys
import time
from pathlib import Path

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

PROBE_TTL = 600  # seconds before triggering a background refresh (10 minutes)


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


def _format_reset(reset_text: str | None, time_remaining_sec: float) -> str:
    """Return a formatted reset string, preferring the probe's reset time."""
    if reset_text:
        # Extract just the time part, e.g. "11:50am" from "Resets 11:50am (Asia/Shanghai)"
        import re
        m = re.search(r"(\d{1,2}:\d{2}[ap]m)", reset_text, re.IGNORECASE)
        if m:
            return m.group(1)
        # Monthly reset e.g. "May 4, 2pm"
        m = re.search(r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+,\s*\d+(?::\d+)?[ap]m)",
                      reset_text, re.IGNORECASE)
        if m:
            return m.group(1)
        return reset_text
    # Fall back to countdown
    if time_remaining_sec <= 0:
        return "now"
    h = int(time_remaining_sec // 3600)
    m = int((time_remaining_sec % 3600) // 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _read_stdin_rate_limits():
    """Read and parse rate_limits from Claude Code's stdin JSON.

    Returns (five_hour, seven_day) dicts or (None, None) if unavailable.
    """
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                data = json.loads(raw)
                rl = data.get("rate_limits", {})
                return rl.get("five_hour"), rl.get("seven_day")
    except Exception:
        pass
    return None, None


def generate_status_line():
    """Generate status line output for Claude Code."""

    # --- Native rate_limits from Claude Code stdin ---
    stdin_5h, stdin_7d = _read_stdin_rate_limits()

    # --- Project / git ---
    try:
        project_path = os.getcwd()
        project_name = Path(project_path).name
        if project_name == "claude-code-usage-tracking":
            project_name = "usage-tracker"
    except Exception:
        project_name = "unknown"

    config = Config()
    tracker = UsageTracker()
    git_info = GitInfo(cache_duration=config.git_cache_duration)

    usage = tracker.update()

    # --- Probe cache (supplementary: sonnet/opus split, 10-min auto-refresh) ---
    cache_path = _probe_cache_path()
    probe_mod.set_cache_path(cache_path)
    cached, cached_at = probe_mod.read_cache()

    now = time.time()
    if cached_at is None or now - cached_at > PROBE_TTL:
        probe_mod.spawn_background_refresh(cache_path, _src_dir())

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

    # Model
    current_model = "Sonnet 4"
    claude_model = os.environ.get("CLAUDE_MODEL", "").lower()
    if "opus" in claude_model:
        current_model = "Opus 4"
    elif "sonnet" in claude_model:
        current_model = "Sonnet 4"
    else:
        try:
            settings_path = Path.home() / ".claude" / "settings.json"
            if settings_path.exists():
                with open(settings_path, encoding="utf-8") as f:
                    s = json.load(f)
                model_setting = s.get("model", "").lower()
                if "opus" in model_setting:
                    current_model = "Opus 4"
                elif "sonnet" in model_setting:
                    current_model = "Sonnet 4"
        except Exception:
            if usage.sessions:
                recent = usage.sessions[-1]
                if recent.opus_responses > recent.sonnet_responses:
                    current_model = "Opus 4"
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
        # Reset time from stdin resets_at, fall back to probe text or countdown
        resets_at = stdin_5h.get("resets_at")
        if resets_at:
            secs_left = resets_at - now
            reset_str = _format_reset(None, secs_left)
        else:
            reset_str = _format_reset(
                cached.session_reset_text if cached else None, time_remaining
            )
        parts.append(f"🔄 {reset_str}")
    elif cached and cached.session_pct_used is not None:
        pct = cached.session_pct_used
        color = _pct_color(config, pct)
        prompt_count = usage.current_5h_prompts
        parts.append(
            f"\033[38;2;{color[0]};{color[1]};{color[2]}m"
            f"⚡{prompt_count}p·{pct}%"
            f"\033[0m"
        )
        reset_str = _format_reset(cached.session_reset_text, time_remaining)
        parts.append(f"🔄 {reset_str}")
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
        parts.append(f"🔄 {_format_reset(None, time_remaining)}")

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
        # Reset: prefer stdin resets_at, then probe reset text
        w_resets_at = stdin_7d.get("resets_at") if stdin_7d else None
        if w_resets_at:
            weekly_str += f" ↻{_format_reset(None, w_resets_at - now)}"
        elif cached and cached.weekly_reset_text:
            weekly_str += f" ↻{_format_reset(cached.weekly_reset_text, 0)}"
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
