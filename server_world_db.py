#!/usr/bin/env python3
import os, json, math, random, time, threading, queue, uuid, copy
from typing import Dict, List, Tuple
from flask import Flask, request, jsonify, Response, send_from_directory, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from sqlalchemy.orm.exc import StaleDataError
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps  # <-- added

# -------------------------- Defaults --------------------------
DEFAULT_CFG = {
    "tick_hz": 10,
    "world": {
        "ring": {"x": 0.0, "y": 0.0, "r": 6000.0},
        "spawn_min_r": 500.0,
        "spawn_max_r": 4500.0,
        "safe_spawn_separation": 800.0,
        # Weather outside the main arena ring
        "weather": {
            # Damage per second applied to submarines outside the ring
            "storm_damage_dps": 4.0,
            # Base attenuation applied to sonar SNR when either party is outside
            "sonar_attenuation_db": 3.0,
            # Distance inside a cloud where contacts can still be heard normally
            "cloud_close_hear_range_m": 400.0,
            # Random "clouds" outside the ring that further degrade sonar
            "clouds": {
                "count": 24,
                "min_r": 6500.0,
                "max_r": 9500.0,
                "min_radius": 400.0,
                "max_radius": 1200.0,
                # Depth range within which cloud centers can appear
                "min_depth": 0.0,
                "max_depth": 350.0,
                # Per-cloud vertical thickness range
                "min_thickness": 60.0,
                "max_thickness": 200.0,
                # Extra attenuation when inside a cloud
                "attenuation_db": 8.0
            }
        }
    },
    "sub": {
        "max_speed": 12.0,
        "acceleration": 2.0,
        "yaw_rate_deg_s": 3.0,
        "pitch_rate_deg_s": 12.0,
        "planes_effect": 1.0,
        "neutral_bias": 0.008,
        "depth_damping": 0.35,
        "snorkel_depth": 15.0,
        "snorkel_off_hysteresis": 2.0,  # <-- prevents flapping at the limit
        "max_per_user": 2,  # Maximum submarines per user
        "emergency_blow": {
            "duration_s": 10.0,
            "upward_mps": 5.0,
            "recharge_per_s_at_snorkel": 0.06,
            "cooldown_s": 0.0
        },
        "battery": {
            "initial_min": 40.0,
            "initial_max": 80.0,
            "drain_per_throttle_per_s": 0.1,
            "high_speed_multiplier": 15.0,
            "recharge_per_s_snorkel": 0.25,
            # Diesel fuel model: how much total charge can be put back into the battery
            # before needing a refuel. Unit is "battery-percent equivalents".
            "max_fuel_capacity": 1000.0,   # ≈ 10x full recharge (10 * 100)
            "initial_fuel": 1000.0
        },
        "crush_depth": 500.0,
        "crush_dps_per_100m": 30.0
    },
    "torpedo": {
        "speed": 6.0,
        "turn_rate_deg_s": 5.0,
        "depth_rate_m_s": 6.0,
        "blast_radius": 60.0,
        "lifetime_s": 240.0,
        "max_range": 6000.0,
        # NOTE: Legacy per-shot battery drain is disabled by default now that
        # torpedoes use a limited-ammo + energy-to-reload mechanic.
        "battery_cost_per_100m": 0.0,
        "proximity_fuze_m": 0.0,
        "arming_delay_s": 1.0,
        # Torpedo magazine / reload configuration
        # Each submarine has a magazine of this many torpedoes ready to fire.
        "magazine_size": 4,
        # Battery cost to reload ONE torpedo back into the magazine.
        "reload_battery_cost_per_torp": 10.0,
        # Per-torpedo internal battery model
        "battery": {
            "capacity": 100.0,
            # Percent per (m/s)^2 per second; with 0.0015 at 18 m/s ≈ 0.5%/s
            "drain_per_mps_per_s": 0.0015,
            # Extra cost per active sonar ping (manual or auto)
            "active_ping_cost": 2.0,
            # Minimum battery required to allow pings
            "min_for_ping": 5.0
        }
    },
    "sonar": {
        "passive": {
            "base_snr": 8.0,
            "speed_noise_gain": 0.6,
            "snorkel_bonus": 15.0,
            "bearing_jitter_deg": 3.0,
            "report_interval_s": [2.0, 4.0]
        },
        "active": {
            "max_range": 6000.0,
            "max_angle": 210.0,
            "sound_speed": 1500.0,
            "rng_sigma_m": 40.0,
            "brg_sigma_deg": 1.5
        },
        "active_power": {
            "base_cost": 0.5,
            "cost_per_degree": 0.04,
            "cost_per_100m_range": 0.2683,
            "min_battery": 5.0
        }
    }
}

def deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst

CFG_PATH = 'game_config.json'
if os.path.exists(CFG_PATH):
    with open(CFG_PATH, 'r') as f:
        user_cfg = json.load(f)
    GAME_CFG = copy.deepcopy(DEFAULT_CFG)
    deep_merge(GAME_CFG, user_cfg)
    print("[CONFIG] Loaded & merged:", CFG_PATH)
else:
    GAME_CFG = copy.deepcopy(DEFAULT_CFG)
    print("[CONFIG] Using defaults")

TICK_HZ = GAME_CFG.get("tick_hz", DEFAULT_CFG["tick_hz"])
TICK = 1.0 / TICK_HZ

# -------------------------- OpenAPI / Swagger --------------------------
# Base OpenAPI info; paths are generated dynamically from Flask's url_map.
OPENAPI_BASE = {
    "openapi": "3.0.0",
    "info": {
        "title": "AISubBrawl API",
        "version": "1.0.0",
        "description": "API for controlling submarines and torpedoes in AISubBrawl."
    },
    "servers": [{"url": "/"}],
    "components": {
        "securitySchemes": {
            "ApiKeyAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "api_key",
                "description": "Paste your raw API key here; Swagger will send it as 'Authorization: Bearer <key>'."
            }
        }
    }
}

# -------------------------- App / DB --------------------------
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'sqlite:///sub_brawl.sqlite3?check_same_thread=false'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me')
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 1800,
    "connect_args": {"check_same_thread": False, "timeout": 60},
}

db = SQLAlchemy(app)
WORLD_LOCK = threading.RLock()

# -------------------------- Models --------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    pw_hash = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    # Timestamps of most recent submarine deaths for this user (seconds since epoch).
    # Used to enforce per-slot respawn cooldowns.
    last_death_ts = db.Column(db.Float, default=0.0)
    prev_death_ts = db.Column(db.Float, default=0.0)

class ApiKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class SubModel(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    x = db.Column(db.Float, default=0.0)
    y = db.Column(db.Float, default=0.0)
    depth = db.Column(db.Float, default=100.0)
    heading = db.Column(db.Float, default=0.0)       # radians, 0=east CCW+
    pitch = db.Column(db.Float, default=0.0)         # + = bow up
    rudder_angle = db.Column(db.Float, default=0.0)  # radians (servo)
    rudder_cmd = db.Column(db.Float, default=0.0)    # -1..+1
    planes = db.Column(db.Float, default=0.0)        # -1..1 manual
    throttle = db.Column(db.Float, default=0.2)      # 0..1
    target_depth = db.Column(db.Float, default=None)
    target_heading = db.Column(db.Float, default=None)  # radians, target heading for auto-steering
    speed = db.Column(db.Float, default=0.0)
    battery = db.Column(db.Float, default=50.0)
    fuel = db.Column(db.Float, default=1000.0)
    refuel_timer = db.Column(db.Float, default=0.0)
    refuel_active = db.Column(db.Boolean, default=False)
    refuel_fueler_id = db.Column(db.String(36), nullable=True)
    is_snorkeling = db.Column(db.Boolean, default=False)
    blow_active = db.Column(db.Boolean, default=False)
    blow_charge = db.Column(db.Float, default=1.0)
    blow_end = db.Column(db.Float, default=0.0)
    health = db.Column(db.Float, default=100.0)
    passive_dir = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.Float, default=lambda: time.time())
    last_report = db.Column(db.Float, default=0.0)
    scanner_noise_until = db.Column(db.Float, default=0.0)
    # Torpedo magazine: limited number of ready-to-fire torpedoes.
    torpedo_ammo = db.Column(db.Integer, default=lambda: int(GAME_CFG.get("torpedo", {}).get("magazine_size", DEFAULT_CFG["torpedo"]["magazine_size"])))
    # Scoring fields
    score = db.Column(db.Float, default=0.0)
    kills = db.Column(db.Integer, default=0)
    last_score_update = db.Column(db.Float, default=lambda: time.time())

