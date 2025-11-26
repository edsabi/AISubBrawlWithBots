### AISubBrawl Agent Scripts & Arguments

This document summarizes the callable bot scripts and their CLI arguments.

---

## `simple_agent.py`

**Module entrypoint**: `python -m bots.simple_agent BASE_URL [options]`

- **Positional**
  - **`BASE_URL`**: Base URL of the AISubBrawl server.  
    - Example: `http://localhost:5000` or `http://18.224.214.86:5000`

- **Options**
  - **`--api-key API_KEY`**
    - Use this API key instead of auto‑signup or a saved state file.
    - If omitted, the script will:
      - First, try to load an API key from the state file (see `--state-file`).
      - If none is found, it will auto‑signup and create a new account.
  - **`--state-file PATH`**
    - JSON file used to cache API key and controlled sub IDs.
    - Default: `agent_state.json` in the `bots` directory.
    - On first run with auto‑signup, the script writes:
      - `base_url`, `api_key`, `subs` (IDs of controlled subs), `username`, timestamps.
    - On later runs, it will:
      - Load `api_key` from this file if `--api-key` is not given.
      - Update `subs` with the currently controlled submarine IDs.

- **Behavior**
  - Ensures at least **2 submarines** exist for the account (spawns if needed).
  - Tracks the first two sub IDs and controls them.
  - For each sub, on each tick:
    - Asks the energy manager (`energy_manager.choose_mode`) which mode to use:
      - `"refuel"` → call fueler / refuel workflow.
      - `"snorkel_recharge"` → climb to snorkel depth, recharge battery, then submerge.
      - `"patrol"` → simple ring patrol.
    - Logs decisions and actions with the `[agent]` prefix.

---

## `energy_manager.py`

**Module entrypoint**: `python -m bots.energy_manager BASE_URL [options]`

This script focuses **only on energy management** (battery + diesel fuel) for one or more subs. It is designed to be called directly by other agent frameworks.

- **Positional**
  - **`BASE_URL`**
    - Base URL of the AISubBrawl server, same as for `simple_agent.py`.

- **Options**
  - **`--api-key API_KEY`**
    - API key to use for all operations.
    - Overrides any key in the state file or environment.
  - **`--state-file PATH`**
    - JSON file to read API key and default submarine IDs from.
    - Default: `agent_state.json` in the `bots` directory.
    - If `--api-key` is not provided, the script will:
      - Try to load `api_key` from this file and use it.
  - **`--sub-id SUB_ID`** (repeatable)
    - Submarine ID to manage.
    - You can provide this option multiple times to manage several subs, e.g.:
      - `--sub-id SUB1 --sub-id SUB2`
    - If **no** `--sub-id` is provided:
      - The script falls back to the `subs` list in the `--state-file` JSON (if present).
  - **`--interval SECONDS`**
    - Control loop interval (how often to poll `/state` and send commands).
    - Default: `0.5` seconds.
  - **`--force-mode MODE`**
    - Override automatic mode selection.
    - Allowed values:
      - `refuel` – Always run the refueling workflow for the managed subs:
        - Call a fueler if none exists.
        - Navigate toward the nearest fueler.
        - Request `/start_refuel` when within ~50 m.
      - `snorkel_recharge` – Always attempt to snorkel‑recharge:
        - Climb toward snorkel depth (~13 m target).
        - Enable snorkel when shallow enough.
        - Recharge battery to ~100%, then submerge again.
        - Trigger an emergency blow if battery hits 0 while deep.
      - `patrol` – Force passive mode from the energy manager’s perspective:
        - Energy manager will not send refuel/snorkel commands; other scripts
          can handle motion and combat.
    - If **omitted**, the script uses `choose_mode(sub)` each tick:
      - `"refuel"` when fuel / battery are low.
      - `"snorkel_recharge"` when battery is below 60% but fuel is healthy.
      - `"patrol"` otherwise.

