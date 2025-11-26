"""
Torpedo battery-aware speed manager for AISubBrawl.

This script adjusts torpedo propulsion speed based on:
  - Remaining torpedo battery, and
  - Estimated range to the target (provided by the caller).

Goal:
  - For a given target range, choose a speed that *should* allow the torpedo
    to reach that range with a configurable safety margin, while conserving
    battery when the target is far away.

Usage example:

  python -m bots.torpedo_manager http://localhost:5000 \\
    --torp-id T_ORP_ID \\
    --target-range-m 2500 \\
    --safety-factor 1.3

You can call this from a higher-level agent whenever you have an updated
estimate of target range (from passive_tracker, active sonar, etc.).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List

from .client import SubBrawlClient


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [torp] {msg}")


def recommend_speed_for_range(
    battery_pct: float,
    required_range_m: float,
    drain_per_mps_per_s: float,
    min_speed: float,
    max_speed: float,
    safety_factor: float = 1.2,
) -> float:
    """
    Recommend a torpedo speed to (roughly) reach required_range_m with the
    available battery, assuming drain ~ k * v^2.

    Model:
      drain_rate = k * v^2  (battery % per second)
      endurance_time = B / (k * v^2)
      max_distance  = v * endurance_time = B / (k * v)

    We want max_distance >= safety_factor * required_range, so:
      B / (k * v) >= safety * R  =>  v <= B / (k * safety * R)

    We pick v_target = clamp( B / (k * safety * R), min_speed, max_speed ).
    """
    B = max(0.0, float(battery_pct))
    R = max(1.0, float(required_range_m))
    k = max(1e-6, float(drain_per_mps_per_s))
    sf = max(1.0, float(safety_factor))

    # Theoretical upper bound on speed for the desired range.
    v_max_for_range = B / (k * sf * R)

    # Clamp into allowed band.
    v = max(min_speed, min(v_max_for_range, max_speed))
    return v


def main() -> None:
    parser = argparse.ArgumentParser(description="Torpedo battery-aware speed manager")
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
        help="Optional JSON file to read API key and default torpedo IDs from",
        default="agent_state.json",
    )
    parser.add_argument(
        "--torp-id",
        dest="torp_ids",
        action="append",
        help=(
            "Torpedo ID to manage (can be given multiple times). "
            "If omitted, applies to all of your live torpedoes from /state."
        ),
    )
    parser.add_argument(
        "--target-range-m",
        type=float,
        required=True,
        help="Estimated range to target in meters (used to size torpedo speed)",
    )
    parser.add_argument(
        "--safety-factor",
        type=float,
        default=1.2,
        help="Safety multiplier on required range when sizing speed (default: 1.2)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Control loop interval in seconds (default: 1.0)",
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

    # Torpedo config from game_config.json; these are static defaults.
    # Mirror the keys so we don't need to query the server for them.
    TORP_BATTERY_CAPACITY = 100.0
    TORP_DRAIN_PER_MPS_PER_S = 0.0015
    TORP_MIN_SPEED = 8.0
    TORP_MAX_SPEED = 24.0

    target_range_m = float(args.target_range_m)
    safety_factor = float(args.safety_factor)

    log(
        f"Managing torpedoes for target_range={target_range_m:.0f}m, "
        f"safety_factor={safety_factor:.2f}"
    )

    while True:
        try:
            st = client.get_state()
        except Exception as e:
            log(f"state fetch failed: {e}")
            time.sleep(args.interval)
            continue

        torps: List[Dict[str, Any]] = st.get("torpedoes") or []

        # Determine which torpedoes to manage this tick.
        if args.torp_ids:
            managed = [t for t in torps if t.get("id") in args.torp_ids]
        else:
            managed = torps

        if not managed and args.torp_ids:
            log("No matching torpedoes from /state for given --torp-id values; waiting...")

        for t in managed:
            tid = t.get("id")
            if not tid:
                continue

            battery = float(t.get("battery", TORP_BATTERY_CAPACITY) or 0.0)
            # If battery is already dead or nearly dead, there's nothing to manage.
            if battery <= 0.0:
                log(f"{tid[:6]}: battery depleted, skipping speed adjustment")
                continue

            rec_speed = recommend_speed_for_range(
                battery_pct=battery,
                required_range_m=target_range_m,
                drain_per_mps_per_s=TORP_DRAIN_PER_MPS_PER_S,
                min_speed=TORP_MIN_SPEED,
                max_speed=TORP_MAX_SPEED,
                safety_factor=safety_factor,
            )

            try:
                resp = client.set_torp_speed(tid, rec_speed)
                if not resp.get("ok", True):
                    log(f"{tid[:6]}: set_torp_speed error: {resp.get('error')}")
                else:
                    log(
                        f"{tid[:6]}: battery={battery:.1f}%, "
                        f"set target_speed={rec_speed:.1f} m/s for range≈{target_range_m:.0f}m"
                    )
            except Exception as e:
                log(f"{tid[:6]}: set_torp_speed exception: {e}")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()


