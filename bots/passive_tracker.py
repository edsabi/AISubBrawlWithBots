"""
Passive sonar tracking helpers for AISubBrawl bots.

This module implements a simple bearing-only tracker to estimate a contact's
world (x, y) position from multiple passive sonar reports as our own sub(s)
move around.

It is designed to be used by a higher-level agent that:
  - Listens for 'contact' events of type "passive".
  - Filters out friendlies using contact_utils.is_friendly_contact.
  - Feeds the remaining (hostile) contacts into PassiveTracker.
  - Uses the estimated (x, y) as input to engagement_agent.py, and only
    escalates to active pings when needed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class PassiveSample:
    """
    One passive contact sample from a given observer pose.

    All angles are in radians, consistent with server-side 'bearing'.
    """

    obs_x: float
    obs_y: float
    bearing_rad: float
    weight: float = 1.0


@dataclass
class PassiveTracker:
    """
    Simple bearing-only tracker for a (roughly) stationary contact.

    It accumulates bearing lines from different observer positions and uses a
    least-squares fit to find the point that best explains all bearings.

    This intentionally ignores target motion; it is meant for short windows
    where our own sub moves and the contact is quasi-stationary.
    """

    samples: List[PassiveSample] = field(default_factory=list)

    def add_sample(self, obs_x: float, obs_y: float, bearing_rad: float, weight: float = 1.0) -> None:
        """
        Add a new passive bearing sample from an observer position.
        """
        self.samples.append(PassiveSample(obs_x=obs_x, obs_y=obs_y, bearing_rad=bearing_rad, weight=weight))

    def clear(self) -> None:
        """Drop all accumulated samples."""
        self.samples.clear()

    def estimate_position(self) -> Tuple[float, float] | None:
        """
        Estimate target (x, y) that best fits all bearing lines.

        Returns (x, y) in world meters, or None if we don't have enough
        information (e.g., fewer than 2 samples or degenerate geometry).

        Method
        ------
        For each sample with observer position p and unit bearing vector u,
        the bearing line is:

            L(t) = p + t * u

        The cross-track error of a candidate target position x relative to
        this line is |(I - u u^T) (x - p)|. We minimize the weighted sum of
        squared cross-track errors over all samples, which yields the normal
        system:

            A x = b

        where:
            A = sum_i w_i * (I - u_i u_i^T)
            b = sum_i w_i * (I - u_i u_i^T) p_i
        """
        if len(self.samples) < 2:
            return None

        # Accumulators for 2x2 matrix A and 2x1 vector b
        a11 = a12 = a22 = 0.0
        b1 = b2 = 0.0

        for s in self.samples:
            w = float(s.weight) if s.weight > 0.0 else 1.0
            ux = math.cos(s.bearing_rad)
            uy = math.sin(s.bearing_rad)

            # Projection matrix onto line: P = u u^T
            # We want Q = I - P (projection onto cross-track).
            p11 = ux * ux
            p12 = ux * uy
            p22 = uy * uy

            q11 = 1.0 - p11
            q12 = -p12
            q21 = -p12
            q22 = 1.0 - p22

            # Weighted contribution to A: w * Q
            a11 += w * q11
            a12 += w * q12
            a22 += w * q22

            # Weighted contribution to b: w * Q * p
            px = s.obs_x
            py = s.obs_y
            b1 += w * (q11 * px + q12 * py)
            b2 += w * (q21 * px + q22 * py)

        # Solve the 2x2 system A x = b.
        det = a11 * a22 - a12 * a12
        if abs(det) < 1e-6:
            # Degenerate geometry (e.g., all bearings nearly parallel).
            return None

        inv11 = a22 / det
        inv12 = -a12 / det
        inv22 = a11 / det

        x = inv11 * b1 + inv12 * b2
        y = inv12 * b1 + inv22 * b2
        return x, y


