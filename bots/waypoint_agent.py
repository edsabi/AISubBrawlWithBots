"""
Simple waypoint navigation agent for AISubBrawl.

Goal:
  - Drive one or more submarines toward a specified (x, y, depth) waypoint.

Behavior:
  - For each managed sub:
      * Compute bearing from current position to target (x, y).
      * Set heading to that bearing.
      * Set throttle toward a configured value.
      * Set target depth toward the requested depth.
  - Continues until all managed subs are within a distance/depth tolerance
    of the waypoint, then exits (unless --no-exit is specified in future).
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
    print(f"[{ts}] [wp] {msg}")


def compass_deg_from_rad(rad: float) -> float:
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


def main() -> None:
    parser = argparse.ArgumentParser(description="Waypoint navigation agent for AISubBrawl")
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
            "Submarine ID(s) to send to the waypoint. "
            "If omitted, all of your subs from /state are used."
        ),
    )
    parser.add_argument(
        "--target-x",
        type=float,
        required=True,
        help="Target X coordinate in world meters",
    )
    parser.add_argument(
        "--target-y",
        type=float,
        required=True,
        help="Target Y coordinate in world meters",
    )
    parser.add_argument(
        "--target-depth-m",
        type=float,
        default=100.0,
        help="Target depth in meters (default: 100m)",
    )
    parser.add_argument(
        "--throttle",
        type=float,
        default=0.5,
        help="Throttle setting [0..1] while transiting to waypoint (default: 0.5)",
    )
    parser.add_argument(
        "--xy-tolerance-m",
        type=float,
        default=100.0,
        help="Distance tolerance in meters to consider waypoint reached (default: 100m)",
    )
    parser.add_argument(
        "--depth-tolerance-m",
        type=float,
        default=10.0,
        help="Depth tolerance in meters to consider waypoint reached (default: 10m)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Control loop interval in seconds (default: 0.5)",
    )

    args = parser.parse_args()

    base_url = args.base_url
    target_x = float(args.target_x)
    target_y = float(args.target_y)
    target_depth = float(args.target_depth_m)
    throttle = max(0.0, min(1.0, float(args.throttle)))
    xy_tol = max(1.0, float(args.xy_tolerance_m))
    depth_tol = max(0.5, float(args.depth_tolerance_m))

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

    log(
        f"Waypoint agent: target=({target_x:.1f}, {target_y:.1f}, {target_depth:.1f}m), "
        f"throttle={throttle:.2f}, tol={xy_tol:.0f}m / {depth_tol:.0f}m"
    )

    while True:
        try:
            st = client.get_state()
        except Exception as e:
            log(f"state fetch failed: {e}")
            time.sleep(args.interval)
            continue

        subs: List[Dict[str, Any]] = st.get("subs") or []
        if args.sub_ids:
            subs = [s for s in subs if s.get("id") in args.sub_ids]

        if not subs:
            log("No submarines found to navigate; exiting.")
            break

        all_reached = True

        for s in subs:
            sid = s.get("id")
            if not sid:
                continue

            sx = float(s.get("x", 0.0) or 0.0)
            sy = float(s.get("y", 0.0) or 0.0)
            sz = float(s.get("depth", 0.0) or 0.0)

            dx = target_x - sx
            dy = target_y - sy
            dxy = math.hypot(dx, dy)
            dz = target_depth - sz

            reached_xy = dxy <= xy_tol
            reached_z = abs(dz) <= depth_tol

            if reached_xy and reached_z:
                log(f"{sid[:6]}: waypoint reached (dxy={dxy:.0f}m, dz={dz:.0f}m)")
                continue

            all_reached = False

            # Heading toward target XY
            heading_rad = math.atan2(dy, dx)
            heading_deg = compass_deg_from_rad(heading_rad)

            log(
                f"{sid[:6]}: steering to waypoint: "
                f"dxy={dxy:.0f}m, dz={dz:.0f}m, "
                f"heading={heading_deg:.0f}°, throttle={throttle:.2f}, "
                f"target_depth={target_depth:.0f}m"
            )

            try:
                client.set_sub_heading(sid, heading_deg)
                client.control_sub(sid, throttle=throttle, target_depth=target_depth)
            except Exception as e:
                log(f"{sid[:6]}: control error while driving to waypoint: {e}")

        if all_reached:
            log("All managed submarines have reached the waypoint; exiting.")
            break

        time.sleep(args.interval)


if __name__ == "__main__":
    main()


