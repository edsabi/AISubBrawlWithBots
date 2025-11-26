"""
Fire control agent for AISubBrawl.

This agent ties together:
  - Passive tracking (bearing-only, via PassiveTracker).
  - Engagement navigation (handled by engagement_agent.py or other code).
  - Torpedo launch and battery-aware speed management.
  - Torpedo active sonar homing (when we decide to go active).

It does NOT change server behaviour; it only orchestrates existing APIs.

High-level behaviour:
  - Assumes some higher layer has already decided a hostile exists and
    provides an estimated (x, y) of the target.
  - Optionally chooses one of our subs as the firing platform.
  - Launches a torpedo.
  - Sets an initial wire-guided heading toward the target estimate.
  - Uses a simple policy to decide when to:
      - Adjust torpedo heading (wire guidance) as the track updates.
      - Enable torpedo active homing via /torp_ping_toggle.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import SubBrawlClient


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [firectl] {msg}")


def compass_deg_from_rad(rad: float) -> float:
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


def pick_firing_sub(subs: List[Dict[str, Any]], target_xy: Tuple[float, float]) -> Dict[str, Any] | None:
    """
    Choose the best firing submarine: currently the closest one to target_xy.
    """
    if not subs:
        return None
    tx, ty = target_xy
    best = None
    best_sub = None
    for s in subs:
        dx = float(s["x"]) - tx
        dy = float(s["y"]) - ty
        r2 = dx * dx + dy * dy
        if best is None or r2 < best:
            best = r2
            best_sub = s
    return best_sub


def launch_torpedo_at_target(
    client: SubBrawlClient,
    firing_sub: Dict[str, Any],
    target_xy: Tuple[float, float],
    homing_range_m: float = 1200.0,
    update_interval: float = 0.5,
    target_updater: Optional[Callable[[], Tuple[float, float]]] = None,
) -> None:
    """
    Core fire-control logic extracted so it can be reused by other agents.

    Assumes:
      - client already has an API key set.
      - firing_sub is a dict from /state["subs"].
      - target_xy is an (x, y) estimate in world meters.
    """
    log(
        f"Selected firing sub {firing_sub['id'][:6]} at "
        f"({firing_sub['x']:.1f}, {firing_sub['y']:.1f}, {firing_sub['depth']:.1f}) "
        f"to engage target ({target_xy[0]:.1f}, {target_xy[1]:.1f})"
    )

    # 1) Launch a torpedo.
    try:
        client.launch_torpedo(firing_sub["id"], tube=0)
    except Exception as e:
        log(f"launch_torpedo failed: {e}")
        return

    # 2) Wait for torpedo to appear in /state and capture its ID.
    torp_id = None
    for _ in range(20):
        try:
            st = client.get_state()
        except Exception:
            time.sleep(0.2)
            continue
        torps = st.get("torpedoes") or []
        if not torps:
            time.sleep(0.2)
            continue
        torp = torps[-1]
        torp_id = torp.get("id")
        if torp_id:
            break
        time.sleep(0.2)

    if not torp_id:
        log("Could not find launched torpedo in /state; aborting guidance loop.")
        return

    log(f"Controlling torpedo {torp_id[:6]} toward target ({target_xy[0]:.1f}, {target_xy[1]:.1f})")

    homing_enabled = False

    # 3) Guidance loop: point torpedo toward target estimate, and decide when to enable homing.
    while True:
        try:
            st = client.get_state()
        except Exception as e:
            log(f"state fetch failed: {e}")
            time.sleep(update_interval)
            continue

        torps = st.get("torpedoes") or []
        cur = next((t for t in torps if t.get("id") == torp_id), None)
        if not cur:
            log(f"{torp_id[:6]}: torpedo no longer present (impact, detonation, or wire lost); exiting.")
            break

        # Use dynamic target updates if provided, otherwise fall back to the
        # original static target coordinates.
        if target_updater is not None:
            try:
                tx, ty = target_updater()
            except Exception:
                tx, ty = target_xy
        else:
            tx, ty = target_xy
        sx = float(cur.get("x", 0.0) or 0.0)
        sy = float(cur.get("y", 0.0) or 0.0)
        rng = math.hypot(tx - sx, ty - sy)

        # Decide whether to enable active homing.
        if (not homing_enabled) and rng <= homing_range_m:
            try:
                resp = client.torp_ping_toggle(torp_id)
                if resp.get("ok"):
                    homing_enabled = True
                    log(f"{torp_id[:6]}: enabling active homing at range {rng:.0f}m")
                else:
                    log(f"{torp_id[:6]}: torp_ping_toggle error: {resp.get('error')}")
            except Exception as e:
                log(f"{torp_id[:6]}: torp_ping_toggle exception: {e}")

        # While wire control is still available, set a target heading toward the estimate.
        try:
            heading_rad = math.atan2(ty - sy, tx - sx)
            heading_deg = compass_deg_from_rad(heading_rad)
            client.set_torp_target_heading(torp_id, heading_deg)
            log(
                f"{torp_id[:6]}: guiding toward target, rng={rng:.0f}m, "
                f"heading={heading_deg:.0f}°, homing={homing_enabled}"
            )
        except Exception as e:
            log(f"{torp_id[:6]}: set_torp_target_heading exception (wire may be lost): {e}")

        time.sleep(update_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fire control agent for AISubBrawl")
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
            "Friendly submarine ID(s) allowed to fire. "
            "If omitted, any of your subs from /state may be chosen."
        ),
    )
    parser.add_argument(
        "--target-x",
        type=float,
        required=True,
        help="Estimated hostile X coordinate in world meters",
    )
    parser.add_argument(
        "--target-y",
        type=float,
        required=True,
        help="Estimated hostile Y coordinate in world meters",
    )
    parser.add_argument(
        "--standoff-m",
        type=float,
        default=800.0,
        help="Desired standoff distance for the firing sub (informational for logs only here)",
    )
    parser.add_argument(
        "--homing-range-m",
        type=float,
        default=1200.0,
        help="Range at which to enable torpedo active homing (default: 1200m)",
    )
    parser.add_argument(
        "--update-interval",
        type=float,
        default=0.5,
        help="Control loop interval for torpedo guidance in seconds (default: 0.5)",
    )

    args = parser.parse_args()

    base_url = args.base_url
    target_xy = (args.target_x, args.target_y)

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

    # 1) Fetch current subs and pick a firing platform.
    try:
        st = client.get_state()
    except Exception as e:
        log(f"Initial state fetch failed: {e}")
        sys.exit(1)

    subs: List[Dict[str, Any]] = st.get("subs") or []
    if args.sub_ids:
        subs = [s for s in subs if s.get("id") in args.sub_ids]

    firing_sub = pick_firing_sub(subs, target_xy)
    if not firing_sub:
        log("No suitable firing submarine found; aborting.")
        sys.exit(1)

    launch_torpedo_at_target(
        client,
        firing_sub,
        target_xy,
        homing_range_m=args.homing_range_m,
        update_interval=args.update_interval,
    )


if __name__ == "__main__":
    main()


