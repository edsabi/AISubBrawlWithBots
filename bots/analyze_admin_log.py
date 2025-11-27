#!/usr/bin/env python3
"""
analyze_admin_log.py

Offline battle replay / visualization for AISubBrawl admin logs.

- Reads a logs/admin_run_*.jsonl file produced by admin_observer.py
- Reconstructs the tracks of all subs and torpedoes over time
- Provides:
    - quick summary of subs & torps seen
    - static snapshot plots
    - optional animation of the battle in 2D top-down view

Usage:

    python analyze_admin_log.py path/to/logs/admin_run_YYYYmmdd_HHMMSS.jsonl

This script uses only matplotlib + standard library.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation


# --------- Data structures ---------


@dataclass
class EntityTrack:
    id: str
    kind: str  # "sub" or "torpedo"
    owner: str | None = None
    label: str | None = None
    times: List[float] = field(default_factory=list)
    xs: List[float] = field(default_factory=list)
    ys: List[float] = field(default_factory=list)
    depths: List[float] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BattleReplay:
    tracks: Dict[str, EntityTrack]
    times: List[float]
    ring_r: float = 6000.0  # default if not known
    world_bounds: float = 8000.0  # visible half-size [m]


# --------- Loading JSONL log ---------


def load_admin_log(path: str) -> BattleReplay:
    """
    Load newline-delimited JSON log from admin_observer.py.
    Returns a BattleReplay with per-entity tracks.
    """
    tracks: Dict[str, EntityTrack] = {}
    all_times: List[float] = []
    ring_r = 6000.0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue

            ts = float(rec.get("ts", 0.0) or 0.0)
            state = rec.get("state") or {}
            if not isinstance(state, dict):
                continue

            all_times.append(ts)

            world = state.get("world") or {}
            ring = world.get("ring") or {}
            if "r" in ring:
                try:
                    ring_r = float(ring["r"])
                except Exception:
                    pass

            subs = state.get("subs") or []
            torps = state.get("torpedoes") or []

            # Subs
            for s in subs:
                sid = s.get("id")
                if not sid:
                    continue
                x = float(s.get("x", 0.0) or 0.0)
                y = float(s.get("y", 0.0) or 0.0)
                depth = float(s.get("depth", 0.0) or 0.0)
                owner = s.get("owner") or s.get("username") or ""
                label = owner or sid[:6]

                if sid not in tracks:
                    tracks[sid] = EntityTrack(
                        id=sid,
                        kind="sub",
                        owner=owner,
                        label=label,
                        extra={
                            "team": s.get("team"),
                        },
                    )
                tr = tracks[sid]
                tr.times.append(ts)
                tr.xs.append(x)
                tr.ys.append(y)
                tr.depths.append(depth)

            # Torpedoes
            for t in torps:
                tid = t.get("id")
                if not tid:
                    continue
                x = float(t.get("x", 0.0) or 0.0)
                y = float(t.get("y", 0.0) or 0.0)
                depth = float(t.get("depth", 0.0) or 0.0)
                parent = t.get("parent_sub") or t.get("owner") or ""
                label = f"T-{tid[:6]}"

                if tid not in tracks:
                    tracks[tid] = EntityTrack(
                        id=tid,
                        kind="torpedo",
                        owner=parent,
                        label=label,
                        extra={
                            "parent_sub": parent,
                        },
                    )
                tr = tracks[tid]
                tr.times.append(ts)
                tr.xs.append(x)
                tr.ys.append(y)
                tr.depths.append(depth)

    all_times = sorted(set(all_times))
    if not all_times:
        raise RuntimeError("No valid records found in log.")

    # Estimate world bounds from track extents
    xs_all = [x for tr in tracks.values() for x in tr.xs]
    ys_all = [y for tr in tracks.values() for y in tr.ys]
    max_extent = max(
        max(abs(x) for x in xs_all) if xs_all else 0.0,
        max(abs(y) for y in ys_all) if ys_all else 0.0,
        ring_r * 1.1,
    )
    world_bounds = max_extent * 1.1

    return BattleReplay(tracks=tracks, times=all_times, ring_r=ring_r, world_bounds=world_bounds)


# --------- Helpers for selecting tracks ---------


def split_tracks_by_kind(replay: BattleReplay):
    subs = [tr for tr in replay.tracks.values() if tr.kind == "sub"]
    torps = [tr for tr in replay.tracks.values() if tr.kind == "torpedo"]
    return subs, torps


def guess_players(replay: BattleReplay):
    """
    Very rough guess: group subs by owner/username.
    Returns dict owner -> list of sub tracks.
    """
    subs, _ = split_tracks_by_kind(replay)
    owners: Dict[str, List[EntityTrack]] = {}
    for tr in subs:
        o = tr.owner or "unknown"
        owners.setdefault(o, []).append(tr)
    return owners


# --------- Static snapshot plotting ---------


def interpolate_entity_at_time(tr: EntityTrack, t: float) -> Tuple[float, float]:
    """
    Simple nearest-neighbor interpolation: find position closest in time.
    """
    if not tr.times:
        return 0.0, 0.0
    # times are in chronological order
    # find index of closest time
    best_i = 0
    best_dt = abs(tr.times[0] - t)
    for i, tt in enumerate(tr.times):
        dt = abs(tt - t)
        if dt < best_dt:
            best_dt = dt
            best_i = i
    return tr.xs[best_i], tr.ys[best_i]


def plot_snapshot(replay: BattleReplay, t_index: int | None = None, save_path: str | None = None):
    """
    Plot a single snapshot at a given index into replay.times.
    If t_index is None, uses the last time.
    """
    if t_index is None:
        t_index = len(replay.times) - 1
    t_index = max(0, min(t_index, len(replay.times) - 1))
    t = replay.times[t_index]

    subs, torps = split_tracks_by_kind(replay)

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_title(f"Snapshot @ t = {t:.1f}s")
    ax.set_aspect("equal", "box")
    ax.set_xlim(-replay.world_bounds, replay.world_bounds)
    ax.set_ylim(-replay.world_bounds, replay.world_bounds)
    ax.set_xlabel("X (m)")
    ax.set_ylabel("Y (m)")

    # Draw ring / world boundary
    ring = plt.Circle((0, 0), replay.ring_r, fill=False, linestyle="--")
    ax.add_artist(ring)

    # Plot subs
    for tr in subs:
        x, y = interpolate_entity_at_time(tr, t)
        label = tr.label or tr.id[:6]
        ax.scatter([x], [y], marker="o")
        ax.text(x, y, label, fontsize=8, ha="center", va="bottom")

    # Plot torpedoes
    for tr in torps:
        x, y = interpolate_entity_at_time(tr, t)
        ax.scatter([x], [y], marker="x")
        # No label by default to avoid clutter; uncomment if you want it:
        # ax.text(x, y, tr.label or tr.id[:4], fontsize=6, ha="center", va="center")

    ax.grid(True, alpha=0.3)

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"[analyze] Saved snapshot to {save_path}")
    else:
        plt.show()

    plt.close(fig)


# --------- Animation ---------


def animate_battle(replay: BattleReplay, stride: int = 2, save_path: str | None = None):
    """
    Create a simple 2D animation over time.

    - `stride` controls how many time steps we skip between frames
      (e.g., 2 = every other tick).
    - If save_path is provided, saves an MP4 or GIF depending on extension.
      Requires ffmpeg or imagemagick installed for some formats.
    """
    times = replay.times[::stride]
    subs, torps = split_tracks_by_kind(replay)

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_aspect("equal", "box")
    ax.set_xlim(-replay.world_bounds, replay.world_bounds)
    ax.set_ylim(-replay.world_bounds, replay.world_bounds)
    ax.set_xlabel("X (m)")
    ax.set_ylabel("Y (m)")
    ax.grid(True, alpha=0.3)

    ring = plt.Circle((0, 0), replay.ring_r, fill=False, linestyle="--")
    ax.add_artist(ring)

    # Lines for trails
    sub_lines = {}
    for tr in subs:
        (line,) = ax.plot([], [], "-", linewidth=1)
        sub_lines[tr.id] = line

    torp_scatter = ax.scatter([], [], marker="x")

    time_text = ax.text(
        0.02,
        0.98,
        "",
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=10,
        bbox=dict(facecolor="white", alpha=0.6, edgecolor="none"),
    )

    def init():
        for line in sub_lines.values():
            line.set_data([], [])
        torp_scatter.set_offsets([])
        time_text.set_text("")
        return list(sub_lines.values()) + [torp_scatter, time_text]

    def update(frame_idx: int):
        t = times[frame_idx]
        # Sub trails
        for tr_id, line in sub_lines.items():
            tr = replay.tracks.get(tr_id)
            if not tr:
                continue
            xs, ys = [], []
            for tt, x, y in zip(tr.times, tr.xs, tr.ys):
                if tt <= t:
                    xs.append(x)
                    ys.append(y)
            line.set_data(xs, ys)

        # Torpedoes as scatter
        torp_points = []
        for tr in torps:
            # last known position before or at t
            xs = [x for tt, x in zip(tr.times, tr.xs) if tt <= t]
            ys = [y for tt, y in zip(tr.times, tr.ys) if tt <= t]
            if xs and ys:
                torp_points.append((xs[-1], ys[-1]))
        if torp_points:
            torp_scatter.set_offsets(torp_points)
        else:
            torp_scatter.set_offsets([])

        time_text.set_text(f"t = {t:.1f}s")
        return list(sub_lines.values()) + [torp_scatter, time_text]

    anim = FuncAnimation(
        fig,
        update,
        init_func=init,
        frames=len(times),
        interval=50,
        blit=True,
    )

    if save_path:
        ext = os.path.splitext(save_path)[1].lower()
        print(f"[analyze] Saving animation to {save_path} (this can take a bit)...")
        if ext in (".mp4", ".m4v"):
            anim.save(save_path, fps=20)
        elif ext in (".gif", ".webp"):
            anim.save(save_path, fps=20, loop=0)
        else:
            print("[analyze] Unknown extension for animation; defaulting to MP4 with .mp4")
            mp4_path = save_path + ".mp4"
            anim.save(mp4_path, fps=20)
        print("[analyze] Done.")
    else:
        plt.show()

    plt.close(fig)


# --------- CLI ---------


def main():
    if len(sys.argv) < 2:
        print("Usage: analyze_admin_log.py path/to/admin_run_*.jsonl", file=sys.stderr)
        sys.exit(1)

    log_path = sys.argv[1]
    if not os.path.exists(log_path):
        print(f"Log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[analyze] Loading {log_path} ...")
    replay = load_admin_log(log_path)
    subs, torps = split_tracks_by_kind(replay)

    print(f"[analyze] Loaded {len(replay.times)} timesteps")
    print(f"[analyze] Subs: {len(subs)}, Torpedoes: {len(torps)}")
    owners = guess_players(replay)
    print("[analyze] Owners / players:")
    for owner, tr_list in owners.items():
        print(f"  - {owner!r}: {len(tr_list)} subs")

    # Example: show final snapshot
    print("[analyze] Showing final snapshot...")
    plot_snapshot(replay)

    # Example: if you want an animation, uncomment:
    # animate_battle(replay, stride=2, save_path="battle.mp4")


if __name__ == "__main__":
    main()
