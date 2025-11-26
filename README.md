# AI Submarine Brawl

A real-time multiplayer submarine warfare simulation game with AI bots, torpedo combat, and sonar systems.

## Features

- **Real-time Multiplayer**: Multiple players can control submarines simultaneously
- **AI Bots**: Intelligent AI submarines with tactical behaviors
- **Torpedo Combat**: Wire-guided and active homing torpedoes
- **Sonar Systems**: Passive and active sonar with realistic detection
- **Physics Simulation**: Realistic submarine physics with depth, speed, and battery management
- **Web-based UI**: Modern web interface for submarine control
- **Configurable Game Rules**: Customizable game parameters via JSON config

## Quick Start

### Prerequisites

- Python 3.8+
- Modern web browser

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd AISubBrawl
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Start the server:
```bash
python server_world_db.py
```

4. Open your browser and navigate to:
```
http://localhost:5000
```

## Game Configuration

The game behavior can be customized by editing `game_config.json`:

```json
{
  "tick_hz": 10,
  "sub": {
    "max_per_user": 2,
    "max_speed": 12.0,
    "yaw_rate_deg_s": 20.0,
    "snorkel_depth": 15.0
  },
  "torpedo": {
    "speed": 18.0,
    "max_range": 6000.0,
    "blast_radius": 120.0
  },
  "sonar": {
    "active": {
      "max_range": 6000.0,
      "max_angle": 210.0
    }
  }
}
```

## API Reference

### Authentication

All API endpoints (except public ones) require authentication via API key in the Authorization header:
```
Authorization: Bearer <api_key>
```

### User Management

#### `POST /signup`
Create a new user account.

**Request Body:**
```json
{
  "username": "player1",
  "password": "password123"
}
```

**Response:**
```json
{
  "ok": true,
  "api_key": "generated_api_key_here"
}
```

#### `POST /login`
Login with existing credentials.

**Request Body:**
```json
{
  "username": "player1",
  "password": "password123"
}
```

**Response:**
```json
{
  "ok": true,
  "api_key": "generated_api_key_here"
}
```

### Game Information

#### `GET /public`
Get public game information (no authentication required).

**Response:**
```json
{
  "ring": {"x": 0.0, "y": 0.0, "r": 6000.0},
  "objectives": [
    {"id": "A", "x": 1500.0, "y": -800.0, "r": 250.0},
    {"id": "B", "x": -1200.0, "y": 1300.0, "r": 250.0}
  ]
}
```

#### `GET /rules`
Get current game configuration.

**Response:**
```json
{
  "tick_hz": 10,
  "sub": { /* submarine configuration */ },
  "torpedo": { /* torpedo configuration */ },
  "sonar": { /* sonar configuration */ }
}
```

### Submarine Management

#### `POST /register_sub`
Spawn a new submarine. Limited by `sub.max_per_user` configuration.

**Response:**
```json
{
  "ok": true,
  "sub_id": "submarine_id_here",
  "spawn": [x, y, depth]
}
```

**Error Response (if limit exceeded):**
```json
{
  "ok": false,
  "error": "Maximum 2 submarines per user"
}
```

#### `GET /state`
Get current game state for your submarines and torpedoes.

**Response:**
```json
{
  "ok": true,
  "time": 1734567890.123,
  "subs": [
    {
      "id": "sub_id",
      "x": 100.0, "y": 200.0, "depth": 120.0,
      "heading": 1.57, "speed": 6.0,
      "battery": 55.2, "health": 100.0,
      "target_heading": 1.8,
      "target_depth": 150.0
    }
  ],
  "torpedoes": [
    {
      "id": "torp_id",
      "x": 13.0, "y": 5.0, "depth": 120.0,
      "heading": 1.57, "speed": 12.0,
      "target_heading": 1.8,
      "target_depth": 150.0
    }
  ]
}
```

### Submarine Control

#### `POST /control/<sub_id>`
Control submarine movement and systems.

**Request Body:**
```json
{
  "throttle": 0.8,           // 0.0 to 1.0
  "planes": 0.2,             // -1.0 to 1.0 (dive/climb)
  "rudder_deg": 15.0,        // -30.0 to 30.0 degrees
  "rudder_nudge_deg": 5.0,   // Relative turn
  "target_depth": 150.0      // Auto depth control
}
```

