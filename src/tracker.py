#!/usr/bin/env python3
"""
Main tracker module for Claude Code usage tracking.

Stdlib-only implementation: ships with the venv that `install.py` creates and
needs no third-party packages, which keeps per-status-line interpreter startup
under 1s even on cold caches.
"""

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

@dataclass
class SessionData:
    """Data class for session information."""
    session_id: str
    start_time: float
    end_time: float
    duration_hours: float
    prompt_count: int
    sonnet_responses: int
    opus_responses: int
    project: str
    prompt_timestamps: List[float] = None

    def __post_init__(self):
        if self.prompt_timestamps is None:
            self.prompt_timestamps = []

@dataclass
class UsageData:
    """Complete usage data structure."""
    current_5h_prompts: int
    current_5h_start: float
    weekly_sonnet_hours: float
    weekly_opus_hours: float
    weekly_prompts: int
    weekly_start: float
    last_updated: float
    sessions: List[SessionData]

class UsageTracker:
    """Main usage tracker with optimized performance."""
    
    def __init__(self, config_path: Optional[Path] = None):
        """Initialize the tracker with configuration."""
        self.home = Path.home()
        self.claude_projects = self.home / ".claude" / "projects"
        self.config_path = config_path or Path(__file__).parent.parent / "config"
        self.data_path = Path(__file__).parent.parent / "data"
        
        # Ensure data directory exists
        self.data_path.mkdir(exist_ok=True)
        
        # Time constants (cycle start is computed per-update from rolling window)
        self.week_start = self._get_week_start()
        self.cycle_5h_start = 0.0
        
        # Cache for parsed data
        self._cache = {}
        self._cache_time = 0
        self.cache_duration = 5  # Cache for 5 seconds
    
    def _get_week_start(self) -> float:
        """Get Monday midnight timestamp in seconds."""
        now = datetime.now()
        days_since_monday = now.weekday()
        monday = now - timedelta(days=days_since_monday)
        monday_midnight = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        return monday_midnight.timestamp()
    
    def _compute_rolling_5h_window(self, prompt_timestamps: List[float]) -> Tuple[float, int]:
        """Find the current rolling 5-hour window.

        Matches Anthropic's behavior: a window opens at the first prompt and lasts 5
        hours; any prompt arriving after the window closes opens a fresh one. Returns
        ``(window_start, prompts_in_window)``. If no window is currently active (the
        most recent prompt is older than 5 hours), returns ``(now, 0)`` so the next
        prompt will be treated as the start of a new window.
        """
        window_size = 5 * 3600
        now = time.time()

        if not prompt_timestamps:
            return (now, 0)

        sorted_ts = sorted(prompt_timestamps)
        window_start = sorted_ts[0]
        count = 0
        for ts in sorted_ts:
            if ts >= window_start + window_size:
                window_start = ts
                count = 1
            else:
                count += 1

        if window_start + window_size <= now:
            return (now, 0)
        return (window_start, count)
    
    def _parse_timestamp(self, ts: str) -> Optional[float]:
        """Parse ISO timestamp to epoch seconds efficiently."""
        if not ts or ts == 'null':
            return None
        try:
            # Remove milliseconds for faster parsing
            clean_ts = ts.split('.')[0] + 'Z' if '.' in ts else ts
            dt = datetime.fromisoformat(clean_ts.replace('Z', '+00:00'))
            return dt.timestamp()
        except:
            return None
    
    def _is_command_message(self, content) -> bool:
        """Check if message is a local command."""
        if isinstance(content, str):
            return '<command-name>' in content or '<local-command-stdout>' in content
        elif isinstance(content, list):
            # Handle array content like [{"type":"text","text":"..."}]
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    text = item.get('text', '')
                    if '<command-name>' in text or '<local-command-stdout>' in text:
                        return True
        return False
    
    def _analyze_jsonl_file(self, jsonl_path: Path) -> SessionData:
        """Analyze a single JSONL file (session) with caching."""
        # Check cache
        cache_key = str(jsonl_path)
        if cache_key in self._cache and (time.time() - self._cache_time) < self.cache_duration:
            return self._cache[cache_key]
        
        timestamps = []
        prompt_timestamps = []
        prompts = 0
        sonnet_responses = 0
        opus_responses = 0

        try:
            with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        msg = json.loads(line)

                        # Collect timestamp
                        ts_epoch = None
                        if ts := msg.get('timestamp'):
                            ts_epoch = self._parse_timestamp(ts)
                            if ts_epoch:
                                timestamps.append(ts_epoch)

                        # Count user prompts (excluding commands and meta messages)
                        if (msg.get('type') == 'user' and
                            msg.get('message', {}).get('role') == 'user' and
                            not msg.get('isMeta', False) and
                            msg.get('userType') == 'external'):  # Only external user messages

                            content = msg.get('message', {}).get('content', '')
                            # Skip empty content and command messages
                            if content and not self._is_command_message(content):
                                prompts += 1
                                if ts_epoch:
                                    prompt_timestamps.append(ts_epoch)

                        # Count model responses
                        elif msg.get('type') == 'assistant':
                            model = msg.get('message', {}).get('model', '').lower()
                            if 'opus' in model:
                                opus_responses += 1
                            elif 'sonnet' in model:
                                sonnet_responses += 1
                    except:
                        continue
        except:
            pass
        
        # Calculate session duration
        duration_hours = 0.0
        start_time = 0.0
        end_time = 0.0
        
        if timestamps:
            start_time = min(timestamps)
            end_time = max(timestamps)
            duration_hours = (end_time - start_time) / 3600
        
        session = SessionData(
            session_id=jsonl_path.stem,
            start_time=start_time,
            end_time=end_time,
            duration_hours=duration_hours,
            prompt_count=prompts,
            sonnet_responses=sonnet_responses,
            opus_responses=opus_responses,
            project=jsonl_path.parent.name,
            prompt_timestamps=prompt_timestamps,
        )
        
        # Update cache
        self._cache[cache_key] = session
        self._cache_time = time.time()
        
        return session
    
    def get_all_sessions(self) -> List[SessionData]:
        """Get all sessions across all projects."""
        sessions = []
        
        if not self.claude_projects.exists():
            return sessions
        
        # Process all projects in parallel-friendly way
        for project_dir in self.claude_projects.iterdir():
            if project_dir.is_dir():
                for jsonl_file in project_dir.glob('*.jsonl'):
                    session = self._analyze_jsonl_file(jsonl_file)
                    # Keep any session with prompts or measurable duration; the
                    # rolling 5h window relies on every prompt timestamp, even
                    # from short single-message sessions.
                    if session.prompt_count > 0 or session.duration_hours > 0:
                        sessions.append(session)

        return sessions
    
    def calculate_usage(self) -> UsageData:
        """Calculate complete usage statistics."""
        sessions = self.get_all_sessions()

        # Filter weekly sessions by start time (Monday 00:00 local)
        week_sessions = [s for s in sessions if s.start_time >= self.week_start]

        # Compute the rolling 5h window from every prompt timestamp seen so far.
        all_prompt_ts = []
        for s in sessions:
            all_prompt_ts.extend(s.prompt_timestamps)
        cycle_start, cycle_prompts = self._compute_rolling_5h_window(all_prompt_ts)
        self.cycle_5h_start = cycle_start

        weekly_prompts = sum(s.prompt_count for s in week_sessions)
        
        # Calculate model-specific hours
        sonnet_hours = 0.0
        opus_hours = 0.0
        
        for session in week_sessions:
            total_responses = session.sonnet_responses + session.opus_responses
            if total_responses > 0:
                sonnet_ratio = session.sonnet_responses / total_responses
                opus_ratio = session.opus_responses / total_responses
                sonnet_hours += session.duration_hours * sonnet_ratio
                opus_hours += session.duration_hours * opus_ratio
        
        return UsageData(
            current_5h_prompts=cycle_prompts,
            current_5h_start=cycle_start,
            weekly_sonnet_hours=round(sonnet_hours, 2),
            weekly_opus_hours=round(opus_hours, 2),
            weekly_prompts=weekly_prompts,
            weekly_start=self.week_start,
            last_updated=time.time(),
            sessions=week_sessions
        )
    
    def save_usage_data(self, usage_data: UsageData):
        """Save usage data to JSON file."""
        data = {
            "current_5h_cycle": {
                "start_time": int(usage_data.current_5h_start * 1000),
                "total_prompts": usage_data.current_5h_prompts,
                "total_hours": round(usage_data.current_5h_prompts / 10, 2)  # Legacy field
            },
            "current_week": {
                "start_time": int(usage_data.weekly_start * 1000),
                "sonnet4_hours": usage_data.weekly_sonnet_hours,
                "opus4_hours": usage_data.weekly_opus_hours,
                "total_sessions": len(usage_data.sessions)
            },
            "last_updated": int(usage_data.last_updated * 1000)
        }

        with open(self.data_path / "usage_data.json", 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    # --- Cross-process disk cache --------------------------------------------
    # `update()` is invoked on every Claude Code status-line refresh (sub-second
    # cadence). A full scan parses every JSONL under ~/.claude/projects/, which
    # is hundreds of files for a real user — concurrent status-line processes
    # pile up faster than they can finish. Memoizing the computed result on disk
    # for a short TTL collapses repeated refreshes into a single small read.
    UPDATE_CACHE_FILE = "tracker_cache.json"
    UPDATE_LOCK_FILE = "tracker_cache.lock"
    # Server-side rate_limits (stdin) / `claude /usage` probe are the
    # authoritative source for percentages and reset times. The tracker only
    # contributes the local prompt count + fallback estimates, so a coarse 60s
    # TTL is fine and slashes the per-render scan cost.
    UPDATE_CACHE_TTL = 60.0  # seconds — fresh-cache window
    UPDATE_LOCK_TTL = 120.0  # seconds — auto-recover from a crashed refresher

    def _update_cache_path(self) -> Path:
        return self.data_path / self.UPDATE_CACHE_FILE

    def _update_lock_path(self) -> Path:
        return self.data_path / self.UPDATE_LOCK_FILE

    def _read_disk_cached_usage(self, allow_stale: bool = False
                                ) -> Tuple[Optional[UsageData], float]:
        """Return (cached, age_seconds). cached is None if file missing/corrupt.

        When allow_stale=False, a cache older than UPDATE_CACHE_TTL is treated
        as missing (returns (None, age)) so callers can decide whether to
        recompute or fall back to the stale copy themselves.
        """
        cache_file = self._update_cache_path()
        try:
            mtime = cache_file.stat().st_mtime
        except OSError:
            return None, float("inf")
        age = time.time() - mtime
        if not allow_stale and age > self.UPDATE_CACHE_TTL:
            return None, age
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None, age
        try:
            usage = UsageData(
                current_5h_prompts=d['current_5h_prompts'],
                current_5h_start=d['current_5h_start'],
                weekly_sonnet_hours=d['weekly_sonnet_hours'],
                weekly_opus_hours=d['weekly_opus_hours'],
                weekly_prompts=d['weekly_prompts'],
                weekly_start=d['weekly_start'],
                last_updated=d['last_updated'],
                sessions=[],  # Sessions list is only used as a last-resort
                              # fallback for model detection; status line now
                              # gets the model from stdin, so dropping it keeps
                              # the cache file tiny.
            )
        except KeyError:
            return None, age
        return usage, age

    def _write_disk_cached_usage(self, usage_data: UsageData) -> None:
        cache_file = self._update_cache_path()
        payload = {
            'current_5h_prompts': usage_data.current_5h_prompts,
            'current_5h_start': usage_data.current_5h_start,
            'weekly_sonnet_hours': usage_data.weekly_sonnet_hours,
            'weekly_opus_hours': usage_data.weekly_opus_hours,
            'weekly_prompts': usage_data.weekly_prompts,
            'weekly_start': usage_data.weekly_start,
            'last_updated': usage_data.last_updated,
        }
        tmp = cache_file.with_suffix('.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
            os.replace(tmp, cache_file)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def _try_acquire_refresh_lock(self) -> bool:
        """Atomic single-flight lock for cache refresh.

        Uses O_CREAT|O_EXCL on a sentinel file so it works across macOS, Linux
        and Windows without fcntl. A stale lock (older than UPDATE_LOCK_TTL —
        e.g. the previous holder crashed mid-scan) is reaped before retrying.
        """
        lock_path = self._update_lock_path()
        try:
            mtime = lock_path.stat().st_mtime
            if (time.time() - mtime) > self.UPDATE_LOCK_TTL:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
        except OSError:
            pass

        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True

    def _release_refresh_lock(self) -> None:
        try:
            self._update_lock_path().unlink()
        except OSError:
            pass

    def update(self) -> UsageData:
        """Update and return current usage data.

        Refresh policy:
          * Fresh cache (<= TTL)            → return as-is.
          * Stale cache + lock acquired     → recompute, write, return fresh.
          * Stale cache + lock held by peer → return the stale copy (the peer
            will refresh it shortly; this prevents a thundering-herd of
            concurrent full scans when the TTL expires under refresh storm).
          * No cache + lock acquired        → recompute (first-ever run).
          * No cache + lock held by peer    → fall back to a fresh scan
            ourselves; we have nothing to serve. Rare.
        """
        fresh, _ = self._read_disk_cached_usage(allow_stale=False)
        if fresh is not None:
            return fresh

        if not self._try_acquire_refresh_lock():
            stale, _ = self._read_disk_cached_usage(allow_stale=True)
            if stale is not None:
                return stale
            # No cache to fall back on → scan inline (rare cold path).
            return self._compute_and_persist()

        try:
            return self._compute_and_persist()
        finally:
            self._release_refresh_lock()

    def _compute_and_persist(self) -> UsageData:
        usage_data = self.calculate_usage()
        self.save_usage_data(usage_data)
        self._write_disk_cached_usage(usage_data)
        return usage_data