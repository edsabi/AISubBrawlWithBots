"""
Ultra-quiet posture agent for AISubBrawl.

Goal:
  - Put one or more submarines into a low-noise, "ultra quiet" state:
      * Minimum safe forward speed (to avoid uncontrolled sinking).
      * Running deep (but above crush depth).

By default this agent continuously re-applies the posture, so if other
scripts bump throttle or depth, it gently nudges the sub back toward
the configured quiet settings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

from .client import SubBrawlClient


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [quiet] {msg}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ultra-quiet posture agent for AISubBrawl")
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
            "Submarine ID(s) to hold in ultra-quiet posture. "
            "If omitted, all of your subs from /state are used."
        ),
    )
    parser.add_argument(
        "--quiet-throttle",
        type=float,
        default=0.15,
        help="Throttle setting [0..1] for ultra-quiet speed (default: 0.15 ≈ min safe speed)",
    )
    parser.add_argument(
        "--quiet-depth-m",
        type=float,
        default=300.0,
        help="Target depth in meters for ultra-quiet running (default: 300m)",
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

    quiet_throttle = max(0.0, min(1.0, float(args.quiet_throttle)))
    quiet_depth = max(0.0, float(args.quiet_depth_m))

    log(
        f"Ultra-quiet agent: setting one-shot posture "
        f"(throttle={quiet_throttle:.2f}, depth={quiet_depth:.0f}m)"
    )

    # One-shot: fetch state once, apply posture once, then exit.
    try:
        st = client.get_state()
    except Exception as e:
        log(f"state fetch failed: {e}")
        sys.exit(1)

    subs: List[Dict[str, Any]] = st.get("subs") or []
    if args.sub_ids:
        subs = [s for s in subs if s.get("id") in args.sub_ids]

    for s in subs:
        sid = s.get("id")
        if not sid:
            continue

        depth = float(s.get("depth", 0.0) or 0.0)
        throttle = float(s.get("throttle", 0.0) or 0.0)

        log(
            f"{sid[:6]}: setting ultra-quiet posture: "
            f"depth {depth:.0f}→{quiet_depth:.0f}m, "
            f"throttle {throttle:.2f}→{quiet_throttle:.2f}"
        )

        body: Dict[str, Any] = {"throttle": quiet_throttle, "planes": 0.0}
        body["target_depth"] = quiet_depth if quiet_depth > 0.0 else 0.1

        try:
            client.control_sub(sid, **body)
        except Exception as e:
            log(f"{sid[:6]}: control error while setting quiet posture: {e}")

    # Note on hazard scanner:
    # This agent never calls /weather_scan, so from the bot's perspective the
    # scanner is "off". UI clients or other agents would need to stop calling
    # /weather_scan as well to remain fully ultra-quiet.


if __name__ == "__main__":
    main()


