import os
import time
from typing import Any, Dict, Optional

import requests


class SubBrawlClient:
    """
    Thin HTTP client for talking to the AISubBrawl server.
    - Can sign up / log in to obtain its own API key.
    - Reads API key from the SUB_BRAWL_API_KEY env var by default.
    """

    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout: float = 3.0):
        self.base = base_url.rstrip("/")
        self.api_key = api_key or os.getenv("SUB_BRAWL_API_KEY", "")
        self.headers: Dict[str, str] = {}
        if self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"
        self.timeout = timeout

    # -------- Low-level HTTP helpers --------

    def _get(self, path: str) -> Dict[str, Any]:
        r = requests.get(f"{self.base}{path}", headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, json_body: Optional[dict] = None) -> Dict[str, Any]:
        headers = dict(self.headers)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        r = requests.post(f"{self.base}{path}", headers=headers, json=json_body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def set_api_key(self, api_key: str) -> None:
        """Update the client to use a new API key."""
        self.api_key = api_key
        self.headers["Authorization"] = f"Bearer {api_key}"

    # -------- Auth: signup / login --------

    def signup(self, username: str, password: str) -> Dict[str, Any]:
        """
        Create a new user and API key.
        Returns JSON, expected to contain {"ok": true, "api_key": "..."}.
        """
        return self._post("/signup", {"username": username, "password": password})

    def login(self, username: str, password: str) -> Dict[str, Any]:
        """
        Log in an existing user and get a new API key.
        Returns JSON, expected to contain {"ok": true, "api_key": "..."}.
        """
        return self._post("/login", {"username": username, "password": password})

    # -------- Core state / control --------

    def get_state(self) -> Dict[str, Any]:
        """Return current state for this user: subs + torpedoes."""
        return self._get("/state")

    def control_sub(self, sub_id: str, **kwargs) -> Dict[str, Any]:
        """
        Control a submarine.
        kwargs can include: throttle, rudder_deg, planes, target_depth, etc.
        """
        return self._post(f"/control/{sub_id}", kwargs)

    def set_sub_heading(self, sub_id: str, heading_deg: Optional[float]) -> Dict[str, Any]:
        """Set or clear target heading for a submarine."""
        body = {"heading_deg": heading_deg}
        return self._post(f"/set_sub_heading/{sub_id}", body)

    def toggle_snorkel(self, sub_id: str, on: bool) -> Dict[str, Any]:
        """Toggle or set snorkel state for a submarine."""
        return self._post(f"/snorkel/{sub_id}", {"on": on})

    def register_sub(self) -> Dict[str, Any]:
        """
        Spawn a new submarine for this user.
        """
        return self._post("/register_sub", {})

    # -------- Sonar / sensing --------

    def active_ping(
        self,
        sub_id: str,
        center_bearing_deg: float,
        beamwidth_deg: float,
        max_range: float,
    ) -> Dict[str, Any]:
        """Fire an active ping."""
        return self._post(
            f"/ping/{sub_id}",
            {
                "center_bearing_deg": center_bearing_deg,
                "beamwidth_deg": beamwidth_deg,
                "max_range": max_range,
            },
        )

    def weather_scan(self, sub_id: str) -> Dict[str, Any]:
        """Scan for nearby hazard fields."""
        return self._post(f"/weather_scan/{sub_id}", {})

    # -------- Weapons --------

    def launch_torpedo(self, sub_id: str, tube: int = 0) -> Dict[str, Any]:
        """Launch a torpedo from a given sub/tube."""
        return self._post(f"/launch_torpedo/{sub_id}", {"tube": tube})

    def set_torp_speed(self, torp_id: str, speed: float) -> Dict[str, Any]:
        """Set desired propulsion speed for a torpedo (m/s)."""
        return self._post(f"/set_torp_speed/{torp_id}", {"speed": speed})

    def set_torp_target_heading(self, torp_id: str, heading_deg: float) -> Dict[str, Any]:
        """Set desired compass heading for a wire-guided torpedo (degrees)."""
        return self._post(f"/set_torp_target_heading/{torp_id}", {"heading_deg": heading_deg})

    def torp_ping(self, torp_id: str, max_range: float = 800.0) -> Dict[str, Any]:
        """Trigger a single active ping from a torpedo."""
        return self._post(f"/torp_ping/{torp_id}", {"max_range": max_range})

    def torp_ping_toggle(self, torp_id: str) -> Dict[str, Any]:
        """Toggle continuous active sonar homing mode for a torpedo."""
        return self._post(f"/torp_ping_toggle/{torp_id}", {})

    # -------- Logistics / fuel --------

    def reload_torpedoes(self, sub_id: str, count: Optional[int] = None) -> Dict[str, Any]:
        """
        Spend battery to reload torpedoes into the submarine's magazine.
        If count is None, the server will attempt to fill to full magazine.
        """
        body: Dict[str, Any] = {}
        if count is not None:
            body["count"] = count
        return self._post(f"/reload_torpedoes/{sub_id}", body)

    def call_fueler(self, sub_id: str) -> Dict[str, Any]:
        """Request a fueler for the given sub."""
        return self._post(f"/call_fueler/{sub_id}")

    def start_refuel(self, sub_id: str) -> Dict[str, Any]:
        """Begin server-side refueling for the given sub."""
        return self._post(f"/start_refuel/{sub_id}", {})

    # -------- Safety / emergency --------

    def emergency_blow(self, sub_id: str) -> Dict[str, Any]:
        """Trigger an emergency blow for the given sub."""
        return self._post(f"/emergency_blow/{sub_id}", {})


def wait_for_subs(client: SubBrawlClient, min_count: int = 1, poll_interval: float = 1.0) -> Dict[str, Any]:
    """
    Block until at least min_count submarines exist for this user.
    Returns the latest state JSON (with subs list).
    """
    while True:
        st = client.get_state()
        subs = st.get("subs") or []
        if len(subs) >= min_count:
            return st
        time.sleep(poll_interval)


