"""
Formation navigation agent for AISubBrawl.

Goal:
  - Keep two submarines moving together in a specified formation:
      * Side-by-side (lateral separation)
      * Line-ahead (trail) formation
  - Maintain an approximate spacing distance between them.

Assumptions:
  - You control at least two subs.
  - This agent uses one sub as the "leader" and the other as the "wingman".
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
  print(f"[{ts}] [form] {msg}")


def compass_deg_from_rad(rad: float) -> float:
  d = (90.0 - rad * 180.0 / math.pi) % 360.0
  if d < 0:
    d += 360.0
  return d


def choose_leader_and_wingman(
  subs: List[Dict[str, Any]],
  explicit_ids: List[str] | None,
) -> Tuple[Dict[str, Any], Dict[str, Any]] | None:
  """
  Choose two subs and return (leader, wingman).
  If explicit_ids are provided, use those in order; otherwise, use the first two.
  """
  if explicit_ids:
    selected = [s for s in subs if s.get("id") in explicit_ids]
  else:
    selected = subs[:2]

  if len(selected) < 2:
    return None
  return selected[0], selected[1]


def main() -> None:
  parser = argparse.ArgumentParser(description="Two-sub formation agent for AISubBrawl")
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
      "Submarine ID(s) to place in formation (two required). "
      "If omitted, the first two subs in /state are used."
    ),
  )
  parser.add_argument(
    "--formation",
    choices=["side", "line"],
    default="side",
    help="Formation type: 'side' (side-by-side) or 'line' (line-ahead) (default: side)",
  )
  parser.add_argument(
    "--spacing-m",
    type=float,
    default=200.0,
    help="Desired spacing between subs in meters (default: 200)",
  )
  parser.add_argument(
    "--leader-throttle",
    type=float,
    default=0.4,
    help="Leader throttle [0..1] (default: 0.4)",
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

  spacing = max(10.0, float(args.spacing_m))
  leader_throttle = max(0.0, min(1.0, float(args.leader_throttle)))

  log(
    f"Formation agent: formation={args.formation}, spacing={spacing:.0f}m, "
    f"leader_throttle={leader_throttle:.2f}"
  )

  while True:
    try:
      st = client.get_state()
    except Exception as e:
      log(f"state fetch failed: {e}")
      time.sleep(args.interval)
      continue

    subs: List[Dict[str, Any]] = st.get("subs") or []
    if len(subs) < 2:
      log("Fewer than two subs present; waiting...")
      time.sleep(args.interval)
      continue

    pair = choose_leader_and_wingman(subs, args.sub_ids)
    if not pair:
      log("Could not identify two subs for formation; waiting...")
      time.sleep(args.interval)
      continue

    leader, wing = pair
    lid = leader["id"]
    wid = wing["id"]

    # Leader: just maintain heading & throttle (we do not override heading here).
    try:
      client.control_sub(lid, throttle=leader_throttle)
    except Exception as e:
      log(f"{lid[:6]}: control_sub error (leader): {e}")

    # Wingman: steer to maintain spacing and orientation.
    lx = float(leader.get("x", 0.0) or 0.0)
    ly = float(leader.get("y", 0.0) or 0.0)
    lz = float(leader.get("depth", 0.0) or 0.0)
    l_heading_rad = float(leader.get("heading", 0.0) or 0.0)

    wx = float(wing.get("x", 0.0) or 0.0)
    wy = float(wing.get("y", 0.0) or 0.0)
    wz = float(wing.get("depth", 0.0) or 0.0)

    # Unit vectors for leader heading (forward) and lateral (right).
    fwd_x = math.cos(l_heading_rad)
    fwd_y = math.sin(l_heading_rad)
    right_x = math.cos(l_heading_rad - math.pi / 2.0)
    right_y = math.sin(l_heading_rad - math.pi / 2.0)

    if args.formation == "side":
      # Desired wingman position: to starboard (right) of leader.
      target_x = lx + right_x * spacing
      target_y = ly + right_y * spacing
    else:
      # Line formation: wing behind leader along negative forward direction.
      target_x = lx - fwd_x * spacing
      target_y = ly - fwd_y * spacing

    # Compute desired heading from wing to its target point.
    dx = target_x - wx
    dy = target_y - wy
    dxy = math.hypot(dx, dy)
    heading_to_target_rad = math.atan2(dy, dx)
    heading_to_target_deg = compass_deg_from_rad(heading_to_target_rad)

    # Simple throttle adjustment: try to close gap if too far, slow if too close.
    # Base throttle roughly matches leader.
    rel = dxy - spacing
    if rel > 50.0:
      wing_thr = min(1.0, leader_throttle + 0.2)
    elif rel < -50.0:
      wing_thr = max(0.1, leader_throttle - 0.2)
    else:
      wing_thr = leader_throttle

    # Depth: aim to match leader depth.
    target_depth = lz

    log(
      f"leader {lid[:6]} / wing {wid[:6]}: form={args.formation}, "
      f"spacing={dxy:.0f}m (target {spacing:.0f}m), "
      f"wing_heading={heading_to_target_deg:.0f}°, wing_thr={wing_thr:.2f}, "
      f"depth {wz:.0f}→{target_depth:.0f}m"
    )

    try:
      client.set_sub_heading(wid, heading_to_target_deg)
      client.control_sub(wid, throttle=wing_thr, target_depth=target_depth)
    except Exception as e:
      log(f"{wid[:6]}: control_sub error (wingman): {e}")

    time.sleep(args.interval)


if __name__ == "__main__":
  main()