class TorpedoModel(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    parent_sub = db.Column(db.String(36), db.ForeignKey('sub_model.id'), nullable=False)
    x = db.Column(db.Float, default=0.0)
    y = db.Column(db.Float, default=0.0)
    depth = db.Column(db.Float, default=100.0)
    target_depth = db.Column(db.Float, default=None)
    heading = db.Column(db.Float, default=0.0)
    target_heading = db.Column(db.Float, default=None)  # radians, target heading for auto-steering
    speed = db.Column(db.Float, default=lambda: GAME_CFG.get("torpedo", {}).get("speed", DEFAULT_CFG["torpedo"]["speed"]))
    target_speed = db.Column(db.Float, default=lambda: GAME_CFG.get("torpedo", {}).get("speed", DEFAULT_CFG["torpedo"]["speed"]))
    created_at = db.Column(db.Float, default=lambda: time.time())
    control_mode = db.Column(db.String(16), default='wire')  # wire | free
    wire_length = db.Column(db.Float, default=lambda: GAME_CFG.get("torpedo", {}).get("max_range", DEFAULT_CFG["torpedo"]["max_range"]))
    updated_at = db.Column(db.Float, default=lambda: time.time())
    passive_sonar_active = db.Column(db.Boolean, default=True)
    passive_sonar_bearing = db.Column(db.Float, default=0.0)
    last_sonar_contact = db.Column(db.Float, default=0.0)
    active_sonar_enabled = db.Column(db.Boolean, default=False)
    last_active_ping = db.Column(db.Float, default=0.0)
    battery = db.Column(db.Float, default=100.0)

class FuelerModel(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    x = db.Column(db.Float, default=0.0)
    y = db.Column(db.Float, default=0.0)
    depth = db.Column(db.Float, default=0.0)
    fuel = db.Column(db.Float, default=500.0)
    max_fuel = db.Column(db.Float, default=500.0)
    spawned_at = db.Column(db.Float, default=lambda: time.time())
    empty_since = db.Column(db.Float, default=None)

with app.app_context():
    db.create_all()
    try:
        with db.engine.connect() as con:
            con.execute(text("PRAGMA journal_mode=WAL;"))
            con.execute(text("PRAGMA synchronous=NORMAL;"))
            con.execute(text("PRAGMA busy_timeout=60000;"))
            con.execute(text("PRAGMA temp_store=MEMORY;"))
    except Exception as e:
        print("[DB] PRAGMA set failed:", e)
    print("[DB] SQLite WAL enabled, busy_timeout=60000ms, synchronous=NORMAL")

# -------------------------- Helpers --------------------------
def clamp(v, lo, hi): return max(lo, min(hi, v))
def wrap_angle(a):
    while a >= math.pi: a -= 2*math.pi
    while a < -math.pi: a += 2*math.pi
    return a
def distance(x1,y1,x2,y2): return math.hypot(x2-x1, y2-y1)
def distance3d(ax, ay, az, bx, by, bz):
    dx = ax - bx; dy = ay - by; dz = az - bz
    return math.sqrt(dx*dx + dy*dy + dz*dz)

# -------------------------- Auth --------------------------
def make_key():
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]

def get_user_from_api():
    k = None
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        k = auth.split(' ', 1)[1].strip()
    if not k:
        k = request.args.get('api_key', '').strip()
    if not k:
        return None
    ak = ApiKey.query.filter_by(key=k).first()
    if not ak: return None
    return User.query.get(ak.user_id)

def require_key(fn):
    @wraps(fn)  # <-- keeps name/docs for flask
    def w(*a, **kw):
        u = get_user_from_api()
        if not u:
            return jsonify({"ok": False, "error": "Invalid api key"}), 401
        request.user = u
        return fn(*a, **kw)
    return w

# -------------------------- SSE per-user --------------------------
USER_QUEUES: Dict[int, "queue.Queue[str]"] = {}
USER_LAST: Dict[int, float] = {}

def _uq(user_id: int) -> "queue.Queue[str]":
    if user_id not in USER_QUEUES:
        USER_QUEUES[user_id] = queue.Queue(maxsize=1000)
        USER_LAST[user_id] = time.time()
    return USER_QUEUES[user_id]

def send_private(user_id: int, event: str, obj: dict):
    try:
        q = _uq(user_id)
        payload = f"event: {event}\ndata: {json.dumps(obj)}\n\n"
        q.put_nowait(payload)
    except queue.Full:
        pass

def _sub_pub(s: SubModel):
    return dict(
        id=s.id, x=s.x, y=s.y, depth=s.depth, heading=s.heading, pitch=s.pitch,
        rudder_angle=s.rudder_angle, rudder_cmd=s.rudder_cmd, planes=s.planes,
        speed=s.speed, battery=s.battery, fuel=getattr(s, "fuel", None),
        is_snorkeling=s.is_snorkeling,
        blow_active=s.blow_active, blow_charge=s.blow_charge, health=s.health,
        target_depth=s.target_depth, target_heading=s.target_heading, throttle=s.throttle,
        torpedo_ammo=getattr(s, "torpedo_ammo", None),
        score=getattr(s, "score", 0.0),
        kills=getattr(s, "kills", 0),
        refuel_active=getattr(s, "refuel_active", False),
        refuel_timer=getattr(s, "refuel_timer", 0.0)
    )

def _torp_pub(t: TorpedoModel):
    return dict(id=t.id, x=t.x, y=t.y, depth=t.depth, heading=t.heading,
                speed=t.speed, mode=t.control_mode, range=t.wire_length,
                passive_sonar_active=t.passive_sonar_active, 
                passive_sonar_bearing=t.passive_sonar_bearing,
                last_sonar_contact=t.last_sonar_contact,
                active_sonar_enabled=getattr(t, 'active_sonar_enabled', False),
                target_heading=getattr(t, 'target_heading', None),
                target_depth=getattr(t, 'target_depth', None),
                battery=getattr(t, 'battery', None))

def _fueler_pub(f: "FuelerModel"):
    return dict(
        id=f.id,
        x=f.x,
        y=f.y,
        depth=f.depth,
        fuel=float(getattr(f, "fuel", 0.0) or 0.0),
        max_fuel=float(getattr(f, "max_fuel", 0.0) or 0.0),
    )

def send_snapshot(user_id: int):
    with WORLD_LOCK:
        subs = SubModel.query.filter_by(owner_id=user_id).all()
        torps = TorpedoModel.query.filter_by(owner_id=user_id).all()
        fuelers = FuelerModel.query.all()
    q = _uq(user_id)
    obj = {
        "subs":[_sub_pub(s) for s in subs],
        "torpedoes":[_torp_pub(t) for t in torps],
        "fuelers":[_fueler_pub(f) for f in fuelers],
        "time": time.time()
    }
    payload = f"event: snapshot\ndata: {json.dumps(obj)}\n\n"
    try: q.put_nowait(payload)
    except queue.Full: pass

def send_snapshot_mem(user_id: int, subs: List[SubModel], torps: List[TorpedoModel]):
    q = _uq(user_id)
    with WORLD_LOCK:
        fuelers = FuelerModel.query.all()
    obj = {
        "subs":[_sub_pub(s) for s in subs if s.owner_id == user_id],
        "torpedoes":[_torp_pub(t) for t in torps if t.owner_id == user_id],
        "fuelers":[_fueler_pub(f) for f in fuelers],
        "time": time.time()
    }
    try: q.put_nowait(f"event: snapshot\ndata: {json.dumps(obj)}\n\n")
    except queue.Full: pass

# -------------------------- World / spawn --------------------------
WORLD_RING = GAME_CFG.get("world", {}).get("ring", DEFAULT_CFG["world"]["ring"])
WORLD_WEATHER = GAME_CFG.get("world", {}).get("weather", DEFAULT_CFG["world"].get("weather", {}))
OBJECTIVES = [
    {"id":"A","x":1500.0,"y":-800.0,"r":250.0},
    {"id":"B","x":-1200.0,"y":1300.0,"r":250.0},
]

def _spawn_fueler_near_sub(s: SubModel) -> FuelerModel:
    """Spawn a fueler 1000-3000m from the given submarine, on the surface, avoiding hazard clouds."""
    scfg = GAME_CFG.get("sub", DEFAULT_CFG["sub"])
    bcfg = scfg.get("battery", DEFAULT_CFG["sub"]["battery"])
    max_fuel_capacity = float(bcfg.get("max_fuel_capacity", 1000.0) or 0.0)
    # Fueler holds half of submarine max fuel capacity
    cap = max_fuel_capacity * 0.5

    # Try several times to find a spawn point that is not inside a hazard cloud
    fx = s.x
    fy = s.y
    fz = 0.0  # surface
    for _ in range(20):
        bearing = random.uniform(-math.pi, math.pi)
        rng = random.uniform(1000.0, 3000.0)
        cand_x = s.x + math.cos(bearing) * rng
        cand_y = s.y + math.sin(bearing) * rng

        # Reject positions inside any hazard cloud (at surface depth)
        in_cloud = False
        if WEATHER_CLOUDS:
            for c in WEATHER_CLOUDS:
                if 0.0 < c["min_depth"] or 0.0 > c["max_depth"]:
                    continue
                if distance(cand_x, cand_y, c["x"], c["y"]) <= c["radius"]:
                    in_cloud = True
                    break
        if not in_cloud:
            fx, fy = cand_x, cand_y
            break

    f = FuelerModel(
        owner_id=None,
        x=fx,
        y=fy,
        depth=fz,
        fuel=cap,
        max_fuel=cap,
        spawned_at=time.time()
    )
    db.session.add(f)
    return f

def random_spawn_pos():
    cfgw = GAME_CFG.get("world", DEFAULT_CFG["world"])
    cx, cy, R = WORLD_RING["x"], WORLD_RING["y"], WORLD_RING["r"]
    rmin = cfgw.get("spawn_min_r", DEFAULT_CFG["world"]["spawn_min_r"])
    rmax = cfgw.get("spawn_max_r", DEFAULT_CFG["world"]["spawn_max_r"])
    sep  = cfgw.get("safe_spawn_separation", DEFAULT_CFG["world"]["safe_spawn_separation"])
    for _ in range(50):
        ang = random.uniform(-math.pi, math.pi)
        r = random.uniform(rmin, rmax)
        x = cx + math.cos(ang)*r
        y = cy + math.sin(ang)*r
        ok = True
        with WORLD_LOCK:
            for s in SubModel.query.all():
                if distance(x, y, s.x, s.y) < sep:
                    ok = False; break
        if ok: return x, y
    return cx, cy

def is_outside_ring(x: float, y: float) -> bool:
    """True if a point is outside the main world ring."""
    cx, cy, R = WORLD_RING["x"], WORLD_RING["y"], WORLD_RING["r"]
    return distance(x, y, cx, cy) > R

def _generate_weather_clouds():
    """Generate random hazardous clouds outside the main ring."""
    cfg = WORLD_WEATHER.get("clouds", {})
    count = int(cfg.get("count", 0) or 0)
    if count <= 0:
        return []
    cx, cy, R = WORLD_RING["x"], WORLD_RING["y"], WORLD_RING["r"]
    min_r = float(cfg.get("min_r", R + 500.0))
    max_r = float(cfg.get("max_r", R + 3000.0))
    min_rad = float(cfg.get("min_radius", 400.0))
    max_rad = float(cfg.get("max_radius", 1200.0))
    min_d = float(cfg.get("min_depth", 0.0))
    max_d = float(cfg.get("max_depth", 400.0))
    min_th = float(cfg.get("min_thickness", 60.0))
    max_th = float(cfg.get("max_thickness", 200.0))
    att = float(cfg.get("attenuation_db", 8.0))
    dmg = float(cfg.get("damage_dps", 2.0))
    clouds = []
    for _ in range(count):
        ang = random.uniform(-math.pi, math.pi)
        # Bias spawn radius so more clouds appear farther from the ring:
        # sample u in [0,1], then skew toward max_r with (1 - u^2)
        u = random.random()
        r = min_r + (max_r - min_r) * (1.0 - u * u)
        x = cx + math.cos(ang) * r
        y = cy + math.sin(ang) * r
        radius = random.uniform(min_rad, max_rad)
        # Pick a random center depth, then random thickness around it
        center_depth = random.uniform(min_d, max_d)
        thickness = random.uniform(min_th, max_th)
        half = thickness / 2.0
        d_min = max(0.0, center_depth - half)
        d_max = max(d_min + 5.0, center_depth + half)
        clouds.append({
            "x": x, "y": y,
            "radius": radius,
            "min_depth": d_min,
            "max_depth": d_max,
            "attenuation_db": att,
            "damage_dps": dmg,
            "spawned_by_sub_id": None,
            "expiry_ts": None,
        })
    print(f"[WEATHER] Generated {len(clouds)} hazard clouds outside ring")
    return clouds

WEATHER_CLOUDS = _generate_weather_clouds()

def ensure_dynamic_weather_clouds(subs: List["SubModel"]):
    """
    Dynamically extend hazard clouds farther from the inner ring as players push outward.
    Keeps approximately constant cloud density per radial km and despawns old inner clouds
    if the total count grows too large.
    """
    global WEATHER_CLOUDS
    cfg = WORLD_WEATHER.get("clouds", {})
    base_count = int(cfg.get("count", 0) or 0)
    if base_count <= 0 or not subs:
        return

    cx, cy, R = WORLD_RING["x"], WORLD_RING["y"], WORLD_RING["r"]
    min_r_cfg = float(cfg.get("min_r", R + 500.0))
    max_r_cfg = float(cfg.get("max_r", R + 3000.0))
    min_rad = float(cfg.get("min_radius", 400.0))
    max_rad = float(cfg.get("max_radius", 1200.0))
    min_d = float(cfg.get("min_depth", 0.0))
    max_d = float(cfg.get("max_depth", 400.0))
    min_th = float(cfg.get("min_thickness", 60.0))
    max_th = float(cfg.get("max_thickness", 200.0))
    att = float(cfg.get("attenuation_db", 8.0))
    dmg = float(cfg.get("damage_dps", 2.0))

    # TTL cleanup for any locally spawned / expired clouds
    now = time.time()
    WEATHER_CLOUDS = [
        c for c in WEATHER_CLOUDS
        if c.get("expiry_ts") is None or float(c["expiry_ts"]) > now
    ]

    # -------- Global radial extension (outer ocean gets more cluttered) --------
    # Estimate desired density in clouds per meter of radius
    band_span = max(1.0, max_r_cfg - min_r_cfg)
    density_per_m = base_count / band_span

    # How far have players actually traveled from center?
    max_player_r = max(distance(cx, cy, s.x, s.y) for s in subs)
    if max_player_r > R:
        # Current max cloud radius
        if WEATHER_CLOUDS:
            current_max_r = max(distance(cx, cy, c["x"], c["y"]) for c in WEATHER_CLOUDS)
        else:
            current_max_r = min_r_cfg

        # We want clouds out to a bit beyond the furthest player
        target_r = max(current_max_r, max_player_r + 1500.0)
        if target_r > current_max_r + 100.0:
            # How many new clouds are needed to maintain density in this new radial band?
            span_new = target_r - current_max_r
            desired_new = int(density_per_m * span_new) or 1

            new_clouds: List[dict] = []
            for _ in range(desired_new):
                ang = random.uniform(-math.pi, math.pi)
                # Bias toward the outer edge of the new band
                u = random.random()
                r = current_max_r + span_new * (1.0 - u * u)
                x = cx + math.cos(ang) * r
                y = cy + math.sin(ang) * r
                radius = random.uniform(min_rad, max_rad)
                center_depth = random.uniform(min_d, max_d)
                thickness = random.uniform(min_th, max_th)
                half = thickness / 2.0
                d_min = max(0.0, center_depth - half)
                d_max = max(d_min + 5.0, center_depth + half)
                new_clouds.append({
                    "x": x, "y": y,
                    "radius": radius,
                    "min_depth": d_min,
                    "max_depth": d_max,
                    "attenuation_db": att,
                    "damage_dps": dmg,
                    "spawned_by_sub_id": None,
                    "expiry_ts": None,
                })

            if new_clouds:
                WEATHER_CLOUDS.extend(new_clouds)
                print(f"[WEATHER] Extended hazards to r≈{target_r:.0f}m (added {len(new_clouds)} clouds, total {len(WEATHER_CLOUDS)})")

    # -------- Per-sub local-band spawning for high local volume --------
    local_cfg = cfg.get("local_spawn", {})
    if local_cfg.get("enabled"):
        far_margin = float(local_cfg.get("far_margin_m", 2000.0))
        inner_off = float(local_cfg.get("inner_offset_m", 2000.0))
        outer_off = float(local_cfg.get("outer_offset_m", 6000.0))
        min_local = int(local_cfg.get("min_local_clouds", 40) or 0)
        ttl_s = float(local_cfg.get("ttl_s", 900.0))

        if min_local > 0:
            for s in subs:
                r_s = distance(cx, cy, s.x, s.y)
                if r_s <= R + far_margin:
                    continue
                band_inner = max(R + 100.0, r_s - inner_off)
                band_outer = r_s + outer_off
                # Count clouds whose radius from center lies in this band
                local_count = 0
                for c in WEATHER_CLOUDS:
                    rc = distance(cx, cy, c["x"], c["y"])
                    if band_inner <= rc <= band_outer:
                        local_count += 1
                if local_count >= min_local:
                    continue

                needed = min_local - local_count
                local_new: List[dict] = []
                for _ in range(needed):
                    ang = random.uniform(-math.pi, math.pi)
                    u = random.random()
                    r = band_inner + (band_outer - band_inner) * (1.0 - u * u)
                    x = cx + math.cos(ang) * r
                    y = cy + math.sin(ang) * r
                    radius = random.uniform(min_rad, max_rad)
                    center_depth = random.uniform(min_d, max_d)
                    thickness = random.uniform(min_th, max_th)
                    half = thickness / 2.0
                    d_min = max(0.0, center_depth - half)
                    d_max = max(d_min + 5.0, center_depth + half)
                    local_new.append({
                        "x": x, "y": y,
                        "radius": radius,
                        "min_depth": d_min,
                        "max_depth": d_max,
                        "attenuation_db": att,
                        "damage_dps": dmg,
                        "spawned_by_sub_id": s.id,
                        "expiry_ts": now + ttl_s,
                    })
                if local_new:
                    WEATHER_CLOUDS.extend(local_new)
                    print(f"[WEATHER] Spawned {len(local_new)} local hazards around sub {s.id[:6]} (r≈{r_s:.0f}m, total {len(WEATHER_CLOUDS)})")

    # -------- Global cap / trimming --------
    max_factor = float(cfg.get("max_count_factor", 4.0) or 4.0)
    max_total = int(base_count * max_factor)
    if len(WEATHER_CLOUDS) > max_total:
        # Prefer trimming innermost clouds first
        WEATHER_CLOUDS.sort(key=lambda c: distance(cx, cy, c["x"], c["y"]))  # inner first
        trim = len(WEATHER_CLOUDS) - max_total
        if trim > 0:
            WEATHER_CLOUDS = WEATHER_CLOUDS[trim:]
            print(f"[WEATHER] Trimmed {trim} inner hazards (total {len(WEATHER_CLOUDS)})")

def process_refueling_mem(subs: List[SubModel], fuelers: List["FuelerModel"], dt: float):
    """
    Handle refueling when a submarine is within 50m of a fueler at snorkel depth.
    Requires 2 minutes of continuous proximity before fuel is transferred.
    """
    now = time.time()
    if not fuelers:
        # Reset timers if no fuelers exist
        for s in subs:
            s.refuel_timer = 0.0
        return

    scfg = GAME_CFG.get("sub", DEFAULT_CFG["sub"])
    bcfg = scfg.get("battery", DEFAULT_CFG["sub"]["battery"])
    max_fuel_capacity = float(bcfg.get("max_fuel_capacity", 1000.0) or 0.0)
    snorkel_depth = scfg.get("snorkel_depth", 15.0)

    for s in subs:
        # Only refuel subs that have explicitly started refueling
        if not getattr(s, "refuel_active", False):
            s.refuel_timer = 0.0
            continue

        # Need some room for more fuel
        if getattr(s, "fuel", 0.0) >= max_fuel_capacity:
            s.refuel_timer = 0.0
            s.refuel_active = False
            s.refuel_fueler_id = None
            continue
        # Must be at snorkel depth
        if not s.is_snorkeling or s.depth > snorkel_depth + 0.5:
            s.refuel_timer = 0.0
            s.refuel_active = False
            s.refuel_fueler_id = None
            continue

        # Find the bound fueler within 50m (3D)
        nearest = None
        nearest_d = None
        for f in fuelers:
            if s.refuel_fueler_id and f.id != s.refuel_fueler_id:
                continue
            if getattr(f, "fuel", 0.0) <= 0.0:
                continue
            d = distance3d(s.x, s.y, s.depth, f.x, f.y, f.depth)
            if d <= 50.0 and (nearest_d is None or d < nearest_d):
                nearest = f
                nearest_d = d

        if not nearest:
            s.refuel_timer = 0.0
            s.refuel_active = False
            s.refuel_fueler_id = None
            continue

        # In-proximity timer
        s.refuel_timer += dt
        if s.refuel_timer < 120.0:
            continue

        # Warmup complete: now transfer fuel gradually at a configured rate
        available = float(getattr(nearest, "fuel", 0.0) or 0.0)
        sub_space = max(0.0, max_fuel_capacity - float(getattr(s, "fuel", 0.0) or 0.0))
        if available > 0.0 and sub_space > 0.0:
            # Default transfer rate: fuel units per second (1 unit ~= 1% battery)
            refuel_rate = float(bcfg.get("refuel_rate_per_s", 50.0) or 0.0)
            if refuel_rate <= 0.0:
                # Fallback: transfer all at once if rate is disabled
                amount = min(available, sub_space)
            else:
                amount = min(refuel_rate * dt, available, sub_space)
            if amount > 0.0:
                s.fuel = float(getattr(s, "fuel", 0.0) or 0.0) + amount
                nearest.fuel = available - amount
                # Mark this fueler as "used" the first time we transfer any fuel
                if getattr(nearest, "empty_since", None) is None:
                    nearest.empty_since = now
                # If fueler is now empty, clamp at zero
                if nearest.fuel <= 0.0:
                    nearest.fuel = 0.0

        # If we've filled the tank or emptied the fueler, stop refueling
        if getattr(s, "fuel", 0.0) >= max_fuel_capacity or getattr(nearest, "fuel", 0.0) <= 0.0:
            s.refuel_active = False
            s.refuel_fueler_id = None
            s.refuel_timer = 0.0

def weather_cloud_attenuation(x: float, y: float, depth: float) -> float:
    """Return additional sonar attenuation (dB) from any hazard cloud at this point."""
    if not WEATHER_CLOUDS:
        return 0.0
    best = 0.0
    for c in WEATHER_CLOUDS:
        if depth < c["min_depth"] or depth > c["max_depth"]:
            continue
        if distance(x, y, c["x"], c["y"]) <= c["radius"]:
            best = max(best, c.get("attenuation_db", 0.0) or 0.0)
    return best

def weather_cloud_occlusion(x1: float, y1: float, d1: float,
                            x2: float, y2: float, d2: float) -> float:
    """
    Attenuation from hazard clouds that lie between two points (line-of-sight occlusion).
    We approximate by checking whether the XY segment intersects any cloud whose
    depth band overlaps the depth segment between d1 and d2.
    """
    if not WEATHER_CLOUDS:
        return 0.0

    def seg_point_dist(px, py, ax, ay, bx, by):
        dx = bx - ax
        dy = by - ay
        if dx == 0.0 and dy == 0.0:
            return distance(px, py, ax, ay)
        t = ((px - ax)*dx + (py - ay)*dy) / (dx*dx + dy*dy)
        t = max(0.0, min(1.0, t))
        cx = ax + t*dx
        cy = ay + t*dy
        return distance(px, py, cx, cy)

    best = 0.0
    seg_min_d = min(d1, d2)
    seg_max_d = max(d1, d2)

    for c in WEATHER_CLOUDS:
        # Depth overlap between segment and cloud band
        if seg_max_d < c["min_depth"] or seg_min_d > c["max_depth"]:
            continue
        # Horizontal intersection: segment passes within radius of cloud center
        dist_xy = seg_point_dist(c["x"], c["y"], x1, y1, x2, y2)
        if dist_xy <= c["radius"]:
            best = max(best, c.get("attenuation_db", 0.0) or 0.0)

    return best

def weather_cloud_damage(x: float, y: float, depth: float) -> float:
    """Return DPS from any hazardous cloud at this point (outside the main ring)."""
    if not WEATHER_CLOUDS:
        return 0.0
    if not is_outside_ring(x, y):
        return 0.0
    dps = 0.0
    for c in WEATHER_CLOUDS:
        if depth < c["min_depth"] or depth > c["max_depth"]:
            continue
        if distance(x, y, c["x"], c["y"]) <= c["radius"]:
            dps = max(dps, float(c.get("damage_dps", 0.0) or 0.0))
    return dps

# -------------------------- Game logic --------------------------
# Active sonar echoes waiting to return
PENDING_PINGS = []
PENDING_PINGS_LOCK = threading.RLock()  # <-- NEW

def update_sub(s: SubModel, dt: float, now: float):
    scfg = GAME_CFG.get("sub", DEFAULT_CFG["sub"])
    max_spd      = scfg["max_speed"]
    yaw_rate_rad = math.radians(scfg["yaw_rate_deg_s"])
    pitch_rate   = math.radians(scfg["pitch_rate_deg_s"])
    planes_eff   = scfg["planes_effect"]
    neutral      = scfg["neutral_bias"]
    crush_depth  = scfg["crush_depth"]
    crush_dps    = scfg["crush_dps_per_100m"]

    # RUDDER servo - requires battery power
    max_rudder_deg  = scfg.get("max_rudder_deg", 30.0)
    rudder_rate_deg = scfg.get("rudder_rate_deg_s", 60.0)
    MAX_RUDDER_RAD  = math.radians(max_rudder_deg)
    MAX_RUDDER_STEP = math.radians(rudder_rate_deg) * dt

    s.rudder_cmd = clamp(float(s.rudder_cmd or 0.0), -1.0, 1.0)
    target_rudder_angle = s.rudder_cmd * MAX_RUDDER_RAD
    if s.rudder_angle is None:
        s.rudder_angle = 0.0
    
    # Only move rudder if battery power available
    if s.battery > 0.0:
        error = target_rudder_angle - s.rudder_angle
        s.rudder_angle += clamp(error, -MAX_RUDDER_STEP, MAX_RUDDER_STEP)
        s.rudder_angle = clamp(s.rudder_angle, -MAX_RUDDER_RAD, MAX_RUDDER_RAD)
    # else: rudder stays in current position when battery dead

    # Auto-steering to target heading
    if s.target_heading is not None:
        heading_error = wrap_angle(s.target_heading - s.heading)
        # Use proportional control to steer toward target heading
        max_turn_rate = math.radians(scfg.get("yaw_rate_deg_s", 3.0))  # degrees per second
        turn_rate = clamp(heading_error * 0.5, -max_turn_rate, max_turn_rate)  # P=0.5
        s.heading = wrap_angle(s.heading + turn_rate * dt)
        # Clear target heading when close enough (within 2 degrees)
        if abs(heading_error) < math.radians(2.0):
            s.target_heading = None
    else:
        # Manual rudder control
        rudder_frac = 0.0 if MAX_RUDDER_RAD == 0 else (s.rudder_angle / MAX_RUDDER_RAD)
        s.heading = wrap_angle(s.heading + yaw_rate_rad * rudder_frac * dt)

    # Planes -> pitch - also requires battery
    target_pitch = clamp(s.planes * planes_eff, -1.0, 1.0) * math.radians(30.0)
    if s.battery > 0.0:
        s.pitch += clamp(target_pitch - s.pitch, -pitch_rate*dt, pitch_rate*dt)
    # else: planes stay in current position when battery dead

    # Speed with acceleration - disabled if no battery
    max_spd = scfg["max_speed"]
    acceleration = scfg.get("acceleration", 2.0)
    
    # If diesel/snorkel is running, cap maximum speed to 75% of normal to
    # model reduced performance while snorkeling on diesels.
    if s.is_snorkeling:
        max_spd = max_spd * 0.75

    # No propulsion if battery is dead
    if s.battery <= 0.0 or getattr(s, "refuel_active", False):
        target_speed = 0.0
    else:
        target_speed = clamp(s.throttle, 0.0, 1.0) * max_spd
    
    speed_diff = target_speed - s.speed
    max_change = acceleration * dt
    s.speed = s.speed + clamp(speed_diff, -max_change, max_change)

    # If the battery is completely dead (and we're not in an active emergency
    # blow), automatic depth hold should no longer function; the sub should
    # behave like a powerless hull that continues to sink.
    if s.battery <= 0.0 and not s.blow_active:
        s.target_depth = None
        # Neutralize planes so they don't hold or climb without power
        s.planes = 0.0

    # Vertical dynamics - sinking when too slow
    v_down = neutral * (1.0 - s.throttle)
    
    # Additional sinking if speed is too low (loss of hydrodynamic control)
    min_control_speed = 2.0  # m/s minimum for depth control
    if s.speed < min_control_speed:
        speed_factor = s.speed / min_control_speed
        sink_rate = (1.0 - speed_factor) * 0.8  # up to 0.8 m/s sinking
        v_down += sink_rate

    # Emergency blow
    ebcfg = scfg["emergency_blow"]
    if s.blow_active and now < s.blow_end and s.blow_charge > 0.0:
        v_down -= ebcfg["upward_mps"]
        s.blow_charge = clamp(s.blow_charge - (dt / ebcfg["duration_s"]), 0.0, 1.0)
    else:
        s.blow_active = False

    # Depth hold autopilot
    autopilot_on = (s.target_depth is not None) and (abs(s.planes) < 0.05)
    if autopilot_on:
        err_m = s.target_depth - s.depth
        ap_pitch = clamp(-err_m * math.radians(0.5), -math.radians(30), math.radians(30))
        s.pitch += clamp(ap_pitch - s.pitch, -pitch_rate*dt, pitch_rate*dt)
        v_down += clamp(err_m * 0.02, -1.5, 1.5)

    # Hydrodynamic lift
    LIFT = 0.45
    v_down -= math.sin(s.pitch) * max(0.0, s.speed) * LIFT

    # Apply vertical
    s.depth = max(0.0, s.depth + v_down * dt)

    # XY
    s.x += math.cos(s.heading) * s.speed * dt
    s.y += math.sin(s.heading) * s.speed * dt

    # Battery, diesel fuel & snorkel recharge + auto-off with hysteresis
    bcfg = scfg["battery"]
    # Initialize fuel if missing
    if getattr(s, "fuel", None) is None:
        s.fuel = float(bcfg.get("initial_fuel",
                                bcfg.get("max_fuel_capacity", 1000.0)) or 0.0)
    # Exponential battery drain for high speeds
    speed_ratio = s.speed / max_spd
    if speed_ratio > 0.5:  # Above 50% max speed
        high_speed_mult = bcfg.get("high_speed_multiplier", 3.0)
        excess_speed = speed_ratio - 0.5
        drain_multiplier = 1.0 + (excess_speed * 2.0) ** 2 * high_speed_mult
    else:
        drain_multiplier = 1.0
    
    if getattr(s, "refuel_active", False):
        # While hard-refueling, don't burn propulsion battery
        s.throttle = 0.0
        battery_drain = 0.0
    else:
        battery_drain = s.throttle * bcfg["drain_per_throttle_per_s"] * drain_multiplier * dt
    s.battery = max(0.0, min(100.0, s.battery - battery_drain))

    snorkel_depth = scfg.get("snorkel_depth", 15.0)
    off_hyst = scfg.get("snorkel_off_hysteresis", 2.0)
    if s.is_snorkeling and s.depth <= snorkel_depth:
        # Recharge battery only while there is diesel fuel left; consume fuel proportional
        # to charge added. 1.0 fuel unit ~= 1% battery recharge.
        recharge_rate = float(bcfg.get("recharge_per_s_snorkel", 0.25) or 0.0)
        has_fuel = getattr(s, "fuel", None) is not None and s.fuel > 0.0
        if recharge_rate > 0.0 and has_fuel:
            # How much we *could* add this frame
            potential = recharge_rate * dt
            # Cap by available fuel and remaining battery headroom
            headroom = 100.0 - float(s.battery)
            delta = min(potential, s.fuel, headroom)
            if delta > 0.0:
                s.battery = clamp(s.battery + delta, 0.0, 100.0)
                s.fuel = max(0.0, float(s.fuel) - delta)
        # Emergency blow recharge also requires diesel fuel; if you're out of fuel,
        # the blow system cannot be recharged.
        if has_fuel:
            s.blow_charge = clamp(s.blow_charge + ebcfg["recharge_per_s_at_snorkel"] * dt, 0.0, 1.0)
    # Auto-off once we exceed snorkel depth + hysteresis (but keep snorkel pinned while refueling)
    if (not getattr(s, "refuel_active", False)) and s.is_snorkeling and s.depth > (snorkel_depth + off_hyst):
        s.is_snorkeling = False

    # If refueling is active, "moor" the sub at snorkel depth so it doesn't sink
    if getattr(s, "refuel_active", False):
        s.depth = max(0.0, snorkel_depth)
    else:
        # Crush damage
        if s.depth > crush_depth:
            over = s.depth - crush_depth
            dps = (over / 100.0) * crush_dps
            s.health = max(0.0, s.health - dps * dt)

    # Weather damage: only from hazardous clouds, and only outside the ring
    if is_outside_ring(s.x, s.y):
        cloud_dps = weather_cloud_damage(s.x, s.y, s.depth)
        if cloud_dps > 0.0:
            s.health = max(0.0, s.health - cloud_dps * dt)

def explode_torpedo_in_mem(t: TorpedoModel, subs: List[SubModel], pending_events: List[Tuple[int,str,dict]]):
    blast = GAME_CFG["torpedo"]["blast_radius"]
    for s in subs:
        d = distance3d(t.x, t.y, t.depth, s.x, s.y, s.depth)
        if d <= blast and s.health > 0.0:
            # Graduated damage based on distance
            if d <= 60.0:      damage = 100.0
            elif d <= 80.0:    damage = 75.0
            elif d <= 100.0:   damage = 50.0
            else:              damage = 25.0
            
            prev_health = s.health
            s.health = max(0.0, s.health - damage)

            # If this hit killed the sub, award a kill + score to the torpedo owner.
            if prev_health > 0.0 and s.health <= 0.0:
                try:
                    killer_owner_id = t.owner_id
                    killer_sub = next((sub for sub in subs if sub.owner_id == killer_owner_id), None)
                    if killer_sub:
                        # Increment kills
                        killer_sub.kills = int(getattr(killer_sub, "kills", 0)) + 1
                        # Simple score bonus per kill
                        killer_sub.score = float(getattr(killer_sub, "score", 0.0)) + 100.0
                except Exception as _:
                    pass

            pending_events.append((
                s.owner_id, 'explosion',
                {"time": time.time(), "at": [t.x, t.y, t.depth], "torpedo_id": t.id, "blast_radius": blast, "damage": damage, "distance": d}
            ))

def update_torpedo(t: TorpedoModel, dt: float, now: float):
    cfg = GAME_CFG["torpedo"]
    turn_rate = math.radians(cfg.get("turn_rate_deg_s", DEFAULT_CFG["torpedo"]["turn_rate_deg_s"]))
    depth_rate = float(cfg.get("depth_rate_m_s", DEFAULT_CFG["torpedo"]["depth_rate_m_s"]))
    max_range = float(cfg.get("max_range", DEFAULT_CFG["torpedo"]["max_range"]))
    prox = float(cfg.get("proximity_fuze_m", DEFAULT_CFG["torpedo"]["proximity_fuze_m"]))
    
    # Variable speed control
    min_speed = float(cfg.get("min_speed", 8.0))
    max_speed = float(cfg.get("max_speed", 18.0))
    target_speed = clamp(float(getattr(t, "target_speed", max_speed)), min_speed, max_speed)
    
    # Speed acceleration
    speed_accel = 5.0  # m/s^2
    speed_diff = target_speed - t.speed
    max_change = speed_accel * dt
    t.speed = t.speed + clamp(speed_diff, -max_change, max_change)

    if getattr(t, "start_x", None) is None:
        t.start_x = t.x
        t.start_y = t.y
        if getattr(t, "created_at", None) is None:
            t.created_at = now

    # Wire control cutoff by geometry
    if t.control_mode == 'wire' and t.parent_sub:
        parent = SubModel.query.get(t.parent_sub)  # occasional lookup
        if parent is not None:
            dist_parent = distance(t.x, t.y, parent.x, parent.y)
            if dist_parent > float(t.wire_length or cfg.get("max_range", 6000.0)):
                t.control_mode = 'free'

    # Heading guidance
    if getattr(t, "target_heading", None) is not None:
        da = wrap_angle(t.target_heading - t.heading)
        step = clamp(da, -turn_rate*dt, turn_rate*dt)
        t.heading = wrap_angle(t.heading + step)
    elif getattr(t, "pending_turn", None) is not None:
        want = math.radians(t.pending_turn)
        step = clamp(want, -turn_rate*dt, turn_rate*dt)
        t.heading = wrap_angle(t.heading + step)
        rem = want - step
        t.pending_turn = (math.degrees(rem)) if abs(rem) > 1e-4 else None

    # Depth guidance
    if getattr(t, "target_depth", None) is not None:
        dz = t.target_depth - t.depth
        t.depth += clamp(dz, -depth_rate*dt, depth_rate*dt)

    # Integrate position
    t.x += math.cos(t.heading) * t.speed * dt
    t.y += math.sin(t.heading) * t.speed * dt

    # Battery drain based on current speed (faster drains disproportionately more)
    bcfg = cfg.get("battery", DEFAULT_CFG["torpedo"].get("battery", {}))
    drain_per_mps = float(bcfg.get("drain_per_mps_per_s", 0.03) or 0.0)
    if getattr(t, "battery", None) is None:
        t.battery = float(bcfg.get("capacity", 100.0) or 100.0)
    speed_for_drain = max(0.0, float(t.speed or 0.0))
    # Use speed^2 so higher speeds burn much more energy per meter,
    # giving shorter effective range at high speed and longer range at low speed.
    t.battery = max(0.0, float(t.battery) - drain_per_mps * (speed_for_drain ** 2) * dt)

    # If torpedo battery is empty, mark it for a terminal detonation and shut down systems.
    if t.battery <= 0.0 and not getattr(t, "_battery_dead", False):
        t.speed = 0.0
        t.target_speed = 0.0
        t.passive_sonar_active = False
        t.active_sonar_enabled = False
        t._battery_dead = True

    # Range self-destruct
    traveled = distance(t.x, t.y, t.start_x, t.start_y)
    if traveled > max_range:
        t._expired = True
        return

    # Proximity fuze check flag (blast handled later)
    min_safe_distance = 150.0  # 150m minimum safe distance
    if hasattr(t, 'parent_sub') and t.parent_sub:
        pass
    t._check_prox = prox > 0.0 and (now - t.created_at) >= cfg.get("arming_delay_s", 1.0)
    t._min_safe_distance = min_safe_distance  # Store for later use

def process_wire_links_mem(torps: List[TorpedoModel], subs: List[SubModel]):
    for t in torps:
        if t.control_mode != 'wire':
            continue
        parent = next((s for s in subs if s.id == t.parent_sub), None)
        if not parent:
            t.control_mode = 'free'; continue
        d = distance(t.x, t.y, parent.x, parent.y)
        if d > float(t.wire_length or GAME_CFG["torpedo"].get("max_range", 6000.0)):
            t.control_mode = 'free'

def process_explosions_mem(torps: List[TorpedoModel], subs: List[SubModel], pending_events: List[Tuple[int,str,dict]]):
    blast = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["blast_radius"]
    prox_fuze = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"]).get("proximity_fuze_m", 30.0)
    
    for t in torps:
        # Battery-dead torpedoes detonate in place once
        if getattr(t, "_battery_dead", False) and not getattr(t, "_delete", False):
            explode_torpedo_in_mem(t, subs, pending_events)
            t._delete = True
            continue
        if getattr(t, "_expired", False):
            t._delete = True
            continue
        if getattr(t, "_check_prox", False):
            # Check minimum safe distance from parent submarine
            min_safe_distance = getattr(t, "_min_safe_distance", 150.0)
            parent = next((s for s in subs if s.id == t.parent_sub), None) if hasattr(t, 'parent_sub') and t.parent_sub else None
            too_close_to_parent = False
            if parent:
                dist_to_parent = distance3d(t.x, t.y, t.depth, parent.x, parent.y, parent.depth)
                too_close_to_parent = dist_to_parent < min_safe_distance
            
            if too_close_to_parent:
                continue  # Don't explode if too close to parent
                
            for s in subs:
                if s.health <= 0: 
                    continue
                if distance3d(t.x, t.y, t.depth, s.x, s.y, s.depth) <= prox_fuze:
                    explode_torpedo_in_mem(t, subs, pending_events)
                    t._delete = True
                    break

def schedule_passive_contacts(now: float, subs: List[SubModel], torps: List[TorpedoModel], pending_events: List[Tuple[int,str,dict]]):
    pcfg = GAME_CFG.get("sonar", {}).get("passive", DEFAULT_CFG["sonar"]["passive"])
    acfg = GAME_CFG.get("sonar", {}).get("active", DEFAULT_CFG["sonar"]["active"])
    subcfg = GAME_CFG.get("sub", DEFAULT_CFG["sub"])
    torpcfg = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])
    
    for obs in subs:
        if now - obs.last_report < random.uniform(*pcfg["report_interval_s"]):
            continue
        
        # Check submarines
        for tgt in subs:
            if tgt.owner_id == obs.owner_id and tgt.id == obs.id:
                continue
            speed_noise = pcfg["speed_noise_gain"] * (tgt.speed / subcfg["max_speed"])
            # Extra noise if target is actively using the hazard scanner
            scanner_bonus = 0.0
            if now < float(getattr(tgt, "scanner_noise_until", 0.0) or 0.0):
                scanner_bonus = float(pcfg.get("scanner_noise_bonus_db", 8.0) or 0.0)
            snorkel_bonus = pcfg["snorkel_bonus"] if tgt.is_snorkeling else 0.0
            blow_bonus = 25.0 if tgt.blow_active else 0.0

            rng = distance(obs.x, obs.y, tgt.x, tgt.y)  # <-- compute before logging

            if tgt.blow_active:
                print(f"[DEBUG] Emergency blow detected: sub {tgt.id[:6]} at range {rng:.0f}m")
            
            base = pcfg["base_snr"] + speed_noise + snorkel_bonus + blow_bonus + scanner_bonus
            
            # Emergency blow can be heard from much farther
            max_range = 8000.0 if tgt.blow_active else acfg["max_range"]
            if rng > max_range:
                continue
                
            snr = base - (rng / 1000.0) * 2.0 - (tgt.depth / 200.0)
            # Distance below which clouds stop blocking so nearby subs can still hear
            close_hear_range = float(WORLD_WEATHER.get("cloud_close_hear_range_m", 400.0) or 0.0)
            if rng >= close_hear_range:
                # Weather attenuation if either observer or target is outside ring
                if is_outside_ring(obs.x, obs.y) or is_outside_ring(tgt.x, tgt.y):
                    snr -= float(WORLD_WEATHER.get("sonar_attenuation_db", 0.0) or 0.0)
                # Additional attenuation from hazard clouds
                snr -= max(
                    weather_cloud_attenuation(obs.x, obs.y, obs.depth),
                    weather_cloud_attenuation(tgt.x, tgt.y, tgt.depth)
                )
                # Occlusion from hazard clouds between observer and target
                snr -= weather_cloud_occlusion(obs.x, obs.y, obs.depth,
                                               tgt.x, tgt.y, tgt.depth)
            if snr < 5.0:
                continue
            brg = math.atan2(tgt.y - obs.y, tgt.x - obs.x)
            jitter = math.radians(pcfg["bearing_jitter_deg"] if tgt.depth < 50.0 else 1.0)
            brg = wrap_angle(brg + random.uniform(-jitter, jitter))
            rel = wrap_angle(brg - obs.heading)
            rc = "short" if rng < 1200 else "medium" if rng < 3000 else "long"
            
            contact_type = "emergency_blow" if tgt.blow_active else "submarine"
            
            pending_events.append((obs.owner_id,'contact',{
                "type": "passive",
                "observer_sub_id": obs.id,
                "bearing": brg,
                "bearing_relative": rel,
                "range_class": rc,
                "snr": snr,
                "contact_type": contact_type,
                "time": now
            }))
            obs.last_report = now
            break
        
        # Check torpedoes
        for torp in torps:
            if torp.owner_id == obs.owner_id:
                continue
            # Torpedoes are much louder due to high-speed propulsion
            torp_noise = pcfg["speed_noise_gain"] * (torp.speed / torpcfg["speed"]) * 2.0
            base = pcfg["base_snr"] * 1.2 + torp_noise  # Actually louder than subs
            rng = distance(obs.x, obs.y, torp.x, torp.y)
            if rng > acfg["max_range"] * 0.8:  # 20% shorter detection range
                continue
            snr = base - (rng / 1000.0) * 2.5 - (torp.depth / 200.0)  # Similar falloff to subs
            close_hear_range = float(WORLD_WEATHER.get("cloud_close_hear_range_m", 400.0) or 0.0)
            if rng >= close_hear_range:
                if is_outside_ring(obs.x, obs.y) or is_outside_ring(torp.x, torp.y):
                    snr -= float(WORLD_WEATHER.get("sonar_attenuation_db", 0.0) or 0.0)
                snr -= max(
                    weather_cloud_attenuation(obs.x, obs.y, obs.depth),
                    weather_cloud_attenuation(torp.x, torp.y, torp.depth)
                )
                snr -= weather_cloud_occlusion(obs.x, obs.y, obs.depth,
                                               torp.x, torp.y, torp.depth)
            if snr < 4.0:  # Lower threshold - easier to detect
                continue
            brg = math.atan2(torp.y - obs.y, torp.x - obs.x)
            jitter = math.radians(pcfg["bearing_jitter_deg"] * 1.2)  # Slightly more bearing error
            brg = wrap_angle(brg + random.uniform(-jitter, jitter))
            rel = wrap_angle(brg - obs.heading)
            rc = "short" if rng < 1000 else "medium" if rng < 2500 else "long"
            pending_events.append((obs.owner_id,'contact',{
                "type": "passive",
                "observer_sub_id": obs.id,
                "bearing": brg,
                "bearing_relative": rel,
                "range_class": rc,
                "snr": snr,
                "contact_type": "torpedo",
                "time": now
            }))
            obs.last_report = now
            break
    
    # Process torpedo passive sonar
    torp_sonar_cfg = GAME_CFG.get("torpedo", {}).get("sonar", {}).get("passive", {
        "max_range": 2000.0, "report_interval_s": [1.0, 3.0], "bearing_jitter_deg": 8.0
    })
    
    for torp in torps:
        if not getattr(torp, 'passive_sonar_active', True):
            continue
        if now - getattr(torp, 'last_sonar_contact', 0.0) < random.uniform(*torp_sonar_cfg["report_interval_s"]):
            continue
            
        # Torpedo detects submarines
        for tgt in subs:
            if tgt.owner_id == torp.owner_id:
                continue
            rng = distance(torp.x, torp.y, tgt.x, tgt.y)
            if rng > torp_sonar_cfg["max_range"]:
                continue
                
            # Check if target is within 210Â° beam (105Â° each side, 150Â° baffle astern)
            brg = math.atan2(tgt.y - torp.y, tgt.x - torp.x)
            rel_bearing = wrap_angle(brg - torp.heading)
            if abs(rel_bearing) > math.radians(105.0):  # 210Â° beam = Â±105Â°
                continue
                
            # Calculate detection probability based on target noise
            speed_noise = pcfg["speed_noise_gain"] * (tgt.speed / subcfg["max_speed"])
            snorkel_bonus = pcfg["snorkel_bonus"] if tgt.is_snorkeling else 0.0
            blow_bonus = 25.0 if tgt.blow_active else 0.0
            base_snr = pcfg["base_snr"] + speed_noise + snorkel_bonus + blow_bonus
            snr = base_snr - (rng / 1000.0) * 2.0 - (tgt.depth / 200.0)
            close_hear_range = float(WORLD_WEATHER.get("cloud_close_hear_range_m", 400.0) or 0.0)
            if rng >= close_hear_range:
                if is_outside_ring(torp.x, torp.y) or is_outside_ring(tgt.x, tgt.y):
                    snr -= float(WORLD_WEATHER.get("sonar_attenuation_db", 0.0) or 0.0)
                snr -= max(
                    weather_cloud_attenuation(torp.x, torp.y, torp.depth),
                    weather_cloud_attenuation(tgt.x, tgt.y, tgt.depth)
                )
                snr -= weather_cloud_occlusion(torp.x, torp.y, torp.depth,
                                               tgt.x, tgt.y, tgt.depth)
            
            if snr < 3.0:  # Lower threshold for torpedo sonar
                continue
                
            jitter = math.radians(torp_sonar_cfg["bearing_jitter_deg"])
            brg = wrap_angle(brg + random.uniform(-jitter, jitter))
            
            # Update torpedo sonar bearing and contact time
            torp.passive_sonar_bearing = brg
            torp.last_sonar_contact = now
            
            # Send contact to torpedo owner
            pending_events.append((torp.owner_id, 'torpedo_contact', {
                "type": "passive",
                "torpedo_id": torp.id,
                "bearing": brg,
                "bearing_relative": wrap_angle(brg - torp.heading),
                "range_class": "short" if rng < 800 else "medium" if rng < 1500 else "long",
                "snr": snr,
                "contact_type": "submarine",
                "time": now
            }))
            break
    
    # Process torpedo active sonar (auto-ping)
    torp_active_cfg = GAME_CFG.get("torpedo", {}).get("sonar", {}).get("active", {
        "max_range": 1500.0, "ping_interval_s": 3.0
    })
    
    for torp in torps:
        if not getattr(torp, 'active_sonar_enabled', False):
            continue
        if now - getattr(torp, 'last_active_ping', 0.0) < torp_active_cfg.get("ping_interval_s", 3.0):
            continue
        # Check torpedo battery before auto-ping
        tcfg = GAME_CFG.get("torpedo", {})
        bcfg = tcfg.get("battery", DEFAULT_CFG["torpedo"].get("battery", {}))
        ping_cost = float(bcfg.get("active_ping_cost", 2.0) or 0.0)
        min_for_ping = float(bcfg.get("min_for_ping", 5.0) or 0.0)
        if getattr(torp, "battery", None) is None:
            torp.battery = float(bcfg.get("capacity", 100.0) or 100.0)
        if torp.battery < max(ping_cost, min_for_ping):
            continue
            
        # Auto-ping with fixed 30Â° beam
        contacts = []
        for s in subs:
            if s.owner_id == torp.owner_id:
                continue
            dx = s.x - torp.x; dy = s.y - torp.y
            rng = math.sqrt(dx*dx + dy*dy + (s.depth - torp.depth)**2)
            if rng > torp_active_cfg["max_range"]:
                continue
            brg = math.atan2(dy, dx)
            rel = abs(wrap_angle(brg - torp.heading))
            if rel > math.radians(15.0):  # 30Â° beam = Â±15Â°
                continue
            contacts.append({
                "bearing": brg,
                "range": rng + random.uniform(-20, 20),
                "depth": s.depth + random.uniform(-20, 20)
            })
        
        torp.last_active_ping = now
        
        if contacts:
            # Deduct battery for auto-ping only if we actually got returns
            torp.battery = max(0.0, float(torp.battery) - ping_cost)
            pending_events.append((torp.owner_id, 'torpedo_ping', {
                "torpedo_id": torp.id,
                "contacts": contacts,
                "time": now
            }))

