#!/usr/bin/env python3
"""
admin_observer.py

Admin observer/logger for AI Submarine Brawl.

- Logs in as an *admin* user (SB_ADMIN_USER / SB_ADMIN_PASS env vars),
  or uses SB_ADMIN_API_KEY if already known.
- Periodically polls /admin/state to capture full world state
  (all subs + torpedoes).
- Optionally polls /perf for tick timing stats.
- Writes newline-delimited JSON records to a log file under ./logs/.

Each log line looks like:

  {
    "ts": 1710000000.123,
    "state": {...},     # response from /admin/state
    "perf": {...}       # optional /perf (may be {})
  }

Usage:

  python admin_observer.py http://localhost:5000

You can adjust polling interval with SB_OBSERVER_HZ (default 5.0 Hz).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional

import requests


class AdminClient:
    def __init__(self, base_url: str, api_key: str | None = None):
        self.base = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("SB_ADMIN_API_KEY") or ""

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        r = requests.get(url, params=params or {}, headers=self._headers(), timeout=10)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {}

    def _post(self, path: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        r = requests.post(url, data=json.dumps(data or {}), headers=self._headers(), timeout=10)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {}

    # --- auth ---

    def login_admin(self, username: str, password: str) -> str:
        """Login as admin user and store api_key."""
        resp = self._post("/login", {"username": username, "password": password})
        if not resp.get("ok"):
            raise RuntimeError(f"login failed for {username}: {resp}")
        key = resp.get("api_key")
        if not key:
            raise RuntimeError("login did not return api_key")
        self.api_key = key
        return key

    # --- admin endpoints ---

    def admin_state(self) -> Dict[str, Any]:
        return self._get("/admin/state")

    def perf(self) -> Dict[str, Any]:
        # /perf is public; ignore failures.
        try:
            return self._get("/perf")
        except Exception:
            return {}


def ensure_admin_client(base_url: str) -> AdminClient:
    client = AdminClient(base_url)

    # Try a persisted key first.
    state_path = os.path.join(os.path.dirname(__file__), "admin_observer_state.json")
    if not client.api_key and os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("base_url") == base_url and data.get("api_key"):
                client.api_key = data["api_key"]
                print(f"[observer] Loaded API key from {state_path}")
        except Exception:
            pass

    # If we still don't have a key, login using admin credentials.
    if not client.api_key:
        admin_user = os.environ.get("SB_ADMIN_USER")
        admin_pass = os.environ.get("SB_ADMIN_PASS")
        if not admin_user or not admin_pass:
            raise SystemExit(
                "No SB_ADMIN_API_KEY and no SB_ADMIN_USER/SB_ADMIN_PASS; "
                "set either env var to use the admin observer."
            )
        print(f"[observer] Logging in as admin user '{admin_user}'")
        key = client.login_admin(admin_user, admin_pass)
        print(f"[observer] Obtained admin api_key={key}")
        # Persist to disk for convenience.
        state_meta = {
            "base_url": base_url,
            "api_key": key,
            "username": admin_user,
            "created_at": time.time(),
        }
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state_meta, f, indent=2)
            print(f"[observer] Saved state to {state_path}")
        except Exception as e:
            print(f"[observer] Failed to write {state_path}: {e}")

    return client


def open_log_file() -> tuple[Any, str]:
    """Create logs/ directory and open a timestamped JSONL log file."""
    root = os.path.dirname(os.path.dirname(__file__))  # repo root
    logs_dir = os.path.join(root, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(logs_dir, f"admin_run_{stamp}.jsonl")
    f = open(path, "a", encoding="utf-8")
    print(f"[observer] Logging to {path}")
    return f, path


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: admin_observer.py BASE_URL (e.g. http://localhost:5000)", file=sys.stderr)
        raise SystemExit(1)

    base_url = sys.argv[1]
    client = ensure_admin_client(base_url)
    log_f, path = open_log_file()

    hz = float(os.environ.get("SB_OBSERVER_HZ", "5.0") or 5.0)
    if hz <= 0:
        hz = 5.0
    interval = 1.0 / hz
    print(f"[observer] Polling /admin/state at {hz:.1f} Hz (every {interval:.3f}s)")

    try:
        while True:
            ts = time.time()
            try:
                state = client.admin_state()
            except Exception as e:
                print(f"[observer] /admin/state error: {e}")
                state = {"ok": False, "error": str(e)}

            try:
                perf = client.perf()
            except Exception:
                perf = {}

            rec = {
                "ts": ts,
                "state": state,
                "perf": perf,
            }
            try:
                log_f.write(json.dumps(rec) + "\n")
                log_f.flush()
            except Exception as e:
                print(f"[observer] write error: {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[observer] Stopping observer.")
    finally:
        try:
            log_f.close()
        except Exception:
            pass
        print(f"[observer] Log closed: {path}")


if __name__ == "__main__":
    main()
