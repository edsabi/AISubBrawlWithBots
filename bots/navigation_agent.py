"""
Navigation agent that uses the hazard scanner when heading outside the ring.

Goals:
  - Monitor one or more friendly submarines.
  - When a sub moves outside the inner ring and is heading roughly outward,
    periodically use the hazard scanner (/weather_scan) to detect nearby
    hazard fields.
  - If a hazard is detected along the current heading, adjust course to
    steer around it.

This is intentionally conservative: it only intervenes when outside the ring
and roughly heading away from the center, and only when a scan shows a hazard
near the forward sector.
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
    print(f"[{ts}] [nav] {msg}")


def compass_deg_from_rad(rad: float) -> float:
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


def bearing_diff_deg(a_deg: float, b_deg: float) -> float:
    d = (a_deg - b_deg + 180.0) % 360.0 - 180.0
    return abs(d)


def main() -> None:
    parser = argparse.ArgumentParser(description="Navigation agent with hazard avoidance outside the ring")
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
            "Submarine ID(s) to manage. "
            "If omitted, all of your subs from /state are considered."
        ),
    )
    parser.add_argument(
        "--ring-radius-m",
        type=float,
        default=6000.0,
        help="Inner ring radius in meters (default: 6000, from game_config.json)",
    )
    parser.add_argument(
        "--scan-interval-s",
        type=float,
        default=5.0,
        help="Minimum seconds between hazard scans per sub (default: 5)",
    )
    parser.add_argument(
        "--forward-sector-deg",
        type=float,
        default=45.0,
        help="Half-width of forward sector to treat as 'in our path' (default: 45°)",
    )
    parser.add_argument(
        "--avoid-turn-deg",
        type=float,
        default=40.0,
        help="Heading change to sidestep around a detected hazard (default: 40°)",
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

    # Track last scan time per sub to avoid spamming the scanner.
    last_scan_time: Dict[str, float] = {}

    ring_r = float(args.ring_radius_m)

    while True:
        now = time.time()
        try:
            st = client.get_state()
        except Exception as e:
            log(f"state fetch failed: {e}")
            time.sleep(args.interval)
            continue

        subs: List[Dict[str, Any]] = st.get("subs") or []
        if args.sub_ids:
            subs = [s for s in subs if s.get("id") in args.sub_ids]

        for s in subs:
            sid = s.get("id")
            if not sid:
                continue

            x = float(s.get("x", 0.0) or 0.0)
            y = float(s.get("y", 0.0) or 0.0)
            heading_rad = float(s.get("heading", 0.0) or 0.0)

            r = math.hypot(x, y)

            # Only consider scanning when we're outside the ring and more or less
            # heading outward (to reduce noise/energy use).
            if r <= ring_r:
                continue

            radial_out_rad = math.atan2(y, x)  # from center (0,0) to sub position
            radial_out_deg = compass_deg_from_rad(radial_out_rad)
            heading_deg = compass_deg_from_rad(heading_rad)

            if bearing_diff_deg(heading_deg, radial_out_deg) > 60.0:
                # Not generally heading outward; skip hazard scanning for this tick.
                continue

            # Check scan rate limiting.
            last = last_scan_time.get(sid, 0.0)
            if now - last < args.scan_interval_s:
                continue

            # Perform a hazard scan.
            try:
                resp = client.weather_scan(sid)
            except Exception as e:
                log(f"{sid[:6]}: weather_scan exception: {e}")
                continue

            if not resp.get("ok", True):
                # Could be low battery etc.
                log(f"{sid[:6]}: weather_scan error: {resp.get('error')}")
                last_scan_time[sid] = now
                continue

            detections = resp.get("detections") or resp.get("clouds") or []
            if not detections:
                log(f"{sid[:6]}: hazard scan: no hazards detected within range")
                last_scan_time[sid] = now
                continue

            # Look for hazards roughly ahead within the forward sector.
            forward_sector = float(args.forward_sector_deg)
            hazards_ahead: List[Dict[str, Any]] = []

            for d in detections:
                brg_deg = float(d.get("bearing_deg", 0.0) or 0.0)
                if bearing_diff_deg(brg_deg, heading_deg) <= forward_sector:
                    hazards_ahead.append(d)

            if not hazards_ahead:
                log(f"{sid[:6]}: hazard scan: hazards present but none directly ahead")
                last_scan_time[sid] = now
                continue

            # Choose the closest hazard ahead.
            closest = min(hazards_ahead, key=lambda d: float(d.get("range", 0.0) or 0.0))
            h_brg_deg = float(closest.get("bearing_deg", 0.0) or 0.0)
            h_rng = float(closest.get("range", 0.0) or 0.0)

            # Decide whether to sidestep left or right: pick the side with larger gap
            # between hazard bearing and heading.
            # Simple rule: if hazard is slightly to the right, turn left, and vice versa.
            turn_sign = -1.0 if ((h_brg_deg - heading_deg + 360.0) % 360.0) < 180.0 else 1.0
            new_heading_deg = (heading_deg + turn_sign * float(args.avoid_turn_deg)) % 360.0

            log(
                f"{sid[:6]}: hazard ahead at brg={h_brg_deg:.0f}°, rng={h_rng:.0f}m; "
                f"turning to new heading {new_heading_deg:.0f}° to evade"
            )

            try:
                client.set_sub_heading(sid, new_heading_deg)
            except Exception as e:
                log(f"{sid[:6]}: set_sub_heading exception during hazard avoidance: {e}")

            last_scan_time[sid] = now

        time.sleep(args.interval)


if __name__ == "__main__":
    main()