def schedule_active_ping(obs: SubModel, beam_deg: float, max_range: float, now: float, center_world: float=None):
    acfg = GAME_CFG.get("sonar", {}).get("active", DEFAULT_CFG["sonar"]["active"])
    max_range = min(max_range, acfg["max_range"])
    if center_world is None:
        center_world = obs.heading

    # Beam focus quality bonus - narrower beams give better quality
    beam_focus_bonus = max(0.0, (90.0 - beam_deg) / 90.0) * 6.0  # up to +6 SNR for narrow beams

    for tgt in SubModel.query.all():  # rare read
        if tgt.id == obs.id and tgt.owner_id == obs.owner_id:
            continue
        dx = tgt.x - obs.x; dy = tgt.y - obs.y
        rng3d = math.sqrt(dx*dx + dy*dy + (tgt.depth - obs.depth)**2)
        if rng3d > max_range: 
            continue
        brg_world = math.atan2(dy, dx)
        rel = abs(wrap_angle(brg_world - center_world))
        if rel > math.radians(beam_deg / 2.0): 
            continue
        echo_lvl = 18.0 - (rng3d / 400.0) + (8.0 if tgt.is_snorkeling else 0.0) + beam_focus_bonus
        close_hear_range = float(WORLD_WEATHER.get("cloud_close_hear_range_m", 400.0) or 0.0)
        if rng3d >= close_hear_range:
            # Weather attenuation if either sub is outside the ring
            if is_outside_ring(obs.x, obs.y) or is_outside_ring(tgt.x, tgt.y):
                echo_lvl -= float(WORLD_WEATHER.get("sonar_attenuation_db", 0.0) or 0.0)
            echo_lvl -= max(
                weather_cloud_attenuation(obs.x, obs.y, obs.depth),
                weather_cloud_attenuation(tgt.x, tgt.y, tgt.depth)
            )
            echo_lvl -= weather_cloud_occlusion(obs.x, obs.y, obs.depth,
                                                tgt.x, tgt.y, tgt.depth)
        c = acfg["sound_speed"]
        eta = now + 2.0 * (rng3d / c)
        with PENDING_PINGS_LOCK:  # <-- thread-safe append
            PENDING_PINGS.append({
                'eta': eta,
                'rng': rng3d,
                'bearing': brg_world,
                'echo_level': echo_lvl,
                'observer_sub_id': obs.id,
                'observer_user_id': obs.owner_id,
                'observer_depth': float(obs.depth),
                'target_depth': float(tgt.depth)
            })

