"""
Engagement / navigation agent for AISubBrawl.

This script assumes that some other process has already "detected" a contact
and produced a target position in world coordinates. Given that target and one
or more friendly subs, this agent:

  - Steers subs toward the target until within a desired standoff distance.
  - Once near standoff, transitions to an orbiting pattern around the target.
  - If too close, opens the range again.

It does *not* query or change server-side detection logic, so it will not
affect how your two bot subs detect each other on the server.
"""

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
    print(f"[{ts}] [engage] {msg}")


def compass_deg_from_rad(rad: float) -> float:
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


def compute_heading_and_throttle(
    sub: Dict[str, Any],
    target_xy: Tuple[float, float],
    standoff_m: float,
) -> Tuple[float, float, str]:
    """
    Given a sub and a target (x, y) in world coordinates, compute a desired
    heading (deg) and throttle [0..1] to achieve a simple engagement pattern:

      - If far outside standoff + margin: close directly.
      - If within standoff band: orbit roughly tangentially.
      - If inside standoff - margin: open range.
    """
    sx = float(sub["x"])
    sy = float(sub["y"])
    tx, ty = target_xy

    dx = tx - sx
    dy = ty - sy
    r = math.hypot(dx, dy)

    # Margins to avoid constant flipping at exactly standoff distance.
    outer_band = standoff_m + 150.0
    inner_band = max(50.0, standoff_m - 150.0)

    if r > outer_band:
        # Too far: close directly.
        heading_rad = math.atan2(dy, dx)
        heading_deg = compass_deg_from_rad(heading_rad)
        throttle = 0.7
        reason = f"closing (range {r:.0f}m > outer {outer_band:.0f}m)"
    elif r < inner_band:
        # Too close: turn away to open range.
        heading_rad = math.atan2(-dy, -dx)
        heading_deg = compass_deg_from_rad(heading_rad)
        throttle = 0.6
        reason = f"opening (range {r:.0f}m < inner {inner_band:.0f}m)"
    else:
        # Within the standoff band: roughly orbit tangentially.
        radial = math.atan2(dy, dx)
        tangent = radial + math.pi / 2.0  # 90° left of radial
        heading_deg = compass_deg_from_rad(tangent)
        throttle = 0.5
        reason = f"orbiting (range {r:.0f}m ≈ standoff {standoff_m:.0f}m)"

    return heading_deg, throttle, reason


def main() -> None:
    parser = argparse.ArgumentParser(description="Engagement/navigation agent for AISubBrawl")
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
        help="Submarine ID to navigate (can be given multiple times). "
             "If omitted, falls back to 'subs' list in state file.",
    )
    parser.add_argument(
        "--target-x",
        type=float,
        required=True,
        help="Target X coordinate in world meters (contact position, resolved upstream)",
    )
    parser.add_argument(
        "--target-y",
        type=float,
        required=True,
        help="Target Y coordinate in world meters (contact position, resolved upstream)",
    )
    parser.add_argument(
        "--standoff-m",
        type=float,
        default=800.0,
        help="Desired standoff distance from target in meters (default: 800)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Control loop interval in seconds (default: 0.5)",
    )

    args = parser.parse_args()

    base_url = args.base_url
    target_xy = (args.target_x, args.target_y)
    standoff_m = args.standoff_m

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
        state: Dict[str, Any] = {}
    else:
        state = {}
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except Exception as e:
                log(f"Failed to read state file {state_path}: {e}")

        if state.get("api_key"):
            client.set_api_key(state["api_key"])
            log(f"Using API key from state file {state_path}")

    if not client.api_key:
        log("No API key available (provide --api-key, state file with api_key, or SUB_BRAWL_API_KEY)")
        sys.exit(1)

    # Determine which sub IDs to manage.
    sub_ids: List[str] = []
    if args.sub_ids:
        sub_ids = args.sub_ids
    else:
        subs_from_state = state.get("subs") if isinstance(state, dict) else None
        if isinstance(subs_from_state, list):
            sub_ids = [str(sid) for sid in subs_from_state]

    if not sub_ids:
        log("No sub IDs provided via --sub-id or state file; nothing to manage.")
        sys.exit(1)

    log(f"Engaging target at ({target_xy[0]:.1f}, {target_xy[1]:.1f}) with standoff {standoff_m:.0f}m")
    log(f"Navigating subs: {sub_ids}")

    while True:
        try:
            st = client.get_state()
        except Exception as e:
            log(f"state fetch failed: {e}")
            time.sleep(args.interval)
            continue

        subs = st.get("subs") or []
        by_id: Dict[str, Dict[str, Any]] = {s["id"]: s for s in subs}

        for sid in sub_ids:
            sub = by_id.get(sid)
            if not sub:
                log(f"{sid[:6]}: not present in current state, skipping this tick")
                continue

            heading_deg, throttle, reason = compute_heading_and_throttle(
                sub, target_xy, standoff_m
            )
            log(f"{sub['id'][:6]}: {reason}, heading {heading_deg:.0f}°, throttle {throttle:.2f}")

            try:
                client.set_sub_heading(sub["id"], heading_deg)
                client.control_sub(sub["id"], throttle=throttle)
            except Exception as e:
                log(f"{sub['id'][:6]}: control error: {e}")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()