**Response:**
```json
{
  "ok": true
}
```

#### `POST /set_sub_heading/<sub_id>`
Set submarine target heading for auto-steering.

**Request Body:**
```json
{
  "heading_deg": 90.0
}
```

**Response:**
```json
{
  "ok": true,
  "current_heading": 85.2,
  "target_heading": 90.0
}
```

#### `POST /snorkel/<sub_id>`
Toggle snorkel mode (must be at snorkel depth).

**Request Body (optional):**
```json
{
  "toggle": true
}
```

**Response:**
```json
{
  "ok": true,
  "is_snorkeling": true,
  "depth": 12.0,
  "limit": 15.0
}
```

#### `POST /emergency_blow/<sub_id>`
Trigger emergency blow for rapid surfacing.

**Response:**
```json
{
  "ok": true
}
```

### Sonar Systems

#### `POST /ping/<sub_id>`
Send active sonar ping.

**Request Body:**
```json
{
  "beamwidth_deg": 20.0,      // Beam width (max 210°)
  "max_range": 3000.0,        // Maximum range
  "center_bearing_deg": 0.0   // Relative to submarine heading
}
```

**Response:**
```json
{
  "ok": true,
  "battery_cost": {
    "base": 0.5,
    "angle": 0.8,
    "range": 8.0,
    "total": 9.3
  },
  "battery_remaining": 45.7,
  "beam_deg": 20.0,
  "max_range": 3000.0
}
```

#### `POST /set_passive_array/<sub_id>`
Electronically steer passive sonar array.

**Request Body:**
```json
{
  "dir_deg": 123.4
}
```

**Response:**
```json
{
  "ok": true
}
```

### Torpedo Management

#### `POST /launch_torpedo/<sub_id>`
Launch a wire-guided torpedo.

**Request Body:**
```json
{
  "range": 1200.0
}
```

**Response:**
```json
{
  "ok": true,
  "torpedo_id": "torp_id_here",
  "range": 1200.0,
  "battery_cost": 16.0,
  "spawn": {"x": 150.0, "y": 250.0, "depth": 120.0}
}
```

#### `POST /set_torp_speed/<torp_id>`
Set torpedo target speed.

**Request Body:**
```json
{
  "speed": 16.0
}
```

**Response:**
```json
{
  "ok": true,
  "speed": 15.8,
  "target_speed": 16.0
}
```

#### `POST /set_torp_depth/<torp_id>`
Set torpedo target depth.

**Request Body:**
```json
{
  "depth": 120.0
}
```

**Response:**
```json
{
  "ok": true,
  "depth": 118.5,
  "target_depth": 120.0
}
```

#### `POST /set_torp_heading/<torp_id>`
Set torpedo heading (wire-guided mode).

**Request Body:**
```json
{
  "heading_deg": 45.0
}
```

**Response:**
```json
{
  "ok": true,
  "torpedo": {
    "id": "torp_id",
    "heading": 0.785
  }
}
```

#### `POST /set_torp_target_heading/<torp_id>`
Set torpedo target heading for auto-steering.

**Request Body:**
```json
{
  "heading_deg": 45.0
}
```

**Response:**
```json
{
  "ok": true,
  "current_heading": 42.3,
  "target_heading": 45.0
}
```

#### `POST /torp_ping/<torp_id>`
Send torpedo active sonar ping.

**Request Body:**
```json
{
  "max_range": 800.0
}
```

**Response:**
```json
{
  "ok": true,
  "contacts": [
    {
      "bearing": 1.57,
      "range": 450.0,
      "depth": 120.0
    }
  ]
}
```

#### `POST /torp_ping_toggle/<torp_id>`
Toggle torpedo auto-ping mode.

**Response:**
```json
{
  "ok": true,
  "active_sonar_enabled": true
}
```

#### `POST /torp_passive_sonar_toggle/<torp_id>`
Toggle torpedo passive sonar.

**Response:**
```json
{
  "ok": true,
  "passive_sonar_active": true
}
```

#### `POST /detonate/<torp_id>`
Detonate torpedo at current position.

**Response:**
```json
{
  "ok": true
}
```

### Real-time Events

