"""
Exploration / navigation agent for AISubBrawl.

Goal:
  - Drive one or more submarines ever farther from the inner ring center,
    i.e. maximize distance from (0, 0).

This agent:
  - Computes the radial-out direction from the origin through the sub's
    current position.
  - Steers the sub along that radial direction with a configurable throttle.
  - Runs continuously, so subs will slowly spiral outward (subject to
    currents / other influences) while generally moving away from the ring.

It is intentionally simple and can be combined with other agents such as:
  - navigation_agent.py (hazard avoidance outside the ring)
  - energy_manager.py   (battery/fuel management)
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
    print(f"[{ts}] [explore] {msg}")


def compass_deg_from_rad(rad: float) -> float:
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


def main() -> None:
    parser = argparse.ArgumentParser(description="Exploration/navigation agent (maximize distance from ring center)")
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
        help="Optional JSON file to read API key and default sub IDs from",
        default="agent_state.json",
    )
    parser.add_argument(
        "--sub-id",
        dest="sub_ids",
        action="append",
        help=(
            "Submarine ID(s) to drive outward. "
            "If omitted, all of your subs in /state are used."
        ),
    )
    parser.add_argument(
        "--throttle",
        type=float,
        default=0.7,
        help="Throttle setting [0..1] to use while exploring (default: 0.7)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Control loop interval in seconds (default: 0.5)",
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
    log(f"Exploration agent starting with throttle={throttle:.2f}")

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

        for s in subs:
            sid = s.get("id")
            if not sid:
                continue

            x = float(s.get("x", 0.0) or 0.0)
            y = float(s.get("y", 0.0) or 0.0)

            r = math.hypot(x, y)
            radial_out_rad = math.atan2(y, x)
            radial_out_deg = compass_deg_from_rad(radial_out_rad)

            log(
                f"{sid[:6]}: r={r:.0f}m, "
                f"setting heading={radial_out_deg:.0f}°, throttle={throttle:.2f} to explore outward"
            )

            try:
                client.set_sub_heading(sid, radial_out_deg)
                client.control_sub(sid, throttle=throttle)
            except Exception as e:
                log(f"{sid[:6]}: control error while exploring: {e}")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()