- **Behavior summary**
  - Loops forever:
    - Fetches `/state`.
    - For each managed sub:
      - Uses either:
        - `--force-mode` (if provided), or
        - `choose_mode(sub)` to determine energy mode.
      - Executes:
        - `manage_refuel` in `refuel` mode.
        - `manage_snorkel_recharge` in `snorkel_recharge` mode.
        - No action in `patrol` mode (left to higher‑level agents).
  - Logs every decision with `[energy]` prefix, including:
    - Mode decisions.
    - Fueler calls, snorkel state, emergency blow triggers, and depth changes.

---

## `engagement_agent.py`

**Module entrypoint**: `python -m bots.engagement_agent BASE_URL [options]`

This script manages **navigation during an engagement** for one or more friendly subs, given a target position (e.g., a detected contact converted to world coordinates by some other process).

- **Positional**
  - **`BASE_URL`**
    - Base URL of the AISubBrawl server.

- **Options**
  - **`--api-key API_KEY`**
    - API key to use (overrides state file and environment).
  - **`--state-file PATH`**
    - JSON file to read an API key and default sub IDs from.
    - Default: `agent_state.json` in the `bots` directory.
  - **`--sub-id SUB_ID`** (repeatable)
    - Submarine ID(s) to navigate.
    - If omitted, falls back to the `subs` list in the state file.
  - **`--target-x X` / `--target-y Y`**
    - **Required.** The target’s world coordinates in meters (e.g., a contact position that a higher‑level detection module has already computed).
  - **`--standoff-m DIST`**
    - Desired standoff distance from the target, in meters. Default: `800`.
  - **`--interval SECONDS`**
    - Control loop interval. Default: `0.5` seconds.

- **Behavior**
  - For each managed sub:
    - Computes range and bearing from the sub to the target (`target_x`, `target_y`).
    - Decides one of three simple patterns:
      - **Closing**: if range > `standoff_m + 150`, head directly toward target with higher throttle.
      - **Orbiting**: if within the standoff band, steer tangentially to orbit around the target.
      - **Opening**: if inside `standoff_m - 150`, turn away to increase distance.
    - Sends `set_sub_heading` and `control` commands accordingly.
  - Logs decisions as `[engage] ...`, including current range, selected pattern, heading, and throttle.

This agent does **not** alter server-side detection rules, so your two bot subs will not be made “invisible” to each other here; instead, higher‑level coordination should simply avoid passing friendly contacts into this engagement script.  For example, a meta‑agent can ignore any contact whose sub ID is one of the friendly `--sub-id` values or those stored in `agent_state.json`.

---

## `contact_utils.py` (library only)

This is not a script but a **helper module** you can import into higher‑level agents to classify passive contacts as friendly or hostile.

- **Key function**
  - **`is_friendly_contact(observer_sub, contact_bearing_rad, range_class, friendly_subs, bearing_tolerance_deg=15.0) -> bool`**
    - **`observer_sub`**: The sub that received the contact (one element from `/state`’s `subs` list).
    - **`contact_bearing_rad`**: The contact’s absolute bearing in radians (the `bearing` from the passive `contact` event).
    - **`range_class`**: The contact’s `range_class` (`"short"`, `"medium"`, or `"long"`).
    - **`friendly_subs`**: Iterable of friendly subs (typically all subs from `/state` for this API key).
    - **`bearing_tolerance_deg`**: Max allowed difference between geometric bearing to a friendly and the contact bearing (default `15°`).

- **What it does**
  - For each friendly candidate sub:
    - Computes geometric bearing and range from observer → friendly.
    - Checks if:
      - The geometric range is consistent with the `range_class` band (with some slack), and
      - The bearing difference is within `bearing_tolerance_deg`.
  - Returns **`True`** if any friendly matches, meaning the contact is probably one of your own subs and should be ignored for engagement.

