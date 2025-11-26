"""
Contact classification helpers for AISubBrawl bots.

These functions help client-side agents decide whether a passive sonar
contact is likely to be a *friendly* submarine (one of our own subs),
so that higher-level logic can ignore those contacts when choosing
engagement targets.

This is purely client-side; it does not modify any server behaviour.
"""

import math
from typing import Any, Dict, Iterable


def _compass_deg_from_rad(rad: float) -> float:
    d = (90.0 - rad * 180.0 / math.pi) % 360.0
    if d < 0:
        d += 360.0
    return d


def _bearing_diff_deg(a_deg: float, b_deg: float) -> float:
    """
    Smallest absolute difference between two compass bearings in degrees.
    """
    d = (a_deg - b_deg + 180.0) % 360.0 - 180.0
    return abs(d)


def _range_band_for_class(range_class: str) -> tuple[float, float]:
    """
    Approximate numeric range band for a passive range_class.

    The server uses (see schedule_passive_contacts):
      - 'short'  if rng < 1200
      - 'medium' if rng < 3000
      - 'long'   otherwise

    We return slightly *widened* bands to be robust to noise:
      - short  => [0, 1500]
      - medium => [800, 3500]
      - long   => [2500, +inf)
    """
    rc = (range_class or "").lower()
    if rc == "short":
        return 0.0, 1500.0
    if rc == "medium":
        return 800.0, 3500.0
    if rc == "long":
        return 2500.0, float("inf")
    # Unknown range class: accept any distance.
    return 0.0, float("inf")


def is_friendly_contact(
    observer_sub: Dict[str, Any],
    contact_bearing_rad: float,
    range_class: str,
    friendly_subs: Iterable[Dict[str, Any]],
    bearing_tolerance_deg: float = 15.0,
) -> bool:
    """
    Return True if a passive contact is likely to be caused by one of our
    own submarines (friendly), based purely on geometry.

    Parameters
    ----------
    observer_sub:
        The sub that received the contact (one element from state["subs"]).
    contact_bearing_rad:
        Absolute contact bearing in *radians* (matches server's "bearing").
    range_class:
        The contact's range_class string: "short", "medium", or "long".
    friendly_subs:
        Iterable of our own subs (typically all subs from /state for this API key).
    bearing_tolerance_deg:
        Maximum allowed difference (in degrees) between the contact bearing
        and the geometric bearing to a friendly for it to be considered a match.

    Logic
    -----
    For each friendly candidate:
      - Compute geometric bearing from observer to friendly.
      - Compute geometric range from observer to friendly.
      - If bearing difference < tolerance AND range falls within the widened
        band implied by range_class, we treat this contact as friendly.
    """
    obs_x = float(observer_sub.get("x", 0.0) or 0.0)
    obs_y = float(observer_sub.get("y", 0.0) or 0.0)
    obs_id = observer_sub.get("id")

    contact_bearing_deg = _compass_deg_from_rad(contact_bearing_rad)
    r_min, r_max = _range_band_for_class(range_class)

    for friend in friendly_subs:
        # Don't compare observer to itself.
        if obs_id and friend.get("id") == obs_id:
            continue

        fx = float(friend.get("x", 0.0) or 0.0)
        fy = float(friend.get("y", 0.0) or 0.0)

        dx = fx - obs_x
        dy = fy - obs_y
        rng = math.hypot(dx, dy)

        # Quick range-class consistency check.
        if not (r_min <= rng <= r_max):
            continue

        brg_rad = math.atan2(fy - obs_y, fx - obs_x)
        friend_bearing_deg = _compass_deg_from_rad(brg_rad)

        if _bearing_diff_deg(contact_bearing_deg, friend_bearing_deg) <= bearing_tolerance_deg:
            return True

    return False


