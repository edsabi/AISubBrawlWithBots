"""
Energy management helpers for AISubBrawl bots.

Responsibilities:
  - Decide high-level mode based on battery + fuel
    ('refuel', 'snorkel_recharge', 'patrol' for now).
  - Drive basic refueling behavior using fuelers.
  - Drive simple snorkel-at-surface battery recharge behavior.

This module is imported by simple_agent.py so that energy logic is kept modular,
but it can also be run as a standalone script to manage energy for specific subs.
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
    print(f"[{ts}] [energy] {msg}")


def choose_mode(sub: Dict[str, Any]) -> Tuple[str, str]:
    """
    Decide which high-level mode the sub should be in from an energy standpoint.

    Modes:
      - 'refuel'           -> go to a fueler and refuel diesel.
      - 'snorkel_recharge'-> climb to snorkel depth and recharge battery using fuel.
      - 'patrol'           -> normal operation (other modules may later return 'hunt').
    Returns (mode, reason).
    """
    fuel = float(sub.get("fuel", 0.0) or 0.0)
    bat = float(sub.get("battery", 0.0) or 0.0)

    # If we're already in a refuel workflow, stay in that mode so we don't flap.
    if sub.get("refuel_active"):
        return "refuel", f"actively refueling (fuel={fuel:.0f}, bat={bat:.0f})"

    # If fuel is critically low, or battery is low and fuel isn't great, refuel.
    if fuel < 200.0 or (bat < 25.0 and fuel < 400.0):
        return "refuel", f"low resources (fuel={fuel:.0f}, bat={bat:.0f})"

    # If battery is getting low but we still have healthy fuel,
    # prefer a snorkel recharge instead of burning a fueler call.
    if bat < 60.0 and fuel >= 400.0:
        return "snorkel_recharge", f"battery low, using snorkel (fuel={fuel:.0f}, bat={bat:.0f})"

    # TODO: when we start tracking contacts, return 'hunt' if enemy present
    return "patrol", f"normal patrol (fuel={fuel:.0f}, bat={bat:.0f})"


def manage_refuel(client: SubBrawlClient, sub: Dict[str, Any]) -> None:
    """
    High-level refuel behavior:
      - If no fueler exists yet, request one.
      - Move toward nearest fueler if far away.
      - Once within ~50m, start server-side refueling and hold position.
    This relies on the server's /start_refuel behavior to moor the sub.
    """
    st = client.get_state()
    fuelers: List[Dict[str, Any]] = st.get("fuelers") or []
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


def manage_snorkel_recharge(client: SubBrawlClient, sub: Dict[str, Any]) -> None:
    """
    Bring the sub to snorkel depth and let the server recharge battery using diesel fuel.

    This does *not* call a fueler; it assumes we have sufficient onboard fuel
    and just wants to top up battery more safely/quietly than going to a fueler.
    """
    fuel = float(sub.get("fuel", 0.0) or 0.0)
    bat = float(sub.get("battery", 0.0) or 0.0)
    depth = float(sub.get("depth", 0.0) or 0.0)

    # If we're out of fuel, snorkel recharge won't help – higher-level logic
    # will steer us toward refuel mode instead.
    if fuel <= 0.0:
        log(f"{sub['id'][:6]}: cannot snorkel-recharge, fuel exhausted (bat={bat:.0f})")
        return

    # Safety: if battery is already at 0 and we're deep, trigger an emergency blow
    # to avoid an uncontrolled sink.
    if bat <= 0.0 and depth > 40.0:
        try:
            log(f"{sub['id'][:6]}: battery=0 at depth={depth:.1f}m, triggering EMERGENCY BLOW")
            client.emergency_blow(sub["id"])
        except Exception as e:
            log(f"{sub['id'][:6]}: emergency_blow exception: {e}")
        # After blow request, don't send more control this tick; let the server lift us.
        return

    # If we're already fully recharged, stop snorkeling and hand back to higher-level logic.
    if bat >= 99.0:
        try:
            log(f"{sub['id'][:6]}: battery full ({bat:.0f}%), stopping snorkel recharge and submerging")
            # Turn snorkel off; server allows this at any depth.
            client.toggle_snorkel(sub["id"], False)
        except Exception as e:
            log(f"{sub['id'][:6]}: toggle_snorkel(off) exception: {e}")
        try:
            # Start descending toward a reasonable patrol depth; high-level mode
            # will soon switch to 'patrol' and take over.
            client.control_sub(sub["id"], target_depth=80.0, throttle=0.4)
        except Exception as e:
            log(f"{sub['id'][:6]}: control_sub to submerge after recharge failed: {e}")
        return

    # Aim a bit shallower than snorkel depth (server default ~15m) to ensure we are
    # clearly inside the allowed band before enabling snorkel.
    target_depth = 13.0

    # If we're still too deep to legally snorkel, just climb toward snorkel depth
    # and don't spam the snorkel endpoint yet (server will 400 if depth > snorkel_depth).
    if depth > target_depth + 0.5:
        try:
            client.control_sub(sub["id"], target_depth=target_depth, throttle=0.3)
            log(f"{sub['id'][:6]}: climbing to snorkel depth for recharge (depth={depth:.1f}m -> target {target_depth}m)")
        except Exception as e:
            log(f"{sub['id'][:6]}: control_sub climb for snorkel_recharge failed: {e}")
        return

    # Shallow enough: ensure snorkel is on so the server actually recharges.
    try:
        client.toggle_snorkel(sub["id"], True)
    except Exception as e:
        log(f"{sub['id'][:6]}: toggle_snorkel exception: {e}")

    try:
        # Hold roughly at snorkel depth with very low throttle.
        client.control_sub(sub["id"], target_depth=target_depth, throttle=0.1)
    except Exception as e:
        log(f"{sub['id'][:6]}: control_sub for snorkel_recharge failed: {e}")


def compass_deg_from_rad(rad: float) -> float:
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


def main() -> None:
    """
    Standalone entrypoint so this module can be run directly, e.g.:

      python -m bots.energy_manager http://localhost:5000 --sub-id SUB1 --sub-id SUB2

    This will:
      - Use an API key from --api-key, a state file, or SUB_BRAWL_API_KEY.
      - Periodically fetch /state.
      - For each listed sub ID, run energy-mode logic ('refuel' / 'snorkel_recharge').
    """
    parser = argparse.ArgumentParser(description="Energy manager for AISubBrawl subs")
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
            "Submarine ID to manage (can be given multiple times). "
            "If omitted, falls back to 'subs' list in state file."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Control loop interval in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--force-mode",
        dest="force_mode",
        choices=["refuel", "snorkel_recharge", "patrol"],
        help="Override automatic mode selection and force a specific energy mode",
        default=None,
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

    log(f"Managing energy for subs: {sub_ids}")

    # Simple loop: fetch state, run energy logic on selected subs, sleep.
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

            fuel = float(sub.get("fuel", 0.0) or 0.0)
            bat = float(sub.get("battery", 0.0) or 0.0)

            # If a force-mode is provided, treat it as a *requested job* but
            # automatically fall back to 'patrol' once the job is clearly done.
            if args.force_mode == "refuel":
                if fuel >= 1000.0:
                    mode = "patrol"
                    reason = "refuel complete (fuel full), auto patrol"
                else:
                    mode = "refuel"
                    reason = "forced via --force-mode"
            elif args.force_mode == "snorkel_recharge":
                if bat >= 99.0:
                    mode = "patrol"
                    reason = "recharge complete (battery full), auto patrol"
                else:
                    mode = "snorkel_recharge"
                    reason = "forced via --force-mode"
            elif args.force_mode == "patrol":
                mode = "patrol"
                reason = "forced via --force-mode"
            else:
                mode, reason = choose_mode(sub)

            log(f"{sub['id'][:6]}: mode={mode} - {reason}")

            if mode == "refuel":
                manage_refuel(client, sub)
            elif mode == "snorkel_recharge":
                manage_snorkel_recharge(client, sub)
            else:
                # 'patrol' or future 'hunt' modes are handled by other scripts;
                # energy manager just observes in that case.
                continue

        time.sleep(args.interval)


if __name__ == "__main__":
    main()