def process_active_pings(now: float):
    with PENDING_PINGS_LOCK:
        due = [p for p in PENDING_PINGS if p['eta'] <= now]
        if not due: 
            return
        PENDING_PINGS[:] = [p for p in PENDING_PINGS if p['eta'] > now]
    acfg = GAME_CFG.get("sonar", {}).get("active", DEFAULT_CFG["sonar"]["active"])
    for p in due:
        lvl = float(p['echo_level'])
        q = 1.0 / (1.0 + math.exp(-(lvl - 10.0)/6.0))
        bearing_noise = math.radians(acfg["brg_sigma_deg"]) * (1.0 - q)
        range_noise   = max(5.0, acfg["rng_sigma_m"] * (1.0 - q))
        est_brg = wrap_angle(float(p['bearing']) + random.uniform(-bearing_noise, bearing_noise))
        est_rng = max(1.0, float(p['rng']) + random.uniform(-range_noise, range_noise))
        
        # Add depth estimation error - much worse at longer ranges and lower quality
        depth_noise = max(15.0, (est_rng / 50.0) * (1.0 - q) * 25.0)  # 15-75m error typical
        est_depth = max(0.0, float(p['target_depth']) + random.uniform(-depth_noise, depth_noise))
        
        obs = SubModel.query.get(p['observer_sub_id'])
        obs_heading = float(obs.heading) if obs else 0.0
        send_private(p['observer_user_id'], 'echo', {
            'type': 'active',
            'observer_sub_id': p['observer_sub_id'],
            'bearing': est_brg,
            'bearing_relative': wrap_angle(est_brg - obs_heading),
            'range': est_rng,
            'estimated_depth': est_depth,
            'quality': q,
            'time': now
        })