- **Typical usage**
  - In your meta‑agent that listens to `contact` events:
    - Get `state = client.get_state()` and `friendly_subs = state["subs"]`.
    - For each passive contact:

      ```python
      from bots.contact_utils import is_friendly_contact

      if is_friendly_contact(observer_sub, contact["bearing"], contact["range_class"], friendly_subs):
          # Ignore this contact – likely friendly
          continue
      # Otherwise, treat as potential hostile and consider passing it to engagement_agent.py
      ```

---

## `passive_tracker.py` (library only)

This module provides a **bearing-only passive tracker** that estimates a contact’s world `(x, y)` from multiple passive reports as your own sub moves.

- **Key classes**
  - **`PassiveSample`**
    - Represents one bearing line:
      - `obs_x`, `obs_y`: Observer world position (meters) when the contact was received.
      - `bearing_rad`: Absolute bearing to contact in radians (server’s `contact["bearing"]`).
      - `weight`: Optional weight (defaults to `1.0`).
  - **`PassiveTracker`**
    - Methods:
      - `add_sample(obs_x, obs_y, bearing_rad, weight=1.0)`
        - Add a new bearing sample.
      - `clear()`
        - Remove all accumulated samples.
      - `estimate_position() -> (x, y) | None`
        - Returns the estimated target world position, or `None` if there isn’t enough information (e.g., fewer than 2 usable bearings or degenerate geometry).

- **What it does**
  - Builds a least‑squares solution for the point `(x, y)` that best fits all the bearing lines from different observer positions (assuming the target is approximately stationary over the window).
  - Ignores target motion on purpose; intended for short time windows where your own sub moves and the contact is quasi‑stationary.

- **Typical usage in a meta‑agent**

  ```python
  from bots.contact_utils import is_friendly_contact
  from bots.passive_tracker import PassiveTracker

  tracker = PassiveTracker()

  # On each passive contact event:
  state = client.get_state()
  friendly_subs = state["subs"]
  by_id = {s["id"]: s for s in friendly_subs}

  for contact in contacts:  # however you receive them
      if contact["type"] != "passive":
          continue

      obs = by_id.get(contact["observer_sub_id"])
      if not obs:
          continue

      # Skip friendlies
      if is_friendly_contact(obs, contact["bearing"], contact["range_class"], friendly_subs):
          continue

      # Add to passive tracker
      tracker.add_sample(obs_x=obs["x"], obs_y=obs["y"], bearing_rad=contact["bearing"])

  # Periodically, or when enough samples exist:
  estimate = tracker.estimate_position()
  if estimate is not None:
      est_x, est_y = estimate
      # Use (est_x, est_y) as target-x / target-y for engagement_agent.py
  ```

With `contact_utils` + `passive_tracker` + `engagement_agent`, you can:
  - Listen passively.
  - Ignore likely friendlies.
  - Build an `(x, y)` estimate for hostile contacts.
  - Steer subs toward that estimate via `engagement_agent.py`,
  - And only introduce a separate “active sonar script” later to ping when this passive solution is too uncertain.

---

## `torpedo_manager.py`

**Module entrypoint**: `python -m bots.torpedo_manager BASE_URL [options]`

This script manages **torpedo propulsion speed** based on remaining torpedo battery and estimated target range, to avoid wasting battery at high speed when the target is far away.

- **Positional**
  - **`BASE_URL`**
    - Base URL of the AISubBrawl server.

- **Options**
  - **`--api-key API_KEY`**
    - API key to use (overrides state file and environment).
  - **`--state-file PATH`**
    - JSON file to read an API key and (optionally) default torpedo IDs from.
    - Default: `agent_state.json` in the `bots` directory.
  - **`--torp-id TORP_ID`** (repeatable)
    - Torpedo ID(s) to manage.
    - If omitted, the script applies to **all** of your live torpedoes in `/state["torpedoes"]`.
  - **`--target-range-m DIST`**
    - **Required.** Estimated range to the target in meters. This is typically derived from your tracking logic (e.g., `passive_tracker` and/or active sonar).
  - **`--safety-factor FACTOR`**
    - Multiplier on the required range when sizing speed. Default: `1.2` (20% extra endurance margin).
  - **`--interval SECONDS`**
    - Control loop interval. Default: `1.0` seconds.

