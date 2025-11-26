"""
Top-level "brain" agent for AISubBrawl.

This script orchestrates several capabilities:
  - Account / sub bootstrap (like simple_agent).
  - Energy management (refuel vs snorkel recharge vs patrol).
  - Basic navigation (explore outward from the inner ring when not refueling).
  - SSE ingestion for this user's own subs / torpedoes / sonar events.

The brain maintains in-memory views of:
  - passive_contacts   (sub & torpedo passive + active ping detections)
  - torp_ping_contacts (torpedo active ping returns)
  - echo_contacts      (active sonar echoes from our own pings)

It currently uses contact data only for observability; higher-level
engagement decisions (firing torpedoes, evasive maneuvers, etc.) are
still delegated to specialized agents such as:
  - engagement_agent.py
  - fire_control_agent.py
  - torpedo_manager.py
  - torpedo_evasion_agent.py
  - aggressive_engagement_agent.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from typing import Any, Dict, List, Tuple

import requests

from .client import SubBrawlClient
from .contact_utils import is_friendly_contact
from .passive_tracker import PassiveTracker
from .energy_manager import (
    choose_mode as energy_choose_mode,
    manage_refuel as energy_manage_refuel,
    manage_snorkel_recharge as energy_manage_snorkel_recharge,
)
from .fire_control_agent import launch_torpedo_at_target, pick_firing_sub


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] [brain] {msg}"
    # Print to stdout for real-time visibility
    print(line)
    # Also append to a persistent brain.log file at the project root
    try:
        root_dir = os.path.dirname(os.path.dirname(__file__))
        log_path = os.path.join(root_dir, "brain.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        # Logging should never crash the brain; ignore file I/O errors.
        pass


def compass_deg_from_rad(rad: float) -> float:
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


# --- SSE-driven observability (own subs/torps/sonar), similar to ui.html ---

PASSIVE_TTL = 60.0  # seconds to keep passive/echo contacts in memory

passive_contacts: List[Dict[str, Any]] = []
torp_ping_contacts: List[Dict[str, Any]] = []
echo_contacts: List[Dict[str, Any]] = []
hostile_trackers: Dict[str, PassiveTracker] = {}
hostile_tracks: Dict[str, Dict[str, Any]] = {}
current_hostile_target: Dict[str, Any] = {}
has_fired_for_target: bool = False
current_shot: Dict[str, Any] = {}  # tracks last torpedo shot toward current_hostile_target
_last_torp_range_class: Dict[Tuple[str, str], str] = {}


def load_brain_config() -> Dict[str, Any]:
    """
    Load brain configuration from brain_config.json.
    This is re-read on each loop so changes on disk take effect at runtime.
    """
    cfg_path = os.path.join(os.path.dirname(__file__), "brain_config.json")
    # Defaults if file missing or invalid.
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


def _sse_listener(base_url: str, api_key: str) -> None:
    """
    Background thread: connect to /stream?api_key=... and ingest events.
    Mirrors part of the client-side logic in ui.html for:
      - contact (passive + active_ping_detected)
      - torpedo_contact
      - torpedo_ping
      - echo (active sonar echoes)
    """
    url = f"{base_url.rstrip('/')}/stream"
    params = {"api_key": api_key}
    log(f"SSE listener connecting to {url} ...")

    while True:
        try:
            with requests.get(url, params=params, stream=True, timeout=60) as resp:
                resp.raise_for_status()

                event_type = None
                data_lines: List[str] = []

                for raw in resp.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    line = raw.strip()
                    if not line:
                        # End of one event
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
    """
    Update in-memory structures based on SSE events.
    We keep this intentionally light: just track passive and torpedo ping contacts
    with timestamps, mirroring ui.html.
    """
    now = time.time()

    if event_type == "contact" and isinstance(payload, dict):
        c = dict(payload)
        if c.get("type") in ("passive", "active_ping_detected"):
            c["time"] = now
            if c.get("type") == "active_ping_detected":
                c["contact_type"] = "ping"
            passive_contacts.insert(0, c)
            # Trim
            del passive_contacts[120:]
    elif event_type == "torpedo_contact" and isinstance(payload, dict):
        c = dict(payload)
        c["time"] = now
        c["contact_type"] = "torpedo_" + str(c.get("contact_type", ""))
        passive_contacts.insert(0, c)
        del passive_contacts[120:]
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
        # Active sonar echo event (from our own active pings).
        e = dict(payload)
        e["time"] = now
        echo_contacts.insert(0, e)
        del echo_contacts[80:]

    # Periodically trim old passive/echo contacts by TTL
    cutoff = now - PASSIVE_TTL
    while passive_contacts and passive_contacts[-1].get("time", 0) < cutoff:
        passive_contacts.pop()
    while echo_contacts and echo_contacts[-1].get("time", 0) < cutoff:
        echo_contacts.pop()


def update_hostile_tracks(state: Dict[str, Any], controlled_ids: List[str]) -> None:
    """
    Use recent passive contacts and current sub state to build very simple
    hostile tracks (bearing-only) for each observer sub.

    For now we assume at most one significant hostile per observer and use
    one PassiveTracker per observer_sub_id.
    """
    subs = state.get("subs") or []
    by_id: Dict[str, Dict[str, Any]] = {s["id"]: s for s in subs if s.get("id")}
    controlled_set = set(controlled_ids or [])

    now = time.time()
    cutoff = now - 30.0  # only use very recent contacts

    # Rebuild trackers fresh from recent contacts each tick so we only use
    # geometry from the last ~30s.
    hostile_trackers.clear()
    hostile_tracks.clear()

    # Feed recent passive contacts into per-observer trackers, skipping friendlies.
    for c in passive_contacts:
        if c.get("time", 0) < cutoff:
            continue
        if c.get("type") != "passive":
            continue
        obs_id = c.get("observer_sub_id")
        if not obs_id or obs_id not in by_id:
            continue
        obs = by_id[obs_id]

        bearing_rad = float(c.get("bearing", 0.0) or 0.0)
        range_class = str(c.get("range_class", "") or "").lower()

        # Extra guard: if this observer is one of our own controlled subs,
        # explicitly check whether this bearing lines up with one of our other
        # controlled subs and skip it if so. This prevents the brain from
        # treating our own wingman as a hostile target.
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
                dx = fx - ox
                dy = fy - oy
                dist = math.hypot(dx, dy)
                if dist <= 0.0:
                    continue
                brg_to_friend = math.atan2(dy, dx)
                # Smallest absolute angle between bearings.
                ang = abs((bearing_rad - brg_to_friend + math.pi) % (2.0 * math.pi) - math.pi)

                # Tolerances by range class: tighter for SHORT, looser for LONG.
                if range_class == "short":
                    max_ang = math.radians(25.0)
                    max_dist = 1500.0
                elif range_class == "medium":
                    max_ang = math.radians(35.0)
                    max_dist = 4000.0
                else:  # long / unknown
                    max_ang = math.radians(45.0)
                    max_dist = 8000.0

                if dist <= max_dist and ang <= max_ang:
                    log(
                        f"Skipping friendly bearing from {obs_id[:6]} toward wingman {friend_id[:6]} "
                        f"(rc={range_class or '?'}, dist={dist:.0f}m, ang={math.degrees(ang):.0f}°)"
                    )
                    skipped_for_friend = True
                    break

            if skipped_for_friend:
                # Treat as friendly; do not feed into hostile tracker.
                continue

        # Generic friendly filter for other subs (including non-controlled).
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

    # Update estimated positions for each tracker and log the geometry used.
    for obs_id, tracker in hostile_trackers.items():
        est = tracker.estimate_position()
        if est is None:
            continue
        x, y = est
        obs = by_id.get(obs_id)
        if obs:
            ox = float(obs.get("x", 0.0) or 0.0)
            oy = float(obs.get("y", 0.0) or 0.0)
            # If the estimate collapses essentially onto the observer's own
            # position, the geometry is degenerate (all bearings nearly parallel
            # or symmetric). Treat this as unusable for firing solutions.
            dist_obs = math.hypot(x - ox, y - oy)
            if dist_obs < 800.0:
                log(
                    f"TRACK[{obs_id[:6]}]: discarding degenerate solution est=({x:.0f},{y:.0f}) "
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
            # Use the *most recent* bearing sample for logging.
            last_sample = tracker.samples[-1]
            brg_deg = compass_deg_from_rad(last_sample.bearing_rad)
            log(
                f"TRACK[{obs_id[:6]}]: est=({x:.0f},{y:.0f}) from {len(tracker.samples)} bearings; "
                f"last_brg={brg_deg:.0f}° at obs=({ox:.0f},{oy:.0f})"
            )

    # Derive a single "best guess" hostile target from all observers, if possible.
    global has_fired_for_target
    if hostile_tracks:
        xs = [t["x"] for t in hostile_tracks.values()]
        ys = [t["y"] for t in hostile_tracks.values()]
        current_hostile_target["x"] = sum(xs) / len(xs)
        current_hostile_target["y"] = sum(ys) / len(ys)
        current_hostile_target["updated_at"] = now
        # New/updated solution; allow firing again.
        has_fired_for_target = False
    else:
        current_hostile_target.clear()
        has_fired_for_target = False


def _intersect_two_bearings(
    p1x: float, p1y: float, b1_rad: float, p2x: float, p2y: float, b2_rad: float
) -> Tuple[float, float] | None:
    """
    Compute the intersection point of two infinite bearing lines:

        L1(t1) = p1 + t1 * u1
        L2(t2) = p2 + t2 * u2

    where u1/u2 are unit vectors of bearings b1/b2 (radians).
    Returns (x, y) of closest intersection point, or None if lines are nearly parallel.
    """
    u1x = math.cos(b1_rad)
    u1y = math.sin(b1_rad)
    u2x = math.cos(b2_rad)
    u2y = math.sin(b2_rad)

    # Solve for t1, t2 in:
    #   p1 + t1*u1 = p2 + t2*u2
    # Rearranged:
    #   t1*u1x - t2*u2x = dx
    #   t1*u1y - t2*u2y = dy
    dx = p2x - p1x
    dy = p2y - p1y

    a11 = u1x
    a12 = -u2x
    a21 = u1y
    a22 = -u2y

    det = a11 * a22 - a12 * a21
    if abs(det) < 1e-6:
        # Nearly parallel; unreliable intersection.
        return None

    inv11 = a22 / det
    inv12 = -a12 / det
    inv21 = -a21 / det
    inv22 = a11 / det

    t1 = inv11 * dx + inv12 * dy
    # t2 = inv21 * dx + inv22 * dy  # not needed explicitly

    ix = p1x + t1 * u1x
    iy = p1y + t1 * u1y
    return ix, iy


def maybe_evade_torpedoes(
    client: SubBrawlClient,
    sub: Dict[str, Any],
    danger_range_m: float = 2000.0,
    max_depth_step_m: float = 60.0,
) -> bool:
    """
    Use recent SSE torpedo_contact events to perform evasive maneuvers.

    Returns True if an evasive command was issued for this sub on this tick.
    """
    sid = sub.get("id")
    if not sid:
        return False

    # Find the most threatening recent torpedo contact for this observer.
    now = time.time()
    cutoff = now - 10.0  # only use very recent torpedo contacts

    nearest: Dict[str, Any] | None = None
    best_r: float | None = None

    for c in passive_contacts:
        if c.get("time", 0) < cutoff:
            break  # list is newest-first
        if not str(c.get("contact_type", "")).startswith("torpedo_"):
            continue
        if c.get("observer_sub_id") != sid:
            continue

        # Prefer explicit range if provided; otherwise, approximate from range_class.
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

    # Determine closing vs non-closing using range_class history, similar to
    # torpedo_evasion_agent.py.
    tid_full = str(nearest.get("torpedo_id", "") or "")
    key = (sid, tid_full)

    rc = str(nearest.get("range_class", "") or "").lower()
    if not rc:
        # Derive a synthetic range_class from numeric range.
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

    # Compute incoming bearing (torp -> sub) if bearing is present.
    brg = nearest.get("bearing")
    if brg is None:
        return False
    try:
        brg_rad = float(brg)
    except Exception:
        return False

    incoming_brg_deg = compass_deg_from_rad(brg_rad)

    sz = float(sub.get("depth", 0.0) or 0.0)
    # Choose evasive behavior.
    if closing:
        evade_turn = 90.0
        # Try to move vertically away from reported torpedo depth if available.
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
            target_depth = sz
        tag = "CLOSING"
    else:
        evade_turn = 30.0
        target_depth = sz
        tag = "THREAT"

    evade_heading_deg = (incoming_brg_deg + evade_turn) % 360.0

    log(
        f"{sid[:6]}: {tag} torp {tid_full[:6] or '?'} at range={best_r:.0f}m "
        f"(rc={rc}, prev={prev_rc}), incoming_brg={incoming_brg_deg:.0f}°, "
        f"new_heading={evade_heading_deg:.0f}°, target_depth={target_depth:.0f}m"
    )

    try:
        client.set_sub_heading(sid, evade_heading_deg)
        client.control_sub(sid, throttle=1.0, target_depth=target_depth)
    except Exception as e:
        log(f"{sid[:6]}: control error during torpedo evasion: {e}")
        return False

    return True


def navigate_toward_hostile_in_formation(
    client: SubBrawlClient,
    sub: Dict[str, Any],
    subs_by_id: Dict[str, Dict[str, Any]],
    controlled_ids: List[str],
    spacing_m: float = 200.0,
    throttle: float = 0.4,
) -> None:
    """
    When a plausible hostile target exists, move controlled subs toward it in
    a simple formation:
      - First controlled sub: leader, heads directly toward target.
      - Second controlled sub: wingman, tries to hold a side-by-side offset.
    """
    if not current_hostile_target:
        # No global hostile target; caller should fall back to default nav.
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

    # Leader always exists in subs_by_id (checked above).
    leader = subs_by_id[leader_id]
    lx = float(leader.get("x", 0.0) or 0.0)
    ly = float(leader.get("y", 0.0) or 0.0)
    lz = float(leader.get("depth", 0.0) or 0.0)

    # Heading from leader to target defines forward direction for formation.
    fwd_rad = math.atan2(target_y - ly, target_x - lx)
    fwd_deg = compass_deg_from_rad(fwd_rad)
    right_x = math.cos(fwd_rad - math.pi / 2.0)
    right_y = math.sin(fwd_rad - math.pi / 2.0)

    if sid == leader_id:
        # Leader: drive straight toward target.
        heading_deg = fwd_deg
        desired_depth = lz
        thr = throttle
        role = "leader"
    else:
        # Wingman: aim for a lateral offset to starboard of the leader.
        spacing = max(10.0, spacing_m)
        target_wx = lx + right_x * spacing
        target_wy = ly + right_y * spacing
        dx = target_wx - sx
        dy = target_wy - sy
        dxy = math.hypot(dx, dy)
        heading_deg = compass_deg_from_rad(math.atan2(dy, dx))

        # Speed up or slow down slightly to hold spacing.
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


def patrol_or_explore_outward(client: SubBrawlClient, sub: Dict[str, Any], throttle: float = 0.4) -> None:
    """
    Default navigation behavior when not refueling or snorkel-recharging:
      - If close to the ring center, roughly circle it.
      - Otherwise, slowly explore outward (radial out).
    """
    x = float(sub.get("x", 0.0) or 0.0)
    y = float(sub.get("y", 0.0) or 0.0)
    r = math.hypot(x, y)

    # Inner ring radius from game_config.json
    ring_r = 6000.0

    if r < ring_r * 0.8:
        # Simple ring patrol: steer tangent to the circle.
        radial = math.atan2(y, x)
        tangent = radial + math.pi / 2.0
        heading_deg = compass_deg_from_rad(tangent)
        mode_desc = "patrol_ring"
    else:
        # Explore outward: steer radially away from center.
        radial_out_rad = math.atan2(y, x)
        heading_deg = compass_deg_from_rad(radial_out_rad)
        mode_desc = "explore_outward"

    try:
        client.set_sub_heading(sub["id"], heading_deg)
        client.control_sub(sub["id"], throttle=throttle)
        log(
            f"{sub['id'][:6]}: nav={mode_desc} r={r:.0f}m "
            f"heading={heading_deg:.0f}°, throttle={throttle:.2f}"
        )
    except Exception as e:
        log(f"{sub['id'][:6]}: navigation error: {e}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: brain_agent.py BASE_URL (e.g. http://localhost:5000)", file=sys.stderr)
        sys.exit(1)

    base_url = sys.argv[1]
    client = SubBrawlClient(base_url)

    # Bootstrap: if no API key is set, auto-signup and persist credentials.
    state_path = os.path.join(os.path.dirname(__file__), "agent_state.json")
    if not client.api_key:
        import secrets

        username = f"brain_{int(time.time())}_{secrets.token_hex(4)}"
        password = secrets.token_hex(8)
        log(f"No API key, signing up as {username}")
        try:
            resp = client.signup(username, password)
        except Exception as e:
            print("[brain] signup failed:", e, file=sys.stderr)
            sys.exit(1)
        api_key = resp.get("api_key") or resp.get("token")
        if not api_key:
            print("[brain] signup did not return an api_key field", file=sys.stderr)
            sys.exit(1)
        client.set_api_key(api_key)
        print(f"[brain] Credentials -> username: {username}  password: {password}")
        print(f"[brain] Obtained API key {api_key}")

        # Persist for reuse
        state_meta: Dict[str, Any] = {
            "base_url": base_url,
            "api_key": api_key,
            "subs": [],
            "created_at": time.time(),
            "username": username,
        }
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                import json

                json.dump(state_meta, f, indent=2)
            log(f"Saved API key and metadata to {state_path}")
        except Exception as e:
            log(f"Failed to write state file {state_path}: {e}")

    # Ensure we have at least two submarines.
    try:
        state = client.get_state()
    except Exception:
        state = {"subs": []}

    subs: List[Dict[str, Any]] = state.get("subs") or []
    while len(subs) < 2:
        try:
            log(f"Spawning submarine {len(subs)+1}/2")
            client.register_sub()
        except Exception as e:
            print("[brain] register_sub failed:", e, file=sys.stderr)
            time.sleep(1.0)
        state = client.get_state()
        subs = state.get("subs") or []

    controlled_ids = [s["id"] for s in subs[:2]]
    log(f"Controlling submarines: {controlled_ids}")

    # Start SSE listener for own subs/torps/sonar events.
    if client.api_key:
        t = threading.Thread(
            target=_sse_listener,
            args=(client.base, client.api_key),
            daemon=True,
        )
        t.start()
        log("SSE listener thread started")

    # Main loop: per-sub energy mode + navigation.
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
            print("[brain] state fetch failed:", e, file=sys.stderr)
            time.sleep(1.0)
            continue

        subs = state.get("subs") or []
        by_id: Dict[str, Dict[str, Any]] = {s["id"]: s for s in subs}

        # Update simple hostile bearing-only tracks from recent contacts and
        # derive a combined target estimate (if any). We pass controlled_ids so
        # that friendly bearings between our own subs can be explicitly ignored.
        update_hostile_tracks(state, controlled_ids)
        if current_hostile_target:
            age = time.time() - current_hostile_target.get("updated_at", 0.0)
            log(
                f"hostile_target @ "
                f"({current_hostile_target['x']:.0f}, {current_hostile_target['y']:.0f}) "
                f"(age {age:.1f}s)"
            )
            # If posture / config allow it and both controlled subs have usable
            # and consistent hostile tracks and we have not fired yet for this
            # solution, trigger a torpedo shot (subject to ammo and battery checks).
            min_samples = 3
            observer_ids_with_tracks = [
                sid
                for sid in controlled_ids
                if sid in hostile_tracks and hostile_tracks[sid].get("sample_count", 0) >= min_samples
            ]
            if (
                len(observer_ids_with_tracks) >= 2
                and not has_fired_for_target
                and age < 10.0
                and not current_shot  # don't stack shots
                and auto_fire
                and posture != "ultra_quiet"
            ):
                # Check consistency between observers: their individual hostile
                # estimates must be reasonably close to each other.
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
                # Require the per-sub solutions to agree within a few km.
                if max_sep > 4000.0:
                    log(
                        f"hostile tracks inconsistent (max_sep={max_sep:.0f}m); "
                        f"waiting for better geometry before firing."
                    )
                    # Skip firing this tick; let subs continue to maneuver for a better solution.
                else:
                    # When exactly two observers contribute, refine the firing
                    # point as the geometric intersection of their most recent
                    # bearing lines, which is closer to the user's intent than
                    # a simple average of per-sub least-squares estimates.
                    if len(observer_ids_with_tracks) == 2:
                        oid1, oid2 = observer_ids_with_tracks
                        obs1 = by_id.get(oid1)
                        obs2 = by_id.get(oid2)
                        bt1 = hostile_trackers.get(oid1)
                        bt2 = hostile_trackers.get(oid2)
                        if obs1 and obs2 and bt1 and bt2 and bt1.samples and bt2.samples:
                            o1x = float(obs1.get("x", 0.0) or 0.0)
                            o1y = float(obs1.get("y", 0.0) or 0.0)
                            o2x = float(obs2.get("x", 0.0) or 0.0)
                            o2y = float(obs2.get("y", 0.0) or 0.0)
                            b1 = bt1.samples[-1].bearing_rad
                            b2 = bt2.samples[-1].bearing_rad
                            inter = _intersect_two_bearings(o1x, o1y, b1, o2x, o2y, b2)
                            if inter is not None:
                                ix, iy = inter
                                log(
                                    f"INTERSECT[{oid1[:6]},{oid2[:6]}]: "
                                    f"p1=({o1x:.0f},{o1y:.0f}) b1={compass_deg_from_rad(b1):.0f}°, "
                                    f"p2=({o2x:.0f},{o2y:.0f}) b2={compass_deg_from_rad(b2):.0f}° -> "
                                    f"ix=({ix:.0f},{iy:.0f})"
                                )
                                current_hostile_target["x"] = ix
                                current_hostile_target["y"] = iy
                    firing_subs = [by_id[sid] for sid in observer_ids_with_tracks if sid in by_id]
                    firing_sub = pick_firing_sub(
                        firing_subs,
                        (current_hostile_target["x"], current_hostile_target["y"]),
                    )
                    if firing_sub:
                        # Log per-sub geometry going into this shot.
                        for oid in observer_ids_with_tracks:
                            ht = hostile_tracks.get(oid, {})
                            estx = float(ht.get("x", 0.0) or 0.0)
                            esty = float(ht.get("y", 0.0) or 0.0)
                            obs = by_id.get(oid)
                            if obs:
                                ox = float(obs.get("x", 0.0) or 0.0)
                                oy = float(obs.get("y", 0.0) or 0.0)
                                brg_to_est = math.atan2(esty - ox, estx - oy)
                                brg_deg = compass_deg_from_rad(brg_to_est)
                                rng = math.hypot(esty - oy, estx - ox)
                                log(
                                    f"SHOT_GEOM[{oid[:6]}]: obs=({ox:.0f},{oy:.0f}) "
                                    f"-> est=({estx:.0f},{esty:.0f}) brg={brg_deg:.0f}° rng={rng:.0f}m"
                                )

                        # Check ammo and battery before firing.
                        ammo = int(firing_sub.get("torpedo_ammo", 0) or 0)
                        bat = float(firing_sub.get("battery", 0.0) or 0.0)
                        if ammo <= 0:
                            log(f"{firing_sub['id'][:6]}: no torpedo ammo available; skipping fire.")
                            # Try to reload if we have sufficient battery.
                            if bat > 30.0:
                                try:
                                    log(f"{firing_sub['id'][:6]}: attempting torpedo reload (battery={bat:.1f}%)")
                                    client.reload_torpedoes(firing_sub["id"])
                                except Exception as e:
                                    log(f"{firing_sub['id'][:6]}: reload_torpedoes exception: {e}")
                            # Do not attempt to fire this tick.
                            continue
                        # If battery is critically low, don't fire; conserve for survival.
                        if bat < 15.0:
                            log(f"{firing_sub['id'][:6]}: battery too low ({bat:.1f}%) for offensive shot; skipping fire.")
                            continue

                        log(
                            f"FIRING SOLUTION: launching torpedo from {firing_sub['id'][:6]} "
                            f"at hostile target ({current_hostile_target['x']:.0f}, "
                            f"{current_hostile_target['y']:.0f})"
                        )
                        # Fire in a background thread so we don't block the brain loop.
                        def _fire_once():
                            def _get_dynamic_target():
                                # Always use the latest fused hostile target from the brain
                                # if available; fall back to the snapshot if it disappears.
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

                        import threading as _th
                        _th.Thread(target=_fire_once, daemon=True).start()
                    # Initialize current_shot ETA based on simple R / v model (using config-ish defaults).
                    sx = float(firing_sub.get("x", 0.0) or 0.0)
                    sy = float(firing_sub.get("y", 0.0) or 0.0)
                    tx = float(current_hostile_target["x"])
                    ty = float(current_hostile_target["y"])
                    r0 = math.hypot(tx - sx, ty - sy)
                    # Approximate torpedo speed (m/s); default 6.0 from game_config, use 6 as baseline.
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

        # Check existing shot ETA: if torpedo likely missed, allow a refire.
        if current_shot:
            shot_age = time.time() - current_shot.get("fired_at", 0.0)
            eta_s = current_shot.get("eta_s", 0.0)
            if shot_age > eta_s:
                # Our torpedo has outlived its expected time-to-impact; treat as evaded.
                log(
                    f"Current shot exceeded ETA (age={shot_age:.1f}s > eta={eta_s:.1f}s); "
                    f"allowing potential refire on same solution."
                )
                current_shot["refires"] = int(current_shot.get("refires", 0)) + 1
                # Clear to permit another firing solution if conditions are still met.
                has_fired_for_target = False
                current_shot.clear()

        active_any = False
        for sid in controlled_ids:
            sub = by_id.get(sid)
            if not sub:
                continue
            active_any = True

            # High-priority: if a torpedo threat is detected for this sub,
            # perform an evasion maneuver and skip other behaviors this tick.
            if maybe_evade_torpedoes(client, sub, danger_range_m=2000.0, max_depth_step_m=60.0):
                continue

            mode, reason = energy_choose_mode(sub)
            log(f"{sub['id'][:6]}: energy_mode={mode} - {reason}")

            if mode == "refuel":
                energy_manage_refuel(client, sub)
            elif mode == "snorkel_recharge":
                energy_manage_snorkel_recharge(client, sub)
            else:
                # If we have a plausible hostile target, move toward it in a
                # simple two-sub formation. Otherwise, keep subs in formation
                # relative to each other using the leader's general nav.
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
                            # Leader: default nav (ring patrol / explore).
                            # Also ensure we are not stuck snorkeling once battery is healthy.
                            if sub.get("is_snorkeling") and float(sub.get("battery", 0.0) or 0.0) >= 95.0:
                                try:
                                    log(f"{sid[:6]}: battery full, forcing snorkel OFF and submerging to cruise depth {cruise_depth:.0f}m")
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
                            # Wingman: maintain side-by-side offset relative to leader.
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