# -------------------------- PERF --------------------------
_perf = {"tick_ms": 0.0, "db_fetch_ms": 0.0, "physics_ms": 0.0, "db_commit_ms": 0.0}
def _ms(t0): return (time.time() - t0) * 1000.0

# -------------------------- Main loop --------------------------
def _apply_fields(dst, src, fields):
    for f in fields:
        setattr(dst, f, getattr(src, f))

def game_loop():
    with app.app_context():
        last_ts = time.time()
        while True:
            loop_start = time.time()
            dt = max(0.0, min(loop_start - last_ts, 0.25))  # clamp dt
            last_ts = loop_start
            try:
                # 1) Snapshot
                t0 = time.time()
                with WORLD_LOCK:
                    subs: List[SubModel] = SubModel.query.all()
                    torps: List[TorpedoModel] = TorpedoModel.query.all()
                    fuelers: List[FuelerModel] = FuelerModel.query.all()
                _perf["db_fetch_ms"] = _ms(t0)

                # 2) Physics outside lock
                t1 = time.time()
                pending_events: List[Tuple[int,str,dict]] = []
                dead_sub_ids = set()

                # Dynamically extend / trim hazard fields based on player distance
                try:
                    ensure_dynamic_weather_clouds(subs)
                except Exception as _:
                    pass

                for s in subs:
                    if s.health <= 0.0:
                        dead_sub_ids.add(s.id); continue
                    # Update survival-based score before physics step
                    try:
                        # Base score per second of survival
                        base_rate = 1.0
                        kills = int(getattr(s, "kills", 0))
                        # Multiplier grows with kills (e.g. 1.0, 1.5, 2.0, 2.5, ...)
                        multiplier = 1.0 + 0.5 * max(0, kills)
                        s.score = float(getattr(s, "score", 0.0)) + base_rate * multiplier * dt
                    except Exception as _:
                        pass

                    update_sub(s, dt, loop_start)

                for t in torps:
                    update_torpedo(t, dt, loop_start)

                # Refueling logic (submarines alongside fuelers)
                process_refueling_mem(subs, fuelers, dt)

                process_wire_links_mem(torps, subs)
                process_explosions_mem(torps, subs, pending_events)
                schedule_passive_contacts(loop_start, subs, torps, pending_events)
                process_active_pings(loop_start)
                _perf["physics_ms"] = _ms(t1)

                # 3) Commit once, resilient to concurrent deletes
                t2 = time.time()
                with WORLD_LOCK:
                    try:
                        # delete dead subs and record death timestamps on owning users
                        for sid in list(dead_sub_ids):
                            srow = db.session.get(SubModel, sid)
                            if srow:
                                try:
                                    urow = db.session.get(User, srow.owner_id)
                                    if urow:
                                        # Shift the previous death timestamp and record the new one.
                                        prev = float(getattr(urow, "last_death_ts", 0.0) or 0.0)
                                        urow.prev_death_ts = prev
                                        urow.last_death_ts = time.time()
                                except Exception:
                                    pass
                                db.session.delete(srow)

                        # delete marked/expired torps
                        for t in torps:
                            if getattr(t, "_delete", False) or getattr(t, "_expired", False):
                                trow = db.session.get(TorpedoModel, t.id)
                                if trow: db.session.delete(trow)

                        # Despawn fuelers either 20 minutes after spawn, or 5 minutes after first use
                        now_commit = time.time()
                        for f in fuelers:
                            age = now_commit - float(getattr(f, "spawned_at", now_commit))
                            empty_since = getattr(f, "empty_since", None)
                            empty_age = None
                            if empty_since is not None:
                                try:
                                    empty_age = now_commit - float(empty_since)
                                except Exception:
                                    empty_age = None
                            if age > 1200.0 or (empty_age is not None and empty_age > 300.0):
                                frow = db.session.get(FuelerModel, f.id)
                                if frow:
                                    db.session.delete(frow)

                        # update subs that still exist
                        sub_fields = ["x","y","depth","heading","pitch","rudder_angle","rudder_cmd",
                                      "planes","throttle","target_depth","speed","battery","fuel",
                                      "is_snorkeling","blow_active","blow_charge","blow_end","health",
                                      "passive_dir","last_report","refuel_timer","refuel_active",
                                      "refuel_fueler_id"]
                        for s in subs:
                            if s.id in dead_sub_ids: 
                                continue
                            srow = db.session.get(SubModel, s.id)
                            if srow:
                                _apply_fields(srow, s, sub_fields)

                        # update torps that still exist
                        torp_fields = ["x","y","depth","target_depth","heading","speed","target_speed",
                                       "control_mode","wire_length","updated_at","created_at",
                                       "passive_sonar_active","passive_sonar_bearing","last_sonar_contact",
                                       "active_sonar_enabled","last_active_ping"]
                        for t in torps:
                            if getattr(t, "_delete", False) or getattr(t, "_expired", False):
                                continue
                            trow = db.session.get(TorpedoModel, t.id)
                            if trow:
                                _apply_fields(trow, t, torp_fields)

                        db.session.commit()

                    except StaleDataError as se:
                        print("[GAME_LOOP] StaleDataError during commit:", se, flush=True)
                        db.session.rollback()
                    except Exception as e:
                        print("[GAME_LOOP] Commit error:", repr(e), flush=True)
                        db.session.rollback()
                _perf["db_commit_ms"] = _ms(t2)

                # 4) Fan-out (no lock)
                for uid, ev, obj in pending_events:
                    send_private(uid, ev, obj)
                now = time.time()
                for uid in list(USER_QUEUES.keys()):
                    if now - USER_LAST.get(uid, 0) > 1.0:
                        send_snapshot_mem(uid, subs, torps)
                        USER_LAST[uid] = now

            except Exception as e:
                print("[GAME_LOOP] ERROR:", repr(e), flush=True)
                try:
                    db.session.rollback()
                except Exception:
                    pass

            _perf["tick_ms"] = _ms(loop_start)
            sleep_left = TICK - (time.time() - loop_start)
            if sleep_left > 0:
                time.sleep(sleep_left)

# -------------------------- Routes --------------------------
@app.get('/')
def ui_home():
    return send_from_directory('.', 'ui.html')

@app.get('/leaderboard_ui')
def ui_leaderboard():
    return send_from_directory('.', 'leaderboard.html')