- **Behavior**
  - Reads your torpedoes from `/state["torpedoes"]`.
  - For each managed torpedo:
    - Reads current `battery` (0–100%) and uses torpedo config (mirrored from `game_config.json`):
      - `capacity = 100.0`
      - `drain_per_mps_per_s = 0.0015` (battery% per second ~ k·v²)
      - `min_speed = 8.0`, `max_speed = 24.0`
    - Computes a **recommended speed** using:

      \[ v \le \frac{B}{k \cdot \text{safety} \cdot R} \]

      where \(B\) is remaining battery, \(k\) is `drain_per_mps_per_s`, and \(R\) is `target_range_m`. It then clamps this into \([min\_speed, max\_speed]\).
    - Calls `client.set_torp_speed(torp_id, speed)` to update propulsion, logging the chosen speed and current battery.
  - If battery is already 0, it skips adjustments for that torpedo.

- **Typical usage**

  ```bash
  # Example: manage one torpedo, target approx 2500m away, with 30% extra range margin
  python -m bots.torpedo_manager http://localhost:5000 \
    --torp-id 123e4567-e89b-12d3-a456-426614174000 \
    --target-range-m 2500 \
    --safety-factor 1.3
  ```

In a full agentic stack, you would call `torpedo_manager.py` after launching a torpedo and obtaining a target range estimate (from `passive_tracker` + engagement logic), so that the torpedo runs **as slow as practical for the required range**, preserving battery for course changes and pings near the endgame.

---

## `fire_control_agent.py`

**Module entrypoint**: `python -m bots.fire_control_agent BASE_URL [options]`

This agent orchestrates **firing and mid‑course torpedo guidance** toward a target whose position has been estimated by your tracking/engagement logic.

- **Positional**
  - **`BASE_URL`**
    - Base URL of the AISubBrawl server.

- **Options**
  - **`--api-key API_KEY`**
    - API key to use (overrides state file and environment).
  - **`--state-file PATH`**
    - JSON file to read an API key and default sub IDs from.
    - Default: `agent_state.json` in the `bots` directory.
  - **`--sub-id SUB_ID`** (repeatable)
    - Restrict firing to the given friendly sub(s). If omitted, the agent will choose the closest of your subs to the target as the firing platform.
  - **`--target-x X` / `--target-y Y`**
    - **Required.** Estimated hostile world coordinates in meters, typically produced by `passive_tracker` (and optionally refined by active sonar).
  - **`--standoff-m DIST`**
    - Desired standoff range for the firing sub. Currently used only for logging/diagnostics; navigation itself is handled by `engagement_agent.py` or another controller.
  - **`--homing-range-m DIST`**
    - Range (meters) at which to enable torpedo active homing (`/torp_ping_toggle`). Default: `1200`.
  - **`--update-interval SECONDS`**
    - How often to update torpedo heading and check for homing activation. Default: `0.5` seconds.

- **Behavior**
  - Fetches `/state` and chooses a **firing submarine**:
    - If `--sub-id` is provided, picks the closest of those subs to `(target_x, target_y)`.
    - Otherwise, picks the closest of all your subs.
  - Launches a torpedo from tube `0` on the firing sub.
  - Waits for the new torpedo to appear in `/state["torpedoes"]` and records its `torp_id`.
  - Enters a **guidance loop** where it:
    - Re‑reads `/state` to find the current torpedo position.
    - Computes range and bearing from the torpedo to `(target_x, target_y)`.
    - While wire control is available, sets `/set_torp_target_heading` toward the estimated target heading.
    - When range drops below `--homing-range-m`, toggles active homing with `/torp_ping_toggle/<torp_id>` once, so the torpedo’s own active sonar takes over for terminal guidance.
    - Logs each step (range, heading, whether homing is enabled) with `[firectl]` prefix.
  - Stops when the torpedo disappears from `/state` (hit, detonation, timeout, or wire loss).

