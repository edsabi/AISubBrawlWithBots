"""
Aggressive engagement agent for AISubBrawl using active sonar.

Goal:
  - Put one or more submarines into an aggressive "hunt" posture:
      * Higher speed toward their current heading.
      * Periodic wide-beam active pings ahead to light up contacts.

This agent does not parse SSE contact events itself; it is meant to be
run alongside other agents (e.g., fire_control_agent, engagement_agent,
or a human in the UI) that react to the active returns.
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
    print(f"[{ts}] [aggr] {msg}")


def compass_deg_from_rad(rad: float) -> float:
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggressive engagement agent (active sonar hunting)")
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
            "Submarine ID(s) to run in aggressive mode. "
            "If omitted, all of your subs from /state are used."
        ),
    )
    parser.add_argument(
        "--throttle",
        type=float,
        default=0.7,
        help="Aggressive throttle setting [0..1] (default: 0.7)",
    )
    parser.add_argument(
        "--ping-interval-s",
        type=float,
        default=8.0,
        help="Seconds between active pings per sub (default: 8)",
    )
    parser.add_argument(
        "--ping-range-m",
        type=float,
        default=4000.0,
        help="Max range for active pings in meters (default: 4000)",
    )
    parser.add_argument(
        "--ping-beam-deg",
        type=float,
        default=60.0,
        help="Active ping beam width in degrees (default: 60°)",
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

    throttle = max(0.0, min(1.0, float(args.throttle)))
    ping_interval = max(1.0, float(args.ping_interval_s))
    ping_range = max(100.0, float(args.ping_range_m))
    ping_beam = max(5.0, min(210.0, float(args.ping_beam_deg)))

    log(
        f"Aggressive engagement: throttle={throttle:.2f}, "
        f"ping every {ping_interval:.1f}s, range={ping_range:.0f}m, beam={ping_beam:.0f}°"
    )

    last_ping_time: Dict[str, float] = {}

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

            # 1) Push the sub into an aggressive movement posture.
            try:
                client.control_sub(sid, throttle=throttle)
            except Exception as e:
                log(f"{sid[:6]}: control_sub error while setting aggressive throttle: {e}")

            # 2) Fire active pings forward on a timer.
            last = last_ping_time.get(sid, 0.0)
            if now - last < ping_interval:
                continue

            heading_rad = float(s.get("heading", 0.0) or 0.0)
            center_brg = compass_deg_from_rad(heading_rad)

            try:
                client.active_ping(
                    sid,
                    center_bearing_deg=center_brg,
                    beamwidth_deg=ping_beam,
                    max_range=ping_range,
                )
                log(
                    f"{sid[:6]}: ACTIVE PING center={center_brg:.0f}°, "
                    f"beam={ping_beam:.0f}°, range={ping_range:.0f}m"
                )
                last_ping_time[sid] = now
            except Exception as e:
                log(f"{sid[:6]}: active_ping error: {e}")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()