@app.get('/openapi.json')
def openapi_json():
    """Serve the OpenAPI / Swagger spec, organized by section and only for API endpoints."""
    # Metadata for each documented endpoint: (rule, method) -> config
    API_META = {
        # Auth
        ('/signup', 'POST'): {
            "tag": "Auth",
            "summary": "Create a new user and API key",
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "username": {"type": "string"},
                                "password": {"type": "string"}
                            },
                            "required": ["username", "password"]
                        }
                    }
                }
            }
        },
        ('/login', 'POST'): {
            "tag": "Auth",
            "summary": "Log in and get a new API key",
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "username": {"type": "string"},
                                "password": {"type": "string"}
                            },
                            "required": ["username", "password"]
                        }
                    }
                }
            }
        },
        # Public info
        ('/public', 'GET'): {
            "tag": "Public",
            "summary": "Get basic world info (ring & objectives)"
        },
        ('/rules', 'GET'): {
            "tag": "Public",
            "summary": "Get current game configuration"
        },
        ('/leaderboard', 'GET'): {
            "tag": "Public",
            "summary": "Get public leaderboard (top players by score)"
        },
        # Core player state
        ('/register_sub', 'POST'): {
            "tag": "Subs",
            "summary": "Spawn a new submarine for the current user",
            "auth": True
        },
        ('/state', 'GET'): {
            "tag": "Subs",
            "summary": "Get current state of your submarines and torpedoes",
            "auth": True
        },
        ('/control/<sub_id>', 'POST'): {
            "tag": "Subs",
            "summary": "Set throttle, rudder, planes, target depth for a submarine",
            "auth": True,
            "requestBody": {"required": True}
        },
        ('/snorkel/<sub_id>', 'POST'): {
            "tag": "Subs",
            "summary": "Toggle or set snorkel state for a submarine",
            "auth": True,
            "requestBody": {"required": False}
        },
        ('/emergency_blow/<sub_id>', 'POST'): {
            "tag": "Subs",
            "summary": "Trigger emergency blow for a submarine",
            "auth": True
        },
        ('/set_sub_heading/<sub_id>', 'POST'): {
            "tag": "Subs",
            "summary": "Set or clear target heading for a submarine",
            "auth": True,
            "requestBody": {"required": True}
        },
        ('/turn_sub/<sub_id>', 'POST'): {
            "tag": "Subs",
            "summary": "Turn a submarine relative to its current heading",
            "auth": True,
            "requestBody": {"required": True}
        },
        ('/set_passive_array/<sub_id>', 'POST'): {
            "tag": "Sonar",
            "summary": "Set passive sonar array direction for a submarine",
            "auth": True,
            "requestBody": {"required": True}
        },
        ('/ping/<sub_id>', 'POST'): {
            "tag": "Sonar",
            "summary": "Fire an active sonar ping from a submarine",
            "auth": True,
            "requestBody": {"required": True}
        },
        ('/weather_scan/<sub_id>', 'POST'): {
            "tag": "Sonar",
            "summary": "Scan for nearby weather/sonar clouds (random terrain) around the submarine",
            "auth": True,
            "requestBody": {"required": False}
        },
        ('/stream', 'GET'): {
            "tag": "Events",
            "summary": "Server-sent events stream of snapshots and contacts"
        },
        # Torpedoes
        ('/launch_torpedo/<sub_id>', 'POST'): {
            "tag": "Torpedoes",
            "summary": "Launch a torpedo from a submarine",
            "auth": True,
            "requestBody": {"required": True}
        },
        ('/reload_torpedoes/<sub_id>', 'POST'): {
            "tag": "Torpedoes",
            "summary": "Spend battery to reload torpedoes for a submarine",
            "auth": True,
            "requestBody": {"required": False}
        },
        ('/set_torp_speed/<torp_id>', 'POST'): {
            "tag": "Torpedoes",
            "summary": "Set target speed for a torpedo",
            "auth": True,
            "requestBody": {"required": True}
        },
        ('/torp_ping/<torp_id>', 'POST'): {
            "tag": "Torpedoes",
            "summary": "Perform a focused active ping from a torpedo",
            "auth": True,
            "requestBody": {"required": True}
        },
        ('/torp_ping_toggle/<torp_id>', 'POST'): {
            "tag": "Torpedoes",
            "summary": "Toggle auto-pinging for a torpedo",
            "auth": True
        },
        ('/set_torp_depth/<torp_id>', 'POST'): {
            "tag": "Torpedoes",
            "summary": "Set target depth for a torpedo",
            "auth": True,
            "requestBody": {"required": True}
        },
        ('/set_torp_heading/<torp_id>', 'POST'): {
            "tag": "Torpedoes",
            "summary": "Apply an immediate heading change to a torpedo",
            "auth": True,
            "requestBody": {"required": True}
        },
        ('/set_torp_target_heading/<torp_id>', 'POST'): {
            "tag": "Torpedoes",
            "summary": "Set auto-steering target heading for a torpedo",
            "auth": True,
            "requestBody": {"required": True}
        },
        ('/torp_passive_sonar_toggle/<torp_id>', 'POST'): {
            "tag": "Torpedoes",
            "summary": "Toggle passive sonar on/off for a wire-guided torpedo",
            "auth": True
        },
        ('/detonate/<torp_id>', 'POST'): {
            "tag": "Torpedoes",
            "summary": "Command-detonate a torpedo",
            "auth": True
        },
        # Admin / diagnostics
        ('/admin/state', 'GET'): {
            "tag": "Admin",
            "summary": "Get full server state (admin only)"
        },
        ('/perf', 'GET'): {
            "tag": "Admin",
            "summary": "Get game loop performance metrics"
        },
    }

    spec = copy.deepcopy(OPENAPI_BASE)
    paths = {}

    for rule in app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        methods = [m for m in rule.methods if m not in ('HEAD', 'OPTIONS')]
        if not methods:
            continue
        for method in methods:
            meta = API_META.get((rule.rule, method))
            if not meta:
                continue  # Skip non-API endpoints (UI, swagger, etc.)

            path = rule.rule.replace('<', '{').replace('>', '}')
            path_item = paths.setdefault(path, {})
            op = {
                "summary": meta.get("summary", f"{method} {path}"),
                "tags": [meta.get("tag", "Other")],
                "responses": {
                    "200": {"description": "OK"}
                }
            }

            # Path parameters
            params = []
            for arg in rule.arguments:
                params.append({
                    "in": "path",
                    "name": arg,
                    "required": True,
                    "schema": {"type": "string"}
                })

            # Auth header / security for protected routes
            if meta.get("auth"):
                op["security"] = [{"ApiKeyAuth": []}]
            if params:
                op["parameters"] = params

            # Basic requestBody so Swagger shows a JSON editor for applicable routes
            rb = meta.get("requestBody")
            if rb:
                if isinstance(rb, dict) and "content" in rb:
                    op["requestBody"] = rb
                else:
                    op["requestBody"] = {
                        "required": rb.get("required", False) if isinstance(rb, dict) else bool(rb),
                        "content": {
                            "application/json": {
                                "schema": {"type": "object"}
                            }
                        }
                    }

            path_item[method.lower()] = op

    spec["paths"] = paths
    return jsonify(spec)

@app.get('/swagger')
def swagger_ui():
    """Simple Swagger UI page to explore the API."""
    html = """
<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>AISubBrawl Swagger UI</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" />
    <style>
      body { margin:0; background:#020b16; }
      #swagger-ui { max-width: 1200px; margin: 0 auto; }
    </style>
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
      window.addEventListener('load', () => {
        SwaggerUIBundle({
          url: '/openapi.json',
          dom_id: '#swagger-ui',
        });
      });
    </script>
  </body>
</html>
"""
    return Response(html, mimetype="text/html")

@app.get('/public')
def public_info():
    return jsonify({"ring": WORLD_RING, "objectives": OBJECTIVES})

@app.post('/signup')
def signup():
    d = request.get_json(force=True)
    u = d.get('username','').strip(); p = d.get('password','')
    if not u or not p: return jsonify({"ok": False, "error": "username and password required"}), 400
    if User.query.filter_by(username=u).first():
        return jsonify({"ok": False, "error": "username taken"}), 400
    user = User(username=u, pw_hash=generate_password_hash(p))
    db.session.add(user); db.session.commit()
    key = make_key()
    db.session.add(ApiKey(key=key, user_id=user.id)); db.session.commit()
    return jsonify({"ok": True, "api_key": key})

@app.post('/login')
def login():
    d = request.get_json(force=True)
    u = d.get('username','').strip(); p = d.get('password','')
    user = User.query.filter_by(username=u).first()
    if not user or not check_password_hash(user.pw_hash, p):
        return jsonify({"ok": False, "error": "invalid credentials"}), 401
    key = make_key()
    db.session.add(ApiKey(key=key, user_id=user.id)); db.session.commit()
    return jsonify({"ok": True, "api_key": key})

@app.get('/rules')
def rules():
    return jsonify(GAME_CFG)

@app.get('/state')
@require_key
def state():
    with WORLD_LOCK:
        subs = SubModel.query.filter_by(owner_id=request.user.id).all()
        torps = TorpedoModel.query.filter_by(owner_id=request.user.id).all()
    return jsonify({
        "ok": True,
        "time": time.time(),
        "subs": [_sub_pub(s) for s in subs],
        "torpedoes": [_torp_pub(t) for t in torps]
    })

@app.post('/register_sub')
@require_key
def register_sub():
    with WORLD_LOCK:
        # Check submarine limit per user
        max_subs_per_user = GAME_CFG.get("sub", {}).get("max_per_user", DEFAULT_CFG["sub"].get("max_per_user", 2))
        current_subs = SubModel.query.filter_by(owner_id=request.user.id).count()

        # Enforce per-slot respawn cooldown ONLY while the user still has at least
        # one active submarine. If both submarines are dead (current_subs == 0),
        # cooldowns are ignored so the player can fully respawn.
        cooldown_s = float(GAME_CFG.get("sub", {}).get("respawn_cooldown_s", 7200.0) or 0.0)
        available_slots = max_subs_per_user
        now_ts = time.time()
        try:
            urow = db.session.get(User, request.user.id)
        except Exception:
            urow = None

        if current_subs > 0 and cooldown_s > 0.0 and urow:
            active_cooldowns = []
            for ts in (getattr(urow, "last_death_ts", 0.0), getattr(urow, "prev_death_ts", 0.0)):
                if ts and now_ts - float(ts or 0.0) < cooldown_s:
                    active_cooldowns.append(now_ts - float(ts or 0.0))
            available_slots = max_subs_per_user - len(active_cooldowns)
            if available_slots < 0:
                available_slots = 0

            if current_subs >= available_slots:
                remaining_s = 0
                # Earliest cooldown expiry among active slots
                rems = []
                for ts in (getattr(urow, "last_death_ts", 0.0), getattr(urow, "prev_death_ts", 0.0)):
                    if ts:
                        age = now_ts - float(ts or 0.0)
                        if age < cooldown_s:
                            rems.append(cooldown_s - age)
                if rems:
                    remaining_s = int(min(rems))
                return jsonify({
                    "ok": False,
                    "error": "respawn cooldown active or slot limit reached",
                    "cooldown_remaining_s": remaining_s,
                    "active_subs": current_subs,
                    "available_slots": available_slots
                }), 400
        
        x, y = random_spawn_pos()
        bcfg = GAME_CFG.get("sub", {}).get("battery", DEFAULT_CFG["sub"]["battery"])
        bmin = bcfg.get("initial_min", DEFAULT_CFG["sub"]["battery"]["initial_min"])
        bmax = bcfg.get("initial_max", DEFAULT_CFG["sub"]["battery"]["initial_max"])
        torp_cfg = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])
        mag_size = int(torp_cfg.get("magazine_size", DEFAULT_CFG["torpedo"]["magazine_size"]))

        s = SubModel(
            owner_id=request.user.id,
            x=x, y=y,
            depth=random.uniform(80.0, 180.0),
            heading=random.uniform(-math.pi, math.pi),
            pitch=0.0,
            rudder_angle=0.0,
            rudder_cmd=0.0,
            planes=0.0,
            throttle=0.2,
            speed=0.0,
            battery=random.uniform(bmin, bmax),
            is_snorkeling=False,
            blow_active=False,
            blow_charge=1.0,
            health=100.0,
            passive_dir=0.0,
            torpedo_ammo=mag_size
        )
        db.session.add(s); db.session.commit()
        return jsonify({"ok": True, "sub_id": s.id, "spawn": [s.x, s.y, s.depth]})

@app.post('/control/<sub_id>')
@require_key
def control(sub_id):
    d = request.get_json(force=True) or {}
    MAX_RUDDER_DEG = GAME_CFG.get("sub", {}).get("max_rudder_deg", 30.0)

    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404

        if "target_depth" in d:
            td = d["target_depth"]
            s.target_depth = None if td is None else float(td)

        if "throttle" in d:
            s.throttle = clamp(float(d["throttle"]), 0.0, 1.0)

        if "planes" in d:
            s.planes = clamp(float(d["planes"]), -1.0, 1.0)

        if "rudder_deg" in d:
            rdeg = clamp(float(d["rudder_deg"]), -MAX_RUDDER_DEG, MAX_RUDDER_DEG)
            s.rudder_cmd = rdeg / MAX_RUDDER_DEG

        if "rudder_nudge_deg" in d:
            nudge = float(d["rudder_nudge_deg"])
            curr_deg = (s.rudder_cmd or 0.0) * MAX_RUDDER_DEG
            new_deg = clamp(curr_deg + nudge, -MAX_RUDDER_DEG, MAX_RUDDER_DEG)
            s.rudder_cmd = new_deg / MAX_RUDDER_DEG

        db.session.commit()
        return jsonify({"ok": True})

# --- UPDATED: snorkel route with toggle + depth enforcement ---
@app.post('/snorkel/<sub_id>')
@require_key
def snorkel(sub_id):
    d = (request.get_json(silent=True) or {})
    scfg = GAME_CFG.get("sub", DEFAULT_CFG["sub"])
    snorkel_depth = scfg.get("snorkel_depth", 15.0)

    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404

        # Toggle if no explicit "on" passed, or toggle:true provided
        do_toggle = bool(d.get("toggle", False)) or ("on" not in d)

        if do_toggle:
            if not s.is_snorkeling:
                if s.depth > snorkel_depth:
                    return jsonify({"ok": False, "error": "too deep to snorkel"}), 400
                s.is_snorkeling = True
            else:
                s.is_snorkeling = False
        else:
            want_on = bool(d.get("on", True))
            if want_on:
                if s.depth > snorkel_depth:
                    return jsonify({"ok": False, "error": "too deep to snorkel"}), 400
                s.is_snorkeling = True
            else:
                s.is_snorkeling = False

        db.session.commit()
        return jsonify({"ok": True, "is_snorkeling": s.is_snorkeling, "depth": s.depth, "limit": snorkel_depth})

@app.post('/emergency_blow/<sub_id>')
@require_key
def emergency_blow(sub_id):
    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404
        if s.blow_charge <= 0.0:
            return jsonify({"ok": False, "error": "no charge"}), 400
        s.blow_active = True
        s.blow_end = time.time() + GAME_CFG.get("sub", DEFAULT_CFG["sub"])["emergency_blow"]["duration_s"]
        db.session.commit()
        return jsonify({"ok": True})

