"""
Simple autonomous submarine agent for AISubBrawl.

Goals:
  - Survive as long as possible.
  - Manage battery and diesel fuel (use snorkel + fuelers).
  - Patrol around the ring and perform basic engagement when contacts appear.

Usage (example):
  SUB_BRAWL_API_KEY=... python bots/simple_agent.py http://localhost:5000
"""

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Tuple

from .client import SubBrawlClient, wait_for_subs
from .energy_manager import (
    choose_mode as energy_choose_mode,
    manage_refuel as energy_manage_refuel,
    manage_snorkel_recharge as energy_manage_snorkel_recharge,
)


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [agent] {msg}")


def compass_deg_from_rad(rad: float) -> float:
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


def choose_mode(sub: Dict[str, Any]) -> Tuple[str, str]:
    """
    Very simple mode selector for now.
    Modes: 'refuel', 'hunt', 'patrol'
    Returns (mode, reason).
    """
    fuel = float(sub.get("fuel", 0.0) or 0.0)
    bat = float(sub.get("battery", 0.0) or 0.0)

    # If both fuel and battery are low, prioritize refuel
    if fuel < 200.0 or (bat < 25.0 and fuel < 400.0):
        return "refuel", f"low resources (fuel={fuel:.0f}, bat={bat:.0f})"

    # TODO: when we start tracking contacts, return 'hunt' if enemy present
    return "patrol", f"normal patrol (fuel={fuel:.0f}, bat={bat:.0f})"


def manage_refuel(client: SubBrawlClient, sub: Dict[str, Any]) -> None:
    """
    High-level refuel behavior:
      - If no fueler exists yet, request one.
      - Move toward nearest fueler if far away.
      - Once within 50m, start server-side refueling and hold position.
    This relies on the server's /start_refuel behavior to moor the sub.
    """
    st = client.get_state()
    fuelers = st.get("fuelers") or []
    fuel = float(sub.get("fuel", 0.0) or 0.0)

    # If already full, nothing to do
    if fuel >= 1000.0:
        log(f"{sub['id'][:6]}: fuel full, skipping refuel")
        return

    # If we don't have a fueler yet, try to call one
    if not fuelers:
        log(f"{sub['id'][:6]}: no fueler present, requesting one")
        try:
            resp = client.call_fueler(sub["id"])
            if not resp.get("ok"):
                log(f"{sub['id'][:6]}: call_fueler failed: {resp.get('error')}")
        except Exception as e:
            log(f"{sub['id'][:6]}: call_fueler exception: {e}")
        return

    # Find nearest fueler
    best = None
    nearest = None
    for f in fuelers:
        dx = f["x"] - sub["x"]
        dy = f["y"] - sub["y"]
        dz = (f.get("depth") or 0.0) - sub["depth"]
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        if best is None or d < best:
            best = d
            nearest = f

    if nearest is None or best is None:
        log(f"{sub['id'][:6]}: no reachable fueler found despite list")
        return

    # If we're far, move toward fueler on the surface
    if best > 80.0:
        # head toward fueler
        heading_rad = math.atan2(nearest["y"] - sub["y"], nearest["x"] - sub["x"])
        heading_deg = compass_deg_from_rad(heading_rad)
        log(f"{sub['id'][:6]}: closing on fueler, range ~{best:.0f}m, heading {heading_deg:.0f}°")
        try:
            client.set_sub_heading(sub["id"], heading_deg)
            # Modest throttle to avoid overshooting too hard
            client.control_sub(sub["id"], throttle=0.3)
        except Exception as e:
            log(f"{sub['id'][:6]}: error sending close-to-fueler controls: {e}")
        return

    # Once reasonably close, ask server to start refuel and let it hold us
    if not sub.get("refuel_active"):
        log(f"{sub['id'][:6]}: within {best:.0f}m of fueler, requesting start_refuel")
        try:
            resp = client.start_refuel(sub["id"])
            if not resp.get("ok"):
                log(f"{sub['id'][:6]}: start_refuel failed: {resp.get('error')}")
        except Exception as e:
            log(f"{sub['id'][:6]}: start_refuel exception: {e}")