#### `GET /stream`
Server-sent events stream for real-time game updates.

**Headers:**
```
Authorization: Bearer <api_key>
Accept: text/event-stream
```

**Event Types:**

**Snapshot:**
```
event: snapshot
data: {"time": 1734567890.123, "subs": [...], "torpedoes": [...]}
```

**Contact (Passive Sonar):**
```
event: contact
data: {"type": "passive", "bearing": 1.57, "range_class": "medium", "snr": 8.5}
```

**Echo (Active Sonar):**
```
event: echo
data: {"type": "active", "bearing": 1.2, "range": 950, "estimated_depth": 130, "quality": 0.82}
```

**Torpedo Contact:**
```
event: torpedo_contact
data: {"type": "passive", "bearing": 0.78, "range_class": "short", "snr": 12.3}
```

**Torpedo Ping:**
```
event: torpedo_ping
data: {"torpedo_id": "torp_id", "contacts": [{"bearing": 1.57, "range": 450, "depth": 120}]}
```

**Explosion:**
```
event: explosion
data: {"time": 1734567890.123, "at": [100, 200, 120], "torpedo_id": "torp_id", "blast_radius": 60, "damage": 50, "distance": 92}
```

### Admin Endpoints

#### `GET /admin/state`
Get complete game state (admin only).

**Response:**
```json
{
  "ok": true,
  "subs": [/* all submarines */],
  "torpedoes": [/* all torpedoes */]
}
```

#### `GET /perf`
Get server performance metrics.

**Response:**
```json
{
  "ok": true,
  "tick_time_ms": 15.2,
  "queues": 3
}
```

## Game Mechanics

### Submarine Physics
- **Speed**: Controlled by throttle (0-100%)
- **Turning**: Rudder control with rate limits
- **Depth**: Manual planes or auto depth control
- **Battery**: Drains with movement and sonar use
- **Snorkeling**: Recharge battery at shallow depths

### Sonar Systems
- **Passive Sonar**: Omnidirectional detection (360°)
- **Active Sonar**: Directional ping with configurable beam width
- **Torpedo Sonar**: 210° forward beam with 150° baffle zone
- **Detection Range**: Based on target speed, depth, and noise

### Torpedo Combat
- **Wire-guided**: Manual control via wire connection
- **Active Homing**: Automatic target acquisition and tracking
- **Proximity Fuze**: Automatic detonation near targets
- **Damage**: Graduated by distance from explosion

### AI Bots
- **Tactical States**: Patrol, Hunt, Attack, Evade, Recharge, Defensive
- **Sonar Usage**: Intelligent ping timing and battery management
- **Target Tracking**: Contact management and range estimation
- **Combat AI**: Torpedo evasion and counter-attack strategies

## Configuration Options

### Submarine Settings
- `max_per_user`: Maximum submarines per player (default: 2)
- `max_speed`: Maximum speed in m/s (default: 12.0)
- `yaw_rate_deg_s`: Turn rate in degrees/second (default: 20.0)
- `snorkel_depth`: Maximum depth for snorkeling (default: 15.0)

### Torpedo Settings
- `speed`: Default torpedo speed (default: 18.0)
- `max_range`: Maximum torpedo range (default: 6000.0)
- `blast_radius`: Explosion radius (default: 120.0)
- `lifetime_s`: Torpedo lifetime in seconds (default: 240.0)

### Sonar Settings
- `active.max_range`: Maximum active sonar range (default: 6000.0)
- `active.max_angle`: Maximum beam width (default: 210.0)
- `passive.base_snr`: Base signal-to-noise ratio (default: 8.0)

## Error Codes

- `400 Bad Request`: Invalid request parameters
- `401 Unauthorized`: Missing or invalid API key
- `403 Forbidden`: Valid key but insufficient permissions
- `404 Not Found`: Entity not found or not owned by user

## Development

### Running Tests
```bash
python test_ui_heading.py
python test_submarine_heading.py
python test_distance_ping.py
```

### AI Bot Development
See `bots/bot_0_1.py` for example AI implementation.

### Adding New Features
1. Modify `server_world_db.py` for server-side logic
2. Update `ui.html` for client-side interface
3. Add configuration options to `game_config.json`
4. Update this README with new API endpoints

## License

[Add your license information here]