@app.post('/launch_torpedo/<sub_id>')
@require_key
def launch_torpedo(sub_id):
    d = request.get_json(force=True)
    torpedo_range = float(d.get('range', 1000.0))  # Default 1000m range

    # Enforce max range limit
    torpedo_cfg = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])
    max_range = torpedo_cfg.get("max_range", 6000.0)
    torpedo_range = min(torpedo_range, max_range)
    
    # Legacy per-shot battery cost is now disabled in favor of an
    # ammo-limited, energy-to-reload mechanic. We keep the variables here
    # in case configs still define them, but we do not deduct battery
    # when firing.
    cost_per_100m = torpedo_cfg.get("battery_cost_per_100m", 0.0)
    battery_cost = 0.0

    NOSE_OFFSET = 12.0

    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "sub not found"}), 404
        # Torpedo ammo check: limited magazine per sub.
        torp_cfg = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])
        mag_size = int(torp_cfg.get("magazine_size", DEFAULT_CFG["torpedo"]["magazine_size"]))
        current_ammo = int(getattr(s, "torpedo_ammo", mag_size))
        if current_ammo <= 0:
            return jsonify({"ok": False, "error": "no torpedoes loaded"}), 400
        # Consume one torpedo from the magazine.
        s.torpedo_ammo = current_ammo - 1

        cosH, sinH = math.cos(s.heading), math.sin(s.heading)

        spawn_x = s.x + cosH * NOSE_OFFSET
        spawn_y = s.y + sinH * NOSE_OFFSET
        spawn_depth = s.depth

        t = TorpedoModel(
            owner_id=request.user.id,
            parent_sub=s.id,
            x=spawn_x, y=spawn_y, depth=spawn_depth,
            heading=s.heading,
            speed=GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["speed"],
            control_mode='wire',
            wire_length=torpedo_range  # stores max range instead of wire length
        )
        t.created_at = time.time()
        db.session.add(t)
        db.session.commit()

        return jsonify({
            "ok": True,
            "torpedo_id": t.id,
            "range": torpedo_range,
            "battery_cost": round(battery_cost, 1),
            "spawn": {"x": spawn_x, "y": spawn_y, "depth": spawn_depth},
            "torpedo_ammo": s.torpedo_ammo,
            "magazine_size": mag_size
        })

@app.post('/reload_torpedoes/<sub_id>')
@require_key
def reload_torpedoes(sub_id):
    """
    Reload torpedoes into the submarine's magazine by spending battery energy.

    Request JSON (all optional):
      - count: how many torpedoes to reload (defaults to fill to full magazine)

    Behavior:
      - Each torpedo reloaded costs `reload_battery_cost_per_torp` battery.
      - Reload is clamped so you can never exceed the magazine size.
    """
    d = request.get_json(silent=True) or {}
    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "sub not found"}), 404

        torp_cfg = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])
        mag_size = int(torp_cfg.get("magazine_size", DEFAULT_CFG["torpedo"]["magazine_size"]))
        reload_cost_per = float(torp_cfg.get("reload_battery_cost_per_torp", DEFAULT_CFG["torpedo"]["reload_battery_cost_per_torp"]))

        current_ammo = int(getattr(s, "torpedo_ammo", mag_size))
        missing = max(0, mag_size - current_ammo)
        if missing <= 0:
            return jsonify({"ok": False, "error": "magazine already full", "torpedo_ammo": current_ammo, "magazine_size": mag_size}), 400

        requested = d.get("count")
        if requested is None:
            to_reload = missing
        else:
            try:
                to_reload = int(requested)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "invalid count"}), 400
            if to_reload <= 0:
                return jsonify({"ok": False, "error": "count must be positive"}), 400
            to_reload = min(to_reload, missing)

        battery_cost = reload_cost_per * to_reload
        if s.battery < battery_cost:
            return jsonify({"ok": False, "error": "not enough battery", "required": battery_cost, "battery": s.battery}), 400

        s.battery = clamp(s.battery - battery_cost, 0.0, 100.0)
        s.torpedo_ammo = current_ammo + to_reload
        db.session.commit()

        return jsonify({
            "ok": True,
            "reloaded": to_reload,
            "battery_cost": round(battery_cost, 1),
            "battery_remaining": round(s.battery, 1),
            "torpedo_ammo": s.torpedo_ammo,
            "magazine_size": mag_size
        })

@app.post('/set_torp_speed/<torp_id>')
@require_key
def set_torp_speed(torp_id):
    data = request.get_json() or {}
    new_speed = float(data.get("speed", 18.0))
    cfg = GAME_CFG["torpedo"]
    min_speed = float(cfg.get("min_speed", 8.0))
    max_speed = float(cfg.get("max_speed", 18.0))
    new_speed = clamp(new_speed, min_speed, max_speed)
    
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t:
            return jsonify({"ok": False, "error": "torpedo not found"}), 404
        if t.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not your torpedo"}), 403
        t.target_speed = new_speed
        db.session.commit()
        return jsonify({"ok": True, "speed": t.speed, "target_speed": t.target_speed})

@app.post('/torp_ping/<torp_id>')
@require_key
def torp_ping(torp_id):
    d = request.get_json(force=True)
    beam = 30.0  # Fixed 30-degree beam angle
    max_r_req = float(d.get('max_range', 800.0))
    
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t or t.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404
            
        # Safe config access with defaults
        cfg_torp = GAME_CFG.get("torpedo", {})
        cfg_active = (cfg_torp
                               .get("sonar", {})
                               .get("active", {"max_range": 1500.0, "rng_sigma_m": 40.0, "max_angle": 60.0}))
        max_r = min(max_r_req, cfg_active.get("max_range", 1500.0))
        beam = min(beam, cfg_active.get("max_angle", 60.0))
        rng_sigma = cfg_active.get("rng_sigma_m", 40.0)

        # Battery cost for manual active ping
        bcfg = cfg_torp.get("battery", DEFAULT_CFG["torpedo"].get("battery", {}))
        ping_cost = float(bcfg.get("active_ping_cost", 2.0) or 0.0)
        min_for_ping = float(bcfg.get("min_for_ping", 5.0) or 0.0)
        if getattr(t, "battery", None) is None:
            t.battery = float(bcfg.get("capacity", 100.0) or 100.0)
        if t.battery < max(ping_cost, min_for_ping):
            return jsonify({"ok": False, "error": "torpedo battery too low"}), 400
        
        contacts = []
        print(f"[DEBUG] Torpedo ping: torp_id={torp_id[:6]}, pos=({t.x:.1f},{t.y:.1f},{t.depth:.1f}), heading={math.degrees(t.heading):.1f}°, max_r={max_r}, beam={beam}°")
        
        for s in SubModel.query.all():
            if s.owner_id == t.owner_id:
                print(f"[DEBUG] Skipping own submarine: {s.id[:6]}")
                continue
            dx = s.x - t.x; dy = s.y - t.y
            rng = math.sqrt(dx*dx + dy*dy + (s.depth - t.depth)**2)
            brg = math.atan2(dy, dx)
            rel = abs(wrap_angle(brg - t.heading))
            rel_deg = math.degrees(rel)
            beam_half = beam / 2.0
            
            print(f"[DEBUG] Sub {s.id[:6]}: pos=({s.x:.1f},{s.y:.1f},{s.depth:.1f}), dist={rng:.1f}m, bearing={math.degrees(brg):.1f}°, rel={rel_deg:.1f}°, beam_half={beam_half:.1f}°")
            
            if rng > max_r:
                print(f"[DEBUG] Sub {s.id[:6]}: OUT OF RANGE ({rng:.1f}m > {max_r}m)")
                continue
            if rel > math.radians(beam_half):
                print(f"[DEBUG] Sub {s.id[:6]}: OUT OF BEAM ({rel_deg:.1f}° > {beam_half:.1f}°)")
                continue
                
            print(f"[DEBUG] Sub {s.id[:6]}: CONTACT DETECTED!")
            contacts.append({
                "bearing": brg,
                "range": rng + random.uniform(-rng_sigma, rng_sigma),
                "depth": s.depth + random.uniform(-20, 20)
            })
        
        print(f"[DEBUG] Torpedo ping result: {len(contacts)} contacts")

        # Deduct battery for ping
        t.battery = max(0.0, float(t.battery) - ping_cost)
        db.session.commit()
        
        return jsonify({"ok": True, "contacts": contacts, "battery_remaining": round(t.battery, 1)})

@app.post('/call_fueler/<sub_id>')
@require_key
def call_fueler(sub_id):
    """
    Request a fueler for a submarine. The fueler spawns 500-1000m away from the
    sub on the surface and is visible to all players.
    """
    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "sub not found"}), 404

        # Enforce one active fueler per account
        existing = FuelerModel.query.filter_by(owner_id=request.user.id).first()
        if existing is not None:
            return jsonify({"ok": False, "error": "you already have an active fueler"}), 400

        # Spawn a new fueler near this submarine
        f = _spawn_fueler_near_sub(s)
        f.owner_id = request.user.id
        db.session.commit()

        return jsonify({
            "ok": True,
            "fueler": _fueler_pub(f)
        })

@app.post('/start_refuel/<sub_id>')
@require_key
def start_refuel(sub_id):
    """
    Begin server-side controlled refueling for a submarine.
    The server will:
      - bind the sub to the nearest fueler within 50m,
      - enable snorkel,
      - drive depth toward snorkel depth,
      - and freeze propulsion while refueling is active.
    """
    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "sub not found"}), 404

        fuelers = FuelerModel.query.all()
        if not fuelers:
            return jsonify({"ok": False, "error": "no fuelers available"}), 400

        # Find nearest fueler with fuel
        nearest = None
        nearest_d = None
        for f in fuelers:
            if getattr(f, "fuel", 0.0) <= 0.0:
                continue
            d = distance3d(s.x, s.y, s.depth, f.x, f.y, f.depth)
            if nearest_d is None or d < nearest_d:
                nearest = f
                nearest_d = d

        if not nearest or nearest_d is None or nearest_d > 50.0:
            return jsonify({"ok": False, "error": "need to be within 50m of a fueler"}), 400

        scfg = GAME_CFG.get("sub", DEFAULT_CFG["sub"])
        snorkel_depth = scfg.get("snorkel_depth", 15.0)

        # Arm refueling state
        s.refuel_active = True
        s.refuel_fueler_id = nearest.id
        s.refuel_timer = 0.0
        # Force snorkel on and drive to snorkel depth; propulsion will be clamped in update_sub
        s.is_snorkeling = True
        s.target_depth = snorkel_depth

        db.session.commit()

        return jsonify({"ok": True, "bound_fueler": _fueler_pub(nearest)})

@app.post('/torp_ping_toggle/<torp_id>')
@require_key
def torp_ping_toggle(torp_id):
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t or t.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404
        
        t.active_sonar_enabled = not getattr(t, 'active_sonar_enabled', False)
        db.session.commit()
        return jsonify({"ok": True, "active_sonar_enabled": t.active_sonar_enabled})

@app.post('/set_torp_depth/<torp_id>')
@require_key
def set_torp_depth(torp_id):
    data = request.get_json() or {}
    new_depth = float(data.get("depth", 0))
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t:
            return jsonify({"ok": False, "error": "torpedo not found"}), 404
        if t.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not your torpedo"}), 403
        t.target_depth = new_depth
        db.session.commit()
        return jsonify({"ok": True, "depth": t.depth, "target_depth": t.target_depth})

@app.post('/set_torp_heading/<torp_id>')
@require_key
def set_torp_heading(torp_id):
    d = request.get_json(force=True)
    dt = float(d.get('dt', TICK))
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t or t.owner_id != request.user.id:
            return jsonify({'ok': False, 'error': 'not found'}), 404
        if t.control_mode != 'wire':
            return jsonify({'ok': False, 'error': 'wire lost'}), 400
        max_turn = math.radians(GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["turn_rate_deg_s"]) * dt
        if 'heading_deg' in d:
            desired = math.radians(float(d['heading_deg']))
            err = wrap_angle(desired - t.heading)
            t.heading = wrap_angle(t.heading + clamp(err, -max_turn, max_turn))
        elif 'turn_deg' in d:
            turn = math.radians(float(d['turn_deg']))
            t.heading = wrap_angle(t.heading + clamp(turn, -max_turn, max_turn))
        else:
            return jsonify({'ok': False, 'error': 'heading_deg or turn_deg required'}), 400
        t.updated_at = time.time()
        db.session.commit()
        return jsonify({'ok': True, 'torpedo': dict(id=t.id, heading=t.heading)})

@app.post('/set_torp_target_heading/<torp_id>')
@require_key
def set_torp_target_heading(torp_id):
    d = request.get_json(force=True)
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t or t.owner_id != request.user.id:
            return jsonify({'ok': False, 'error': 'not found'}), 404
        if t.control_mode != 'wire':
            return jsonify({'ok': False, 'error': 'wire lost'}), 400
        
        if 'heading_deg' in d:
            if d['heading_deg'] is None:
                t.target_heading = None
            else:
                # Convert from compass degrees (0°=North, 90°=East) to server radians (0=east, CCW+)
                compass_deg = float(d['heading_deg'])
                server_deg = (90 - compass_deg) % 360
                desired = math.radians(server_deg)
                t.target_heading = desired
        else:
            return jsonify({'ok': False, 'error': 'heading_deg required'}), 400
        
        t.updated_at = time.time()
        db.session.commit()
        # Convert back to compass degrees for response
        compass_heading = (t.heading * 180 / math.pi + 90) % 360
        compass_target = (t.target_heading * 180 / math.pi + 90) % 360 if t.target_heading else None
        return jsonify({'ok': True, 'torpedo': dict(
            id=t.id, 
            heading=t.heading, 
            target_heading=t.target_heading,
            compass_heading=compass_heading,
            compass_target=compass_target
        )})

@app.post('/torp_passive_sonar_toggle/<torp_id>')
@require_key
def torp_passive_sonar_toggle(torp_id):
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t or t.owner_id != request.user.id:
            return jsonify({'ok': False, 'error': 'not found'}), 404
        if t.control_mode != 'wire':
            return jsonify({'ok': False, 'error': 'wire lost'}), 400
        
        t.passive_sonar_active = not t.passive_sonar_active
        t.updated_at = time.time()
        db.session.commit()
        return jsonify({'ok': True, 'passive_sonar_active': t.passive_sonar_active})


@app.post('/set_sub_heading/<sub_id>')
@require_key
def set_sub_heading(sub_id):
    d = request.get_json(force=True)
    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({'ok': False, 'error': 'not found'}), 404
        
        if 'heading_deg' in d:
            if d['heading_deg'] is None:
                s.target_heading = None
            else:
                # Convert from compass degrees (0°=North, 90°=East) to server radians (0=east, CCW+)
                compass_deg = float(d['heading_deg'])
                # Convert: North(0°) -> East(90°) -> South(180°) -> West(270°)
                # Server: East(0°) -> North(90°) -> West(180°) -> South(270°)
                # Formula: server_deg = (90 - compass_deg) % 360
                server_deg = (90 - compass_deg) % 360
                desired = math.radians(server_deg)
                s.target_heading = desired
                print(f"[DEBUG] Heading conversion: UI {compass_deg}° -> Server {server_deg}° ({desired:.3f} rad)")
        else:
            return jsonify({'ok': False, 'error': 'heading_deg required'}), 400
        
        s.updated_at = time.time()
        db.session.commit()
        # Convert back to compass degrees for response
        compass_heading = (s.heading * 180 / math.pi + 90) % 360
        compass_target = (s.target_heading * 180 / math.pi + 90) % 360 if s.target_heading else None
        return jsonify({'ok': True, 'submarine': dict(
            id=s.id, 
            heading=s.heading, 
            target_heading=s.target_heading,
            compass_heading=compass_heading,
            compass_target=compass_target
        )})

