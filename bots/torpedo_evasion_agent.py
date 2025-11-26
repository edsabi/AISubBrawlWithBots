"""
Torpedo evasion agent for AISubBrawl.

Goal:
  - Monitor enemy torpedoes around our submarines.
  - When a torpedo is detected within a danger radius, command an evasive
    maneuver (lateral turn and optional depth change) to increase miss
    probability.

This agent only uses existing APIs and does not modify server behaviour.
It is meant to run alongside other agents (engagement, exploration, etc.).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Tuple

from .client import SubBrawlClient


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [evade] {msg}")


def compass_deg_from_rad(rad: float) -> float:
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


def range_class_from_dist(dist_m: float) -> str:
    """
    Approximate the server's passive range_class thresholds:
      - 'short'  if rng < 1200
      - 'medium' if rng < 3000
      - 'long'   otherwise
    """
    d = float(dist_m)
    if d < 1200.0:
        return "short"
    if d < 3000.0:
        return "medium"
    return "long"


def main() -> None:
    parser = argparse.ArgumentParser(description="Torpedo evasion agent for AISubBrawl")
    parser.add_argument(
        "base_url",
        help="Base URL of the AISubBrawl server (e.g. http://localhost:5000)",
    )
    parser.add_argument(
        "--api-key",
        dest="api_key",
        help="API key to use (overrides state file and environment)",
        default=None,
    )
    parser.add_argument(
        "--state-file",
        dest="state_file",
        help="Optional JSON file to read API key and default sub IDs",
        default="agent_state.json",
    )
    parser.add_argument(
        "--sub-id",
        dest="sub_ids",
        action="append",
        help=(
            "Submarine ID(s) to protect. "
            "If omitted, all of your subs from /state are considered."
        ),
    )
    parser.add_argument(
        "--danger-range-m",
        type=float,
        default=2000.0,
        help="Range inside which a torpedo is considered a threat (default: 2000m)",
    )
    parser.add_argument(
        "--max-evade-depth-step-m",
        type=float,
        default=60.0,
        help="Maximum depth change (up or down) when evading (default: 60m)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Main control loop interval in seconds (default: 0.5)",
    )

    args = parser.parse_args()

    base_url = args.base_url

    # Resolve state file path relative to this module, unless absolute.
    if os.path.isabs(args.state_file):
        state_path = args.state_file
    else:
        state_path = os.path.join(os.path.dirname(__file__), args.state_file)

    client = SubBrawlClient(base_url)

    # Resolve API key: CLI arg > state file > existing client.api_key / env.
    if args.api_key:
        client.set_api_key(args.api_key)
        log("Using API key from --api-key")
        state_meta: Dict[str, Any] = {}
    else:
        state_meta = {}
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state_meta = json.load(f)
            except Exception as e:
                log(f"Failed to read state file {state_path}: {e}")

        if state_meta.get("api_key"):
            client.set_api_key(state_meta["api_key"])
            log(f"Using API key from state file {state_path}")

    if not client.api_key:
        log("No API key available (provide --api-key, state file with api_key, or SUB_BRAWL_API_KEY)")
        sys.exit(1)

    danger_range = float(args.danger_range_m)
    max_depth_step = float(args.max_evade_depth_step_m)

    # Track last observed range_class per (sub, torp) so we can see if the
    # threat is closing (e.g. long->medium->short).
    last_range_class: Dict[Tuple[str, str], str] = {}

    while True:
        try:
            st = client.get_state()
        except Exception as e:
            log(f"state fetch failed: {e}")
            time.sleep(args.interval)
            continue

        subs: List[Dict[str, Any]] = st.get("subs") or []
        torps: List[Dict[str, Any]] = st.get("torpedoes") or []

        if args.sub_ids:
            subs = [s for s in subs if s.get("id") in args.sub_ids]

        for sub in subs:
            sid = sub.get("id")
            if not sid:
                continue

            sx = float(sub.get("x", 0.0) or 0.0)
            sy = float(sub.get("y", 0.0) or 0.0)
            sz = float(sub.get("depth", 0.0) or 0.0)

            # Find nearest torpedo.
            nearest = None
            best_r = None
            for t in torps:
                # Skip our own torpedoes.
                # Server does not include owner_id in _torp_pub, so we assume all
                # torpedoes in /state belong to this user (friendly). If a future
                # state endpoint exposes global torps, we'd filter by owner_id here.
                # For now, just treat all as potential threats if we see any.
                tx = float(t.get("x", 0.0) or 0.0)
                ty = float(t.get("y", 0.0) or 0.0)
                tz = float(t.get("depth", 0.0) or 0.0)
                rng = math.sqrt((tx - sx) ** 2 + (ty - sy) ** 2 + (tz - sz) ** 2)
                if best_r is None or rng < best_r:
                    best_r = rng
                    nearest = t

            if not nearest or best_r is None:
                continue

            tid_full = nearest.get("id", "") or ""
            tid = tid_full[:6]
            tx = float(nearest.get("x", 0.0) or 0.0)
            ty = float(nearest.get("y", 0.0) or 0.0)
            tz = float(nearest.get("depth", 0.0) or 0.0)

            # Always treat as a threat if inside overall danger range, but
            # use closing / non-closing to decide *how* to maneuver.
            if best_r > danger_range:
                continue

            current_rc = range_class_from_dist(best_r)
            key = (sid, tid_full)
            prev_rc = last_range_class.get(key)

            # Determine if this threat is clearly closing in range_class space.
            closing = False
            # If we're already in 'short' band, treat as closing regardless of history.
            if current_rc == "short":
                closing = True
            elif current_rc == "medium" and prev_rc == "long":
                closing = True

            last_range_class[key] = current_rc

            # Bearing from torpedo to sub (direction of incoming threat).
            incoming_brg_rad = math.atan2(sy - ty, sx - tx)
            incoming_brg_deg = compass_deg_from_rad(incoming_brg_rad)

            # Choose evasive behavior based on closing vs non-closing.
            if closing:
                # Strong dodge: ~90° lateral turn + depth step.
                evade_turn = 90.0
                depth_delta = tz - sz
                if abs(depth_delta) < max_depth_step:
                    step = max_depth_step if depth_delta > 0 else -max_depth_step
                    target_depth = max(0.0, sz + step)
                else:
                    target_depth = sz
                maneuver_desc = "CLOSING"
            else:
                # Threat detected but not clearly closing yet: lighter sidestep.
                evade_turn = 30.0
                target_depth = sz
                maneuver_desc = "THREAT"

            # Turn to the right of the incoming bearing for now.
            evade_heading_deg = (incoming_brg_deg + evade_turn) % 360.0

            log(
                f"{sid[:6]}: {maneuver_desc} torp {tid} at range={best_r:.0f}m "
                f"(rc={current_rc}, prev={prev_rc}), "
                f"incoming_brg={incoming_brg_deg:.0f}°, "
                f"new_heading={evade_heading_deg:.0f}°, target_depth={target_depth:.0f}m"
            )

            try:
                client.set_sub_heading(sid, evade_heading_deg)
                client.control_sub(sid, throttle=1.0, target_depth=target_depth)
            except Exception as e:
                log(f"{sid[:6]}: control error during evasion: {e}")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()