- **Integration in the full stack**
  - **Detection / classification**: A higher‑level agent uses `/contact` events + `contact_utils.is_friendly_contact` to find non‑friendly passive contacts.
  - **Tracking**: `passive_tracker.PassiveTracker` builds an `(x, y)` estimate of the hostile.
  - **Engagement navigation**: `engagement_agent.py` maneuvers your sub toward that estimated contact.
  - **Fire control**: `fire_control_agent.py` is invoked with `--target-x/--target-y` to:
    - Select a firing sub,
    - Launch a torpedo toward that estimate,
    - Wire‑guide it during mid‑course,
    - And decide when to flip on torpedo active sonar for endgame homing.

---

## `navigation_agent.py`

**Module entrypoint**: `python -m bots.navigation_agent BASE_URL [options]`

This agent handles **navigation with hazard avoidance** when subs move outside the inner ring, using the hazard scanner (`/weather_scan`) to detect fields ahead and adjust course.

- **Positional**
  - **`BASE_URL`**
    - Base URL of the AISubBrawl server.

- **Options**
  - **`--api-key API_KEY`**
    - API key to use (overrides state file and environment).
  - **`--state-file PATH`**
    - JSON file to read an API key and default sub IDs from.
    - Default: `agent_state.json` in the `bots` directory.
  - **`--sub-id SUB_ID`** (repeatable)
    - Submarine ID(s) this agent should manage.
    - If omitted, it considers all of your subs in `/state["subs"]`.
  - **`--ring-radius-m R`**
    - Inner ring radius in meters. Default: `6000` (from `game_config.json`).
  - **`--scan-interval-s SECONDS`**
    - Minimum time between hazard scans per sub. Default: `5` seconds.
  - **`--forward-sector-deg DEGREES`**
    - Half‑width of the forward sector considered “in our path” relative to current heading. Default: `45` degrees (i.e., ±45° ahead).
  - **`--avoid-turn-deg DEGREES`**
    - Heading change used to sidestep around a detected hazard. Default: `40` degrees.
  - **`--interval SECONDS`**
    - Main control loop interval. Default: `0.5` seconds.

- **Behavior**
  - On each loop:
    - Fetches `/state` and filters subs (by `--sub-id` if provided).
    - For each sub:
      - Computes its distance from the origin (ring center) and its current heading.
      - If it is **inside** the ring (`r <= ring_radius_m`), the agent does nothing for that sub.
      - If it is **outside** the ring and heading roughly outward (heading within ~60° of the radial‑out direction):
        - Ensures at least `--scan-interval-s` has passed since the last scan for that sub.
        - Calls `weather_scan(sub_id)` to get hazard detections.
        - Filters detections to those whose `bearing_deg` lies within `±forward-sector-deg` of the sub’s current heading (i.e., hazards roughly ahead).
        - If there is at least one hazard ahead:
          - Chooses the **closest** one by `range`.
          - Decides a turn direction (left/right) to sidestep away from the hazard.
          - Sets a new heading using `/set_sub_heading/<sub_id>` shifted by `±avoid-turn-deg`.
  - Logs decisions with `[nav]` prefix, including when it scans, finds no hazards, or turns to avoid a specific hazard bearing/range.

This agent is meant to run alongside others (e.g., `engagement_agent.py` or a patrol script): it only intervenes when subs are outside the ring and heading outward, using the hazard scanner to nudge the course away from dangerous fields rather than controlling the full navigation pattern by itself.

---

## `exploration_agent.py`

**Module entrypoint**: `python -m bots.exploration_agent BASE_URL [options]`

This agent focuses on **exploration**: it continuously drives subs radially outward from the ring center `(0, 0)` to maximize distance from the circle.