def patrol_ring(client: SubBrawlClient, sub: Dict[str, Any], center=(0.0, 0.0), radius=4000.0) -> None:
    """
    Simple patrol: try to stay near a circle of given radius around the origin.
    """
    cx, cy = center
    dx = sub["x"] - cx
    dy = sub["y"] - cy
    r = math.hypot(dx, dy)

    # If too far inside, steer outward; if too far outside, steer inward.
    # Otherwise, roughly follow a tangent to circle.
    if r < radius - 300.0:
        # steer outward
        desired_heading = compass_deg_from_rad(math.atan2(dy, dx))
    elif r > radius + 300.0:
        # steer inward
        desired_heading = compass_deg_from_rad(math.atan2(-dy, -dx))
    else:
        # roughly tangent (90 deg left of radial)
        radial = math.atan2(dy, dx)
        tangent = radial + math.pi / 2.0
        desired_heading = compass_deg_from_rad(tangent)

    try:
        client.set_sub_heading(sub["id"], desired_heading)
        client.control_sub(sub["id"], throttle=0.4)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Simple AISubBrawl autonomous agent")
    parser.add_argument(
        "base_url",
        help="Base URL of the AISubBrawl server (e.g. http://localhost:5000)",
    )
    parser.add_argument(
        "--api-key",
        dest="api_key",
        help="Existing API key to use instead of auto-signup",
        default=None,
    )
    parser.add_argument(
        "--state-file",
        dest="state_file",
        help="Path to JSON file where API key and sub IDs are stored",
        default="agent_state.json",
    )
    args = parser.parse_args()

    base_url = args.base_url

    # Resolve state file path relative to this script, unless an absolute path is given.
    if os.path.isabs(args.state_file):
        state_path = args.state_file
    else:
        state_path = os.path.join(os.path.dirname(__file__), args.state_file)

    client = SubBrawlClient(base_url)

    # 1) Highest priority: explicit --api-key argument.
    if args.api_key:
        client.set_api_key(args.api_key)
        log("Using API key provided via --api-key")
    else:
        # 2) Next: state file (if present) for cached API key.
        state = None
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except Exception as e:
                log(f"Failed to read state file {state_path}: {e}")

        if state and state.get("api_key"):
            client.set_api_key(state["api_key"])
            log(f"Using API key loaded from {state_path}")
        elif not client.api_key:
            # 3) Fallback: no key anywhere -> auto-signup and persist.
            import secrets

            username = f"agent_{int(time.time())}_{secrets.token_hex(4)}"
            password = secrets.token_hex(8)
            print(f"[agent] No API key, signing up as {username}")
            try:
                resp = client.signup(username, password)
            except Exception as e:
                print("[agent] signup failed:", e, file=sys.stderr)
                sys.exit(1)
            api_key = resp.get("api_key") or resp.get("token")
            if not api_key:
                print("[agent] signup did not return an api_key field", file=sys.stderr)
                sys.exit(1)
            client.set_api_key(api_key)
            print(f"[agent] Credentials -> username: {username}  password: {password}")
            print(f"[agent] Obtained API key {api_key}")

            # Persist to state file for later reuse.
            state = {
                "base_url": base_url,
                "api_key": api_key,
                "subs": [],
                "created_at": time.time(),
                "username": username,
            }
            try:
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
                log(f"Saved API key and metadata to {state_path}")
            except Exception as e:
                log(f"Failed to write state file {state_path}: {e}")

    # Ensure we have at least two submarines
    try:
        state = client.get_state()
    except Exception:
        state = {"subs": []}

    subs: List[Dict[str, Any]] = state.get("subs") or []
    while len(subs) < 2:
        try:
            print(f"[agent] Spawning submarine {len(subs)+1}/2")
            client.register_sub()
        except Exception as e:
            print("[agent] register_sub failed:", e, file=sys.stderr)
            time.sleep(1.0)
        state = client.get_state()
        subs = state.get("subs") or []

    # Track the IDs of subs we control (first two)
    controlled_ids = [s["id"] for s in subs[:2]]
    print(f"[agent] Controlling submarines: {controlled_ids}")

    # Update state file with controlled sub IDs for later use (if we have a state file).
    if client.api_key:
        current_state: Dict[str, Any] = {
            "base_url": base_url,
            "api_key": client.api_key,
            "subs": controlled_ids,
            "updated_at": time.time(),
        }
        # Try to preserve any extra fields (e.g., username) from existing state.
        try:
            if os.path.exists(state_path):
                with open(state_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                existing.update(current_state)
                current_state = existing
        except Exception:
            pass
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(current_state, f, indent=2)
            log(f"Updated state file {state_path} with sub IDs")
        except Exception as e:
            log(f"Failed to update state file {state_path}: {e}")

    # Very simple loop controlling both subs
    while True:
        try:
            state = client.get_state()
        except Exception as e:
            print("[agent] state fetch failed:", e, file=sys.stderr)
            time.sleep(1.0)
            continue

        subs = state.get("subs") or []
        by_id = {s["id"]: s for s in subs}

        active_any = False
        for sid in controlled_ids:
            sub = by_id.get(sid)
            if not sub:
                continue
            active_any = True
            mode, reason = energy_choose_mode(sub)
            log(f"{sub['id'][:6]}: mode={mode} - {reason}")
            if mode == "refuel":
                energy_manage_refuel(client, sub)
            elif mode == "snorkel_recharge":
                energy_manage_snorkel_recharge(client, sub)
            else:
                patrol_ring(client, sub)

        if not active_any:
            print("[agent] All controlled subs gone, exiting.")
            break

        time.sleep(0.5)


if __name__ == "__main__":
    main()