@app.post('/turn_sub/<sub_id>')
@require_key
def turn_sub(sub_id):
    d = request.get_json(force=True)
    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({'ok': False, 'error': 'not found'}), 404
        
        if 'turn_deg' not in d:
            return jsonify({'ok': False, 'error': 'turn_deg required'}), 400
        
        turn_deg = float(d['turn_deg'])
        # Convert turn degrees to radians and add to current heading
        turn_rad = math.radians(turn_deg)
        new_heading = wrap_angle(s.heading + turn_rad)
        
        # Convert to compass degrees for target heading
        compass_deg = (new_heading * 180 / math.pi + 90) % 360
        server_deg = (90 - compass_deg) % 360
        s.target_heading = math.radians(server_deg)
        
        s.updated_at = time.time()
        db.session.commit()
        
        print(f"[DEBUG] Turn: {turn_deg}° -> New heading: {compass_deg:.1f}° compass")
        
        # Convert back to compass degrees for response
        compass_heading = (s.heading * 180 / math.pi + 90) % 360
        compass_target = (s.target_heading * 180 / math.pi + 90) % 360 if s.target_heading else None
        return jsonify({'ok': True, 'submarine': dict(
            id=s.id, 
            heading=s.heading, 
            target_heading=s.target_heading,
            compass_heading=compass_heading,
            compass_target=compass_target,
            turn_applied=turn_deg
        )})

@app.post('/set_passive_array/<sub_id>')
@require_key
def set_passive_array(sub_id):
    d = request.get_json(force=True)
    deg = float(d.get('dir_deg', 0.0))
    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404
        s.passive_dir = math.radians(deg)
        db.session.commit()
        return jsonify({"ok": True})

@app.post('/ping/<sub_id>')
@require_key
def ping(sub_id):
    d = request.get_json(force=True)
    beam = float(d.get('beamwidth_deg', 20.0))
    max_r = float(d.get('max_range', GAME_CFG.get("sonar", {}).get("active", DEFAULT_CFG["sonar"]["active"])["max_range"]))
    center_rel_deg = d.get('center_bearing_deg', 0.0)

    # Enforce angle limit
    active_cfg = GAME_CFG.get("sonar", {}).get("active", DEFAULT_CFG["sonar"]["active"])
    max_angle = active_cfg.get("max_angle", 210.0)
    beam = min(beam, max_angle)

    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404

        # Battery cost calculation based on angle and range
        power_cfg = GAME_CFG.get("sonar", {}).get("active_power", DEFAULT_CFG["sonar"]["active_power"])
        base_cost = power_cfg.get("base_cost", DEFAULT_CFG["sonar"]["active_power"]["base_cost"])
        angle_cost = beam * power_cfg.get("cost_per_degree", DEFAULT_CFG["sonar"]["active_power"]["cost_per_degree"])  # <-- 0.04 default
        range_cost = (max_r / 100.0) * power_cfg.get("cost_per_100m_range", DEFAULT_CFG["sonar"]["active_power"]["cost_per_100m_range"])
        cost = base_cost + angle_cost + range_cost
        
        if s.battery < power_cfg.get("min_battery", DEFAULT_CFG["sonar"]["active_power"]["min_battery"]):
            return jsonify({"ok": False, "error": "battery too low"}), 400
        if s.battery < cost:
            return jsonify({"ok": False, "error": "not enough battery"}), 400

        s.battery = clamp(s.battery - cost, 0.0, 100.0)

        if hasattr(s, 'ping_cooldown') and s.ping_cooldown > time.time():
            return jsonify({"ok": False, "error": "ping recharging"}), 400
        s.ping_cooldown = time.time() + 5.0

        center_world = wrap_angle(s.heading + math.radians(float(center_rel_deg)))
        schedule_active_ping(s, beam, max_r, time.time(), center_world=center_world)

        # Notify others - make pings much more detectable
        others = SubModel.query.all()
        for other in others:
            if other.id == s.id:
                continue
            dist = distance(s.x, s.y, other.x, other.y)
            # Much stronger base signal + beam width + range factors
            snr = 15.0 * (beam / 90.0) + (max_r / 1000.0) * 3.0 - (dist / 600.0)
            if snr > 1.0:
                send_private(other.owner_id, 'contact', {
                    "type": "active_ping_detected",
                    "observer_sub_id": other.id,
                    "bearing": math.atan2(s.y - other.y, s.x - other.x),
                    "snr": snr,
                    "time": time.time()
                })

        db.session.commit()
        return jsonify({
            "ok": True,
            "battery_cost": round(cost, 2),
            "cost_breakdown": {
                "base": round(base_cost, 2),
                "angle": round(angle_cost, 2),
                "range": round(range_cost, 2)
            },
            "battery_remaining": round(s.battery, 2),
            "beam_deg": beam,
            "max_range": max_r
        })

@app.post('/weather_scan/<sub_id>')
@require_key
def weather_scan(sub_id):
    """
    Scan for nearby weather/sonar clouds around a submarine.

    This "device" helps detect random terrain (clouds) outside the ring.
    It returns approximate bearings and ranges to any clouds within scanner range.
    """
    cfg = WORLD_WEATHER.get("scanner", {})
    max_range = float(cfg.get("max_range_m", 500.0))
    battery_cost = float(cfg.get("battery_cost", 1.0))
    rng_sigma = float(cfg.get("rng_sigma_m", 40.0))
    brg_sigma_deg = float(cfg.get("brg_sigma_deg", 5.0))
    noise_duration = float(cfg.get("noise_duration_s", 8.0))

    with WORLD_LOCK:
        s = SubModel.query.get(sub_id)
        if not s or s.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not found"}), 404

        if s.battery < battery_cost:
            return jsonify({"ok": False, "error": "not enough battery"}), 400

        s.battery = clamp(s.battery - battery_cost, 0.0, 100.0)
        # Mark this sub as acoustically noisy for a short period
        now = time.time()
        s.scanner_noise_until = max(float(getattr(s, "scanner_noise_until", 0.0) or 0.0),
                                    now + noise_duration)

        # For realism, we don't "see past" clouds: for any given bearing sector,
        # only the nearest cloud edge is reported (others behind it are occluded).
        sector_size_rad = math.radians(10.0)  # 10° sectors
        best_by_sector = {}

        for c in WEATHER_CLOUDS:
            # Only consider clouds whose depth band overlaps the sub (with small margin)
            if s.depth < c["min_depth"] - 50.0 or s.depth > c["max_depth"] + 50.0:
                continue
            # Geometric distance to center and radius
            center_dist = distance(s.x, s.y, c["x"], c["y"])
            edge_dist = max(0.0, center_dist - c["radius"])  # distance to nearest edge along line to center
            # Allow detecting clouds whose edge (not just center) lies within scanner range
            if edge_dist > max_range:
                continue

            true_brg = math.atan2(c["y"] - s.y, c["x"] - s.x)
            sector_key = int(math.floor(true_brg / sector_size_rad))
            prev = best_by_sector.get(sector_key)
            if (prev is None) or (edge_dist < prev["edge_dist"]):
                best_by_sector[sector_key] = {
                    "cloud": c,
                    "true_bearing": true_brg,
                    "edge_dist": edge_dist
                }

        detections = []
        for info in best_by_sector.values():
            c = info["cloud"]
            edge_dist = info["edge_dist"]
            true_brg = info["true_bearing"]
            noisy_rng = max(0.0, edge_dist + random.uniform(-rng_sigma, rng_sigma))
            noisy_brg = wrap_angle(true_brg + math.radians(random.uniform(-brg_sigma_deg, brg_sigma_deg)))

            detections.append({
                "bearing": noisy_brg,
                "bearing_deg": (90 - noisy_brg * 180 / math.pi) % 360,
                "range": noisy_rng,
                "approx_radius": c["radius"],
                "depth_band": [c["min_depth"], c["max_depth"]]
            })

        db.session.commit()

        return jsonify({
            "ok": True,
            "battery_cost": round(battery_cost, 2),
            "battery_remaining": round(s.battery, 2),
            "max_range": max_range,
            "clouds": detections
        })

@app.get('/stream')
def stream():
    user = get_user_from_api()
    if not user:
        return Response("unauthorized\n", status=401)
    q = _uq(user.id)

    @stream_with_context
    def gen():
        with app.app_context():
            try:
                yield ":" + (" " * 2048) + "\n"
                yield "retry: 2000\n"
                yield "event: hello\ndata: {}\n\n"
                send_snapshot(user.id)
                while True:
                    try:
                        chunk = q.get(timeout=15.0)
                        yield chunk
                    except queue.Empty:
                        now = time.time()
                        yield f"event: ping\ndata: {{\"t\": {now} }}\n\n"
            except GeneratorExit:
                return
            except Exception as e:
                try:
                    yield f"event: error\ndata: {{\"msg\": \"{str(e)}\"}}\n\n"
                except Exception:
                    pass

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(gen(), headers=headers)

@app.get('/admin/state')
@require_key
def admin_state():
    if not request.user.is_admin:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    with WORLD_LOCK:
        subs = [dict(
            id=s.id,x=s.x,y=s.y,depth=s.depth,heading=s.heading,pitch=s.pitch,
            rudder_angle=s.rudder_angle,rudder_cmd=s.rudder_cmd,planes=s.planes,
            speed=s.speed,battery=s.battery,is_snorkeling=s.is_snorkeling,
            blow_active=s.blow_active,blow_charge=s.blow_charge,health=s.health,
            target_depth=s.target_depth, target_heading=s.target_heading, throttle=s.throttle,
            torpedo_ammo=getattr(s, "torpedo_ammo", None),
            score=getattr(s, "score", 0.0),
            kills=getattr(s, "kills", 0),
            owner_id=s.owner_id
        ) for s in SubModel.query.all()]
        torps = [dict(
            id=t.id,x=t.x,y=t.y,depth=t.depth,heading=t.heading,speed=t.speed,
            mode=t.control_mode,range=t.wire_length,owner_id=t.owner_id,parent_sub=t.parent_sub
        ) for t in TorpedoModel.query.all()]
    return jsonify({"ok": True, "subs": subs, "torpedoes": torps})

@app.get('/perf')
def perf():
    return jsonify(dict(ok=True, **_perf, queues=len(USER_QUEUES)))

@app.get('/leaderboard')
def leaderboard():
    """
    Public leaderboard that everyone can see.
    Aggregates score and kills per user across all their submarines.
    """
    with WORLD_LOCK:
        users = {u.id: u.username for u in User.query.all()}
        subs = SubModel.query.all()
    per_user = {}
    for s in subs:
        uid = s.owner_id
        uentry = per_user.setdefault(uid, {
            "user_id": uid,
            "username": users.get(uid, f"user:{uid}"),
            "score": 0.0,
            "kills": 0,
            "subs": 0
        })
        uentry["score"] += float(getattr(s, "score", 0.0) or 0.0)
        uentry["kills"] += int(getattr(s, "kills", 0) or 0)
        uentry["subs"] += 1
    rows = sorted(per_user.values(), key=lambda r: (-r["score"], -r["kills"]))
    # Add rank
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return jsonify({"ok": True, "leaders": rows[:50]})

# -------------------------- Admin & boot (Flask 3.x safe) --------------------------
def ensure_admin():
    admin_user = os.environ.get('SB_ADMIN_USER')
    admin_pass = os.environ.get('SB_ADMIN_PASS')
    if not admin_user or not admin_pass:
        return
    u = User.query.filter_by(username=admin_user).first()
    if not u:
        u = User(username=admin_user, pw_hash=generate_password_hash(admin_pass), is_admin=True)
        db.session.add(u); db.session.commit()
        k = make_key(); db.session.add(ApiKey(key=k, user_id=u.id)); db.session.commit()
        print(f"[ADMIN] created @{admin_user}  API={k}")
    elif not u.is_admin:
        u.is_admin = True; db.session.commit()

from threading import Lock
_loop_started = False
_loop_lock = Lock()

def _start_loop_once():
    global _loop_started
    if _loop_started: return
    with _loop_lock:
        if _loop_started: return
        with app.app_context():
            ensure_admin()
        th = threading.Thread(target=game_loop, daemon=True)
        th.start()
        _loop_started = True
        print("[GAME] loop started")

@app.before_request
def _ensure_loop():
    if not _loop_started:
        _start_loop_once()

@app.post('/detonate/<torp_id>')
@require_key
def detonate_torp(torp_id):
    with WORLD_LOCK:
        t = TorpedoModel.query.get(torp_id)
        if not t:
            return jsonify({"ok": False, "error": "torpedo not found"}), 404
        if t.owner_id != request.user.id:
            return jsonify({"ok": False, "error": "not allowed"}), 403
        blast = GAME_CFG.get("torpedo", DEFAULT_CFG["torpedo"])["blast_radius"]
        affected = []
        for s in SubModel.query.all():
            horiz = distance(t.x, t.y, s.x, s.y)
            vert = abs(t.depth - s.depth)
            d = math.sqrt(horiz*horiz + vert*vert)
            if d <= blast:
                # Graduated damage based on distance
                if d <= 60.0:      damage = 100.0
                elif d <= 80.0:    damage = 75.0
                elif d <= 100.0:   damage = 50.0
                else:              damage = 25.0
                
                s.health = max(0.0, s.health - damage)
                affected.append(s)
                send_private(s.owner_id, 'explosion', {
                    "time": time.time(),
                    "at": [t.x, t.y, t.depth],
                    "torpedo_id": t.id,
                    "blast_radius": blast,
                    "damage": damage,
                    "distance": d
                })
        db.session.delete(t)
        db.session.commit()
        return jsonify({"ok": True, "affected": len(affected)})

if __name__ == '__main__':
    with app.app_context():
        ensure_admin()
    _start_loop_once()  # <-- single source of truth; prevents double-start
    app.run(host='0.0.0.0', port=5000, threaded=True)