- **Positional**
  - **`BASE_URL`**
    - Base URL of the AISubBrawl server.

- **Options**
  - **`--api-key API_KEY`**
    - API key to use (overrides state file and environment).
  - **`--state-file PATH`**
    - JSON file to read an API key and default sub IDs from.
    - Default: `agent_state.json` in the `bots` directory.
  - **`--sub-id SUB_ID`** (repeatable)
    - Submarine ID(s) to drive outward.
    - If omitted, all of your subs from `/state["subs"]` are used.
  - **`--throttle VALUE`**
    - Throttle setting `[0..1]` to use while exploring. Default: `0.7`.
  - **`--interval SECONDS`**
    - Control loop interval. Default: `0.5` seconds.

- **Behavior**
  - On each loop:
    - Fetches `/state`, filters subs by `--sub-id` if provided.
    - For each managed sub:
      - Computes its current position `(x, y)` and radial distance `r = sqrt(x² + y²)`.
      - Computes the **radial-out** direction `atan2(y, x)` from the origin through the sub.
      - Converts that radial-out direction to a compass heading.
      - Calls:
        - `/set_sub_heading/<sub_id>` with that radial heading.
        - `/control/<sub_id>` with the configured throttle.
      - Logs `[explore]` messages with current `r` and the heading/throttle being set.
  - The result is that each sub steadily marches away from the center, maximizing distance from the inner ring. This can be combined with `navigation_agent.py` (hazard avoidance) and `energy_manager.py` (battery/fuel) to explore safely and efficiently far from the circle.

---

## `torpedo_evasion_agent.py`

**Module entrypoint**: `python -m bots.torpedo_evasion_agent BASE_URL [options]`

This agent focuses on **defensive maneuvering** against nearby torpedoes.

- **Positional**
  - **`BASE_URL`**
    - Base URL of the AISubBrawl server.

- **Options**
  - **`--api-key API_KEY`**
    - API key to use (overrides state file and environment).
  - **`--state-file PATH`**
    - JSON file to read an API key and default sub IDs from.
    - Default: `agent_state.json` in the `bots` directory.
  - **`--sub-id SUB_ID`** (repeatable)
    - Submarine ID(s) to protect.
    - If omitted, all of your subs from `/state["subs"]` are considered.
  - **`--danger-range-m DIST`**
    - Range inside which a torpedo is considered a threat. Default: `2000` meters.
  - **`--max-evade-depth-step-m DEPTH`**
    - Maximum depth change (up or down) applied during an evade maneuver. Default: `60` meters.
  - **`--interval SECONDS`**
    - Main control loop interval. Default: `0.5` seconds.

- **Behavior**
  - On each loop:
    - Fetches `/state` and filters subs by `--sub-id` if provided.
    - For each sub:
      - Computes distance to each torpedo in `/state["torpedoes"]` and finds the **nearest** one.
      - If there is no torpedo, or the nearest is beyond `danger_range_m`, it does nothing for that sub.
      - If a torpedo is within `danger_range_m`:
        - Computes the bearing **from torpedo to sub** (incoming direction).
        - Chooses an evasive heading roughly **90° off** that incoming bearing (lateral turn).
        - Optionally shifts depth:
          - If the torpedo depth is close to the sub depth (within `max-evade-depth-step-m`), it moves the sub up or down by that step to increase vertical separation.
          - Otherwise, it holds current depth.
        - Issues `/set_sub_heading/<sub_id>` with the evasive heading and `/control/<sub_id>` with `throttle=1.0` and the chosen `target_depth`.
    - Logs `[evade]` messages describing the detected torpedo, range, chosen heading, and target depth.

This agent is designed to run alongside your offensive/mission scripts. It only intervenes when a torpedo is nearby, briefly overriding heading/throttle/depth to perform an aggressive dodge. For best results, combine it with `energy_manager.py` (so you have battery to sprint) and `navigation_agent.py` / `exploration_agent.py` for broader movement logic.


