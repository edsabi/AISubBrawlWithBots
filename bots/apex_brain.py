#!/usr/bin/env python3
"""
apex_brain.py

Single-file "apex brain" bot for AI Submarine Brawl.

- Handles its own auth (signup/login) and state.
- Spawns up to MAX_SUBS submarines for one user.
- Listens to /stream SSE for contacts + torpedo events.
- Builds simple bearing-only hostile tracks from passive sonar.
- Performs torpedo evasion maneuvers.
- Navigates in formation and hunts a fused hostile target.
- Fires torpedoes aggressively or conservatively depending on posture.

Drop this into your repo (e.g. bots/apex_brain.py) and run:

    python bots/apex_brain.py http://localhost:5000

You can tune behavior in an optional brain_config.json sitting next
to this file, e.g.:

{
  "formation_spacing_m": 200.0,
  "default_throttle": 0.6,
  "cruise_depth_m": 70.0,
  "posture": "aggressive",   // "ultra_quiet", "balanced", "aggressive"
  "auto_fire": true
}

This file is intentionally self-contained: it does not import any
other project modules.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


# ----------------- Logging helpers -----------------


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] [apex] {msg}"
    print(line)
    try:
        root_dir = os.path.dirname(os.path.dirname(__file__))
    except Exception:
        root_dir = os.path.dirname(__file__)
    log_path = os.path.join(root_dir, "apex_brain.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def compass_deg_from_rad(rad: float) -> float:
    """World radians (0 = east, CCW+) -> compass degrees (0 = north, CW+)."""
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


def bearing_rad_between(ax: float, ay: float, bx: float, by: float) -> float:
    """Return world-radians bearing from A to B (0 = east, CCW+)."""
    return math.atan2(by - ay, bx - ax)


def distance(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(bx - ax, by - ay)


# ----------------- HTTP client -----------------


class SubBrawlClient:
    def __init__(self, base_url: str, api_key: Optional[str] = None):
        self.base = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("SB_API_KEY") or ""

    # --- low-level helpers ---

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

    # --- auth / setup ---

    def set_api_key(self, key: str) -> None:
        self.api_key = key

    def signup(self, username: str, password: str) -> Dict[str, Any]:
        return self._post("/signup", {"username": username, "password": password})

    def login(self, username: str, password: str) -> Dict[str, Any]:
        return self._post("/login", {"username": username, "password": password})

    # --- game API ---

    def get_rules(self) -> Dict[str, Any]:
        return self._get("/rules")

    def get_state(self) -> Dict[str, Any]:
        return self._get("/state")

    def register_sub(self) -> Dict[str, Any]:
        return self._post("/register_sub", {})

    def set_sub_heading(self, sub_id: str, heading_deg: float) -> Dict[str, Any]:
        return self._post(f"/set_sub_heading/{sub_id}", {"heading_deg": float(heading_deg)})

    def control_sub(
        self,
        sub_id: str,
        throttle: Optional[float] = None,
        planes: Optional[float] = None,
        target_depth: Optional[float] = None,
        rudder_deg: Optional[float] = None,
        rudder_nudge_deg: Optional[float] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if throttle is not None:
            body["throttle"] = float(throttle)
        if planes is not None:
            body["planes"] = float(planes)
        if target_depth is not None:
            body["target_depth"] = float(target_depth)
        if rudder_deg is not None:
            body["rudder_deg"] = float(rudder_deg)
        if rudder_nudge_deg is not None:
            body["rudder_nudge_deg"] = float(rudder_nudge_deg)
        return self._post(f"/control/{sub_id}", body)

    def toggle_snorkel(self, sub_id: str, on: Optional[bool] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if on is not None:
            body["on"] = bool(on)
        else:
            body["toggle"] = True
        return self._post(f"/snorkel/{sub_id}", body)

    def emergency_blow(self, sub_id: str) -> Dict[str, Any]:
        return self._post(f"/emergency_blow/{sub_id}", {})

    def launch_torpedo(self, sub_id: str, torp_range_m: float) -> Dict[str, Any]:
        return self._post(f"/launch_torpedo/{sub_id}", {"range": float(torp_range_m)})

    def set_torp_depth(self, torp_id: str, depth: float) -> Dict[str, Any]:
        return self._post(f"/set_torp_depth/{torp_id}", {"depth": float(depth)})

    def set_torp_target_heading(self, torp_id: str, heading_deg: float) -> Dict[str, Any]:
        return self._post(f"/set_torp_target_heading/{torp_id}", {"heading_deg": float(heading_deg)})

    def torp_ping_toggle(self, torp_id: str) -> Dict[str, Any]:
        # Optional convenience for enabling torpedo active sonar.
        try:
            return self._post(f"/torp_ping_toggle/{torp_id}", {})
        except Exception:
            return {}

    def reload_torpedoes(self, sub_id: str) -> Dict[str, Any]:
        # Some versions of the server may not implement this; ignore failures.
        try:
            return self._post(f"/reload_torpedoes/{sub_id}", {})
        except Exception:
            return {}


# ----------------- Config + globals -----------------


PASSIVE_TTL = 60.0

# Filled by SSE listener:
passive_contacts: List[Dict[str, Any]] = []
torp_ping_contacts: List[Dict[str, Any]] = []
echo_contacts: List[Dict[str, Any]] = []

# Hostile tracking:
hostile_trackers: Dict[str, "PassiveTracker"] = {}
hostile_tracks: Dict[str, Dict[str, Any]] = {}
current_hostile_target: Dict[str, Any] = {}
has_fired_for_target: bool = False
current_shot: Dict[str, Any] = {}
_last_torp_range_class: Dict[Tuple[str, str], str] = {}

# Game config (from /rules); defaults if /rules is unavailable:
GAME_RULES: Dict[str, Any] = {}
SNORKEL_DEPTH: float = 15.0
RING_R: float = 6000.0
TORP_MAX_RANGE: float = 6000.0


def load_brain_config() -> Dict[str, Any]:
    """
    Load brain configuration from brain_config.json (optional).
    Re-read each loop so you can tweak at runtime.
    """
    here = os.path.dirname(__file__)
    cfg_path = os.path.join(here, "brain_config.json")
    cfg: Dict[str, Any] = {
        "formation_spacing_m": 200.0,
        "default_throttle": 0.4,
        "cruise_depth_m": 80.0,
        "posture": "balanced",  # "ultra_quiet", "balanced", "aggressive"
        "auto_fire": True,
    }
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            disk = json.load(f)
        if isinstance(disk, dict):
            cfg.update(disk)
    except Exception:
        pass
    return cfg


# ----------------- Passive tracking -----------------


@dataclass
class BearingSample:
    obs_x: float
    obs_y: float
    bearing_rad: float
    weight: float = 1.0
    t: float = time.time()


class PassiveTracker:
    """
    Lightweight bearing-only tracker.

    For each passive bearing from an observer, we store a BearingSample.
    The estimate_position() method intersects all pairs of bearings and
    returns the average intersection point.
    """

    def __init__(self) -> None:
        self.samples: List[BearingSample] = []

    def add_sample(self, obs_x: float, obs_y: float, bearing_rad: float, weight: float = 1.0) -> None:
        self.samples.append(BearingSample(obs_x, obs_y, bearing_rad, weight, time.time()))
        # Limit history a bit
        if len(self.samples) > 40:
            self.samples = self.samples[-40:]

    def estimate_position(self) -> Optional[Tuple[float, float]]:
        n = len(self.samples)
        if n < 2:
            return None
        pts: List[Tuple[float, float]] = []
        for i in range(n):
            for j in range(i + 1, n):
                s1 = self.samples[i]
                s2 = self.samples[j]
                inter = _intersect_two_bearings(
                    s1.obs_x,
                    s1.obs_y,
                    s1.bearing_rad,
                    s2.obs_x,
                    s2.obs_y,
                    s2.bearing_rad,
                )
                if inter is not None:
                    pts.append(inter)
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return sum(xs) / len(xs), sum(ys) / len(ys)


def _intersect_two_bearings(
    p1x: float, p1y: float, b1_rad: float, p2x: float, p2y: float, b2_rad: float
) -> Optional[Tuple[float, float]]:
    """
    Intersection of two infinite bearing lines; returns (x, y) or None if nearly parallel.
    """
    u1x = math.cos(b1_rad)
    u1y = math.sin(b1_rad)
    u2x = math.cos(b2_rad)
    u2y = math.sin(b2_rad)

    dx = p2x - p1x
    dy = p2y - p1y

    a11 = u1x
    a12 = -u2x
    a21 = u1y
    a22 = -u2y

    det = a11 * a22 - a12 * a21
    if abs(det) < 1e-6:
        return None

    inv11 = a22 / det
    inv12 = -a12 / det

    t1 = inv11 * dx + inv12 * dy
    ix = p1x + t1 * u1x
    iy = p1y + t1 * u1y
    return ix, iy


def is_friendly_contact(
    observer_sub: Dict[str, Any],
    contact_bearing_rad: float,
    range_class: str,
    friendly_subs: List[Dict[str, Any]],
    bearing_tolerance_deg: float = 30.0,
) -> bool:
    """
    Heuristic friendly filter: if a known friendly lies roughly along the
    contact bearing at a plausible range, treat the contact as friendly.
    """
    ox = float(observer_sub.get("x", 0.0) or 0.0)
    oy = float(observer_sub.get("y", 0.0) or 0.0)

    rc = (range_class or "").lower()
    if rc == "short":
        max_dist = 2000.0
        tol_deg = min(20.0, bearing_tolerance_deg)
    elif rc == "medium":
        max_dist = 5000.0
        tol_deg = min(25.0, bearing_tolerance_deg)
    else:
        max_dist = 8000.0
        tol_deg = bearing_tolerance_deg

    tol_rad = math.radians(tol_deg)

    for fs in friendly_subs:
        fid = fs.get("id")
        if fid == observer_sub.get("id"):
            continue
        fx = float(fs.get("x", 0.0) or 0.0)
        fy = float(fs.get("y", 0.0) or 0.0)
        d = distance(ox, oy, fx, fy)
        if d <= 0.0 or d > max_dist:
            continue
        brg_to_friend = bearing_rad_between(ox, oy, fx, fy)
        ang = abs((contact_bearing_rad - brg_to_friend + math.pi) % (2.0 * math.pi) - math.pi)
        if ang <= tol_rad:
            return True
    return False


# ----------------- SSE listener -----------------


def _sse_listener(base_url: str, api_key: str) -> None:
    """
    Background thread: connect to /stream and ingest events into global lists.
    """
    url = f"{base_url.rstrip('/')}/stream"
    params = {"api_key": api_key}
    log(f"SSE listener connecting to {url} ...")

    while True:
        try:
            with requests.get(url, params=params, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                event_type: Optional[str] = None
                data_lines: List[str] = []

                for raw in resp.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    line = raw.strip()
                    if not line:
                        if event_type and data_lines:
                            raw_data = "\n".join(data_lines)
                            try:
                                payload = json.loads(raw_data)
                            except Exception:
                                payload = None
                            _handle_sse_event(event_type, payload)
                        event_type = None
                        data_lines = []
                        continue

                    if line.startswith("event:"):
                        event_type = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:") :].strip())
        except Exception as e:
            log(f"SSE listener error: {e}; reconnecting in 3s")
            time.sleep(3.0)


def _handle_sse_event(event_type: str, payload: Any) -> None:
    now = time.time()

    if event_type == "contact" and isinstance(payload, dict):
        c = dict(payload)
        if c.get("type") in ("passive", "active_ping_detected"):
            c["time"] = now
            if c.get("type") == "active_ping_detected":
                c["contact_type"] = "ping"
            passive_contacts.insert(0, c)
            del passive_contacts[160:]
    elif event_type == "torpedo_contact" and isinstance(payload, dict):
        c = dict(payload)
        c["time"] = now
        c["contact_type"] = "torpedo_" + str(c.get("contact_type", "") or "")
        passive_contacts.insert(0, c)
        del passive_contacts[160:]
    elif event_type == "torpedo_ping" and isinstance(payload, dict):
        torp_id = payload.get("torpedo_id")
        contacts = payload.get("contacts") or []
        for contact in contacts:
            try:
                torp_ping_contacts.insert(
                    0,
                    {
                        "torpedo_id": torp_id,
                        "bearing": contact.get("bearing"),
                        "range": contact.get("range"),
                        "depth": contact.get("depth"),
                        "time": now,
                    },
                )
            except Exception:
                continue
        del torp_ping_contacts[120:]
    elif event_type == "echo" and isinstance(payload, dict):
        e = dict(payload)
        e["time"] = now
        echo_contacts.insert(0, e)
        del echo_contacts[80:]

    # TTL trim
    cutoff = now - PASSIVE_TTL
    while passive_contacts and passive_contacts[-1].get("time", 0) < cutoff:
        passive_contacts.pop()
    while echo_contacts and echo_contacts[-1].get("time", 0) < cutoff:
        echo_contacts.pop()


# ----------------- Hostile tracking -----------------


def update_hostile_tracks(state: Dict[str, Any], controlled_ids: List[str]) -> None:
    """
    Build simple bearing-only hostile tracks per observer sub based on recent
    passive contacts, then fuse into a single global current_hostile_target.
    """
    subs = state.get("subs") or []
    by_id: Dict[str, Dict[str, Any]] = {s["id"]: s for s in subs if s.get("id")}
    controlled_set = set(controlled_ids or [])

    now = time.time()
    cutoff = now - 30.0

    hostile_trackers.clear()
    hostile_tracks.clear()

    for c in passive_contacts:
        if c.get("time", 0) < cutoff:
            break
        if c.get("type") != "passive":
            continue
        obs_id = c.get("observer_sub_id")
        if not obs_id or obs_id not in by_id:
            continue
        obs = by_id[obs_id]

        bearing_rad = float(c.get("bearing", 0.0) or 0.0)
        range_class = str(c.get("range_class", "") or "").lower()

        # Extra friendly guard for our own controlled wingman.
        if obs_id in controlled_set and controlled_ids:
            skipped_for_friend = False
            for friend_id in controlled_ids:
                if friend_id == obs_id or friend_id not in by_id:
                    continue
                friend = by_id[friend_id]
                fx = float(friend.get("x", 0.0) or 0.0)
                fy = float(friend.get("y", 0.0) or 0.0)
                ox = float(obs.get("x", 0.0) or 0.0)
                oy = float(obs.get("y", 0.0) or 0.0)
                d = distance(ox, oy, fx, fy)
                if d <= 0.0:
                    continue
                brg_to_friend = bearing_rad_between(ox, oy, fx, fy)
                ang = abs((bearing_rad - brg_to_friend + math.pi) % (2.0 * math.pi) - math.pi)

                if range_class == "short":
                    max_ang = math.radians(25.0)
                    max_dist = 1500.0
                elif range_class == "medium":
                    max_ang = math.radians(35.0)
                    max_dist = 4000.0
                else:
                    max_ang = math.radians(45.0)
                    max_dist = 8000.0

                if d <= max_dist and ang <= max_ang:
                    log(
                        f"Skipping friendly bearing from {obs_id[:6]} toward wingman {friend_id[:6]} "
                        f"(rc={range_class or '?'}, dist={d:.0f}m, ang={math.degrees(ang):.0f}°)"
                    )
                    skipped_for_friend = True
                    break
            if skipped_for_friend:
                continue

        if is_friendly_contact(
            observer_sub=obs,
            contact_bearing_rad=bearing_rad,
            range_class=range_class,
            friendly_subs=subs,
            bearing_tolerance_deg=30.0,
        ):
            continue

        tracker = hostile_trackers.get(obs_id)
        if tracker is None:
            tracker = PassiveTracker()
            hostile_trackers[obs_id] = tracker

        tracker.add_sample(
            obs_x=float(obs.get("x", 0.0) or 0.0),
            obs_y=float(obs.get("y", 0.0) or 0.0),
            bearing_rad=bearing_rad,
            weight=1.0,
        )

    for obs_id, tracker in hostile_trackers.items():
        est = tracker.estimate_position()
        if est is None:
            continue
        x, y = est
        obs = by_id.get(obs_id)
        if obs:
            ox = float(obs.get("x", 0.0) or 0.0)
            oy = float(obs.get("y", 0.0) or 0.0)
            dist_obs = distance(ox, oy, x, y)
            if dist_obs < 800.0:
                log(
                    f"TRACK[{obs_id[:6]}]: discarding degenerate est=({x:.0f},{y:.0f}) "
                    f"near obs=({ox:.0f},{oy:.0f}) (dist={dist_obs:.0f}m)"
                )
                continue

        hostile_tracks[obs_id] = {
            "x": x,
            "y": y,
            "updated_at": now,
            "sample_count": len(tracker.samples),
        }
        if obs:
            last_sample = tracker.samples[-1]
            brg_deg = compass_deg_from_rad(last_sample.bearing_rad)
            log(
                f"TRACK[{obs_id[:6]}]: est=({x:.0f},{y:.0f}) from {len(tracker.samples)} bearings; "
                f"last_brg={brg_deg:.0f}° at obs=({ox:.0f},{oy:.0f})"
            )

    global has_fired_for_target
    if hostile_tracks:
        xs = [t["x"] for t in hostile_tracks.values()]
        ys = [t["y"] for t in hostile_tracks.values()]
        current_hostile_target.clear()
        current_hostile_target.update(
            {"x": sum(xs) / len(xs), "y": sum(ys) / len(ys), "updated_at": now}
        )
        has_fired_for_target = False
    else:
        current_hostile_target.clear()
        has_fired_for_target = False


# ----------------- Torpedo evasion -----------------


def maybe_evade_torpedoes(
    client: SubBrawlClient,
    sub: Dict[str, Any],
    danger_range_m: float = 2000.0,
    max_depth_step_m: float = 60.0,
) -> bool:
    """
    Use recent torpedo_contact events to perform evasive maneuvers.

    Returns True if an evasive command was issued for this sub on this tick.
    """
    sid = sub.get("id")
    if not sid:
        return False

    now = time.time()
    cutoff = now - 10.0

    nearest: Optional[Dict[str, Any]] = None
    best_r: Optional[float] = None

    for c in passive_contacts:
        if c.get("time", 0) < cutoff:
            break
        if not str(c.get("contact_type", "")).startswith("torpedo_"):
            continue
        if c.get("observer_sub_id") != sid:
            continue

        rng = c.get("range")
        try:
            rng_val = float(rng) if rng is not None else None
        except Exception:
            rng_val = None

        if rng_val is None:
            rc = str(c.get("range_class", "") or "").lower()
            if rc == "short":
                rng_val = 800.0
            elif rc == "medium":
                rng_val = 2000.0
            elif rc == "long":
                rng_val = 4000.0
            else:
                continue

        if best_r is None or rng_val < best_r:
            best_r = rng_val
            nearest = c

    if not nearest or best_r is None or best_r > danger_range_m:
        return False

    tid_full = str(nearest.get("torpedo_id", "") or "")
    key = (sid, tid_full)

    rc = str(nearest.get("range_class", "") or "").lower()
    if not rc:
        if best_r < 1200.0:
            rc = "short"
        elif best_r < 3000.0:
            rc = "medium"
        else:
            rc = "long"

    prev_rc = _last_torp_range_class.get(key)
    closing = False
    if rc == "short":
        closing = True
    elif rc == "medium" and prev_rc == "long":
        closing = True

    _last_torp_range_class[key] = rc

    brg = nearest.get("bearing")
    if brg is None:
        return False
    try:
        brg_rad = float(brg)
    except Exception:
        return False

    incoming_brg_deg = compass_deg_from_rad(brg_rad)
    sz = float(sub.get("depth", 0.0) or 0.0)

    ultra_close = best_r < 800.0

    if closing:
        if ultra_close:
            evade_turn = 135.0
            tag = "CLOSING_ULTRA"
        else:
            evade_turn = 90.0
            tag = "CLOSING"
    else:
        evade_turn = 45.0 if ultra_close else 30.0
        tag = "THREAT"

    tz = nearest.get("depth")
    try:
        tz_val = float(tz) if tz is not None else None
    except Exception:
        tz_val = None

    if tz_val is not None:
        depth_delta = tz_val - sz
        if abs(depth_delta) < max_depth_step_m:
            step = max_depth_step_m if depth_delta > 0 else -max_depth_step_m
            target_depth = max(0.0, sz + step)
        else:
            target_depth = sz
    else:
        if sz > 80.0:
            step = max_depth_step_m if sz < 200.0 else -max_depth_step_m
            target_depth = max(0.0, sz + step)
        else:
            target_depth = sz

    evade_heading_deg = (incoming_brg_deg + evade_turn) % 360.0

    log(
        f"{sid[:6]}: {tag} torp {tid_full[:6] or '?'} at range={best_r:.0f}m "
        f"(rc={rc}, prev={prev_rc}), incoming_brg={incoming_brg_deg:.0f}°, "
        f"new_heading={evade_heading_deg:.0f}°, target_depth={target_depth:.0f}m"
    )

    try:
        if ultra_close and sz > 180.0:
            try:
                log(f"{sid[:6]}: initiating EMERGENCY BLOW (depth={sz:.0f}m, range={best_r:.0f}m)")
                client.emergency_blow(sid)
            except Exception as e:
                log(f"{sid[:6]}: emergency_blow failed: {e}")

        client.set_sub_heading(sid, evade_heading_deg)
        client.control_sub(sid, throttle=1.0, target_depth=target_depth)
    except Exception as e:
        log(f"{sid[:6]}: control error during torpedo evasion: {e}")
        return False

    return True


# ----------------- Energy manager (simplified) -----------------


def energy_choose_mode(sub: Dict[str, Any]) -> Tuple[str, str]:
    """
    Decide high-level energy mode for a submarine.

    Modes:
      - "snorkel_recharge": go to snorkel depth and recharge battery.
      - "normal": normal submerged / hunting behavior.

    This is intentionally simple and only based on battery + snorkel depth.
    """
    bat = float(sub.get("battery", 0.0) or 0.0)
    is_snorkeling = bool(sub.get("is_snorkeling"))
    depth = float(sub.get("depth", 0.0) or 0.0)

    # Critical: must recharge now.
    if bat < 15.0:
        return "snorkel_recharge", f"battery critically low ({bat:.1f}%)"

    # Low battery and shallow enough for snorkel or already snorkelling.
    if bat < 30.0 and (is_snorkeling or depth <= SNORKEL_DEPTH + 3.0):
        return "snorkel_recharge", f"battery low ({bat:.1f}%), shallow enough to snorkel"

    return "normal", f"battery ok ({bat:.1f}%)"


def energy_manage_snorkel_recharge(
    client: SubBrawlClient,
    sub: Dict[str, Any],
    cruise_depth_m: float,
    default_throttle: float,
) -> None:
    """
    Get to snorkel depth, enable snorkel, and gently recharge.
    """
    sid = sub["id"]
    depth = float(sub.get("depth", 0.0) or 0.0)
    bat = float(sub.get("battery", 0.0) or 0.0)
    is_snorkeling = bool(sub.get("is_snorkeling"))

    if depth > SNORKEL_DEPTH + 1.0:
        target_depth = SNORKEL_DEPTH
    else:
        target_depth = SNORKEL_DEPTH

    try:
        if not is_snorkeling:
            resp = client.toggle_snorkel(sid, True)
            if resp.get("ok"):
                log(f"{sid[:6]}: snorkel ON at depth={depth:.1f}m (battery={bat:.1f}%)")
        client.control_sub(sid, throttle=0.3, target_depth=target_depth)
    except Exception as e:
        log(f"{sid[:6]}: snorkel_recharge control error: {e}")


def energy_manage_refuel(client: SubBrawlClient, sub: Dict[str, Any]) -> None:
    """
    Placeholder for any future refuel/base behavior. Not used in this simplified apex brain.
    """
    log(f"{sub['id'][:6]}: refuel mode requested but not implemented; treating as normal.")


# ----------------- Navigation -----------------


def patrol_or_explore_outward(client: SubBrawlClient, sub: Dict[str, Any], throttle: float = 0.4) -> None:
    x = float(sub.get("x", 0.0) or 0.0)
    y = float(sub.get("y", 0.0) or 0.0)
    r = math.hypot(x, y)

    if r < RING_R * 0.8:
        radial = math.atan2(y, x)
        tangent = radial + math.pi / 2.0
        heading_deg = compass_deg_from_rad(tangent)
        mode_desc = "patrol_ring"
    else:
        radial_out = math.atan2(y, x)
        heading_deg = compass_deg_from_rad(radial_out)
        mode_desc = "explore_outward"

    sid = sub["id"]
    try:
        client.set_sub_heading(sid, heading_deg)
        client.control_sub(sid, throttle=throttle)
        log(f"{sid[:6]}: nav={mode_desc} r={r:.0f}m hdg={heading_deg:.0f}°, thr={throttle:.2f}")
    except Exception as e:
        log(f"{sid[:6]}: navigation error: {e}")


def navigate_toward_hostile_in_formation(
    client: SubBrawlClient,
    sub: Dict[str, Any],
    subs_by_id: Dict[str, Dict[str, Any]],
    controlled_ids: List[str],
    spacing_m: float,
    throttle: float,
) -> None:
    if not current_hostile_target:
        patrol_or_explore_outward(client, sub, throttle=throttle)
        return

    leader_id = controlled_ids[0] if controlled_ids else None
    if not leader_id or leader_id not in subs_by_id:
        patrol_or_explore_outward(client, sub, throttle=throttle)
        return

    target_x = float(current_hostile_target["x"])
    target_y = float(current_hostile_target["y"])

    sid = sub["id"]
    sx = float(sub.get("x", 0.0) or 0.0)
    sy = float(sub.get("y", 0.0) or 0.0)
    sz = float(sub.get("depth", 0.0) or 0.0)

    leader = subs_by_id[leader_id]
    lx = float(leader.get("x", 0.0) or 0.0)
    ly = float(leader.get("y", 0.0) or 0.0)
    lz = float(leader.get("depth", 0.0) or 0.0)

    fwd_rad = math.atan2(target_y - ly, target_x - lx)
    fwd_deg = compass_deg_from_rad(fwd_rad)
    right_x = math.cos(fwd_rad - math.pi / 2.0)
    right_y = math.sin(fwd_rad - math.pi / 2.0)

    if sid == leader_id:
        heading_deg = fwd_deg
        desired_depth = lz
        thr = throttle
        role = "leader"
    else:
        spacing = max(10.0, spacing_m)
        target_wx = lx + right_x * spacing
        target_wy = ly + right_y * spacing
        dx = target_wx - sx
        dy = target_wy - sy
        dxy = math.hypot(dx, dy)
        heading_deg = compass_deg_from_rad(math.atan2(dy, dx))

        rel = dxy - spacing
        if rel > 50.0:
            thr = min(1.0, throttle + 0.2)
        elif rel < -50.0:
            thr = max(0.1, throttle - 0.2)
        else:
            thr = throttle

        desired_depth = lz
        role = "wing"

    try:
        client.set_sub_heading(sid, heading_deg)
        client.control_sub(sid, throttle=thr, target_depth=desired_depth)
        d_to_target = math.hypot(target_x - sx, target_y - sy)
        log(
            f"{sid[:6]}: form_nav role={role} d_target={d_to_target:.0f}m "
            f"hdg={heading_deg:.0f}°, thr={thr:.2f}, depth {sz:.0f}→{desired_depth:.0f}m"
        )
    except Exception as e:
        log(f"{sid[:6]}: formation/nav error: {e}")


# ----------------- Fire control -----------------


def pick_firing_sub(candidates: List[Dict[str, Any]], target_xy: Tuple[float, float]) -> Optional[Dict[str, Any]]:
    """
    Choose the best firing submarine: prefer closer + higher battery + ammo>0 if present.
    """
    if not candidates:
        return None

    tx, ty = target_xy
    best = None
    best_score = None

    for sub in candidates:
        sx = float(sub.get("x", 0.0) or 0.0)
        sy = float(sub.get("y", 0.0) or 0.0)
        bat = float(sub.get("battery", 0.0) or 0.0)
        ammo = int(sub.get("torpedo_ammo", 999) or 0)

        if ammo <= 0 or bat < 5.0:
            continue

        d = distance(sx, sy, tx, ty)
        score = d - bat * 50.0  # closer and more battery => smaller score
        if best is None or score < best_score:
            best = sub
            best_score = score

    return best


def launch_torpedo_at_target(
    client: SubBrawlClient,
    firing_sub: Dict[str, Any],
    target_xy: Tuple[float, float],
    homing_range_m: float = 1200.0,
    update_interval: float = 0.5,
    target_updater: Optional[callable] = None,
) -> None:
    """
    Simple launch: fire a torpedo toward the current target position and set its initial heading.

    This version is intentionally lightweight: it does *not* do continuous wire-guided
    updates in a loop. The server-side homing and torpedo pings can still make it effective.
    """
    tx, ty = target_xy
    sx = float(firing_sub.get("x", 0.0) or 0.0)
    sy = float(firing_sub.get("y", 0.0) or 0.0)

    rng = distance(sx, sy, tx, ty)
    fire_range = min(TORP_MAX_RANGE, rng + 400.0)

    sid = firing_sub["id"]
    try:
        resp = client.launch_torpedo(sid, fire_range)
    except Exception as e:
        log(f"{sid[:6]}: launch_torpedo exception: {e}")
        return

    if not resp or not resp.get("ok"):
        log(f"{sid[:6]}: launch_torpedo failed: {resp}")
        return

    torp_id = str(resp.get("torpedo_id"))
    if not torp_id:
        log(f"{sid[:6]}: launch_torpedo returned no torpedo_id")
        return

    log(
        f"{sid[:6]}: LAUNCH torp {torp_id[:6]} at target ({tx:.0f},{ty:.0f}), "
        f"fire_range={fire_range:.0f}m"
    )

    # Compute heading from sub to target and apply to torpedo.
    heading_rad = bearing_rad_between(sx, sy, tx, ty)
    heading_deg = compass_deg_from_rad(heading_rad)
    try:
        client.set_torp_target_heading(torp_id, heading_deg)
    except Exception as e:
        log(f"{sid[:6]}: set_torp_target_heading exception: {e}")

    # Optionally enable active ping later with torp_ping_toggle(), but we keep it simple here.


# ----------------- Main brain loop -----------------


def bootstrap_client(base_url: str) -> SubBrawlClient:
    client = SubBrawlClient(base_url)

    state_path = os.path.join(os.path.dirname(__file__), "apex_agent_state.json")
    if not client.api_key and os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("base_url") == base_url and data.get("api_key"):
                client.set_api_key(data["api_key"])
                log("Loaded API key from apex_agent_state.json")
        except Exception:
            pass

    if not client.api_key:
        import secrets

        username = f"apex_{int(time.time())}_{secrets.token_hex(4)}"
        password = secrets.token_hex(8)
        log(f"No API key, signing up as {username}")
        resp = client.signup(username, password)
        api_key = resp.get("api_key") or resp.get("token")
        if not api_key:
            print("[apex] signup did not return an api_key field", file=sys.stderr)
            sys.exit(1)
        client.set_api_key(api_key)
        print(f"[apex] Credentials -> username: {username}  password: {password}")
        print(f"[apex] Obtained API key {api_key}")

        state_meta: Dict[str, Any] = {
            "base_url": base_url,
            "api_key": api_key,
            "subs": [],
            "created_at": time.time(),
            "username": username,
        }
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state_meta, f, indent=2)
            log(f"Saved API key and metadata to {state_path}")
        except Exception as e:
            log(f"Failed to write state file {state_path}: {e}")

    return client


def load_rules_into_globals(client: SubBrawlClient) -> None:
    global GAME_RULES, SNORKEL_DEPTH, RING_R, TORP_MAX_RANGE
    try:
        GAME_RULES = client.get_rules() or {}
    except Exception as e:
        log(f"/rules fetch failed: {e}")
        GAME_RULES = {}

    world = GAME_RULES.get("world", {})
    ring = world.get("ring", {})
    RING_R = float(ring.get("r", RING_R))

    sub_cfg = GAME_RULES.get("sub", {})
    SNORKEL_DEPTH = float(sub_cfg.get("snorkel_depth", SNORKEL_DEPTH))

    torp_cfg = GAME_RULES.get("torpedo", {})
    TORP_MAX_RANGE = float(torp_cfg.get("max_range", TORP_MAX_RANGE))


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: apex_brain.py BASE_URL (e.g. http://localhost:5000)", file=sys.stderr)
        sys.exit(1)

    base_url = sys.argv[1]
    client = bootstrap_client(base_url)

    # Load game config from /rules once at startup (optional).
    load_rules_into_globals(client)

    # Ensure we have up to MAX_SUBS subs.
    MAX_SUBS = 2
    try:
        state = client.get_state()
    except Exception:
        state = {"subs": []}

    subs = state.get("subs") or []
    while len(subs) < MAX_SUBS:
        try:
            log(f"Spawning submarine {len(subs)+1}/{MAX_SUBS}")
            client.register_sub()
        except Exception as e:
            print("[apex] register_sub failed:", e, file=sys.stderr)
            time.sleep(1.0)
        state = client.get_state()
        subs = state.get("subs") or []

    controlled_ids = [s["id"] for s in subs[:MAX_SUBS]]
    log(f"Controlling submarines: {controlled_ids}")

    # SSE listener
    if client.api_key:
        t = threading.Thread(target=_sse_listener, args=(client.base, client.api_key), daemon=True)
        t.start()
        log("SSE listener thread started")

    global has_fired_for_target, current_shot

    while True:
        cfg = load_brain_config()
        formation_spacing = float(cfg.get("formation_spacing_m", 200.0) or 200.0)
        default_throttle = float(cfg.get("default_throttle", 0.4) or 0.4)
        cruise_depth = float(cfg.get("cruise_depth_m", 80.0) or 80.0)
        posture = str(cfg.get("posture", "balanced") or "balanced").lower()
        auto_fire = bool(cfg.get("auto_fire", True))

        try:
            state = client.get_state()
        except Exception as e:
            print("[apex] state fetch failed:", e, file=sys.stderr)
            time.sleep(1.0)
            continue

        subs = state.get("subs") or []
        by_id: Dict[str, Dict[str, Any]] = {s["id"]: s for s in subs}

        # Update hostile tracks from passive bearings.
        update_hostile_tracks(state, controlled_ids)

        # Fused hostile target logging + fire control.
        if current_hostile_target:
            age = time.time() - current_hostile_target.get("updated_at", 0.0)
            log(
                f"hostile_target @ "
                f"({current_hostile_target['x']:.0f}, {current_hostile_target['y']:.0f}) "
                f"(age {age:.1f}s)"
            )

            # Posture-based aggression: tune how eager we are to fire.
            if posture == "aggressive":
                min_samples = 2
                required_observers = 1
                max_track_age = 20.0
                max_sep_ok = 6000.0
            elif posture == "ultra_quiet":
                min_samples = 999
                required_observers = 99
                max_track_age = 5.0
                max_sep_ok = 2000.0
            else:  # balanced
                min_samples = 3
                required_observers = 2
                max_track_age = 10.0
                max_sep_ok = 4000.0

            observer_ids_with_tracks = [
                sid
                for sid in controlled_ids
                if sid in hostile_tracks and hostile_tracks[sid].get("sample_count", 0) >= min_samples
            ]

            if (
                len(observer_ids_with_tracks) >= required_observers
                and not has_fired_for_target
                and age < max_track_age
                and auto_fire
                and posture != "ultra_quiet"
            ):
                # Non-aggressive posture: only one active shot at a time.
                if current_shot and posture != "aggressive":
                    pass
                else:
                    positions = [
                        (hostile_tracks[sid]["x"], hostile_tracks[sid]["y"])
                        for sid in observer_ids_with_tracks
                    ]
                    max_sep = 0.0
                    for i in range(len(positions)):
                        for j in range(i + 1, len(positions)):
                            dx = positions[i][0] - positions[j][0]
                            dy = positions[i][1] - positions[j][1]
                            d = math.hypot(dx, dy)
                            if d > max_sep:
                                max_sep = d

                    if max_sep > max_sep_ok:
                        log(
                            f"hostile tracks inconsistent (max_sep={max_sep:.0f}m, posture={posture}); "
                            f"waiting for better geometry before firing."
                        )
                    else:
                        firing_subs = [by_id[sid] for sid in observer_ids_with_tracks if sid in by_id]
                        firing_sub = pick_firing_sub(
                            firing_subs,
                            (current_hostile_target["x"], current_hostile_target["y"]),
                        )
                        if firing_sub:
                            ammo = int(firing_sub.get("torpedo_ammo", 999) or 0)
                            bat = float(firing_sub.get("battery", 0.0) or 0.0)
                            if ammo <= 0:
                                log(f"{firing_sub['id'][:6]}: no torpedo ammo; skipping fire.")
                                if bat > 30.0:
                                    try:
                                        log(f"{firing_sub['id'][:6]}: attempting torpedo reload (battery={bat:.1f}%)")
                                        client.reload_torpedoes(firing_sub["id"])
                                    except Exception as e:
                                        log(f"{firing_sub['id'][:6]}: reload_torpedoes exception: {e}")
                                # No fire this tick
                            elif bat < 15.0:
                                log(
                                    f"{firing_sub['id'][:6]}: battery too low ({bat:.1f}%) "
                                    f"for offensive shot; skipping fire."
                                )
                            else:
                                log(
                                    f"FIRING SOLUTION: launching torpedo from {firing_sub['id'][:6]} "
                                    f"at hostile target ({current_hostile_target['x']:.0f}, "
                                    f"{current_hostile_target['y']:.0f})"
                                )

                                def _fire_once():
                                    def _get_dynamic_target():
                                        if current_hostile_target:
                                            return (
                                                float(current_hostile_target["x"]),
                                                float(current_hostile_target["y"]),
                                            )
                                        snap = current_shot.get("target_snapshot")
                                        if snap:
                                            return float(snap["x"]), float(snap["y"])
                                        return (
                                            float(firing_sub.get("x", 0.0) or 0.0),
                                            float(firing_sub.get("y", 0.0) or 0.0),
                                        )

                                    launch_torpedo_at_target(
                                        client,
                                        firing_sub,
                                        (current_hostile_target["x"], current_hostile_target["y"]),
                                        homing_range_m=1200.0,
                                        update_interval=0.5,
                                        target_updater=_get_dynamic_target,
                                    )

                                threading.Thread(target=_fire_once, daemon=True).start()

                                sx = float(firing_sub.get("x", 0.0) or 0.0)
                                sy = float(firing_sub.get("y", 0.0) or 0.0)
                                tx = float(current_hostile_target["x"])
                                ty = float(current_hostile_target["y"])
                                r0 = math.hypot(tx - sx, ty - sy)
                                v_torp = 6.0
                                safety = 1.3
                                eta_s = (r0 / max(v_torp, 0.1)) * safety
                                current_shot.clear()
                                current_shot.update(
                                    {
                                        "fired_at": time.time(),
                                        "eta_s": eta_s,
                                        "target_snapshot": {"x": tx, "y": ty},
                                        "refires": 0,
                                    }
                                )
                                has_fired_for_target = True

        # Shot ETA management: if torpedo likely missed, allow refire.
        if current_shot:
            shot_age = time.time() - current_shot.get("fired_at", 0.0)
            eta_s = current_shot.get("eta_s", 0.0)
            if shot_age > eta_s:
                log(
                    f"Current shot exceeded ETA (age={shot_age:.1f}s > eta={eta_s:.1f}s); "
                    f"allowing potential refire on same solution."
                )
                current_shot["refires"] = int(current_shot.get("refires", 0)) + 1
                has_fired_for_target = False
                current_shot.clear()

        active_any = False
        for sid in controlled_ids:
            sub = by_id.get(sid)
            if not sub:
                continue
            active_any = True

            # High-priority: torpedo evasion.
            if maybe_evade_torpedoes(client, sub, danger_range_m=2000.0, max_depth_step_m=60.0):
                continue

            mode, reason = energy_choose_mode(sub)
            log(f"{sid[:6]}: energy_mode={mode} - {reason}")

            if mode == "refuel":
                energy_manage_refuel(client, sub)
            elif mode == "snorkel_recharge":
                energy_manage_snorkel_recharge(client, sub, cruise_depth_m=cruise_depth, default_throttle=default_throttle)
            else:
                if current_hostile_target:
                    navigate_toward_hostile_in_formation(
                        client,
                        sub,
                        by_id,
                        controlled_ids,
                        spacing_m=formation_spacing,
                        throttle=default_throttle,
                    )
                else:
                    leader_id = controlled_ids[0] if controlled_ids else None
                    if not leader_id or leader_id not in by_id:
                        patrol_or_explore_outward(client, sub, throttle=default_throttle)
                    else:
                        leader = by_id[leader_id]
                        lx = float(leader.get("x", 0.0) or 0.0)
                        ly = float(leader.get("y", 0.0) or 0.0)
                        lz = float(leader.get("depth", 0.0) or 0.0)
                        l_heading_rad = float(leader.get("heading", 0.0) or 0.0)

                        if sid == leader_id:
                            # Leader: default nav + clear snorkel once charged.
                            if sub.get("is_snorkeling") and float(sub.get("battery", 0.0) or 0.0) >= 95.0:
                                try:
                                    log(
                                        f"{sid[:6]}: battery full, forcing snorkel OFF and "
                                        f"submerging to cruise depth {cruise_depth:.0f}m"
                                    )
                                    client.toggle_snorkel(sid, False)
                                except Exception as e:
                                    log(f"{sid[:6]}: toggle_snorkel(off) exception in leader: {e}")
                                try:
                                    client.control_sub(sid, throttle=default_throttle, target_depth=cruise_depth)
                                except Exception as e:
                                    log(f"{sid[:6]}: control_sub to cruise_depth failed: {e}")
                            else:
                                patrol_or_explore_outward(client, sub, throttle=default_throttle)
                        else:
                            sx = float(sub.get("x", 0.0) or 0.0)
                            sy = float(sub.get("y", 0.0) or 0.0)
                            sz = float(sub.get("depth", 0.0) or 0.0)

                            spacing = formation_spacing
                            fwd_x = math.cos(l_heading_rad)
                            fwd_y = math.sin(l_heading_rad)
                            right_x = math.cos(l_heading_rad - math.pi / 2.0)
                            right_y = math.sin(l_heading_rad - math.pi / 2.0)
                            target_wx = lx + right_x * spacing
                            target_wy = ly + right_y * spacing
                            dx = target_wx - sx
                            dy = target_wy - sy
                            dxy = math.hypot(dx, dy)
                            heading_deg = compass_deg_from_rad(math.atan2(dy, dx))

                            rel = dxy - spacing
                            if rel > 50.0:
                                wing_thr = min(1.0, default_throttle + 0.2)
                            elif rel < -50.0:
                                wing_thr = max(0.1, default_throttle - 0.2)
                            else:
                                wing_thr = default_throttle

                            try:
                                client.set_sub_heading(sid, heading_deg)
                                client.control_sub(sid, throttle=wing_thr, target_depth=lz)
                                log(
                                    f"{sid[:6]}: default_form role=wing spacing={dxy:.0f}m "
                                    f"(target {spacing:.0f}m), hdg={heading_deg:.0f}°, thr={wing_thr:.2f}, "
                                    f"depth {sz:.0f}→{lz:.0f}m"
                                )
                            except Exception as e:
                                log(f"{sid[:6]}: formation error (no-hostile): {e}")

        if not active_any:
            log("All controlled subs gone, exiting.")
            break

        time.sleep(0.5)


if __name__ == "__main__":
    main()
