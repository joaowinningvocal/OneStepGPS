import math
import os
import json
import base64
import threading
import time
import requests
import urllib.parse
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "devverse_secret_CHANGE_ME")
CORS(app)

# ─── DATABASE CONFIG ───────────────────────────────────────────────────────────
os.makedirs('/data', exist_ok=True)
UPLOAD_FOLDER = '/data/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
database_url = os.environ.get('DATABASE_URL', 'sqlite:////data/clublifter.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ─── SETTINGS ─────────────────────────────────────────────────────────────────
API_KEY      = os.environ.get("ONESTEPGPS_API_KEY", "")
URL_API      = "https://track.onestepgps.com/v3/api/public/marker"
MAKE_WEBHOOK = os.environ.get("MAKE_WEBHOOK_URL", "https://hook.us1.make.com/ur1qljbumjhfa1meu7rh0hjb25a9hxj2")
# Public base URL of this app (for building absolute image URLs for Twilio MMS)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://www.clublifter.com").rstrip("/")

# ─── TWILIO (direct SMS/MMS, no Make.com in the middle) ───────────────────────
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
# Use EITHER a plain sender number OR a Messaging Service SID (recommended)
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
TWILIO_MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "").strip()
# Master switch — set SMS_ENABLED=false to mute all outgoing SMS (useful for testing)
SMS_ENABLED = os.environ.get("SMS_ENABLED", "true").strip().lower() not in ("false", "0", "no")

# ─── SHOPIFY ──────────────────────────────────────────────────────────────────
SHOPIFY_STORE   = os.environ.get("SHOPIFY_STORE", "vip-packages.myshopify.com")
SHOPIFY_TOKEN   = os.environ.get("SHOPIFY_TOKEN", "")
SHOPIFY_API_VER = "2026-04"
# Shopify is off by default (no longer used). Set SHOPIFY_ENABLED=true to re-enable.
SHOPIFY_ENABLED = os.environ.get("SHOPIFY_ENABLED", "false").strip().lower() in ("true", "1", "yes")
def get_shopify_headers():
    return {
        "X-Shopify-Access-Token": os.environ.get("SHOPIFY_TOKEN", SHOPIFY_TOKEN),
        "Content-Type": "application/json"
    }

# Map ClubLifter package names → Shopify Product ID
# Add more packages here as needed: "Package Name": variant_id
SHOPIFY_VARIANT_MAP = {
    # Product ID 8213478408449 — Kings of Hustler Las Vegas FREE ENTRY PASS ($0.00 test)
    # We fetch the first variant automatically below
}

def get_shopify_variant_id(product_id: int) -> str | None:
    """Fetch the default variant ID for a given Shopify product ID."""
    try:
        url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VER}/products/{product_id}/variants.json"
        res = requests.get(url, headers=get_shopify_headers(), timeout=5)
        data = res.json()
        print(f"[SHOPIFY] variant fetch status={res.status_code} data={data}", flush=True)
        variants = data.get("variants", [])
        if variants:
            return str(variants[0]["id"])
    except Exception:
        pass
    return None

def create_shopify_order(customer_name: str, customer_phone: str,
                          package_name: str, guests: int,
                          pickup_datetime: str, destination: str,
                          driver_name: str) -> dict:
    """
    Create a Shopify order for the given customer and package.
    Returns the Shopify order dict or an error dict.
    Disabled by default — set SHOPIFY_ENABLED=true to turn it back on.
    """
    if not SHOPIFY_ENABLED:
        return {"error": "Shopify disabled"}
    try:
        # checkout_url flow — no variant lookup needed
        pkg_obj    = Package.query.filter_by(name=package_name).first()
        variant_id = ""
        if not variant_id:
            variant_id = SHOPIFY_VARIANT_MAP.get(package_name, "")
        if not variant_id:
            # Fall back to the $0.00 test product
            variant_id = get_shopify_variant_id(8213478408449)
        if not variant_id:
            return {"error": "Shopify variant not found"}

        # Split name
        parts      = customer_name.strip().split(" ", 1)
        first_name = parts[0]
        last_name  = parts[1] if len(parts) > 1 else ""

        order_payload = {
            "order": {
                "line_items": [
                    {
                        "variant_id": variant_id,
                        "quantity":   1,
                        "title":      package_name,
                    }
                ],
                "customer": {
                    "first_name": first_name,
                    "last_name":  last_name,
                    "phone":      customer_phone or None,
                },
                "note": (
                    f"Pickup: {pickup_datetime} | "
                    f"Destination: {destination} | "
                    f"Guests: {guests} | "
                    f"Driver: {driver_name}"
                ),
                "financial_status": "paid",
                "send_receipt":     False,
                "tags":             "clublifter,pickup",
            }
        }

        url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VER}/orders.json"
        res = requests.post(url, json=order_payload, headers=get_shopify_headers(), timeout=10)
        data = res.json()
        print(f"[SHOPIFY] status={res.status_code} response={data}", flush=True)

        if "order" in data:
            return {
                "shopify_order_id":     data["order"]["id"],
                "shopify_order_number": data["order"]["order_number"],
                "shopify_order_url":    f"https://{SHOPIFY_STORE}/admin/orders/{data['order']['id']}"
            }
        else:
            return {"error": str(data)}

    except Exception as e:
        return {"error": str(e)}

# ─── MODELS ───────────────────────────────────────────────────────────────────

# Many-to-many: users (promoters) ↔ clubs
user_clubs = db.Table('user_clubs',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('club_id', db.Integer, db.ForeignKey('club.id'), primary_key=True)
)

class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), nullable=False, unique=True)
    password_hash = db.Column(db.String(200), nullable=False)
    role          = db.Column(db.String(20), default="promoter")
    email         = db.Column(db.String(200), default="")
    activation_token = db.Column(db.String(64), default="")   # for set-your-own-password links
    is_active     = db.Column(db.Boolean, default=True)       # False until password is set via link
    # Legacy single club (kept for backward compat)
    club_id       = db.Column(db.Integer, db.ForeignKey('club.id'), nullable=True)
    club          = db.relationship('Club', foreign_keys=[club_id])
    # NEW: multiple clubs (many-to-many)
    clubs         = db.relationship('Club', secondary=user_clubs, backref='promoters')
    # NEW: commission amount the admin sets per promoter (manual $ per sale)
    commission    = db.Column(db.Float, default=0.0)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_club_names(self):
        names = [c.name for c in self.clubs]
        if self.club and self.club.name not in names:
            names.append(self.club.name)
        return names

    def to_dict(self):
        return {
            "id": self.id, "username": self.username, "role": self.role,
            "club_id": self.club_id,
            "club_name": self.club.name if self.club else None,
            "clubs": [c.to_dict() for c in self.clubs],
            "club_names": self.get_club_names(),
            "commission": self.commission
        }

class Club(db.Model):
    id      = db.Column(db.Integer, primary_key=True)
    name    = db.Column(db.String(100), nullable=False, unique=True)
    address = db.Column(db.String(255), default="")
    active  = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "address": self.address, "active": self.active}

class Setting(db.Model):
    """Generic key-value store for app settings (e.g. the global API key)."""
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(80), nullable=False, unique=True)
    value = db.Column(db.String(255), default="")

def get_setting(key, default=""):
    s = Setting.query.filter_by(key=key).first()
    return s.value if s else default

def set_setting(key, value):
    s = Setting.query.filter_by(key=key).first()
    if s:
        s.value = value
    else:
        s = Setting(key=key, value=value)
        db.session.add(s)
    db.session.commit()
    return value

class Package(db.Model):
    id                 = db.Column(db.Integer, primary_key=True)
    name               = db.Column(db.String(100), nullable=False)
    description        = db.Column(db.String(255), default="")
    price              = db.Column(db.Float, default=0.0)
    max_guests         = db.Column(db.Integer, default=0)
    active             = db.Column(db.Boolean, default=True)
    checkout_url = db.Column(db.String(500), default="")
    club_id            = db.Column(db.Integer, db.ForeignKey('club.id'), nullable=True)  # null = all clubs

    def club_name(self):
        if not self.club_id:
            return ""
        c = Club.query.get(self.club_id)
        return c.name if c else ""

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "price": self.price, "max_guests": self.max_guests, "active": self.active,
            "checkout_url": self.checkout_url, "club_id": self.club_id,
            "club_name": self.club_name()
        }

class Driver(db.Model):
    """A person who drives. Cars are separate and assigned via Shifts."""
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), nullable=False, unique=True)
    phone      = db.Column(db.String(20), default="")
    # available: False means driver reported a problem and is temporarily disabled
    available  = db.Column(db.Boolean, default=True)
    assigned_car_id = db.Column(db.Integer, db.ForeignKey('car.id'), nullable=True)  # 1:1 car assignment
    # DEPRECATED car fields (kept for backward-compat migration into Car)
    car_model  = db.Column(db.String(100), default="")
    car_color  = db.Column(db.String(50), default="")
    car_plate  = db.Column(db.String(30), default="")
    car_photo  = db.Column(db.String(255), default="")

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "phone": self.phone,
            "available": self.available
        }

class Car(db.Model):
    """A vehicle. `name` must match the OneStepGPS display_name to get GPS."""
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), nullable=False, unique=True)  # = OneStepGPS display_name
    model      = db.Column(db.String(100), default="")
    color      = db.Column(db.String(50), default="")
    plate      = db.Column(db.String(30), default="")
    photo      = db.Column(db.String(255), default="")  # filename in /data/uploads
    active     = db.Column(db.Boolean, default=True)

    def car_string(self):
        # If model is blank, use the GPS display name (e.g. "2013 Mercedes Sprinter 3500")
        model = self.model or self.name
        parts = [self.color, model]
        s = " ".join(p for p in parts if p).strip().upper()
        if self.plate:
            s = f"{s} ({self.plate})" if s else self.plate
        return s or self.name or "N/A"

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "model": self.model,
            "color": self.color, "plate": self.plate, "photo": self.photo,
            "active": self.active, "car_string": self.car_string()
        }

class DriverStop(db.Model):
    """A period where a vehicle stayed in the same place long enough to count as a stop.
    Built from the GPS samples the tracker already polls every couple of minutes."""
    id           = db.Column(db.Integer, primary_key=True)
    car_name     = db.Column(db.String(120), index=True)   # OneStepGPS display_name
    driver_name  = db.Column(db.String(120), default="")   # driver on shift at the time
    lat          = db.Column(db.Float)
    lng          = db.Column(db.Float)
    address      = db.Column(db.String(300), default="")   # reverse-geocoded once
    started_at   = db.Column(db.DateTime)
    ended_at     = db.Column(db.DateTime, nullable=True)   # null while still parked there
    duration_min = db.Column(db.Integer, default=0)
    ongoing      = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            "id": self.id, "car_name": self.car_name, "driver_name": self.driver_name,
            "lat": self.lat, "lng": self.lng, "address": self.address,
            "started_at": vegas_time(self.started_at),
            "ended_at": vegas_time(self.ended_at) if self.ended_at else "",
            "started_full": vegas_datetime(self.started_at),
            "duration_min": self.duration_min, "ongoing": self.ongoing,
        }

class Shift(db.Model):
    """
    Assigns a driver + car for a time window.
    Either weekly (day_of_week 0=Mon..6=Sun) OR a specific date (MM/DD/YYYY).
    Specific-date shifts take priority over weekly ones.
    """
    id            = db.Column(db.Integer, primary_key=True)
    driver_id     = db.Column(db.Integer, db.ForeignKey('driver.id'), nullable=False)
    car_id        = db.Column(db.Integer, db.ForeignKey('car.id'), nullable=False)
    day_of_week   = db.Column(db.Integer, nullable=True)   # 0=Mon .. 6=Sun (weekly)
    specific_date = db.Column(db.String(20), nullable=True)  # MM/DD/YYYY (one-off)
    start_time    = db.Column(db.String(8), default="18:00")  # 24h HH:MM
    end_time      = db.Column(db.String(8), default="05:30")  # 24h HH:MM (can cross midnight)
    active        = db.Column(db.Boolean, default=True)

    driver = db.relationship('Driver', foreign_keys=[driver_id])
    car    = db.relationship('Car', foreign_keys=[car_id])

    def to_dict(self):
        return {
            "id": self.id,
            "driver_id": self.driver_id,
            "driver_name": self.driver.name if self.driver else "",
            "car_id": self.car_id,
            "car_name": self.car.name if self.car else "",
            "car_string": self.car.car_string() if self.car else "",
            "day_of_week": self.day_of_week,
            "specific_date": self.specific_date,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "active": self.active
        }

class Customer(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    nome            = db.Column(db.String(100))
    phone           = db.Column(db.String(20), default="")            # primary phone (kept for backward compat)
    phones_json     = db.Column(db.Text, default="[]")                # JSON array of additional phones
    endereco        = db.Column(db.String(500))
    details         = db.Column(db.String(500), default="")
    motorista       = db.Column(db.String(100))
    motorista_phone = db.Column(db.String(20), default="")
    car_name        = db.Column(db.String(100), default="")   # GPS display_name of assigned car
    car_string_val  = db.Column(db.String(200), default="")   # cached "RED HONDA CIVIC (NV-123)"
    car_photo       = db.Column(db.String(255), default="")   # photo filename of assigned car
    distancia       = db.Column(db.Float)
    package         = db.Column(db.String(100))
    guests          = db.Column(db.Integer)
    pickup_datetime = db.Column(db.String(50), default="")
    destination     = db.Column(db.String(100), default="")
    needs_transport = db.Column(db.Boolean, default=True)              # NEW: walk-in vs transport
    club_status     = db.Column(db.String(20), default="coming")       # NEW: coming | arrived | left
    dispatch_status = db.Column(db.String(20), default="none")         # none | sent | confirmed | enroute
    here_photo      = db.Column(db.Text, default="")                   # base64 image from driver "I'm here"
    here_photo_at   = db.Column(db.DateTime, nullable=True)            # when photo was uploaded (for 24h cleanup)
    priority        = db.Column(db.Boolean, default=False)             # club managers can flag priority pickups
    picked_up_at    = db.Column(db.DateTime, nullable=True)            # when the driver collected the guest
    dropped_off_at  = db.Column(db.DateTime, nullable=True)            # when the guest was dropped at the venue
    dropoff_verified = db.Column(db.Boolean, default=False)            # GPS confirmed arrival at the property
    dropoff_distance_mi = db.Column(db.Float, default=0.0)             # car-to-venue distance at drop-off
    promoter        = db.Column(db.String(80), default="")             # NEW: which promoter created this
    # Pickup status: 'scheduled', 'picked_up'
    status          = db.Column(db.String(20), default="scheduled")
    # Distance notification flags (so we don't fire 2x)
    notified_15km   = db.Column(db.Boolean, default=False)
    notified_10km   = db.Column(db.Boolean, default=False)
    notified_5km    = db.Column(db.Boolean, default=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    def get_phones(self):
        try:
            extra = json.loads(self.phones_json or "[]")
        except Exception:
            extra = []
        result = []
        if self.phone:
            result.append(self.phone)
        for p in extra:
            if p and p not in result:
                result.append(p)
        return result

    def to_dict(self):
        return {
            "id": self.id, "nome": self.nome, "phone": self.phone,
            "phones": self.get_phones(),
            "endereco": self.endereco, "details": self.details,
            "motorista": self.motorista, "motorista_phone": self.motorista_phone,
            "distancia": self.distancia, "package": self.package,
            "guests": self.guests, "pickup_datetime": self.pickup_datetime,
            "destination": self.destination,
            "needs_transport": self.needs_transport,
            "club_status": self.club_status,
            "dispatch_status": self.dispatch_status,
            "promoter": self.promoter,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else ""
        }

# ─── UTILITY ──────────────────────────────────────────────────────────────────
def calcular_distancia(lat1, lon1, lat2, lon2):
    """Haversine distance in MILES (R = 3959 mi). Whole system uses miles."""
    try:
        R = 3959  # Earth radius in miles (was 6371 km)
        phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
        dlat = math.radians(float(lat2) - float(lat1))
        dlon = math.radians(float(lon2) - float(lon1))
        a = math.sin(dlat/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlon/2)**2
        return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))
    except:
        return float('inf')

def is_master():
    return session.get("role") == "master"

def is_admin_level():
    """Master + club_owner + club_manager: near-full admin access.
    Club owner/manager see everything EXCEPT the API tab."""
    return session.get("role") in ("master", "club_owner", "club_manager")

def can_see_api():
    """Only master sees the API tab."""
    return session.get("role") == "master"

def can_dispatch():
    """Operational tabs (Today, Schedule, Guest List, Tracking) + scheduling."""
    return session.get("role") in ("master", "club_owner", "club_manager", "dispatch", "dispatch_manager")

def is_dispatch_manager():
    """Can create/manage dispatch accounts."""
    return session.get("role") in ("master", "club_owner", "club_manager", "dispatch_manager")

# Which roles each account type is allowed to CREATE
CREATABLE_ROLES = {
    "master":           ["promoter", "driver", "dispatch", "dispatch_manager", "club_manager", "club_owner", "master"],
    "club_owner":       ["promoter", "driver", "dispatch", "dispatch_manager", "club_manager"],
    "club_manager":     ["promoter", "driver", "dispatch", "dispatch_manager"],
    "dispatch_manager": ["dispatch"],
}

def can_create_accounts():
    return session.get("role") in CREATABLE_ROLES

def creatable_roles():
    return CREATABLE_ROLES.get(session.get("role"), [])

def is_driver():
    return session.get("role") == "driver"

# ─── API KEY PROTECTION ───────────────────────────────────────────────────────
from functools import wraps

API_ACCESS_KEY = os.environ.get("API_ACCESS_KEY", "")

def require_api_key(f):
    """
    Protects /api routes. Accepts either:
      - A logged-in admin session, OR
      - A valid X-API-Key header matching the stored key (DB) or env var.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Allow logged-in admins (so the app itself can call its own APIs)
        if session.get("logged") and is_master():
            return f(*args, **kwargs)
        # Otherwise require the API key header
        provided = request.headers.get("X-API-Key", "")
        # DB key takes priority, fall back to env var
        valid_key = get_setting("api_access_key", "") or API_ACCESS_KEY
        if valid_key and provided == valid_key:
            return f(*args, **kwargs)
        return jsonify({"error": "Unauthorized — valid API key or admin login required"}), 401
    return wrapper

def _fire_webhook_sync(payload: dict):
    try:
        r = requests.post(MAKE_WEBHOOK, json=payload, timeout=10)
        print(f"[WEBHOOK] type={payload.get('type') or payload.get('event')} status={r.status_code} url={MAKE_WEBHOOK}", flush=True)
        print(f"[WEBHOOK] response={r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[WEBHOOK] FAILED: {e} url={MAKE_WEBHOOK}", flush=True)

def fire_webhook(payload: dict):
    """Fire-and-forget: runs in a background thread so the HTTP response isn't
    delayed by Make/Twilio round-trips (which was causing client-side timeouts)."""
    try:
        threading.Thread(target=_fire_webhook_sync, args=(payload,), daemon=True).start()
    except Exception as e:
        print(f"[WEBHOOK] thread start failed: {e}", flush=True)
        _fire_webhook_sync(payload)

def twilio_configured():
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and (TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID))

STREETVIEW_LINKS = {
    "aria": "https://maps.app.goo.gl/up5uiB4NMudFYDfg9",  # Aria
    "allegiantstadium": "https://maps.app.goo.gl/efu94gi9DRuXTgzZ8",  # Allegiant Stadium
    "bellagiodropoff": "https://maps.app.goo.gl/vsB5Ykx75qcyH7jB9",  # Bellagio (Drop off)
    "caesarspalace": "https://www.google.com/maps/search/Caesars+Palace+Las+Vegas+Main+parking+garage+lower+level+and+open-air+area+near+the+Colosseum+street+view",  # Caesars Palace
    "circadowntown": "https://www.google.com/maps/search/Circa+Downtown+Las+Vegas+Main+level+of+the+Garage+Mahal%2C+follow+signage+near+the+main+entrance+street+view",  # Circa Downtown
    "cosmopolitan": "https://www.google.com/maps/search/Cosmopolitan+Las+Vegas+Boulevard+Tower+entrance+near+valet%3B+Chelsea+Tower+Uber-labeled+option+when+available+street+view",  # Cosmopolitan
    "encore": "https://www.google.com/maps/search/Encore+Las+Vegas+Main+resort+North+Valet+area%2C+accessible+off+Encore+Resort+Blvd+east+of+Las+Vegas+Strip+street+view",  # Encore
    "excalibur": "https://www.google.com/maps/search/Excalibur+Las+Vegas+Southern+Royal+Tower+entrance%3B+North+Valet+sometimes+used+as+alternate+street+view",  # Excalibur
    "flamingolasvegas": "https://www.google.com/maps/search/Flamingo+Las+Vegas+Las+Vegas+Dedicated+area+past+the+roundabout+at+the+hotel%27s+main+entrance+Porte+Coch%C3%A8re+street+view",  # Flamingo Las Vegas
    "fontainebleau": "https://www.google.com/maps/search/Fontainebleau+Las+Vegas+North+and+South+Valet+Entrances%3B+South+is+deeper+in+property+garage+level+street+view",  # Fontainebleau
    "goldennuggetdowntown": "https://www.google.com/maps/search/Golden+Nugget+Downtown+Las+Vegas+Second+floor+of+the+main+self-parking+garage+on+the+south+side+of+the+hotel+street+view",  # Golden Nugget Downtown
    "harrahslasvegas": "https://www.google.com/maps/search/Harrah%27s+Las+Vegas+Las+Vegas+Main+Porte+Cochere+%2F+front+entrance+off+Las+Vegas+Strip+or+drop-off+lane+before+valet+in+parking+garage+street+view",  # Harrah\'s Las Vegas
    "harryreidinternationalairport": "https://www.google.com/maps/search/Harry+Reid+International+Airport+Las+Vegas+Inside+parking+garages+at+Terminals+1+and+3+street+view",  # Harry Reid International Airport
    "horseshoelasvegas": "https://www.google.com/maps/search/Horseshoe+Las+Vegas+Las+Vegas+Tour+bus+and+shuttle+area+on+north+side+of+property+near+Flamingo+Road+street+view",  # Horseshoe Las Vegas
    "luxor": "https://www.google.com/maps/search/Luxor+Las+Vegas+North+Entrance+off+Reno+Drive+near+valet+area+street+view",  # Luxor
    "mandalaybay": "https://www.google.com/maps/search/Mandalay+Bay+Las+Vegas+South+of+hotel+lobby%2C+down+escalator%2C+beach+level+street+view",  # Mandalay Bay
    "mgmgrand": "https://www.google.com/maps/search/MGM+Grand+Las+Vegas+Ground+floor+of+self-parking+garage+%2F+Rideshare+Lounge+via+MGM+Underground+exit+near+Monorail+station+street+view",  # MGM Grand
    "newyorknewyork": "https://www.google.com/maps/search/New+York-New+York+Las+Vegas+South+Entrance+near+dedicated+rideshare+zone+street+view",  # New York-New York
    "palmscasinoresort": "https://www.google.com/maps/search/Palms+Casino+Resort+Las+Vegas+Main+entrance+valet+area+street+view",  # Palms Casino Resort
    "parislasvegas": "https://www.google.com/maps/search/Paris+Las+Vegas+Las+Vegas+Back+Valet+Entrance+near+convention+area+street+view",  # Paris Las Vegas
    "parkmgm": "https://www.google.com/maps/search/Park+MGM+Las+Vegas+Dedicated+lower+level+pickup+area%2C+usually+near+bell+desk+street+view",  # Park MGM
    "planethollywood": "https://www.google.com/maps/search/Planet+Hollywood+Las+Vegas+Entrance+near+Miracle+Mile+Shops+Valet+street+view",  # Planet Hollywood
    "plazahotelcasinodowntown": "https://www.google.com/maps/search/Plaza+Hotel+%26+Casino+Downtown+Las+Vegas+Valet+area+on+Main+Street+between+Stewart+and+Ogden+avenues+street+view",  # Plaza Hotel & Casino Downtown
    "resortsworld": "https://www.google.com/maps/search/Resorts+World+Las+Vegas+Lower+Level+Rideshare+Lobby%3B+take+elevator+or+escalator+down+street+view",  # Resorts World
    "saharalasvegas": "https://www.google.com/maps/search/SAHARA+Las+Vegas+Las+Vegas+Main+Entrance+Valet+%2F+designated+lanes+at+primary+porte+coch%C3%A8re+street+view",  # SAHARA Las Vegas
    "southpointhotel": "https://www.google.com/maps/search/South+Point+Hotel+Las+Vegas+Designated+lane+near+main+Valet+Entrance+or+Bingo+Hall+entrance+street+view",  # South Point Hotel
    "tmobilearena": "https://www.google.com/maps/search/T-Mobile+Arena+Las+Vegas+Pickup+requires+walking+to+New+York-New+York+or+Park+MGM+street+view",  # T-Mobile Arena
    "thelinq": "https://www.google.com/maps/search/The+LINQ+Las+Vegas+End+of+the+Promenade+near+the+High+Roller+Ferris+Wheel+street+view",  # The LINQ
    "thepalazzo": "https://www.google.com/maps/search/The+Palazzo+Las+Vegas+Lower+Porte+Cochere%3B+down+escalators+across+lobby+from+front+desk%3B+pickup+near+fountain.+Alternate%3A+Venetian+Parking+Garage+Level+2+street+view",  # The Palazzo
    "thevenetian": "https://www.google.com/maps/search/The+Venetian+Las+Vegas+Level+2+of+The+Venetian+Guest+Parking+Garage+street+view",  # The Venetian
    "treasureisland": "https://www.google.com/maps/search/Treasure+Island+Las+Vegas+South+Valet+Entrance+near+Sirens+Cove+street+view",  # Treasure Island
    "vdarahotelspa": "https://www.google.com/maps/search/Vdara+Hotel+%26+Spa+Las+Vegas+Main+Valet+and+Porte+Coch%C3%A8re+located+off+Harmon+Avenue+street+view",  # Vdara Hotel & Spa
    "westgatelasvegas": "https://www.google.com/maps/search/Westgate+Las+Vegas+Las+Vegas+East+Tower+Entrance+near+designated+rideshare+zone+street+view",  # Westgate Las Vegas
    "wynn": "https://www.google.com/maps/search/Wynn+Las+Vegas+Main+Valet+entrance+and+South+Gate%2FTour+Lobby+entrance+street+view",  # Wynn
    "vanderpumphotel": "https://www.google.com/maps/search/Vanderpump+Hotel+Las+Vegas+Designated+lower-level+valet+or+secondary+pickup+area+street+view",  # Vanderpump Hotel
}


def _sv_key(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())

def streetview_for(pickup_text):
    """Find the Street View link for a pickup location by matching the venue name
    at the start of the pickup address against the known venues."""
    if not pickup_text:
        return ""
    key = _sv_key(pickup_text)
    if not key:
        return ""
    # exact key match first
    if key in STREETVIEW_LINKS:
        return STREETVIEW_LINKS[key]
    # otherwise, find the longest venue key that the pickup text starts with / contains
    best = ""
    for vkey, link in STREETVIEW_LINKS.items():
        if key.startswith(vkey) or vkey in key:
            if len(vkey) > len(best):
                best, best_link = vkey, link
    return STREETVIEW_LINKS.get(best, "") if best else ""

def clean_phone(p):
    """Strip invisible Unicode marks (LRE/RLE/PDF etc. that come from iPhone
    copy-paste) and stray spaces/dashes, keeping a clean E.164-ish number."""
    if not p:
        return ""
    s = str(p)
    for ch in ('\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
               '\u200e', '\u200f', '\u2066', '\u2067', '\u2068', '\u2069', '\u00a0'):
        s = s.replace(ch, '')
    s = s.strip()
    keep = '+' if s.startswith('+') else ''
    digits = ''.join(ch for ch in s if ch.isdigit())
    return keep + digits

def send_sms(to, body, media_url=""):
    """Send one SMS/MMS straight to Twilio (no Make.com).

    Key detail: MediaUrl is only included when it's a real absolute URL. Sending an
    empty MediaUrl is what triggers Twilio error 21620 ("Invalid media URL").
    Never raises — messaging failures must not break a booking.
    """
    to = clean_phone(to)
    body = (body or "").strip()
    if not SMS_ENABLED:
        print(f"[SMS] muted (SMS_ENABLED=false) to={to}", flush=True)
        return {"ok": False, "skipped": "sms_disabled"}
    if not to or not body:
        print(f"[SMS] skipped: missing to/body (to={to!r})", flush=True)
        return {"ok": False, "skipped": "missing_to_or_body"}
    if not twilio_configured():
        print("[SMS] skipped: Twilio env vars not configured", flush=True)
        return {"ok": False, "skipped": "not_configured"}

    data = {"To": to, "Body": body[:1550]}
    if TWILIO_MESSAGING_SERVICE_SID:
        data["MessagingServiceSid"] = TWILIO_MESSAGING_SERVICE_SID
    else:
        data["From"] = TWILIO_FROM_NUMBER

    # Only attach media when it's a valid absolute http(s) URL
    media_url = (media_url or "").strip()
    if media_url.startswith("http://") or media_url.startswith("https://"):
        data["MediaUrl"] = media_url

    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
            data=data,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=15,
        )
        ok = r.status_code in (200, 201)
        if ok:
            sid = ""
            try: sid = r.json().get("sid", "")
            except Exception: pass
            print(f"[SMS] sent to={to} media={'yes' if 'MediaUrl' in data else 'no'} sid={sid}", flush=True)
            return {"ok": True, "sid": sid}
        print(f"[SMS] FAILED to={to} status={r.status_code} resp={r.text[:300]}", flush=True)
        return {"ok": False, "status": r.status_code, "error": r.text[:300]}
    except Exception as e:
        print(f"[SMS] EXCEPTION to={to}: {e}", flush=True)
        return {"ok": False, "error": str(e)}

def _send_many_sync(phones, body, media_url):
    seen = set()
    for p in (phones or []):
        p = clean_phone(p)
        if not p or p in seen:
            continue
        seen.add(p)
        send_sms(p, body, media_url)

def send_sms_many(phones, body, media_url=""):
    """Send to a list of numbers in the BACKGROUND so the web response returns
    immediately (Twilio round-trips were causing client-side timeouts)."""
    try:
        threading.Thread(target=_send_many_sync, args=(list(phones or []), body, media_url), daemon=True).start()
    except Exception as e:
        print(f"[SMS] thread start failed: {e}", flush=True)
        _send_many_sync(phones, body, media_url)
    return {"queued": True}

def send_sms_bg(to, body, media_url=""):
    """Background single send."""
    return send_sms_many([to], body, media_url)

def vegas_time(dt):
    """Format a stored UTC timestamp in Las Vegas local time (handles DST)."""
    if not dt:
        return ""
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone as _tz
        return dt.replace(tzinfo=_tz.utc).astimezone(ZoneInfo("America/Los_Angeles")).strftime("%I:%M %p")
    except Exception:
        return (dt - timedelta(hours=7)).strftime("%I:%M %p")

def vegas_datetime(dt):
    if not dt:
        return ""
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone as _tz
        return dt.replace(tzinfo=_tz.utc).astimezone(ZoneInfo("America/Los_Angeles")).strftime("%m/%d/%Y %I:%M %p")
    except Exception:
        return (dt - timedelta(hours=7)).strftime("%m/%d/%Y %I:%M %p")

def parse_pickup_datetime(dt_str):
    """
    Parse pickup_datetime string like '04/20/2026 08:00 PM'
    Returns a datetime object or None.
    """
    try:
        return datetime.strptime(dt_str.strip(), "%m/%d/%Y %I:%M %p")
    except Exception:
        return None

def driver_is_busy(driver_name: str, pickup_dt: datetime) -> bool:
    """
    A driver is busy if they already have a 'scheduled' customer
    within the same clock-hour as the requested pickup_dt.
    Same hour = same HH:00 – HH:59 block.
    """
    if pickup_dt is None:
        return False

    hour_start = pickup_dt.replace(minute=0, second=0, microsecond=0)

    existing = Customer.query.filter_by(
        motorista=driver_name,
        status='scheduled'
    ).all()

    for c in existing:
        existing_dt = parse_pickup_datetime(c.pickup_datetime)
        if existing_dt is None:
            continue
        existing_hour = existing_dt.replace(minute=0, second=0, microsecond=0)
        if existing_hour == hour_start:
            return True
    return False

def _time_in_window(t_minutes, start_str, end_str):
    """True if t_minutes (minutes since midnight) falls in [start,end], handling overnight."""
    def to_min(s):
        try:
            h, m = s.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return None
    start = to_min(start_str)
    end   = to_min(end_str)
    if start is None or end is None:
        return True
    if start <= end:
        return start <= t_minutes <= end
    # overnight window (e.g. 18:00 → 05:30)
    return t_minutes >= start or t_minutes <= end

def get_scheduled_shifts(pickup_dt: datetime):
    """
    Returns list of "shifts" (real or synthesized) that cover the given pickup.
    Direct 1:1 Driver→Car assignments (Driver Management) are always included as
    pseudo-shifts. Specific-date shifts take priority over weekly ones.
    Returns [] if nothing is configured (caller falls back to nearest-car).
    """
    class _PseudoShift:
        __slots__ = ("driver", "car", "start_time", "end_time", "specific_date", "day_of_week")
        def __init__(self, driver, car):
            self.driver = driver; self.car = car
            self.start_time = ""; self.end_time = ""
            self.specific_date = None; self.day_of_week = None

    # Direct assignments always apply (no time window)
    direct = []
    for d in Driver.query.filter(Driver.assigned_car_id.isnot(None)).all():
        car = Car.query.get(d.assigned_car_id)
        if car:
            direct.append(_PseudoShift(d, car))

    if pickup_dt is None:
        return direct
    date_str = pickup_dt.strftime("%m/%d/%Y")
    dow      = pickup_dt.weekday()  # 0=Mon..6=Sun
    t_min    = pickup_dt.hour * 60 + pickup_dt.minute

    all_active = Shift.query.filter_by(active=True).all()

    # Specific-date matches first
    specific = [s for s in all_active
                if s.specific_date and s.specific_date.strip() == date_str
                and _time_in_window(t_min, s.start_time, s.end_time)]
    if specific:
        return direct + specific

    # Otherwise weekly matches
    weekly = [s for s in all_active
              if s.specific_date in (None, "")
              and s.day_of_week == dow
              and _time_in_window(t_min, s.start_time, s.end_time)]
    return direct + weekly

# ─── AUTH ─────────────────────────────────────────────────────────────────────
# ─── SIMPLE IN-MEMORY RATE LIMITER FOR LOGIN ──────────────────────────────────
from collections import defaultdict
_login_attempts = defaultdict(list)  # ip → [timestamps]
LOGIN_MAX_ATTEMPTS = 5      # max attempts
LOGIN_WINDOW_SEC   = 300    # per 5 minutes

def _is_rate_limited(ip):
    now = time.time()
    # Drop attempts older than the window
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < LOGIN_WINDOW_SEC]
    return len(_login_attempts[ip]) >= LOGIN_MAX_ATTEMPTS

def _record_attempt(ip):
    _login_attempts[ip].append(time.time())

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()

        if _is_rate_limited(ip):
            return render_template('login.html',
                error="Too many login attempts. Please wait a few minutes and try again.")

        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and getattr(user, 'is_active', True) is False:
            return render_template('login.html', error="This account hasn't been activated yet. Please use the setup link that was emailed to you.")
        if user and user.check_password(password):
            # Successful login — clear their attempt history
            _login_attempts.pop(ip, None)
            session['logged']   = True
            session['username'] = user.username
            session['role']     = user.role
            session['user_id']  = user.id
            # Show all assigned clubs (multi-club aware)
            club_names = user.get_club_names()
            session['club_name'] = ", ".join(club_names) if club_names else None
            # Drivers go to their own dashboard
            if user.role == 'driver':
                return redirect(url_for('driver_dashboard'))
            return redirect(url_for('index'))

        # Failed login — record the attempt
        _record_attempt(ip)
        return render_template('login.html', error="Invalid credentials")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── MAIN (PROMOTER/ADMIN DASHBOARD) ─────────────────────────────────────────
@app.route('/')
def index():
    if not session.get("logged"):
        return redirect(url_for("login"))
    # Drivers should not access the main dashboard
    if session.get("role") == "driver":
        return redirect(url_for("driver_dashboard"))

    packages = Package.query.filter_by(active=True).all()
    user     = User.query.get(session.get("user_id"))

    if session.get("role") == "master":
        # Admin sees all clubs and all customers
        clubs     = Club.query.filter_by(active=True).all()
        customers = Customer.query.order_by(Customer.id.desc()).all()
    else:
        # Promoter sees only their assigned clubs + their own customers
        assigned = user.get_club_names() if user else []
        if assigned:
            clubs = Club.query.filter(Club.active == True, Club.name.in_(assigned)).all()
        else:
            clubs = Club.query.filter_by(active=True).all()  # fallback: all clubs
        customers = Customer.query.filter_by(promoter=session.get("username")).order_by(Customer.id.desc()).all()

    return render_template('index.html', clientes=customers, packages=packages, clubs=clubs)

@app.route('/promoter/dashboard')
def promoter_dashboard():
    if not session.get("logged"):
        return redirect(url_for("login"))
    if session.get("role") == "master":
        return redirect(url_for("index"))
    if session.get("role") == "driver":
        return redirect(url_for("driver_dashboard"))

    username = session.get("username")
    user     = User.query.get(session.get("user_id"))

    # This promoter's customers
    my_customers = Customer.query.filter_by(promoter=username).order_by(Customer.id.desc()).all()

    # Commission calc: commission per sale × number of sales
    commission_rate  = user.commission if user else 0
    total_sales      = len(my_customers)
    total_commission = commission_rate * total_sales

    # Car availability — which cars are free right now (this hour)
    now = datetime.now()
    cars = Car.query.filter_by(active=True).all()
    # Find which cars are tied up by a scheduled customer this hour
    car_status = []
    for car in cars:
        # A car is "in use" if any scheduled customer this hour uses it
        busy = False
        hour_customers = Customer.query.filter_by(status='scheduled', car_name=car.name).all()
        for c in hour_customers:
            cdt = parse_pickup_datetime(c.pickup_datetime)
            if cdt and cdt.replace(minute=0, second=0, microsecond=0) == now.replace(minute=0, second=0, microsecond=0):
                busy = True
                break
        car_status.append({
            "name": car.name, "car_model": car.model, "car_color": car.color,
            "car_plate": car.plate, "car_photo": car.photo,
            "in_use": busy
        })

    return render_template('promoter_dashboard.html',
        customers=my_customers,
        commission_rate=commission_rate,
        total_sales=total_sales,
        total_commission=total_commission,
        car_status=car_status,
        club_names=user.get_club_names() if user else []
    )

@app.route('/limpar')
def limpar():
    if not session.get("logged"):
        return redirect(url_for("login"))
    Customer.query.delete()
    db.session.commit()
    return redirect(url_for('index'))

# ─── REGISTER CUSTOMER ────────────────────────────────────────────────────────
@app.route('/cadastrar_cep', methods=['POST'])
def cadastrar_cep():
    if not session.get("logged"):
        return jsonify({"success": False, "error": "Unauthorized"})

    nome              = request.form.get('nome', '').strip()
    client_phone      = request.form.get('client_phone', '').strip()
    extra_phones_raw  = request.form.get('extra_phones', '').strip()  # JSON array
    endereco_completo = request.form.get('endereco_completo', '').strip()
    geocode_address   = request.form.get('geocode_address', '').strip() or endereco_completo
    details           = request.form.get('details', '').strip()
    package           = request.form.get('package', '').strip()
    try:
        guests = int(request.form.get('guests', 0) or 0)
    except (ValueError, TypeError):
        guests = 1
    pickup_datetime   = request.form.get('pickup_datetime', '').strip()
    destination       = request.form.get('destination', '').strip()
    needs_transport   = request.form.get('needs_transport', 'true').lower() == 'true'
    force_waitlist    = request.form.get('force_waitlist', 'false').lower() == 'true'

    # Parse extra phones JSON array
    try:
        extra_phones = json.loads(extra_phones_raw) if extra_phones_raw else []
        extra_phones = [p.strip() for p in extra_phones if p and p.strip()]
    except Exception:
        extra_phones = []
    all_phones = ([client_phone] if client_phone else []) + extra_phones

    try:
        # ── WALK-IN PATH (no transport): skip geocoding, GPS, driver assignment ──
        if not needs_transport:
            customer = Customer(
                nome=nome, phone=client_phone, phones_json=json.dumps(extra_phones),
                endereco="(walk-in)", details=details,
                motorista="(walk-in)", motorista_phone="",
                distancia=0, package=package,
                guests=guests, pickup_datetime=pickup_datetime,
                destination=destination,
                needs_transport=False, club_status="coming",
                promoter=session.get('username', ''),
                status='scheduled',
                created_at=datetime.utcnow()
            )
            db.session.add(customer)
            db.session.commit()

            # Still create Shopify order
            shopify_result = create_shopify_order(
                customer_name=nome, customer_phone=client_phone,
                package_name=package, guests=guests,
                pickup_datetime=pickup_datetime, destination=destination,
                driver_name="(walk-in)"
            )

            fire_webhook({
                "event":                "walk_in_registered",
                "customer_id":          customer.id,
                "customer_name":        nome,
                "customer_phone":       client_phone,
                "customer_phones":      all_phones,
                "details":              details,
                "pickup_datetime":      pickup_datetime,
                "package":              package,
                "guests":               guests,
                "destination":          destination,
                "needs_transport":      False,
                "shopify_order_id":     shopify_result.get("shopify_order_id"),
                "shopify_order_number": shopify_result.get("shopify_order_number"),
                "shopify_order_url":    shopify_result.get("shopify_order_url"),
            })

            return jsonify({
                "success": True, "walk_in": True,
                "customer_id": customer.id,
                "package": package, "guests": guests,
                "destination": destination
            })

        # ── TRANSPORT PATH (original flow) ──
        # Parse the requested pickup time for availability checking
        requested_dt = parse_pickup_datetime(pickup_datetime)

        # 1. GEOCODING — use the clean street address (geocode_address), not the
        # descriptive valet text, so Nominatim can resolve it.
        encoded = urllib.parse.quote(geocode_address)
        geo_res = requests.get(
            f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1&addressdetails=1",
            headers={'User-Agent': 'ClubLifter_LasVegas_App'}, timeout=8
        ).json()

        # Fallback: if the clean address failed, try the full text
        if not geo_res and geocode_address != endereco_completo:
            encoded2 = urllib.parse.quote(endereco_completo)
            geo_res = requests.get(
                f"https://nominatim.openstreetmap.org/search?q={encoded2}&format=json&limit=1&addressdetails=1",
                headers={'User-Agent': 'ClubLifter_LasVegas_App'}, timeout=8
            ).json()

        if not geo_res:
            return jsonify({"success": False, "error": "Address not found on global map."})

        lat_cli = float(geo_res[0]['lat'])
        lng_cli = float(geo_res[0]['lon'])

        # 2. GET ALL VEHICLES FROM ONESTEPGPS (live coords keyed by display_name)
        headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        res_v = requests.get(
            "https://track.onestepgps.com/v3/api/public/device-info?lat_lng=1",
            headers=headers_api, timeout=8
        ).json()

        lista = res_v if isinstance(res_v, list) else [res_v]
        gps_by_name = {}
        for v in lista:
            v_lat = v.get('lat') or v.get('last_tap', {}).get('lat')
            v_lng = v.get('lng') or v.get('last_tap', {}).get('lng')
            if v_lat and v_lng:
                gps_by_name[v.get('display_name', '')] = {"lat": float(v_lat), "lng": float(v_lng)}

        # 3. PICK THE DRIVER + CAR
        # Strategy A: use the SHIFT SCHEDULE for this pickup time (preferred)
        # Strategy B (fallback): nearest available car if no shift is configured
        melhor_v        = "Unavailable"   # driver name (person)
        menor_d         = float('inf')
        motorista_coords = None
        chosen_car      = None            # Car object

        # Strategy 0: manual override — the booker explicitly chose a driver
        driver_mode   = (request.form.get('driver_mode', 'auto') or 'auto').strip()
        manual_driver = (request.form.get('manual_driver', '') or '').strip()
        if driver_mode == 'manual' and manual_driver:
            mdrv = Driver.query.filter_by(name=manual_driver).first()
            if not mdrv:
                mdrv = get_driver_record(manual_driver)
            if mdrv:
                melhor_v = mdrv.name
                # Their assigned car (1:1), then compute distance if GPS is available
                if mdrv.assigned_car_id:
                    chosen_car = Car.query.get(mdrv.assigned_car_id)
                if chosen_car:
                    coords = gps_by_name.get(chosen_car.name)
                    if coords:
                        motorista_coords = coords
                        menor_d = calcular_distancia(lat_cli, lng_cli, coords["lat"], coords["lng"])

        shifts = get_scheduled_shifts(requested_dt)

        if melhor_v == "Unavailable" and shifts:
            # Among scheduled drivers, pick the closest one whose driver is free this hour
            best = None
            for sh in shifts:
                drv = sh.driver
                car = sh.car
                if not drv or not car:
                    continue
                if not drv.available:
                    continue
                if driver_is_busy(drv.name, requested_dt):
                    continue
                coords = gps_by_name.get(car.name)
                if not coords:
                    # car has no live GPS — still assignable, distance unknown
                    dist = float('inf')
                else:
                    dist = calcular_distancia(lat_cli, lng_cli, coords["lat"], coords["lng"])
                if best is None or dist < best[1]:
                    best = (drv, dist, coords, car)
            if best:
                # best[0] is a Driver object; store the driver NAME in melhor_v.
                # Passing the Driver object into SQLAlchemy filters causes:
                # "SQL expression element or literal value expected, got <Driver ...>"
                drv, dist, coords, car = best
                melhor_v = drv.name
                menor_d = dist
                motorista_coords = coords
                chosen_car = car

        if melhor_v == "Unavailable":
            # FALLBACK: no shift matched (or all busy) → nearest available car
            candidates = []
            for name, coords in gps_by_name.items():
                d = calcular_distancia(lat_cli, lng_cli, coords["lat"], coords["lng"])
                candidates.append((d, name, coords))
            candidates.sort(key=lambda x: x[0])
            for dist, name, coords in candidates:
                car = Car.query.filter_by(name=name).first()
                # If car registered & inactive, skip
                if car and not car.active:
                    continue
                # Try to find a driver assigned via shift to this car; else leave name
                melhor_v        = name   # use car/display name as fallback "driver"
                menor_d         = dist
                motorista_coords = coords
                chosen_car      = car
                break

        # 4. RESOLVE PHONE + CAR INFO
        driver_profile  = Driver.query.filter_by(name=melhor_v).first()
        motorista_phone = driver_profile.phone if driver_profile else ""
        if chosen_car:
            # The vehicle's descriptive name (e.g. "2013 Mercedes Sprinter 3500") is
            # often stored in `name` (the OneStepGPS display name) while `model` is blank.
            # Fall back to `name` so the car never shows up empty in SMS/webhooks.
            car_model = chosen_car.model or chosen_car.name
            car_color = chosen_car.color
            car_plate = chosen_car.plate
            car_photo = chosen_car.photo
        else:
            car_model = car_color = car_plate = car_photo = ""

        # 4b. NO DRIVER AVAILABLE → ask the user, or schedule to waitlist if confirmed
        no_driver = (melhor_v == "Unavailable")
        if no_driver and not force_waitlist:
            # Stop here and let the frontend ask whether to waitlist
            return jsonify({
                "success": False,
                "no_driver": True,
                "message": "All drivers are currently busy for this time slot. Would you like to add this customer to the waitlist and assign a driver later?"
            })

        is_waitlist = no_driver and force_waitlist

        # 5. REGISTER ON ONESTEPGPS
        payload_gps = {
            "display_name": nome, "active": True, "status": "active", "marker_type": "point",
            "detail": {
                "description": f"{endereco_completo} | {details}" if details else endereco_completo,
                "lat_lng": {"lat": lat_cli, "lng": lng_cli}
            }
        }
        requests.post(URL_API, json=payload_gps, headers=headers_api, timeout=8)

        # 6. SAVE TO DATABASE
        distancia_arredondada = round(menor_d, 2) if menor_d != float('inf') else 0
        customer = Customer(
            nome=nome, phone=client_phone, phones_json=json.dumps(extra_phones),
            endereco=endereco_completo, details=details,
            motorista=("Waitlist" if is_waitlist else melhor_v),
            motorista_phone=motorista_phone,
            car_name=(chosen_car.name if chosen_car else ""),
            car_string_val=(chosen_car.car_string() if chosen_car else ""),
            car_photo=car_photo,
            distancia=distancia_arredondada, package=package,
            guests=guests, pickup_datetime=pickup_datetime,
            destination=destination,
            needs_transport=True, club_status="coming",
            promoter=session.get('username', ''),
            status=('waitlist' if is_waitlist else 'scheduled'),
            created_at=datetime.utcnow()
        )
        db.session.add(customer)
        db.session.commit()

        # 7. CREATE SHOPIFY ORDER
        shopify_result = create_shopify_order(
            customer_name   = nome,
            customer_phone  = client_phone,
            package_name    = package,
            guests          = guests,
            pickup_datetime = pickup_datetime,
            destination     = destination,
            driver_name     = melhor_v
        )

        # 8. FIRE WEBHOOK — waitlist alert OR normal scheduled
        if is_waitlist:
            fire_webhook({
                "event":            "no_driver_available",
                "customer_id":      customer.id,
                "customer_name":    nome,
                "customer_phone":   client_phone,
                "customer_phones":  all_phones,
                "pickup_address":   endereco_completo,
                "pickup_datetime":  pickup_datetime,
                "destination":      destination,
                "package":          package,
                "guests":           guests,
                "status":           "waitlist",
            })
        else:
            fire_webhook({
                "type":                 "driver",
                "customer_id":          customer.id,
                "driver_name":          melhor_v,
                "driver_phone":         motorista_phone,
                "customer_name":        nome,
                "customer_phone":       client_phone,
                "customer_phones":      all_phones,
                "pickup_address":       endereco_completo,
                "details":              details,
                "pickup_datetime":      pickup_datetime,
                "package":              package,
                "guests":               guests,
                "distance_km":          distancia_arredondada,
                "distance_mi":          distancia_arredondada,
                "distance_unit":        "mi",
                "destination":          destination,
                "needs_transport":      True,
                "car_model":            car_model,
                "car_color":            car_color,
                "car_plate":            car_plate,
                "model":                car_model,
                "color":                car_color,
                "license_plate":        car_plate,
                "driver_car":           " ".join(p for p in [car_color, car_model] if p).strip() + (f" ({car_plate})" if car_plate else ""),
                "status":               "scheduled",
                "shopify_order_id":     shopify_result.get("shopify_order_id"),
                "shopify_order_number": shopify_result.get("shopify_order_number"),
                "shopify_order_url":    shopify_result.get("shopify_order_url"),
            })

            # --- Direct SMS to the DRIVER (new pickup assigned) ---
            _drv_time = ""
            if pickup_datetime and len(pickup_datetime.split(' ')) >= 3:
                _p = pickup_datetime.split(' '); _drv_time = f"{_p[1]} {_p[2]}"
            _drv_sv = streetview_for(endereco_completo)
            driver_sms = (
                f"New ClubLifter pickup{' at ' + _drv_time if _drv_time else ''}\n"
                f"Guest: {nome} ({guests} guest{'s' if (guests or 0) != 1 else ''})\n"
                f"Pickup: {endereco_completo}\n"
                f"Drop-off: {destination or 'N/A'}\n"
                f"Distance: {distancia_arredondada} mi"
                + (f"\nNotes: {details}" if details else "")
                + (f"\nPickup spot: {_drv_sv}" if _drv_sv else "")
            )
            send_sms_bg(motorista_phone, driver_sms)

        # 9. SMS TO CUSTOMER (direct Twilio)
        car_full = " ".join(p for p in [car_color, car_model] if p).strip().upper()
        if car_plate:
            car_full = f"{car_full} ({car_plate})" if car_full else car_plate
        # Build absolute car photo URL (for Twilio MMS Media URL)
        car_photo_url = ""
        if car_photo:
            car_photo_url = f"{PUBLIC_BASE_URL}/uploads/{car_photo}"
        # Extract just the time portion for a cleaner message
        time_part = ""
        if pickup_datetime and len(pickup_datetime.split(' ')) >= 3:
            parts = pickup_datetime.split(' ')
            time_part = f"{parts[1]} {parts[2]}"
        # Only send the "ride booked" SMS when a real driver was assigned
        if not is_waitlist:
            _sv = streetview_for(endereco_completo)
            sms_text = (
                f"Hi {nome}! Your ClubLifter ride is booked. "
                f"{melhor_v} will pick you up"
                f"{' at ' + time_part if time_part else ''}"
                f"{' in a ' + car_full if car_full and car_full != 'N/A' else ''}. "
                f"See you soon!"
                f"{chr(10) + 'Your pickup spot: ' + _sv if _sv else ''}"
            )
            fire_webhook({
                "type":            "scheduled",
                "customer_name":   nome,
                "customer_phone":  client_phone,   # phone #1
                "driver_name":     melhor_v,
                "driver_car":      car_full or "N/A",
                "driver_car_photo_url": car_photo_url,
                "pickup_datetime": pickup_datetime,
                "pickup_time":     time_part,
                "destination":     destination,
                "pickup_streetview": streetview_for(endereco_completo),
                "message":         sms_text,
                "customer_id":     customer.id,
            })
            # Direct SMS/MMS to the customer (photo attached only if one exists)
            send_sms_many(all_phones or [client_phone], sms_text, car_photo_url)

        return jsonify({
            "success": True,
            "waitlist": is_waitlist,
            "motorista": ("Waitlist — no driver yet" if is_waitlist else melhor_v),
            "motorista_phone": motorista_phone,
            "distancia": distancia_arredondada,
            "cliente_coords": {"lat": lat_cli, "lng": lng_cli},
            "motorista_coords": motorista_coords,
            "package": package, "guests": guests, "pickup_datetime": pickup_datetime,
            "destination": destination,
            "shopify_order_id":     shopify_result.get("shopify_order_id"),
            "shopify_order_number": shopify_result.get("shopify_order_number"),
            "shopify_order_url":    shopify_result.get("shopify_order_url"),
            "shopify_error":        shopify_result.get("error"),
            "checkout_url":         Package.query.filter_by(name=package).first().checkout_url if Package.query.filter_by(name=package).first() else "",
        })

    except Exception as e:
        import traceback
        print(f"[SCHEDULE ERROR] {e}", flush=True)
        traceback.print_exc()
        return jsonify({"success": False, "error": f"{type(e).__name__}: {str(e)}"})

# ─── ADMIN: TODAY'S SCHEDULE ──────────────────────────────────────────────────
@app.route('/admin/today')
def admin_today():
    if not session.get("logged") or not can_dispatch():
        return redirect(url_for("login"))

    today = date.today()
    # Match both non-padded (7/1/2026) and padded (07/01/2026) date formats,
    # since pickups may be stored either way.
    today_np = f"{today.month}/{today.day}/{today.year}"           # 7/1/2026
    today_p  = f"{today.month:02d}/{today.day:02d}/{today.year}"   # 07/01/2026
    today_str = today_np

    all_customers = Customer.query.order_by(Customer.pickup_datetime).all()
    today_customers = [c for c in all_customers
                       if today_np in (c.pickup_datetime or "")
                       or today_p in (c.pickup_datetime or "")]

    month_start = datetime(today.year, today.month, 1)
    month_customers = Customer.query.filter(Customer.created_at >= month_start).all()
    month_count = len(month_customers)
    month_revenue = sum(
        next((p.price for p in Package.query.filter_by(name=c.package).all()), 0)
        for c in month_customers
    )
    month_guests = sum(c.guests or 0 for c in month_customers)

    return render_template('admin_today.html',
        today_customers=today_customers,
        today_str=today_str,
        month_count=month_count,
        month_revenue=month_revenue,
        month_guests=month_guests,
        today=today
    )

# ─── API: LAST CLIENT (for AI voice calls) ────────────────────────────────────
@app.route('/api/bug-report', methods=['POST'])
def bug_report():
    """Any logged-in user can submit a bug report / message → fires webhook."""
    if not session.get("logged"):
        return jsonify({"success": False, "error": "Unauthorized"})
    description = (request.form.get('description', '') or "").strip()
    category    = (request.form.get('category', '') or "general").strip()
    if not description:
        return jsonify({"success": False, "error": "Description is required"})
    if len(description) > 2000:
        description = description[:2000]
    fire_webhook({
        "type":        "bug_report",
        "category":    category,
        "description": description,
        "username":    session.get("username", ""),
        "role":        session.get("role", ""),
        "page":        request.form.get('page', ''),
        "timestamp":   datetime.utcnow().isoformat(),
    })
    return jsonify({"success": True})

@app.route('/api/last-client')
@require_api_key
def last_client():
    c = Customer.query.order_by(Customer.id.desc()).first()
    if not c:
        return jsonify({"error": "No clients found"})
    return jsonify(c.to_dict())

@app.route('/api/sms-test', methods=['GET', 'POST'])
def sms_test():
    """Check Twilio config and optionally send a test SMS.
    GET  /api/sms-test            → shows config status (no SMS sent)
    POST /api/sms-test  to=+1...  → sends a test message to that number
    """
    if not (session.get("logged") and is_master()):
        return jsonify({"error": "Unauthorized"}), 401
    status = {
        "configured":            twilio_configured(),
        "sms_enabled":           SMS_ENABLED,
        "account_sid_set":       bool(TWILIO_ACCOUNT_SID),
        "auth_token_set":        bool(TWILIO_AUTH_TOKEN),
        "from_number":           TWILIO_FROM_NUMBER or None,
        "messaging_service_sid": TWILIO_MESSAGING_SERVICE_SID or None,
        "public_base_url":       PUBLIC_BASE_URL,
    }
    if request.method == 'GET':
        return jsonify(status)
    to = request.form.get('to', '').strip() or request.args.get('to', '').strip()
    if not to:
        return jsonify({"error": "Provide ?to=+1702...", "status": status}), 400
    result = send_sms(to, "ClubLifter test message — your Twilio integration is working.")
    return jsonify({"status": status, "result": result})

@app.route('/api/debug/schema')
def debug_schema():
    """TEMP: shows which columns exist in the live DB, to diagnose migration issues.
    Requires admin login OR API key."""
    if not (session.get("logged") and is_master()):
        provided = request.headers.get("X-API-Key", "") or request.args.get("key", "")
        valid = get_setting("api_access_key", "") or API_ACCESS_KEY
        if not (valid and provided == valid):
            return jsonify({"error": "Unauthorized"}), 401
    from sqlalchemy import inspect as _inspect
    insp = _inspect(db.engine)
    out = {}
    for tbl in ("customer", "driver", "car", "shift", "user", "package"):
        try:
            out[tbl] = [c["name"] for c in insp.get_columns(tbl)]
        except Exception as e:
            out[tbl] = f"ERROR: {e}"
    return jsonify(out)

# ─── ADMIN: USER MANAGEMENT ───────────────────────────────────────────────────
@app.route('/admin/users')
def admin_users():
    if not session.get("logged"):
        return redirect(url_for("login"))
    if not can_create_accounts():
        return redirect(url_for("login"))
    allowed = creatable_roles()
    # Master sees every account (so masters can be promoted/demoted too)
    if is_master():
        users = User.query.order_by(User.role, User.username).all()
    else:
        users = User.query.filter(User.role.in_(allowed)).all()
    clubs  = Club.query.filter_by(active=True).all()
    return render_template('admin_users.html', users=users, clubs=clubs,
                           viewer_role=session.get("role"), creatable_roles=allowed)

@app.route('/admin/users/new', methods=['POST'])
def new_user():
    if not can_create_accounts():
        return jsonify({"success": False, "error": "Unauthorized"})
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    email    = request.form.get('email', '').strip()
    role     = request.form.get('role', 'promoter').strip()
    club_ids = request.form.getlist('club_ids')  # multi-select
    commission = request.form.get('commission', '0')
    if not username or not password:
        return jsonify({"success": False, "error": "Username and password are required"})
    if User.query.filter_by(username=username).first():
        return jsonify({"success": False, "error": "Username already exists"})

    allowed = creatable_roles()
    if role not in allowed:
        return jsonify({"success": False, "error": f"You are not allowed to create '{role}' accounts"})

    user = User(username=username, role=role, email=email)
    user.set_password(password)
    try:
        user.commission = float(commission or 0)
    except ValueError:
        user.commission = 0
    if club_ids:
        user.clubs = Club.query.filter(Club.id.in_([int(c) for c in club_ids])).all()
        user.club_id = int(club_ids[0])
    db.session.add(user)
    db.session.commit()

    if role == 'driver' and not Driver.query.filter_by(name=username).first():
        db.session.add(Driver(name=username, phone=request.form.get('phone', '').strip(), available=True))
        db.session.commit()

    # Fire webhook so an email with the credentials can be sent
    fire_webhook({
        "type":     "acc_created",
        "username": username,
        "password": password,   # plaintext, for the welcome email
        "email":    email,
        "role":     role,
        "created_by": session.get("username", ""),
    })

    return jsonify({"success": True, "user": user.to_dict()})

@app.route('/admin/users/invite', methods=['POST'])
def invite_user():
    """Create an account WITHOUT a password. The user sets their own password via a
    one-time activation link. The link (not a password) is sent in the acc_created webhook."""
    import secrets as _s
    if not can_create_accounts():
        return jsonify({"success": False, "error": "Unauthorized"})
    username = request.form.get('username', '').strip()
    email    = request.form.get('email', '').strip()
    role     = request.form.get('role', 'promoter').strip()
    club_ids = request.form.getlist('club_ids')
    if not username:
        return jsonify({"success": False, "error": "Username is required"})
    if User.query.filter_by(username=username).first():
        return jsonify({"success": False, "error": "Username already exists"})
    allowed = creatable_roles()
    if role not in allowed:
        return jsonify({"success": False, "error": f"You are not allowed to create '{role}' accounts"})

    token = _s.token_urlsafe(32)
    user = User(username=username, role=role, email=email,
                activation_token=token, is_active=False)
    # Set an unusable random password until they choose one
    user.set_password(_s.token_hex(24))
    if club_ids:
        user.clubs = Club.query.filter(Club.id.in_([int(c) for c in club_ids])).all()
        user.club_id = int(club_ids[0])
    db.session.add(user)
    db.session.commit()

    if role == 'driver' and not Driver.query.filter_by(name=username).first():
        db.session.add(Driver(name=username, phone=request.form.get('phone', '').strip(), available=True))
        db.session.commit()

    activation_link = f"{PUBLIC_BASE_URL}/activate/{token}"
    fire_webhook({
        "type":            "acc_created",
        "username":        username,
        "email":           email,
        "role":            role,
        "activation_link": activation_link,   # send THIS instead of a password
        "created_by":      session.get("username", ""),
    })
    return jsonify({"success": True, "user": user.to_dict(), "activation_link": activation_link})

@app.route('/activate/<token>', methods=['GET'])
def activate_page(token):
    user = User.query.filter_by(activation_token=token).first()
    if not user:
        return render_template('activate.html', valid=False, username="")
    return render_template('activate.html', valid=True, username=user.username, token=token)

@app.route('/activate/<token>', methods=['POST'])
def activate_submit(token):
    user = User.query.filter_by(activation_token=token).first()
    if not user:
        return jsonify({"success": False, "error": "This link is invalid or has already been used."})
    password = request.form.get('password', '').strip()
    if len(password) < 6:
        return jsonify({"success": False, "error": "Password must be at least 6 characters."})
    user.set_password(password)
    user.is_active = True
    user.activation_token = ""   # one-time use
    db.session.commit()
    return jsonify({"success": True})

@app.route('/admin/users/edit/<int:user_id>', methods=['POST'])
def edit_user(user_id):
    if not can_create_accounts():
        return jsonify({"success": False, "error": "Unauthorized"})
    user     = User.query.get_or_404(user_id)
    allowed  = creatable_roles()
    # Can only edit accounts of roles you're allowed to manage (master edits anyone)
    if not is_master() and user.role not in allowed:
        return jsonify({"success": False, "error": "Unauthorized"})
    club_ids = request.form.getlist('club_ids')
    role     = request.form.get('role', user.role).strip()
    commission = request.form.get('commission', None)
    email    = request.form.get('email', None)
    if email is not None:
        user.email = email.strip()
    # Safety: you can't change your own role (prevents locking yourself out)
    if user.id == session.get("user_id") and role != user.role:
        return jsonify({"success": False, "error": "You can't change your own role."})
    # Can change role only to a role you're allowed to assign
    if role in allowed and role != user.role:
        # Never allow removing the last master account
        if user.role == 'master' and role != 'master':
            if User.query.filter_by(role='master').count() <= 1:
                return jsonify({"success": False, "error": "Can't demote the last master account."})
        user.role = role
        if role == 'driver' and not Driver.query.filter_by(name=user.username).first():
            db.session.add(Driver(name=user.username, available=True))
    if commission is not None:
        try:
            user.commission = float(commission or 0)
        except ValueError:
            pass
    if club_ids:
        user.clubs = Club.query.filter(Club.id.in_([int(c) for c in club_ids])).all()
        user.club_id = int(club_ids[0])
    else:
        user.clubs = []
        user.club_id = None
    db.session.commit()
    return jsonify({"success": True, "user": user.to_dict()})

@app.route('/admin/users/reset/<int:user_id>', methods=['POST'])
def reset_password(user_id):
    if not can_create_accounts():
        return jsonify({"success": False, "error": "Unauthorized"})
    user = User.query.get_or_404(user_id)
    allowed = creatable_roles()
    if not is_master() and user.role not in allowed:
        return jsonify({"success": False, "error": "Unauthorized"})
    new_password = request.form.get('password', '').strip()
    if not new_password: return jsonify({"success": False, "error": "Password cannot be empty"})
    user.set_password(new_password)
    db.session.commit()
    return jsonify({"success": True})

@app.route('/admin/users/delete/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    user = User.query.get_or_404(user_id)
    if user.role == 'master': return jsonify({"success": False, "error": "Cannot delete master account"})
    db.session.delete(user)
    db.session.commit()
    return jsonify({"success": True})

# ─── ADMIN: API KEY MANAGEMENT ────────────────────────────────────────────────
import secrets as _secrets

@app.route('/admin/api')
def admin_api():
    if not session.get("logged") or not is_master():
        return redirect(url_for("login"))
    current_key = get_setting("api_access_key", "")
    return render_template('admin_api.html', api_key=current_key)

@app.route('/admin/api/generate', methods=['POST'])
def generate_api_key():
    if not is_master():
        return jsonify({"success": False, "error": "Unauthorized"})
    new_key = "clk_" + _secrets.token_hex(32)
    set_setting("api_access_key", new_key)
    return jsonify({"success": True, "api_key": new_key})

@app.route('/admin/api/revoke', methods=['POST'])
def revoke_api_key():
    if not is_master():
        return jsonify({"success": False, "error": "Unauthorized"})
    set_setting("api_access_key", "")
    return jsonify({"success": True})

# ─── ADMIN: CLUBS ─────────────────────────────────────────────────────────────
@app.route('/admin/clubs')
def admin_clubs():
    if not session.get("logged") or not is_admin_level():
        return redirect(url_for("login"))
    return render_template('admin_clubs.html', clubs=Club.query.all())

@app.route('/admin/clubs/new', methods=['POST'])
def new_club():
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    name = request.form.get('name', '').strip()
    if not name: return jsonify({"success": False, "error": "Name is required"})
    if Club.query.filter_by(name=name).first():
        return jsonify({"success": False, "error": "Club already exists"})
    club = Club(name=name, address=request.form.get('address', '').strip())
    db.session.add(club)
    db.session.commit()
    return jsonify({"success": True, "club": club.to_dict()})

@app.route('/admin/clubs/edit/<int:club_id>', methods=['POST'])
def edit_club(club_id):
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    club = Club.query.get_or_404(club_id)
    club.name    = request.form.get('name', club.name).strip()
    club.address = request.form.get('address', club.address).strip()
    club.active  = request.form.get('active', 'true').lower() == 'true'
    db.session.commit()
    return jsonify({"success": True, "club": club.to_dict()})

@app.route('/admin/clubs/delete/<int:club_id>', methods=['POST'])
def delete_club(club_id):
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    club = Club.query.get_or_404(club_id)
    db.session.delete(club)
    db.session.commit()
    return jsonify({"success": True})

# ─── ADMIN: PACKAGES ──────────────────────────────────────────────────────────
@app.route('/admin/packages')
def admin_packages():
    if not session.get("logged") or not is_admin_level(): return redirect(url_for("login"))
    return render_template('admin_packages.html',
                           packages=Package.query.order_by(Package.club_id, Package.name).all(),
                           clubs=Club.query.filter_by(active=True).order_by(Club.name).all())

@app.route('/admin/packages/new', methods=['POST'])
def new_package():
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    name = request.form.get('name', '').strip()
    if not name: return jsonify({"success": False, "error": "Name is required"})
    try: _price = float(request.form.get('price') or 0)
    except (ValueError, TypeError): _price = 0.0
    try: _maxg = int(request.form.get('max_guests') or 0)
    except (ValueError, TypeError): _maxg = 0
    _club = request.form.get('club_id', '').strip()
    pkg = Package(name=name, description=request.form.get('description','').strip(),
                  price=_price, max_guests=_maxg,
                  checkout_url=request.form.get('checkout_url','').strip(),
                  club_id=int(_club) if _club else None)
    db.session.add(pkg); db.session.commit()
    return jsonify({"success": True, "package": pkg.to_dict()})

@app.route('/admin/packages/edit/<int:pkg_id>', methods=['POST'])
def edit_package(pkg_id):
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    pkg = Package.query.get_or_404(pkg_id)
    pkg.name               = request.form.get('name', pkg.name).strip()
    pkg.description        = request.form.get('description', pkg.description).strip()
    try: pkg.price = float(request.form.get('price') or pkg.price or 0)
    except (ValueError, TypeError): pass
    try: pkg.max_guests = int(request.form.get('max_guests') or pkg.max_guests or 0)
    except (ValueError, TypeError): pass
    pkg.active             = request.form.get('active', 'true').lower() == 'true'
    pkg.checkout_url = request.form.get('checkout_url', pkg.checkout_url).strip()
    if 'club_id' in request.form:
        _c = request.form.get('club_id', '').strip()
        pkg.club_id = int(_c) if _c else None
    db.session.commit()
    return jsonify({"success": True, "package": pkg.to_dict()})

@app.route('/admin/packages/delete/<int:pkg_id>', methods=['POST'])
def delete_package(pkg_id):
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    pkg = Package.query.get_or_404(pkg_id)
    db.session.delete(pkg); db.session.commit()
    return jsonify({"success": True})

# ─── ADMIN: DRIVERS ───────────────────────────────────────────────────────────
import uuid as _uuid
from werkzeug.utils import secure_filename
from flask import send_from_directory

ALLOWED_PHOTO_EXT = {'png', 'jpg', 'jpeg', 'webp', 'gif'}

def save_car_photo(file_storage):
    """Save an uploaded car photo and return its filename, or '' if none."""
    if not file_storage or file_storage.filename == '':
        return ''
    ext = file_storage.filename.rsplit('.', 1)[-1].lower() if '.' in file_storage.filename else ''
    if ext not in ALLOWED_PHOTO_EXT:
        return ''
    fname = f"{_uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(UPLOAD_FOLDER, fname))
    return fname

@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static', 'img'), 'favicon.ico')

@app.route('/about')
@app.route('/welcome')
def landing():
    # Public landing / about page (no login required)
    return render_template('landing.html')

@app.route('/manifest.webmanifest')
def pwa_manifest():
    manifest = {
        "name": "ClubLifter",
        "short_name": "ClubLifter",
        "description": "VIP transport & nightclub management",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#080b12",
        "theme_color": "#080b12",
        "icons": [
            {"src": "/static/img/pwa-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/img/pwa-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/img/pwa-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return app.response_class(json.dumps(manifest), mimetype="application/manifest+json")

@app.route('/service-worker.js')
def service_worker():
    # Minimal service worker — just enough to make the app installable.
    # (No offline caching per scope; kept intentionally simple.)
    sw = """
const CACHE = 'clublifter-v1';
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', e => { /* network passthrough */ });
"""
    resp = app.response_class(sw, mimetype="application/javascript")
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/admin/drivers')
def admin_drivers():
    if not session.get("logged") or not is_admin_level(): return redirect(url_for("login"))
    return render_template('admin_drivers.html',
                           drivers=Driver.query.all(),
                           cars=Car.query.all())

@app.route('/admin/drivers/new', methods=['POST'])
def new_driver():
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    name = request.form.get('name', '').strip()
    if not name: return jsonify({"success": False, "error": "Name is required"})
    if Driver.query.filter_by(name=name).first():
        return jsonify({"success": False, "error": "A driver with this name already exists"})
    driver = Driver(name=name, phone=request.form.get('phone','').strip())
    db.session.add(driver); db.session.commit()
    return jsonify({"success": True, "driver": driver.to_dict()})

@app.route('/admin/drivers/edit/<int:driver_id>', methods=['POST'])
def edit_driver(driver_id):
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    driver = Driver.query.get_or_404(driver_id)
    driver.name  = request.form.get('name', driver.name).strip()
    driver.phone = request.form.get('phone', driver.phone).strip()
    db.session.commit()
    return jsonify({"success": True, "driver": driver.to_dict()})

@app.route('/admin/drivers/delete/<int:driver_id>', methods=['POST'])
def delete_driver(driver_id):
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    driver = Driver.query.get_or_404(driver_id)
    db.session.delete(driver); db.session.commit()
    return jsonify({"success": True})

# ─── ADMIN: CARS ──────────────────────────────────────────────────────────────
@app.route('/admin/cars/new', methods=['POST'])
def new_car():
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    name = request.form.get('name', '').strip()
    if not name: return jsonify({"success": False, "error": "Car name is required (must match OneStepGPS)"})
    if Car.query.filter_by(name=name).first():
        return jsonify({"success": False, "error": "A car with this name already exists"})
    photo = save_car_photo(request.files.get('photo'))
    car = Car(
        name=name,
        model=request.form.get('model','').strip(),
        color=request.form.get('color','').strip(),
        plate=request.form.get('plate','').strip(),
        photo=photo
    )
    db.session.add(car); db.session.commit()
    return jsonify({"success": True, "car": car.to_dict()})

@app.route('/admin/cars/edit/<int:car_id>', methods=['POST'])
def edit_car(car_id):
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    car = Car.query.get_or_404(car_id)
    car.name  = request.form.get('name', car.name).strip()
    car.model = request.form.get('model', car.model).strip()
    car.color = request.form.get('color', car.color).strip()
    car.plate = request.form.get('plate', car.plate).strip()
    new_photo = save_car_photo(request.files.get('photo'))
    if new_photo:
        car.photo = new_photo
    db.session.commit()
    return jsonify({"success": True, "car": car.to_dict()})

@app.route('/admin/cars/delete/<int:car_id>', methods=['POST'])
def delete_car(car_id):
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    car = Car.query.get_or_404(car_id)
    db.session.delete(car); db.session.commit()
    return jsonify({"success": True})

# ─── ADMIN: SHIFTS (driver schedule) ──────────────────────────────────────────
@app.route('/admin/schedule')
@app.route('/admin/driver-management')
def admin_driver_management():
    if not session.get("logged") or not can_dispatch(): return redirect(url_for("login"))
    drivers = Driver.query.order_by(Driver.name).all()
    cars = Car.query.filter_by(active=True).order_by(Car.name).all()
    # Map for quick display of each driver's assigned car
    car_by_id = {c.id: c for c in Car.query.all()}
    rows = []
    for d in drivers:
        car = car_by_id.get(d.assigned_car_id) if d.assigned_car_id else None
        rows.append({"driver": d, "car": car})
    return render_template('admin_schedule.html', rows=rows, drivers=drivers, cars=cars)

@app.route('/admin/driver-management/assign', methods=['POST'])
def assign_car():
    """Assign (or clear) a driver's car — 1:1."""
    if not can_dispatch(): return jsonify({"success": False, "error": "Unauthorized"})
    driver_id = request.form.get('driver_id')
    car_id    = request.form.get('car_id', '').strip()
    drv = Driver.query.get_or_404(int(driver_id))
    if not car_id:
        drv.assigned_car_id = None
        db.session.commit()
        return jsonify({"success": True, "car": None})
    car = Car.query.get_or_404(int(car_id))
    drv.assigned_car_id = car.id
    db.session.commit()
    return jsonify({"success": True, "car": car.car_string(), "car_id": car.id})

@app.route('/admin/driver-management/toggle', methods=['POST'])
def dm_toggle_available():
    """Toggle a driver's available/busy status from Driver Management."""
    if not can_dispatch(): return jsonify({"success": False, "error": "Unauthorized"})
    drv = Driver.query.get_or_404(int(request.form.get('driver_id')))
    drv.available = not drv.available
    db.session.commit()
    fire_webhook({
        "type": "driver_availability_changed", "driver_name": drv.name,
        "driver_phone": drv.phone, "available": drv.available,
        "changed_by": session.get("username", ""), "source": "driver_management",
    })
    return jsonify({"success": True, "available": drv.available})

@app.route('/admin/schedule/new', methods=['POST'])
def new_shift():
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    driver_id = request.form.get('driver_id')
    car_id    = request.form.get('car_id')
    if not driver_id or not car_id:
        return jsonify({"success": False, "error": "Driver and car are required"})
    mode = request.form.get('mode', 'weekly')  # 'weekly' | 'specific' | 'range'
    start_time = request.form.get('start_time', '18:00').strip()
    end_time   = request.form.get('end_time', '05:30').strip()
    created = 0

    if mode == 'specific':
        # single specific date
        d = request.form.get('specific_date', '').strip()
        if not d: return jsonify({"success": False, "error": "Pick a date"})
        db.session.add(Shift(driver_id=int(driver_id), car_id=int(car_id),
                             specific_date=d, day_of_week=None,
                             start_time=start_time, end_time=end_time))
        created += 1

    elif mode == 'range':
        # a date range: create a specific-date shift for each day in [start_date, end_date]
        sd = request.form.get('start_date', '').strip()
        ed = request.form.get('end_date', '').strip()
        try:
            d0 = datetime.strptime(sd, '%Y-%m-%d').date()
            d1 = datetime.strptime(ed, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"success": False, "error": "Invalid date range"})
        if d1 < d0:
            return jsonify({"success": False, "error": "End date is before start date"})
        if (d1 - d0).days > 60:
            return jsonify({"success": False, "error": "Range too large (max 60 days)"})
        cur = d0
        while cur <= d1:
            ds = f"{cur.month:02d}/{cur.day:02d}/{cur.year}"
            db.session.add(Shift(driver_id=int(driver_id), car_id=int(car_id),
                                 specific_date=ds, day_of_week=None,
                                 start_time=start_time, end_time=end_time))
            created += 1
            cur += timedelta(days=1)

    else:  # weekly — supports multiple days at once (e.g. Mon-Fri)
        days = request.form.getlist('days_of_week')  # list of "0".."6"
        if not days:
            single = request.form.get('day_of_week')
            days = [single] if single not in (None, '') else []
        if not days:
            return jsonify({"success": False, "error": "Pick at least one day"})
        for dow in days:
            db.session.add(Shift(driver_id=int(driver_id), car_id=int(car_id),
                                 day_of_week=int(dow), specific_date=None,
                                 start_time=start_time, end_time=end_time))
            created += 1

    db.session.commit()
    return jsonify({"success": True, "created": created})

@app.route('/admin/schedule/delete/<int:shift_id>', methods=['POST'])
def delete_shift(shift_id):
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    shift = Shift.query.get_or_404(shift_id)
    db.session.delete(shift); db.session.commit()
    return jsonify({"success": True})

@app.route('/admin/schedule/toggle/<int:shift_id>', methods=['POST'])
def toggle_shift(shift_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    shift = Shift.query.get_or_404(shift_id)
    shift.active = not shift.active
    db.session.commit()
    return jsonify({"success": True, "active": shift.active})

# ─── DRIVER PORTAL ────────────────────────────────────────────────────────────
@app.route('/driver')
def driver_dashboard():
    if not session.get("logged"):
        return redirect(url_for("login"))
    if session.get("role") != "driver":
        return redirect(url_for("index"))

    driver_name = session.get("username")

    today = date.today()
    today_np = f"{today.month}/{today.day}/{today.year}"
    today_p  = f"{today.month:02d}/{today.day:02d}/{today.year}"
    today_str = today_np

    # Today's pickups for this driver. Matched by any of the driver's identities
    # (username / Driver record name) OR the car they're assigned to today, so a
    # pickup still shows up when `motorista` holds the car name or a variant.
    names, car_names = driver_scope(driver_name)
    all_customers = Customer.query.order_by(Customer.pickup_datetime).all()
    my_customers  = [c for c in all_customers
                     if (today_np in (c.pickup_datetime or "") or today_p in (c.pickup_datetime or ""))
                     and ((c.motorista and c.motorista in names)
                          or (c.car_name and c.car_name in car_names))]

    # Get driver availability status (loose match on the Driver record)
    driver_profile = Driver.query.filter_by(name=driver_name).first()
    if not driver_profile:
        for n in names:
            driver_profile = Driver.query.filter_by(name=n).first()
            if driver_profile:
                break
    driver_available = driver_profile.available if driver_profile else True

    # Driver's live car GPS (for the map) — look up by the car on their pickups or shift
    car_lat = car_lng = None
    car_name = None
    for c in my_customers:
        if c.car_name:
            car_name = c.car_name
            break
    try:
        if car_name:
            headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
            res = requests.get("https://track.onestepgps.com/v3/api/public/device-info?lat_lng=1",
                               headers=headers_api, timeout=8).json()
            lista = res if isinstance(res, list) else [res]
            for v in lista:
                if v.get('display_name', '') == car_name:
                    car_lat = v.get('lat') or v.get('last_tap', {}).get('lat')
                    car_lng = v.get('lng') or v.get('last_tap', {}).get('lng')
                    break
    except Exception:
        pass

    # Build pickup list with coords + distance from car
    pickups = []
    for c in my_customers:
        p_lat = p_lng = None
        # geocode pickup address (best-effort, cached would be better but fine for now)
        try:
            if c.endereco:
                encoded = urllib.parse.quote(c.endereco)
                geo = requests.get(
                    f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1",
                    headers={'User-Agent': 'ClubLifter_Driver'}, timeout=8).json()
                if geo:
                    p_lat = float(geo[0]['lat']); p_lng = float(geo[0]['lon'])
        except Exception:
            pass
        dist_mi = None
        if car_lat and car_lng and p_lat and p_lng:
            dist_mi = round(calcular_distancia(float(car_lat), float(car_lng), p_lat, p_lng), 1)
        time_part = ""
        if c.pickup_datetime and len(c.pickup_datetime.split(' ')) >= 3:
            parts = c.pickup_datetime.split(' '); time_part = f"{parts[1]} {parts[2]}"
        pickups.append({
            "id": c.id, "nome": c.nome, "endereco": c.endereco, "destination": c.destination,
            "package": c.package, "guests": c.guests, "details": c.details,
            "time": time_part, "status": c.status,
            "dispatch_status": c.dispatch_status,
            "dropped_at": vegas_time(c.dropped_off_at),
            "picked_at": vegas_time(c.picked_up_at),
            "lat": p_lat, "lng": p_lng, "dist_mi": dist_mi,
            "car_string": c.car_string_val,
            "streetview": streetview_for(c.endereco),
            "_dt": parse_pickup_datetime(c.pickup_datetime),
        })

    # ── GROUP PICKUPS INTO ROUTES ──────────────────────────────────────────────
    # A route = same destination + pickup times within 90 min of each other.
    # Stops are ordered by nearest-neighbor starting from the driver's car position.
    ROUTE_WINDOW_MIN = 90

    def order_route(stops, start_lat, start_lng):
        """Nearest-neighbor ordering of stops from a start point."""
        remaining = [s for s in stops]
        ordered = []
        cur_lat, cur_lng = start_lat, start_lng
        while remaining:
            if cur_lat and cur_lng:
                # pick nearest with known coords; those without coords go last
                with_coords = [s for s in remaining if s["lat"] and s["lng"]]
                if with_coords:
                    nxt = min(with_coords, key=lambda s: calcular_distancia(cur_lat, cur_lng, s["lat"], s["lng"]))
                else:
                    nxt = remaining[0]
            else:
                nxt = remaining[0]
            ordered.append(nxt)
            remaining.remove(nxt)
            if nxt["lat"] and nxt["lng"]:
                cur_lat, cur_lng = nxt["lat"], nxt["lng"]
        return ordered

    # Sort all pickups by time first
    timed = sorted(pickups, key=lambda p: (p["_dt"] or datetime.max))
    routes = []
    used = set()
    for i, p in enumerate(timed):
        if p["id"] in used:
            continue
        group = [p]; used.add(p["id"])
        for q in timed[i+1:]:
            if q["id"] in used:
                continue
            same_dest = (q["destination"] or "") == (p["destination"] or "")
            close_time = True
            if p["_dt"] and q["_dt"]:
                close_time = abs((q["_dt"] - p["_dt"]).total_seconds()) <= ROUTE_WINDOW_MIN * 60
            if same_dest and close_time:
                group.append(q); used.add(q["id"])
        # order the stops
        ordered = order_route(group, car_lat, car_lng) if len(group) > 1 else group
        # assign stop numbers
        for idx, s in enumerate(ordered, 1):
            s["stop_no"] = idx
        routes.append({
            "destination": p["destination"] or "—",
            "stops": ordered,
            "is_multi": len(ordered) > 1,
            "total_guests": sum((s["guests"] or 0) for s in ordered),
            "start_time": ordered[0]["time"] if ordered else "",
        })

    # strip internal _dt before sending to template (not JSON serializable)
    for r in routes:
        for s in r["stops"]:
            s.pop("_dt", None)
    for p in pickups:
        p.pop("_dt", None)

    return render_template('driver_dashboard.html',
        pickups=pickups,
        routes=routes,
        driver_name=driver_name,
        driver_available=driver_available,
        today_str=today_str,
        car_lat=car_lat, car_lng=car_lng
    )

@app.route('/driver/pickup/<int:customer_id>', methods=['POST'])
def mark_picked_up(customer_id):
    """Driver marks a customer as picked up."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    customer = Customer.query.get_or_404(customer_id)
    # Verify the driver owns this customer
    if not _driver_owns(customer):
        return jsonify({"success": False, "error": "Not your customer"})
    customer.status = "picked_up"
    customer.picked_up_at = datetime.utcnow()
    db.session.commit()
    fire_webhook({
        "type":           "picked_up",
        "customer_id":    customer.id,
        "customer_name":  customer.nome,
        "driver_name":    customer.motorista,
        "destination":    customer.destination,
        "picked_up_at":   vegas_datetime(customer.picked_up_at),
    })
    return jsonify({"success": True, "picked_up_at": vegas_time(customer.picked_up_at)})

@app.route('/driver/dropoff/<int:customer_id>', methods=['POST'])
def driver_dropoff(customer_id):
    """Driver confirms the guest was dropped off at the venue.
    Records the time and, when GPS is available, verifies the car was actually
    at the destination club (so 'made it to property' isn't just self-reported)."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    if not _driver_owns(c):
        return jsonify({"success": False, "error": "Not your customer"})

    c.status = "dropped_off"
    c.dropped_off_at = datetime.utcnow()
    if not c.picked_up_at:
        c.picked_up_at = c.dropped_off_at
    c.club_status = "arrived"

    # GPS verification: how far is the car from the destination club right now?
    verified, dist_mi = False, 0.0
    try:
        club = Club.query.filter_by(name=c.destination).first()
        if club and club.address and c.car_name:
            headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
            res = requests.get("https://track.onestepgps.com/v3/api/public/device-info?lat_lng=1",
                               headers=headers_api, timeout=8).json()
            lista = res if isinstance(res, list) else [res]
            car_lat = car_lng = None
            for v in lista:
                if _norm_name(v.get('display_name', '')) == _norm_name(c.car_name):
                    car_lat = v.get('lat') or v.get('last_tap', {}).get('lat')
                    car_lng = v.get('lng') or v.get('last_tap', {}).get('lng')
                    break
            if car_lat and car_lng:
                encoded = urllib.parse.quote(club.address)
                geo = requests.get(
                    f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1",
                    headers={'User-Agent': 'ClubLifter_App'}, timeout=8).json()
                if geo:
                    dist_mi = round(calcular_distancia(float(car_lat), float(car_lng),
                                                       float(geo[0]['lat']), float(geo[0]['lon'])), 2)
                    verified = dist_mi <= 0.25   # within ~400 m of the venue
    except Exception as e:
        print(f"[DROPOFF] GPS verification skipped: {e}", flush=True)

    c.dropoff_verified = verified
    c.dropoff_distance_mi = dist_mi
    db.session.commit()

    fire_webhook({
        "type":            "dropped_off",
        "customer_id":     c.id,
        "customer_name":   c.nome,
        "customer_phone":  c.phone,
        "driver_name":     c.motorista,
        "driver_car":      c.car_string_val or "",
        "destination":     c.destination,
        "dropped_off_at":  vegas_datetime(c.dropped_off_at),
        "verified_at_venue": verified,
        "distance_to_venue_mi": dist_mi,
        "guests":          c.guests,
    })
    return jsonify({"success": True, "dropped_off_at": vegas_time(c.dropped_off_at),
                    "verified": verified, "distance_mi": dist_mi})

@app.route('/driver/report-problem', methods=['POST'])
def report_problem():
    """
    Driver reports a problem:
    1. Marks driver as unavailable
    2. Reassigns all their 'scheduled' customers to the next available driver
    3. Fires webhook for each reassigned customer
    """
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})

    driver_name = session.get("username")
    driver_profile = Driver.query.filter_by(name=driver_name).first()
    if not driver_profile:
        return jsonify({"success": False, "error": "Driver profile not found"})

    # Disable the driver
    driver_profile.available = False
    db.session.commit()

    # Get all their remaining scheduled pickups
    pending = Customer.query.filter_by(motorista=driver_name, status='scheduled').all()
    reassigned = []

    for customer in pending:
        requested_dt = parse_pickup_datetime(customer.pickup_datetime)

        # Find the next available driver (excluding the current one)
        all_drivers = Driver.query.filter(
            Driver.name != driver_name,
            Driver.available == True
        ).all()

        new_driver = None
        for d in all_drivers:
            if not driver_is_busy(d.name, requested_dt):
                new_driver = d
                break

        if new_driver:
            old_driver = customer.motorista
            customer.motorista       = new_driver.name
            customer.motorista_phone = new_driver.phone
            db.session.commit()

            # Notify via webhook
            fire_webhook({
                "event":           "driver_reassignment",
                "customer_id":     customer.id,
                "customer_name":   customer.nome,
                "customer_phone":  customer.phone,
                "pickup_address":  customer.endereco,
                "pickup_datetime": customer.pickup_datetime,
                "destination":     customer.destination,
                "package":         customer.package,
                "guests":          customer.guests,
                "old_driver":      old_driver,
                "new_driver_name": new_driver.name,
                "new_driver_phone": new_driver.phone,
            })
            reassigned.append({"customer": customer.nome, "new_driver": new_driver.name})

    return jsonify({
        "success": True,
        "disabled": True,
        "reassigned": reassigned,
        "message": f"You are now marked as unavailable. {len(reassigned)} pickup(s) were reassigned."
    })

@app.route('/driver/back-online', methods=['POST'])
def driver_back_online():
    """Driver confirms they are back and available."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})

    driver_name = session.get("username")
    driver_profile = Driver.query.filter_by(name=driver_name).first()
    if not driver_profile:
        return jsonify({"success": False, "error": "Driver profile not found"})

    driver_profile.available = True
    db.session.commit()

    fire_webhook({
        "event":       "driver_back_online",
        "driver_name": driver_name,
        "driver_phone": driver_profile.phone,
        "timestamp":   datetime.utcnow().isoformat()
    })

    return jsonify({"success": True, "message": "You are back online!"})

# ─── DRIVER PICKUP ACTIONS (enroute / I'm here / custom message) ───────────────
def driver_scope(username):
    """Everything that identifies a logged-in driver's pickups.

    A pickup's `motorista` field isn't always the driver's username: it can hold the
    Driver record name, or (when assignment falls back to nearest car) the vehicle's
    GPS display name. So we resolve the driver to a set of names AND the cars they're
    on today, and match pickups against any of them.
    Returns (names, car_names).
    """
    names = set()
    car_names = set()
    uname = (username or "").strip()
    if not uname:
        return names, car_names
    names.add(uname)

    drv = Driver.query.filter_by(name=uname).first()
    if not drv:
        # loose match (e.g. Driver "tj.madzhary" vs user "tj.madzharyan")
        key = _norm_name(uname)
        for d in Driver.query.all():
            dk = _norm_name(d.name)
            if dk and (dk == key or dk in key or key in dk):
                drv = d
                break
    if drv:
        names.add(drv.name)
        today = date.today()
        today_p = f"{today.month:02d}/{today.day:02d}/{today.year}"
        dow = today.weekday()
        # Direct 1:1 car assignment
        if drv.assigned_car_id:
            ac = Car.query.get(drv.assigned_car_id)
            if ac:
                car_names.add(ac.name)
                names.add(ac.name)
        shifts = (Shift.query.filter_by(driver_id=drv.id, active=True)
                  .filter((Shift.specific_date == today_p) | (Shift.day_of_week == dow)).all())
        for sh in shifts:
            car = Car.query.get(sh.car_id)
            if car:
                car_names.add(car.name)
                # the car's GPS display name may differ slightly
                names.add(car.name)
    return names, car_names

def get_driver_record(username):
    """Driver row for a logged-in username, tolerating small name differences."""
    uname = (username or "").strip()
    if not uname:
        return None
    drv = Driver.query.filter_by(name=uname).first()
    if drv:
        return drv
    key = _norm_name(uname)
    for d in Driver.query.all():
        dk = _norm_name(d.name)
        if dk and (dk == key or dk in key or key in dk):
            return d
    return None

def customer_belongs_to_driver(c, username):
    names, car_names = driver_scope(username)
    if c.motorista and c.motorista in names:
        return True
    if c.car_name and c.car_name in car_names:
        return True
    return False

def _driver_owns(customer):
    return customer_belongs_to_driver(customer, session.get("username"))

@app.route('/driver/available', methods=['POST'])
def driver_set_available():
    """Driver toggles their own availability (finished a pickup → ready for next)."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    driver_name = session.get("username")
    prof = get_driver_record(driver_name)
    if not prof:
        return jsonify({"success": False, "error": "Driver profile not found"})
    prof.available = request.form.get('available', 'true').lower() == 'true'
    db.session.commit()
    if prof.available:
        fire_webhook({"event": "driver_back_online", "driver_name": driver_name,
                      "driver_phone": prof.phone, "timestamp": datetime.utcnow().isoformat()})
    return jsonify({"success": True, "available": prof.available})

@app.route('/driver/enroute/<int:customer_id>', methods=['POST'])
def driver_enroute(customer_id):
    """I'm enroute → text the customer that the driver is on the way."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    if not _driver_owns(c):
        return jsonify({"success": False, "error": "Not your pickup"})
    c.dispatch_status = "enroute"
    db.session.commit()
    car_str = c.car_string_val or "your ride"
    enroute_msg = f"Hi {c.nome}! Your ClubLifter driver {c.motorista} is on the way in a {car_str}. See you soon!"
    fire_webhook({
        "type":            "enroute",
        "customer_id":     c.id,
        "customer_name":   c.nome,
        "customer_phone":  c.phone,
        "customer_phones": c.get_phones(),
        "driver_name":     c.motorista,
        "driver_phone":    c.motorista_phone,
        "driver_car":      car_str,
        "pickup_address":  c.endereco,
        "destination":     c.destination,
        "pickup_datetime": c.pickup_datetime,
        "message":         enroute_msg,
    })
    send_sms_many(c.get_phones() or [c.phone], enroute_msg)
    return jsonify({"success": True})

@app.route('/driver/imhere/<int:customer_id>', methods=['POST'])
def driver_imhere(customer_id):
    """I'm here → text the customer the driver arrived; optional photo (auto-deletes in 24h)."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    if not _driver_owns(c):
        return jsonify({"success": False, "error": "Not your pickup"})

    photo_url = ""
    photo_b64 = request.form.get('photo_b64', '').strip()
    if photo_b64:
        # Store base64 in DB with timestamp; served via /here-photo/<id>, purged after 24h
        c.here_photo = photo_b64
        c.here_photo_at = datetime.utcnow()
        db.session.commit()
        photo_url = f"{PUBLIC_BASE_URL}/here-photo/{c.id}"

    car_str = c.car_string_val or "your ride"
    imhere_msg = f"Hi {c.nome}! Your ClubLifter driver {c.motorista} has arrived in a {car_str}. Come on out!"
    fire_webhook({
        "type":            "imhere",
        "customer_id":     c.id,
        "customer_name":   c.nome,
        "customer_phone":  c.phone,
        "customer_phones": c.get_phones(),
        "driver_name":     c.motorista,
        "driver_phone":    c.motorista_phone,
        "driver_car":      car_str,
        "pickup_address":  c.endereco,
        "destination":     c.destination,
        "photo_url":       photo_url,
        "message":         imhere_msg,
    })
    send_sms_many(c.get_phones() or [c.phone], imhere_msg, photo_url)
    return jsonify({"success": True, "photo_url": photo_url})

@app.route('/driver/customsg/<int:customer_id>', methods=['POST'])
def driver_customsg(customer_id):
    """Custom → driver types a short message sent to the customer via SMS."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    if not _driver_owns(c):
        return jsonify({"success": False, "error": "Not your pickup"})
    msg = (request.form.get('message', '') or "").strip()
    if not msg:
        return jsonify({"success": False, "error": "Message is empty"})
    if len(msg) > 300:
        msg = msg[:300]
    fire_webhook({
        "type":            "customsg",
        "customer_id":     c.id,
        "customer_name":   c.nome,
        "customer_phone":  c.phone,
        "customer_phones": c.get_phones(),
        "driver_name":     c.motorista,
        "destination":     c.destination,
        "custom_msg":      msg,
        "message":         msg,
    })
    send_sms_many(c.get_phones() or [c.phone], msg)
    return jsonify({"success": True})

@app.route('/driver/startcall/<int:customer_id>', methods=['POST'])
def driver_startcall(customer_id):
    """Start Call → notifies the call center (Aloware) to bridge a 3-way call
    between driver and customer, masking the customer's number from the driver."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    if not _driver_owns(c):
        return jsonify({"success": False, "error": "Not your pickup"})
    fire_webhook({
        "type":            "aloware",
        "customer_id":     c.id,
        "customer_name":   c.nome,
        "customer_phone":  c.phone,
        "customer_phones": c.get_phones(),
        "driver_name":     c.motorista,
        "driver_phone":    c.motorista_phone,
        "pickup_address":  c.endereco,
        "destination":     c.destination,
        "pickup_datetime": c.pickup_datetime,
        "car":             c.car_string_val,
        "package":         c.package,
        "guests":          c.guests,
    })
    return jsonify({"success": True})

@app.route('/driver/cars')
def driver_cars():
    """List available cars for the driver's car-switch picker, flagging which are
    currently assigned to another driver (via today's shifts)."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    driver_name = session.get("username")
    me = get_driver_record(driver_name)
    today = date.today()
    dow = today.weekday()  # 0=Mon
    today_p = f"{today.month:02d}/{today.day:02d}/{today.year}"

    cars = []
    for car in Car.query.filter_by(active=True).all():
        # who is assigned to this car today (by shift)?
        assigned_to = None
        shift = (Shift.query
                 .filter_by(car_id=car.id, active=True)
                 .filter((Shift.day_of_week == dow) | (Shift.specific_date == today_p))
                 .first())
        if shift:
            drv = Driver.query.get(shift.driver_id)
            if drv and drv.name != driver_name:
                assigned_to = drv.name
        cars.append({
            "id": car.id, "name": car.name, "car_string": car.car_string(),
            "assigned_to": assigned_to,
            "is_mine": bool(shift and Driver.query.get(shift.driver_id) and Driver.query.get(shift.driver_id).name == driver_name),
        })
    return jsonify({"success": True, "cars": cars})

@app.route('/driver/switch-car', methods=['POST'])
def driver_switch_car():
    """Driver switches to a different car. If it's currently assigned to another
    driver today, the frontend confirms first (confirm=true)."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    driver_name = session.get("username")
    me = get_driver_record(driver_name)
    if not me:
        return jsonify({"success": False, "error": "Driver profile not found"})
    car_id = request.form.get('car_id')
    confirm = request.form.get('confirm', 'false').lower() == 'true'
    car = Car.query.get_or_404(int(car_id))

    today = date.today()
    dow = today.weekday()
    today_p = f"{today.month:02d}/{today.day:02d}/{today.year}"

    # Is the car assigned to someone else today?
    existing = (Shift.query
                .filter_by(car_id=car.id, active=True)
                .filter((Shift.day_of_week == dow) | (Shift.specific_date == today_p))
                .first())
    other_driver = None
    if existing:
        drv = Driver.query.get(existing.driver_id)
        if drv and drv.name != driver_name:
            other_driver = drv.name

    if other_driver and not confirm:
        # Ask the frontend to confirm the takeover
        return jsonify({"success": False, "needs_confirm": True,
                        "assigned_to": other_driver,
                        "car_name": car.car_string()})

    # Perform the switch: create a specific-date shift for me today with this car.
    # Deactivate the other driver's shift for this car today (if taking over).
    if existing and other_driver and confirm:
        existing.active = False

    # Remove any of MY existing shifts for today so I only hold one car
    my_today = (Shift.query
                .filter_by(driver_id=me.id, active=True)
                .filter((Shift.day_of_week == dow) | (Shift.specific_date == today_p))
                .all())
    old_start, old_end = '18:00', '05:30'
    for sh in my_today:
        old_start, old_end = sh.start_time, sh.end_time
        sh.active = False

    db.session.add(Shift(driver_id=me.id, car_id=car.id, specific_date=today_p,
                         day_of_week=None, start_time=old_start, end_time=old_end, active=True))
    db.session.commit()

    # Reassign today's not-yet-picked-up customers of mine to the new car name/string
    updated = 0
    for c in Customer.query.filter_by(motorista=driver_name, status='scheduled').all():
        if today_p in (c.pickup_datetime or "") or f"{today.month}/{today.day}/{today.year}" in (c.pickup_datetime or ""):
            c.car_name = car.name
            c.car_string_val = car.car_string()
            updated += 1
    db.session.commit()

    fire_webhook({
        "type":         "driver_car_switch",
        "driver_name":  driver_name,
        "new_car":      car.car_string(),
        "took_over_from": other_driver or "",
        "customers_updated": updated,
    })
    return jsonify({"success": True, "car": car.car_string(), "took_over_from": other_driver})

@app.route('/here-photo/<int:customer_id>')
def serve_here_photo(customer_id):
    """Serves the driver's 'I'm here' photo (public, so Twilio MMS can fetch it).
    Returns 404 once the photo has been purged (after 24h)."""
    c = Customer.query.get_or_404(customer_id)
    if not c.here_photo:
        return ("Not found", 404)
    try:
        raw = c.here_photo
        if ',' in raw and raw.strip().startswith('data:'):
            raw = raw.split(',', 1)[1]
        img_bytes = base64.b64decode(raw)
        return app.response_class(img_bytes, mimetype='image/jpeg')
    except Exception:
        return ("Not found", 404)



@app.route('/api/packages', methods=['GET'])
@require_api_key
def api_packages():
    return jsonify([p.to_dict() for p in Package.query.filter_by(active=True).all()])

@app.route('/api/drivers', methods=['GET'])
@require_api_key
def api_drivers():
    return jsonify([d.to_dict() for d in Driver.query.all()])

@app.route('/api/clubs', methods=['GET'])
@require_api_key
def api_clubs():
    return jsonify([c.to_dict() for c in Club.query.filter_by(active=True).all()])

# ══════════════════════════════════════════════════════════════════════════════
# CARTVIP INTEGRATION API (v1) — external partners POST here with X-API-Key
# ══════════════════════════════════════════════════════════════════════════════

def _assign_driver_and_car(lat_cli, lng_cli, requested_dt):
    """Shared driver/car assignment logic. Returns dict with assignment details."""
    headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    try:
        res_v = requests.get(
            "https://track.onestepgps.com/v3/api/public/device-info?lat_lng=1",
            headers=headers_api, timeout=10
        ).json()
    except Exception:
        res_v = []
    lista = res_v if isinstance(res_v, list) else [res_v]
    gps_by_name = {}
    for v in lista:
        v_lat = v.get('lat') or v.get('last_tap', {}).get('lat')
        v_lng = v.get('lng') or v.get('last_tap', {}).get('lng')
        if v_lat and v_lng:
            gps_by_name[v.get('display_name', '')] = {"lat": float(v_lat), "lng": float(v_lng)}

    melhor_v, menor_d, motorista_coords, chosen_car = "Unavailable", float('inf'), None, None

    shifts = get_scheduled_shifts(requested_dt)
    if shifts:
        best = None
        for sh in shifts:
            drv, car = sh.driver, sh.car
            if not drv or not car or not drv.available:
                continue
            if driver_is_busy(drv.name, requested_dt):
                continue
            coords = gps_by_name.get(car.name)
            dist = calcular_distancia(lat_cli, lng_cli, coords["lat"], coords["lng"]) if coords else float('inf')
            if best is None or dist < best[1]:
                best = (drv, dist, coords, car)
        if best:
            melhor_v, menor_d, motorista_coords, chosen_car = best

    if melhor_v == "Unavailable":
        candidates = []
        for name, coords in gps_by_name.items():
            d = calcular_distancia(lat_cli, lng_cli, coords["lat"], coords["lng"])
            candidates.append((d, name, coords))
        candidates.sort(key=lambda x: x[0])
        for dist, name, coords in candidates:
            car = Car.query.filter_by(name=name).first()
            if car and not car.active:
                continue
            melhor_v, menor_d, motorista_coords, chosen_car = name, dist, coords, car
            break

    driver_profile = Driver.query.filter_by(name=melhor_v).first() if melhor_v != "Unavailable" else None
    return {
        "driver_name": melhor_v,
        "driver_phone": driver_profile.phone if driver_profile else "",
        "distance_km": round(menor_d, 2) if menor_d != float('inf') else 0,
        "driver_coords": motorista_coords,
        "car": chosen_car,
    }

@app.route('/api/v1/schedule', methods=['POST'])
@require_api_key
def api_v1_schedule():
    """
    Schedule a customer WITH transport. CartVIP posts customer + pickup address.
    Body (JSON):
      customer_name (required), customer_phone, extra_phones [list],
      pickup_address (required), details, package, guests,
      pickup_datetime (required, "MM/DD/YYYY HH:MM AM/PM"), destination
    """
    data = request.get_json(silent=True) or {}
    name = (data.get("customer_name") or "").strip()
    pickup_address = (data.get("pickup_address") or "").strip()
    pickup_datetime = (data.get("pickup_datetime") or "").strip()
    if not name or not pickup_address or not pickup_datetime:
        return jsonify({"success": False, "error": "customer_name, pickup_address and pickup_datetime are required"}), 400

    client_phone = (data.get("customer_phone") or "").strip()
    extra_phones = data.get("extra_phones") or []
    if not isinstance(extra_phones, list):
        extra_phones = []
    all_phones = ([client_phone] if client_phone else []) + [p for p in extra_phones if p]
    details     = (data.get("details") or "").strip()
    package     = (data.get("package") or "").strip()
    guests      = int(data.get("guests") or 0)
    destination = (data.get("destination") or "").strip()

    requested_dt = parse_pickup_datetime(pickup_datetime)

    # Geocode
    try:
        encoded = urllib.parse.quote(pickup_address)
        geo = requests.get(
            f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1",
            headers={'User-Agent': 'ClubLifter_CartVIP'}, timeout=10
        ).json()
        if not geo:
            return jsonify({"success": False, "error": "Address not found"}), 422
        lat_cli, lng_cli = float(geo[0]['lat']), float(geo[0]['lon'])
    except Exception as e:
        return jsonify({"success": False, "error": f"Geocoding failed: {e}"}), 500

    a = _assign_driver_and_car(lat_cli, lng_cli, requested_dt)
    chosen_car = a["car"]
    car_string = chosen_car.car_string() if chosen_car else ""
    car_photo  = chosen_car.photo if chosen_car else ""
    car_photo_url = f"{PUBLIC_BASE_URL}/uploads/{car_photo}" if car_photo else ""

    # Register marker on OneStepGPS (best-effort)
    try:
        headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        requests.post(URL_API, json={
            "display_name": name, "active": True, "status": "active", "marker_type": "point",
            "detail": {"description": pickup_address, "lat_lng": {"lat": lat_cli, "lng": lng_cli}}
        }, headers=headers_api, timeout=8)
    except Exception:
        pass

    customer = Customer(
        nome=name, phone=client_phone, phones_json=json.dumps([p for p in extra_phones if p]),
        endereco=pickup_address, details=details,
        motorista=a["driver_name"], motorista_phone=a["driver_phone"],
        car_name=(chosen_car.name if chosen_car else ""),
        car_string_val=car_string, car_photo=car_photo,
        distancia=a["distance_km"], package=package, guests=guests,
        pickup_datetime=pickup_datetime, destination=destination,
        needs_transport=True, club_status="coming",
        promoter="cartvip", status='scheduled', created_at=datetime.utcnow()
    )
    db.session.add(customer); db.session.commit()

    # Fire the scheduled SMS webhook (same as the web flow)
    time_part = ""
    if pickup_datetime and len(pickup_datetime.split(' ')) >= 3:
        parts = pickup_datetime.split(' ')
        time_part = f"{parts[1]} {parts[2]}"
    sms_text = (f"Hi {name}! Your ClubLifter ride is booked. {a['driver_name']} will pick you up"
                f"{' at ' + time_part if time_part else ''}"
                f"{' in a ' + car_string if car_string else ''}. See you soon!")
    fire_webhook({
        "type": "scheduled", "source": "cartvip",
        "customer_name": name, "customer_phone": client_phone,
        "driver_name": a["driver_name"], "driver_car": car_string or "N/A",
        "driver_car_photo_url": car_photo_url,
        "pickup_datetime": pickup_datetime, "pickup_time": time_part,
        "destination": destination, "message": sms_text, "customer_id": customer.id,
    })
    # Direct SMS to the customer for API-sourced bookings as well
    _api_phones = [client_phone] + [p for p in (extra_phones or []) if p]
    send_sms_many(_api_phones, sms_text, car_photo_url)

    return jsonify({
        "success": True,
        "customer_id": customer.id,
        "driver_name": a["driver_name"],
        "driver_phone": a["driver_phone"],
        "car": car_string,
        "distance_km": a["distance_km"],
        "status": "scheduled"
    })

@app.route('/api/v1/walkin', methods=['POST'])
@require_api_key
def api_v1_walkin():
    """Register a walk-in (no transport). Body: customer_name (req), customer_phone,
    extra_phones[], details, package, guests, destination, pickup_datetime."""
    data = request.get_json(silent=True) or {}
    name = (data.get("customer_name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "customer_name is required"}), 400
    client_phone = (data.get("customer_phone") or "").strip()
    extra_phones = data.get("extra_phones") or []
    if not isinstance(extra_phones, list):
        extra_phones = []

    customer = Customer(
        nome=name, phone=client_phone, phones_json=json.dumps([p for p in extra_phones if p]),
        endereco="(walk-in)", details=(data.get("details") or "").strip(),
        motorista="(walk-in)", motorista_phone="",
        distancia=0, package=(data.get("package") or "").strip(),
        guests=int(data.get("guests") or 0),
        pickup_datetime=(data.get("pickup_datetime") or "").strip(),
        destination=(data.get("destination") or "").strip(),
        needs_transport=False, club_status="coming",
        promoter="cartvip", status='scheduled', created_at=datetime.utcnow()
    )
    db.session.add(customer); db.session.commit()

    fire_webhook({
        "event": "walk_in_registered", "source": "cartvip",
        "customer_id": customer.id, "customer_name": name,
        "customer_phone": client_phone, "destination": customer.destination,
        "package": customer.package, "guests": customer.guests,
    })
    return jsonify({"success": True, "customer_id": customer.id, "status": "scheduled"})

@app.route('/api/v1/customer/<int:customer_id>', methods=['GET'])
@require_api_key
def api_v1_customer_status(customer_id):
    """Get the current status of a customer/booking."""
    c = Customer.query.get(customer_id)
    if not c:
        return jsonify({"success": False, "error": "Customer not found"}), 404
    return jsonify({
        "success": True,
        "customer_id": c.id, "customer_name": c.nome,
        "needs_transport": c.needs_transport,
        "driver_name": c.motorista, "driver_phone": c.motorista_phone,
        "car": c.car_string_val, "distance_km": c.distancia,
        "pickup_datetime": c.pickup_datetime, "destination": c.destination,
        "pickup_status": c.status,          # scheduled | picked_up
        "club_status": c.club_status,       # coming | arrived | left
        "package": c.package, "guests": c.guests,
    })

@app.route('/api/v1/customer/<int:customer_id>/cancel', methods=['POST'])
@require_api_key
def api_v1_cancel(customer_id):
    """Cancel/remove a booking."""
    c = Customer.query.get(customer_id)
    if not c:
        return jsonify({"success": False, "error": "Customer not found"}), 404
    db.session.delete(c); db.session.commit()
    fire_webhook({"event": "booking_cancelled", "source": "cartvip", "customer_id": customer_id})
    return jsonify({"success": True, "cancelled": customer_id})

@app.route('/api/v1/packages', methods=['GET'])
@require_api_key
def api_v1_packages():
    """List active packages for CartVIP's checkout."""
    return jsonify({"success": True, "packages": [
        {"name": p.name, "description": p.description, "price": p.price,
         "max_guests": p.max_guests, "checkout_url": p.checkout_url}
        for p in Package.query.filter_by(active=True).all()
    ]})

@app.route('/api/v1/clubs', methods=['GET'])
@require_api_key
def api_v1_clubs():
    """List active clubs/destinations."""
    return jsonify({"success": True, "clubs": [
        {"name": c.name, "address": c.address}
        for c in Club.query.filter_by(active=True).all()
    ]})

# ─── LIVE DRIVER TRACKING (admin only) ────────────────────────────────────────
@app.route('/admin/tracking')
def admin_tracking():
    if not session.get("logged") or not can_dispatch():
        return redirect(url_for("login"))
    return render_template('admin_tracking.html')

def _norm_name(s):
    """Loose comparison key for vehicle names (case/space/punctuation-insensitive)."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())

def _match_car(gps_name):
    """Find the Car record for a GPS display_name: exact first, then a loose match
    (so 'Cadillac Escalade' still matches '2017 Cadillac Escalade ESV SUV')."""
    car = Car.query.filter_by(name=gps_name).first()
    if car:
        return car
    key = _norm_name(gps_name)
    if not key:
        return None
    for c in Car.query.all():
        ck = _norm_name(c.name)
        if not ck:
            continue
        if ck == key or ck in key or key in ck:
            return c
    return None

def _driver_for_car_today(car, today_p):
    """Driver assigned to this car: direct 1:1 assignment first, then today's shift."""
    if not car:
        return None
    # Direct assignment (Driver Management) wins
    direct = Driver.query.filter_by(assigned_car_id=car.id).first()
    if direct:
        return direct
    dow = date.today().weekday()
    sh = (Shift.query.filter_by(car_id=car.id, active=True)
          .filter(Shift.specific_date == today_p).first())
    if not sh:
        sh = (Shift.query.filter_by(car_id=car.id, active=True)
              .filter(Shift.day_of_week == dow).first())
    if not sh:
        return None
    return Driver.query.get(sh.driver_id)


def can_see_driver_tracking():
    """Driver Tracking (stop history) is limited to master and club owners."""
    return session.get("role") in ("master", "club_owner")

@app.route('/admin/driver-tracking')
def admin_driver_tracking():
    if not session.get("logged") or not can_see_driver_tracking():
        return redirect(url_for("login"))
    return render_template('admin_driver_tracking.html')

@app.route('/api/driver-stops')
def api_driver_stops():
    """Stop history. ?date=YYYY-MM-DD (default today), ?driver=name, ?min=minutes"""
    if not session.get("logged") or not can_see_driver_tracking():
        return jsonify({"error": "Unauthorized"}), 401
    day_iso = request.args.get('date', date.today().strftime("%Y-%m-%d"))
    driver_f = (request.args.get('driver', '') or "").strip()
    try:
        min_min = int(request.args.get('min', 5))
    except ValueError:
        min_min = 5

    # Vegas day → UTC window
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone as _tz
        tz = ZoneInfo("America/Los_Angeles")
        y, m, d = [int(x) for x in day_iso.split("-")]
        start_local = datetime(y, m, d, 0, 0, tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(_tz.utc).replace(tzinfo=None)
        end_utc = end_local.astimezone(_tz.utc).replace(tzinfo=None)
    except Exception:
        y, m, d = [int(x) for x in day_iso.split("-")]
        start_utc = datetime(y, m, d) + timedelta(hours=7)
        end_utc = start_utc + timedelta(days=1)

    q = (DriverStop.query
         .filter(DriverStop.started_at >= start_utc, DriverStop.started_at < end_utc)
         .filter(DriverStop.duration_min >= min_min))
    if driver_f:
        q = q.filter((DriverStop.driver_name == driver_f) | (DriverStop.car_name == driver_f))
    stops = q.order_by(DriverStop.started_at.desc()).all()

    # Group by driver (falling back to the vehicle when no driver is on shift)
    groups = {}
    for s in stops:
        key = s.driver_name or s.car_name or "Unknown"
        g = groups.setdefault(key, {"driver": key, "car_name": s.car_name,
                                    "stops": [], "total_min": 0, "count": 0})
        g["stops"].append(s.to_dict())
        g["total_min"] += (s.duration_min or 0)
        g["count"] += 1
    out = sorted(groups.values(), key=lambda g: g["total_min"], reverse=True)

    drivers = sorted({s.driver_name for s in DriverStop.query.all() if s.driver_name})
    return jsonify({"groups": out, "date": day_iso, "min": min_min,
                    "known_drivers": drivers,
                    "total_stops": len(stops),
                    "total_minutes": sum(s.duration_min or 0 for s in stops)})

@app.route('/admin/tracking/availability', methods=['POST'])
def tracking_set_availability():
    """Manager-side override of a driver's Busy/Available status."""
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    driver_name = (request.form.get('driver_name') or "").strip()
    drv = Driver.query.filter_by(name=driver_name).first()
    if not drv:
        return jsonify({"success": False, "error": f"Driver '{driver_name}' not found"})
    drv.available = (request.form.get('available', 'true').lower() == 'true')
    db.session.commit()
    fire_webhook({
        "type":        "driver_availability_changed",
        "driver_name": drv.name,
        "driver_phone": drv.phone,
        "available":   drv.available,
        "changed_by":  session.get("username", ""),
        "source":      "manager",
    })
    return jsonify({"success": True, "available": drv.available})

@app.route('/api/assignable-drivers', methods=['GET'])
def assignable_drivers():
    """Drivers available to take a pickup, with the car they're on today."""
    if not can_dispatch():
        return jsonify({"error": "Unauthorized"}), 401
    today = date.today()
    today_p = f"{today.month:02d}/{today.day:02d}/{today.year}"
    dow = today.weekday()
    out = []
    for d in Driver.query.order_by(Driver.name).all():
        sh = (Shift.query.filter_by(driver_id=d.id, active=True)
              .filter(Shift.specific_date == today_p).first())
        if not sh:
            sh = (Shift.query.filter_by(driver_id=d.id, active=True)
                  .filter(Shift.day_of_week == dow).first())
        car = Car.query.get(sh.car_id) if sh else None
        out.append({
            "id": d.id, "name": d.name, "phone": d.phone or "",
            "available": bool(d.available),
            "car_name": car.name if car else "",
            "car_string": car.car_string() if car else "",
        })
    return jsonify({"drivers": out})

@app.route('/admin/tracking/reassign/<int:customer_id>', methods=['POST'])
def reassign_pickup(customer_id):
    """Move a pickup to a different driver (and that driver's car for today)."""
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    driver_name = (request.form.get('driver_name') or "").strip()
    drv = Driver.query.filter_by(name=driver_name).first()
    if not drv:
        return jsonify({"success": False, "error": f"Driver '{driver_name}' not found"})

    previous = c.motorista or ""
    today = date.today()
    today_p = f"{today.month:02d}/{today.day:02d}/{today.year}"
    dow = today.weekday()
    sh = (Shift.query.filter_by(driver_id=drv.id, active=True)
          .filter(Shift.specific_date == today_p).first())
    if not sh:
        sh = (Shift.query.filter_by(driver_id=drv.id, active=True)
              .filter(Shift.day_of_week == dow).first())
    car = Car.query.get(sh.car_id) if sh else None

    c.motorista = drv.name
    c.motorista_phone = drv.phone or ""
    if car:
        c.car_name = car.name
        c.car_string_val = car.car_string()
        c.car_photo = car.photo or ""
    # Re-arm proximity alerts for the new driver
    c.notified_15km = False
    c.notified_10km = False
    c.notified_5km = False
    db.session.commit()

    fire_webhook({
        "type":             "pickup_reassigned",
        "customer_id":      c.id,
        "customer_name":    c.nome,
        "customer_phone":   c.phone,
        "previous_driver":  previous,
        "driver_name":      drv.name,
        "driver_phone":     drv.phone or "",
        "driver_car":       c.car_string_val or "",
        "pickup_address":   c.endereco,
        "pickup_datetime":  c.pickup_datetime,
        "destination":      c.destination,
        "reassigned_by":    session.get("username", ""),
    })
    # Let the new driver know
    if drv.phone:
        send_sms_bg(drv.phone,
            f"ClubLifter: pickup reassigned to you.\n"
            f"Guest: {c.nome}\nPickup: {c.endereco}\n"
            f"Time: {c.pickup_datetime}\nDrop-off: {c.destination or 'N/A'}")
    return jsonify({"success": True, "driver": drv.name,
                    "car": c.car_string_val or "", "previous": previous})

@app.route('/admin/tracking/pickup-done/<int:customer_id>', methods=['POST'])
def tracking_mark_done(customer_id):
    """Manager marks a pickup as completed (or reopens it)."""
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    c.status = 'scheduled' if c.status == 'picked_up' else 'picked_up'
    db.session.commit()
    return jsonify({"success": True, "status": c.status})

@app.route('/api/live-drivers', methods=['GET'])
def api_live_drivers():
    """Every vehicle OneStepGPS reports, plus that driver's full pickup queue for today.
    Pickups are linked by DRIVER (via today's shift) as well as by car name, so a
    driver switching cars — or a car name that doesn't exactly match GPS — still works."""
    if not session.get("logged") or not can_dispatch():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        res = requests.get(
            "https://track.onestepgps.com/v3/api/public/device-info?lat_lng=1",
            headers=headers_api, timeout=10
        )
        data = res.json()
        lista = data if isinstance(data, list) else [data]

        today = date.today()
        today_np = f"{today.month}/{today.day}/{today.year}"
        today_p  = f"{today.month:02d}/{today.day:02d}/{today.year}"

        # All of today's transport customers, fetched once
        todays = [c for c in Customer.query.filter_by(needs_transport=True).all()
                  if today_np in (c.pickup_datetime or "") or today_p in (c.pickup_datetime or "")]

        def _t(c):
            if c.pickup_datetime and len(c.pickup_datetime.split(' ')) >= 3:
                p = c.pickup_datetime.split(' '); return f"{p[1]} {p[2]}"
            return ""

        def _item(c):
            return {
                "customer_id":     c.id,
                "customer_name":   c.nome,
                "pickup_address":  c.endereco,
                "destination":     c.destination,
                "pickup_time":     _t(c),
                "dispatch_status": c.dispatch_status,
                "guests":          c.guests or 0,
                "status":          c.status,
                "priority":        bool(getattr(c, 'priority', False)),
                "is_current":      False,
            }

        def _build(matched):
            matched.sort(key=lambda c: (parse_pickup_datetime(c.pickup_datetime) or datetime.max))
            queue, current, done = [], None, 0
            for c in matched:
                it = _item(c)
                if c.status == 'picked_up':
                    done += 1
                elif current is None:
                    it["is_current"] = True
                    current = it
                queue.append(it)
            return queue, current, done

        result = []
        claimed = set()   # customer ids already shown under a vehicle

        for v in lista:
            v_lat = v.get('lat') or v.get('last_tap', {}).get('lat')
            v_lng = v.get('lng') or v.get('last_tap', {}).get('lng')
            if not (v_lat and v_lng):
                continue
            gps_name = v.get('display_name', 'Unknown')
            car = _match_car(gps_name)
            drv = _driver_for_car_today(car, today_p)
            driver_name = drv.name if drv else ""

            car_names = {gps_name}
            if car:
                car_names.add(car.name)

            matched = [c for c in todays
                       if (driver_name and c.motorista == driver_name)
                       or (c.car_name and c.car_name in car_names)]
            for c in matched:
                claimed.add(c.id)
            if not driver_name and matched:
                driver_name = matched[0].motorista or ""

            queue, current, done = _build(matched)
            result.append({
                "name": gps_name,
                "driver_name": driver_name,
                "lat": float(v_lat),
                "lng": float(v_lng),
                "registered": bool(car),
                "available": (drv.available if drv else (car.active if car else None)),
                "car": car.car_string() if car else "",
                "phone": "",
                "current_pickup": current,
                "queue": queue,
                "total_pickups": len(queue),
                "remaining": len(queue) - done,
                "completed": done,
                "total_guests": sum(i["guests"] for i in queue),
                "priority_count": sum(1 for i in queue if i["priority"] and i["status"] != 'picked_up'),
                "no_gps": False,
            })

        # Drivers with pickups today whose vehicle isn't reporting GPS — dispatch
        # still needs to see these, otherwise the pickups silently disappear.
        offline = {}
        for c in todays:
            if c.id in claimed:
                continue
            key = c.motorista or "Unassigned"
            offline.setdefault(key, []).append(c)

        offline_list = []
        for dname, custs in offline.items():
            queue, current, done = _build(custs)
            offline_list.append({
                "name": dname,
                "driver_name": dname,
                "lat": None, "lng": None,
                "registered": False,
                "available": None,
                "car": (custs[0].car_string_val or ""),
                "phone": "",
                "current_pickup": current,
                "queue": queue,
                "total_pickups": len(queue),
                "remaining": len(queue) - done,
                "completed": done,
                "total_guests": sum(i["guests"] for i in queue),
                "priority_count": sum(1 for i in queue if i["priority"] and i["status"] != 'picked_up'),
                "no_gps": True,
            })

        return jsonify({"drivers": result, "offline_drivers": offline_list,
                        "count": len(result), "offline_count": len(offline_list)})
    except Exception as e:
        print(f"[TRACKING] failed: {e}", flush=True)
        return jsonify({"error": str(e), "drivers": [], "offline_drivers": []})

@app.route('/admin/guestlist')
def admin_guestlist():
    if not session.get("logged") or not can_dispatch():
        return redirect(url_for("login"))

    # Filters
    filter_type = request.args.get('type', 'all')   # all | transport | walkin
    filter_club = request.args.get('club', 'all')
    view_all    = request.args.get('view', '') == 'all'
    # date arrives as ISO (YYYY-MM-DD) from the date picker; default = today
    filter_date_iso = request.args.get('date', date.today().strftime("%Y-%m-%d"))

    q = Customer.query
    if filter_type == 'transport':
        q = q.filter_by(needs_transport=True)
    elif filter_type == 'walkin':
        q = q.filter_by(needs_transport=False)
    if filter_club != 'all':
        q = q.filter_by(destination=filter_club)

    if view_all:
        # Newest first (most recently scheduled at top)
        all_customers = q.order_by(Customer.id.desc()).all()
    else:
        all_customers = q.order_by(Customer.pickup_datetime).all()
        # Convert ISO date → MM/DD/YYYY for matching against stored pickup_datetime
        match = ""
        try:
            y, m, d = filter_date_iso.split("-")
            match = f"{int(m)}/{int(d)}/{y}"   # e.g. 6/20/2026
        except Exception:
            match = ""
        if match:
            # Match both non-padded (6/20/2026) and padded (06/20/2026) forms
            padded = f"{int(m):02d}/{int(d):02d}/{y}"
            all_customers = [c for c in all_customers
                             if match in (c.pickup_datetime or "")
                             or padded in (c.pickup_datetime or "")]

    clubs = Club.query.filter_by(active=True).all()

    # Stats
    total    = len(all_customers)
    coming   = sum(1 for c in all_customers if c.club_status == 'coming')
    arrived  = sum(1 for c in all_customers if c.club_status == 'arrived')
    left_    = sum(1 for c in all_customers if c.club_status == 'left')

    return render_template('admin_guestlist.html',
        customers=all_customers, clubs=clubs,
        filter_type=filter_type, filter_club=filter_club,
        filter_date_iso=filter_date_iso, view_all=view_all,
        total=total, coming=coming, arrived=arrived, left=left_,
        vt=vegas_time
    )

@app.route('/admin/guestlist/export')
def export_guestlist():
    """CSV export of the guest list, including who drove and the ride timeline."""
    if not session.get("logged") or not can_dispatch():
        return redirect(url_for("login"))
    import csv as _csv, io as _io
    view_all = request.args.get('view', '') == 'all'
    filter_date_iso = request.args.get('date', date.today().strftime("%Y-%m-%d"))
    rows = Customer.query.order_by(Customer.pickup_datetime).all()
    if not view_all:
        try:
            y, m, d = filter_date_iso.split("-")
            np_, pd_ = f"{int(m)}/{int(d)}/{y}", f"{int(m):02d}/{int(d):02d}/{y}"
            rows = [c for c in rows if np_ in (c.pickup_datetime or "") or pd_ in (c.pickup_datetime or "")]
        except Exception:
            pass
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["Date", "Scheduled Time", "Guest", "Phone", "Guests", "Package",
                "Pickup Address", "Destination", "Driver", "Car",
                "Picked Up At", "Dropped Off At", "Arrived At Property",
                "Distance To Venue (mi)", "Status", "Priority", "Promoter"])
    for c in rows:
        parts = (c.pickup_datetime or "").split(" ")
        d_part = parts[0] if parts else ""
        t_part = " ".join(parts[1:3]) if len(parts) >= 3 else ""
        w.writerow([
            d_part, t_part, c.nome, c.phone, c.guests, c.package,
            c.endereco, c.destination, c.motorista, c.car_string_val,
            vegas_datetime(c.picked_up_at), vegas_datetime(c.dropped_off_at),
            ("Yes" if c.dropoff_verified else ("No" if c.dropped_off_at else "")),
            (c.dropoff_distance_mi or "") if c.dropped_off_at else "",
            c.status, ("Yes" if getattr(c, 'priority', False) else "No"), c.promoter or "",
        ])
    out = buf.getvalue()
    fname = f"clublifter_guestlist_{'all' if view_all else filter_date_iso}.csv"
    return app.response_class(out, mimetype='text/csv',
        headers={"Content-Disposition": f"attachment; filename={fname}"})

@app.route('/admin/guestlist/status/<int:customer_id>', methods=['POST'])
def update_club_status(customer_id):
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    customer = Customer.query.get_or_404(customer_id)
    new_status = request.form.get('club_status', '').strip()
    if new_status not in ('coming', 'arrived', 'left'):
        return jsonify({"success": False, "error": "Invalid status"})
    customer.club_status = new_status
    db.session.commit()

    # Fire webhook for guest status change
    fire_webhook({
        "event":           "club_status_change",
        "customer_id":     customer.id,
        "customer_name":   customer.nome,
        "customer_phone":  customer.phone,
        "destination":     customer.destination,
        "new_status":      new_status,
        "timestamp":       datetime.utcnow().isoformat()
    })

    return jsonify({"success": True, "club_status": new_status})

@app.route('/admin/guestlist/delete/<int:customer_id>', methods=['POST'])
def delete_guest(customer_id):
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    customer = Customer.query.get_or_404(customer_id)
    db.session.delete(customer)
    db.session.commit()
    return jsonify({"success": True})

@app.route('/admin/guestlist/priority/<int:customer_id>', methods=['POST'])
def toggle_priority(customer_id):
    """Club managers (and admin-level/dispatch) flag/unflag a pickup as priority."""
    if not (is_admin_level() or can_dispatch()):
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    c.priority = not c.priority
    db.session.commit()
    if c.priority:
        fire_webhook({
            "type":            "priority_flagged",
            "customer_id":     c.id,
            "customer_name":   c.nome,
            "pickup_address":  c.endereco,
            "destination":     c.destination,
            "pickup_datetime": c.pickup_datetime,
            "driver_name":     c.motorista,
            "flagged_by":      session.get("username", ""),
        })
    return jsonify({"success": True, "priority": c.priority})

@app.route('/admin/guestlist/dispatch/<int:customer_id>', methods=['POST'])
def update_dispatch_status(customer_id):
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    customer = Customer.query.get_or_404(customer_id)
    new_status = request.form.get('dispatch_status', '').strip()
    if new_status not in ('none', 'sent', 'confirmed', 'enroute'):
        return jsonify({"success": False, "error": "Invalid status"})
    customer.dispatch_status = new_status
    db.session.commit()

    # Fire webhook for dispatch status change
    fire_webhook({
        "event":           "dispatch_status_change",
        "customer_id":     customer.id,
        "customer_name":   customer.nome,
        "customer_phone":  customer.phone,
        "driver_name":     customer.motorista,
        "driver_phone":    customer.motorista_phone,
        "car":             customer.car_string_val,
        "pickup_address":  customer.endereco,
        "destination":     customer.destination,
        "dispatch_status": new_status,
        "timestamp":       datetime.utcnow().isoformat()
    })
    return jsonify({"success": True, "dispatch_status": new_status})

@app.route('/api/eta/<int:customer_id>', methods=['GET'])
def customer_eta(customer_id):
    """Live ETA: distance from the assigned car's GPS to the destination club."""
    if not (session.get("logged") and can_dispatch()):
        return jsonify({"error": "Unauthorized"}), 401
    customer = Customer.query.get_or_404(customer_id)
    if not customer.car_name:
        return jsonify({"eta_min": None, "distance_km": None, "reason": "no car assigned"})
    try:
        # 1. Get the car's live position
        headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        res = requests.get(
            "https://track.onestepgps.com/v3/api/public/device-info?lat_lng=1",
            headers=headers_api, timeout=10
        ).json()
        lista = res if isinstance(res, list) else [res]
        car_lat = car_lng = None
        for v in lista:
            if v.get('display_name', '') == customer.car_name:
                car_lat = v.get('lat') or v.get('last_tap', {}).get('lat')
                car_lng = v.get('lng') or v.get('last_tap', {}).get('lng')
                break
        if not (car_lat and car_lng):
            return jsonify({"eta_min": None, "distance_km": None, "reason": "car not reporting GPS"})

        # 2. Geocode the destination club
        club = Club.query.filter_by(name=customer.destination).first()
        target_addr = club.address if (club and club.address) else customer.destination
        if not target_addr:
            return jsonify({"eta_min": None, "distance_km": None, "reason": "no destination"})
        encoded = urllib.parse.quote(target_addr + ", Las Vegas, NV")
        geo = requests.get(
            f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1",
            headers={'User-Agent': 'ClubLifter_ETA'}, timeout=10
        ).json()
        if not geo:
            return jsonify({"eta_min": None, "distance_km": None, "reason": "could not locate club"})
        club_lat = float(geo[0]['lat']); club_lng = float(geo[0]['lon'])

        # 3. Distance (miles) + rough ETA (avg 25 mph city speed)
        dist = calcular_distancia(float(car_lat), float(car_lng), club_lat, club_lng)
        eta_min = round((dist / 25.0) * 60)
        return jsonify({"eta_min": eta_min, "distance_mi": round(dist, 1), "distance_km": round(dist, 1)})
    except Exception as e:
        return jsonify({"eta_min": None, "distance_km": None, "reason": str(e)}), 500

# ─── BACKGROUND DISTANCE TRACKER (15km/5km notifications) ─────────────────────
# ─── DRIVER STOP DETECTION ────────────────────────────────────────────────────
STOP_RADIUS_MI = 0.05    # ~80 m — within this, the vehicle counts as "not moving"
MIN_STOP_MIN   = 5       # only record a stop once it lasts this long
_stop_anchors  = {}      # car_name -> {"lat","lng","since","stop_id"}

def _reverse_geocode(lat, lng):
    try:
        r = requests.get(
            f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}&format=json&zoom=18",
            headers={'User-Agent': 'ClubLifter_StopTracker'}, timeout=8).json()
        return (r.get('display_name') or "")[:300]
    except Exception:
        return ""

def detect_driver_stops():
    """Compare each vehicle's current position with its anchor point.
    Still there → the stop grows. Moved away → the stop is closed."""
    try:
        headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        res = requests.get("https://track.onestepgps.com/v3/api/public/device-info?lat_lng=1",
                           headers=headers_api, timeout=10).json()
        lista = res if isinstance(res, list) else [res]
    except Exception as e:
        print(f"[STOPS] GPS fetch failed: {e}", flush=True)
        return

    now = datetime.utcnow()
    today = date.today()
    today_p = f"{today.month:02d}/{today.day:02d}/{today.year}"

    for v in lista:
        name = v.get('display_name', '')
        lat = v.get('lat') or v.get('last_tap', {}).get('lat')
        lng = v.get('lng') or v.get('last_tap', {}).get('lng')
        if not name or not lat or not lng:
            continue
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            continue

        anchor = _stop_anchors.get(name)
        if anchor is None:
            _stop_anchors[name] = {"lat": lat, "lng": lng, "since": now, "stop_id": None}
            continue

        moved = calcular_distancia(anchor["lat"], anchor["lng"], lat, lng)
        if moved > STOP_RADIUS_MI:
            # Vehicle left — close any open stop and re-anchor
            if anchor.get("stop_id"):
                st = DriverStop.query.get(anchor["stop_id"])
                if st and st.ongoing:
                    st.ended_at = now
                    st.duration_min = max(1, int((now - st.started_at).total_seconds() // 60))
                    st.ongoing = False
                    db.session.commit()
                    print(f"[STOPS] {name} left after {st.duration_min} min", flush=True)
            _stop_anchors[name] = {"lat": lat, "lng": lng, "since": now, "stop_id": None}
            continue

        # Still in the same place
        minutes = int((now - anchor["since"]).total_seconds() // 60)
        if minutes < MIN_STOP_MIN:
            continue

        car = _match_car(name)
        drv = _driver_for_car_today(car, today_p)
        driver_name = drv.name if drv else ""

        if anchor.get("stop_id"):
            st = DriverStop.query.get(anchor["stop_id"])
            if st:
                st.duration_min = minutes
                if driver_name and not st.driver_name:
                    st.driver_name = driver_name
                db.session.commit()
        else:
            st = DriverStop(car_name=name, driver_name=driver_name,
                            lat=anchor["lat"], lng=anchor["lng"],
                            address=_reverse_geocode(anchor["lat"], anchor["lng"]),
                            started_at=anchor["since"], duration_min=minutes, ongoing=True)
            db.session.add(st)
            db.session.commit()
            anchor["stop_id"] = st.id
            print(f"[STOPS] {name} parked at {st.address[:60]} ({minutes} min)", flush=True)

def distance_tracker_loop():
    """
    Runs in background. Every 2 minutes checks all 'scheduled' customers
    that need transport. Computes driver→customer distance via OneStepGPS.
    Fires webhook when distance crosses 15km or 5km threshold.
    """
    while True:
        try:
            time.sleep(120)  # check every 2 minutes
            with app.app_context():
                # ── Driver stop detection (parked-too-long tracking) ──
                try:
                    detect_driver_stops()
                except Exception as e:
                    print(f"[STOPS] detection error: {e}", flush=True)

                # ── Purge "I'm here" photos older than 24h ──
                try:
                    cutoff = datetime.utcnow() - timedelta(hours=24)
                    stale = Customer.query.filter(
                        Customer.here_photo != "",
                        Customer.here_photo_at != None,
                        Customer.here_photo_at < cutoff
                    ).all()
                    for sc in stale:
                        sc.here_photo = ""
                        sc.here_photo_at = None
                    if stale:
                        db.session.commit()
                        print(f"[CLEANUP] Purged {len(stale)} expired here-photo(s)", flush=True)
                except Exception as e:
                    print(f"[CLEANUP] photo purge error: {e}", flush=True)

                today = date.today()
                today_np = f"{today.month}/{today.day}/{today.year}"
                today_p  = f"{today.month:02d}/{today.day:02d}/{today.year}"

                # Only check today's scheduled customers that still need transport tracking
                scheduled = Customer.query.filter_by(
                    status='scheduled',
                    needs_transport=True
                ).all()
                scheduled = [c for c in scheduled
                             if (today_np in (c.pickup_datetime or "") or today_p in (c.pickup_datetime or ""))
                             and (not c.notified_5km or not c.notified_10km or not c.notified_15km)]

                if not scheduled:
                    continue

                # Fetch all vehicles once
                headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
                try:
                    res_v = requests.get(
                        "https://track.onestepgps.com/v3/api/public/device-info?lat_lng=1",
                        headers=headers_api, timeout=10
                    ).json()
                except Exception as e:
                    print(f"[TRACKER] OneStepGPS error: {e}", flush=True)
                    continue

                lista = res_v if isinstance(res_v, list) else [res_v]
                # Build lookup: driver display_name → (lat, lng)
                driver_pos = {}
                for v in lista:
                    v_lat = v.get('lat') or v.get('last_tap', {}).get('lat')
                    v_lng = v.get('lng') or v.get('last_tap', {}).get('lng')
                    if v_lat and v_lng:
                        driver_pos[v.get('display_name', '')] = (float(v_lat), float(v_lng))

                # Re-geocode each customer's address to get current target
                for c in scheduled:
                    # Look up GPS by the assigned car's name (fallback to motorista for old records)
                    gps_key = c.car_name or c.motorista
                    if gps_key not in driver_pos:
                        continue
                    d_lat, d_lng = driver_pos[gps_key]

                    # Geocode customer address
                    try:
                        encoded = urllib.parse.quote(c.endereco)
                        geo_res = requests.get(
                            f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1",
                            headers={'User-Agent': 'ClubLifter_Tracker'}, timeout=10
                        ).json()
                        if not geo_res:
                            continue
                        c_lat = float(geo_res[0]['lat'])
                        c_lng = float(geo_res[0]['lon'])
                    except Exception:
                        continue

                    dist_mi = calcular_distancia(c_lat, c_lng, d_lat, d_lng)
                    dist_km = dist_mi * 1.60934
                    print(f"[TRACKER] {c.nome}: driver {c.motorista} is {dist_km:.2f} km ({dist_mi:.2f} mi) away", flush=True)

                    # Look up the driver's car string
                    drv = Driver.query.filter_by(name=c.motorista).first()
                    car_str = c.car_string_val or (Car.query.filter_by(name=c.car_name).first().car_string() if c.car_name and Car.query.filter_by(name=c.car_name).first() else "N/A")
                    car_photo_url = f"{PUBLIC_BASE_URL}/uploads/{c.car_photo}" if c.car_photo else ""

                    def fire_distance(threshold_label, sms_body):
                        fire_webhook({
                            "type":             "distance",
                            "current_distance": round(dist_mi, 2),
                            "distance_mi":      round(dist_mi, 2),
                            "distance_km":      round(dist_km, 2),
                            "distance_unit":    "mi",
                            "driver_name":      c.motorista,
                            "driver_car":       car_str,
                            "driver_car_photo_url": car_photo_url,
                            "threshold":        threshold_label,
                            "customer_name":    c.nome,
                            "customer_phone":   c.phone,
                            "customer_phones":  c.get_phones(),
                            "destination":      c.destination,
                            "pickup_datetime":  c.pickup_datetime,
                            "customer_id":      c.id,
                        })
                        send_sms_many(c.get_phones() or [c.phone], sms_body, car_photo_url)

                    # Thresholds: 1 km → 500 m → arrived
                    # (reusing notified_15km=1km, notified_10km=500m, notified_5km=arrived)
                    ARRIVED_MI = 0.035   # ~55 m — treat as "arrived"
                    HALF_KM_MI = 0.311   # 500 m
                    ONE_KM_MI  = 0.621   # 1 km

                    if dist_mi <= ARRIVED_MI and not c.notified_5km:
                        c.notified_5km = True
                        c.notified_10km = True
                        c.notified_15km = True
                        c.club_status = "arrived"
                        db.session.commit()
                        fire_distance("arrived",
                            f"Hi {c.nome}! Your ClubLifter driver {c.motorista} has arrived"
                            + (f" in a {car_str}" if car_str and car_str != "N/A" else "")
                            + ". Please head out to meet your driver now!")
                    elif dist_mi <= HALF_KM_MI and not c.notified_10km:
                        c.notified_10km = True
                        c.notified_15km = True
                        db.session.commit()
                        fire_distance("500m",
                            f"Hi {c.nome}! Your ClubLifter driver {c.motorista} is about 500 meters away"
                            + (f" in a {car_str}" if car_str and car_str != "N/A" else "")
                            + ". Please start heading out!")
                    elif dist_mi <= ONE_KM_MI and not c.notified_15km:
                        c.notified_15km = True
                        db.session.commit()
                        fire_distance("1km",
                            f"Hi {c.nome}! Your ClubLifter driver {c.motorista} is about 1 km away"
                            + (f" in a {car_str}" if car_str and car_str != "N/A" else "")
                            + ". Get ready!")
        except Exception as e:
            print(f"[TRACKER] Loop error: {e}", flush=True)

def start_distance_tracker():
    """Start the tracker in a background thread (only once)."""
    thread = threading.Thread(target=distance_tracker_loop, daemon=True)
    thread.start()
    print("[TRACKER] Distance tracker started (checks every 2 min)", flush=True)

# ─── INIT ─────────────────────────────────────────────────────────────────────
def seed_data():
    # Master admin accounts
    master_accounts = [
        ("joaoacess",  "0904jM681213!"),
        ("guyacess",   "winningvocalguy2026!"),
        ("randyacess", "winningvocalguy2026!"),
    ]
    for uname, pwd in master_accounts:
        if not User.query.filter_by(username=uname).first():
            u = User(username=uname, role='master')
            u.set_password(pwd)
            db.session.add(u)

    if Package.query.count() == 0:
        db.session.add_all([
            Package(name="Bronze", description="Basic package",                price=99.0,  max_guests=5),
            Package(name="Silver", description="Mid-tier package",             price=199.0, max_guests=10),
            Package(name="Gold",   description="Premium package",              price=349.0, max_guests=20),
            Package(name="VIP",    description="All-inclusive VIP experience", price=599.0, max_guests=50),
        ])
    db.session.commit()
    seed_venues()

# ─── REAL VENUES + CARTVIP PACKAGES ───────────────────────────────────────────
HUSTLER_ADDRESS = "6007 Dean Martin Dr, Las Vegas, NV 89118"

VENUES = {
    "Hustler Las Vegas": {
        "address": HUSTLER_ADDRESS,
        # (name, checkout_url, price, max_guests, description)
        "packages": [
            ("Free Ride and Entry Pass", "https://app.cartvip.com/vegashustlerclub/package/free-ride-and-entry-pass-32/checkout",
             0.0, 1, "Free VIP transportation and entry pass."),
            ("$20 Special", "https://app.cartvip.com/vegashustlerclub/package/20-special-1/checkout",
             20.0, 1, "$20 entry special."),
            ("Just the Two of Us", "https://app.cartvip.com/vegashustlerclub/package/just-the-two-of-us-30/checkout",
             150.0, 2, "Front-of-the-line access • Admission for 2 • VIP seating • Hustler Brut Champagne • VIP transportation"),
            ("Couch with a View", "https://app.cartvip.com/vegashustlerclub/package/couch-with-a-view-25/checkout",
             250.0, 4, "VIP skip-the-line access • VIP section • 1 premium bottle • 1 round of shots • VIP transportation"),
            ("Blowout Fest", "https://app.cartvip.com/vegashustlerclub/package/blowout-fest-26/checkout",
             450.0, 10, "VIP skip-the-line • VIP section • 2 premium bottles • Party with the DJ & Go-Go's • Personal stage dance for the guest of honor • 1 round of shots • VIP transportation"),
            ("What Happens In Vegas", "https://app.cartvip.com/vegashustlerclub/package/what-happens-in-vegas-28/checkout",
             800.0, 15, "Front-of-the-line with VIP entry • Best VIP section • 3 premium bottles • Personal stage dance • Hustler collection gift • Round of shooters • 2 confetti cannons • VIP transportation"),
            ("Guaranteed Over The Top Experience", "https://app.cartvip.com/vegashustlerclub/package/over-the-top-29/checkout",
             1200.0, 20, "VIP entry • Private VIP host & server • Owners' stage-side seating • 4 premium bottles • Hustler champagne • 2 stage dance parties • 4 confetti cannons • Round of specialty shots • VIP transportation"),
        ],
    },
    "Kings of Hustler": {
        "address": HUSTLER_ADDRESS,   # same venue address
        "packages": [
            ("Free Ride and Free Entry", "https://app.cartvip.com/kingsofhustler/package/free-ride-and-free-entry-40/checkout",
             0.0, 1, "Free VIP transportation and free entry."),
            ("Showstopper", "https://app.cartvip.com/kingsofhustler/package/showstopper-41/checkout",
             50.0, 1, "Admission & entertainment fee • Front-of-the-line, no cover • VIP entry • VIP transportation"),
            ("Bad Mom's Club", "https://app.cartvip.com/kingsofhustler/package/bad-moms-club-33/checkout",
             300.0, 4, "Admission & entertainment fee • Front-of-the-line, no cover • VIP entry & reserved seating • 1 premium bottle of champagne • VIP transportation"),
            ("Champagne with a King", "https://app.cartvip.com/kingsofhustler/package/champagne-with-a-king-13/checkout",
             400.0, 4, "Admission & entertainment fee • Front-of-the-line, no cover • VIP entry & reserved seating • $80 in Hunk Bucks • 1 premium bottle of champagne • VIP transportation"),
            ("Rosè All Day", "https://app.cartvip.com/kingsofhustler/package/rose-all-day-35/checkout",
             700.0, 6, "Front-of-the-line, no cover • VIP entry & reserved seating • 1 premium bottle with mixers • 1 bottle of champagne • Stage show for the guest of honor • $100 in Hunk Bucks • VIP transportation"),
            ("Screaming Orgasm", "https://app.cartvip.com/kingsofhustler/package/screaming-orgasm-36/checkout",
             900.0, 8, "Front-of-the-line, no cover • VIP entry & reserved seating • 2 premium bottles with mixers • Stage show for the guest of honor • $100 in Hunk Bucks • VIP transportation"),
            ("One Last Hoerahh", "https://app.cartvip.com/kingsofhustler/package/one-last-hoerahh-37/checkout",
             1400.0, 12, "Front-of-the-line, no cover • VIP entry & reserved seating • 2 premium bottles with mixers • 1 premium champagne • Stage show for the guest of honor • $200 in Hunk Bucks • VIP transportation"),
            ("Bride and Boujee", "https://app.cartvip.com/kingsofhustler/package/bride-and-boujee-38/checkout",
             1800.0, 15, "Front-of-the-line, no cover • VIP entry & reserved seating • 2 premium bottles with mixers • 2 premium champagnes • Stage show for the guest of honor • $300 in Hunk Bucks • VIP transportation"),
            ("One King Forever!", "https://app.cartvip.com/kingsofhustler/package/one-king-forever-39/checkout",
             2000.0, 20, "VIP table • 3 premium bottles with mixers • 1 premium champagne • Stage show for the guest of honor • $400 in Hunk Bucks • VIP entry & transportation"),
        ],
    },
}

def seed_venues():
    """Idempotently create/update the real clubs and their CartVIP packages.
    Matches existing rows by checkout_url (stable) so names can be corrected
    without creating duplicates. Runs safely on every boot."""
    try:
        for club_name, info in VENUES.items():
            club = Club.query.filter_by(name=club_name).first()
            if not club:
                club = Club(name=club_name, address=info["address"], active=True)
                db.session.add(club)
                db.session.flush()
                print(f"[SEED] club created: {club_name}", flush=True)
            elif club.address != info["address"]:
                club.address = info["address"]
                print(f"[SEED] club address updated: {club_name}", flush=True)

            for pkg_name, url, price, max_guests, desc in info["packages"]:
                # Match by checkout_url first (stable), then by name as a fallback
                pkg = Package.query.filter_by(checkout_url=url).first()
                if not pkg:
                    pkg = Package.query.filter_by(name=pkg_name).first()
                if pkg:
                    pkg.name = pkg_name
                    pkg.checkout_url = url
                    pkg.club_id = club.id
                    pkg.active = True
                    # Only fill pricing/desc when unset, so manual edits are kept
                    if not pkg.price:
                        pkg.price = price
                    if not pkg.max_guests:
                        pkg.max_guests = max_guests
                    if not pkg.description or pkg.description.endswith("package"):
                        pkg.description = desc[:250]
                else:
                    db.session.add(Package(name=pkg_name, checkout_url=url, club_id=club.id,
                                           active=True, price=price, max_guests=max_guests,
                                           description=desc[:250]))
                    print(f"[SEED] package created: {club_name} / {pkg_name}", flush=True)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[SEED] venues seed failed: {e}", flush=True)

with app.app_context():
    db.create_all()

    # ── MIGRATIONS: add new columns to existing databases ─────────────────────
    from sqlalchemy import text, inspect
    inspector = inspect(db.engine)

    def safe_migrate(table, column, ddl):
        """Add a column if missing. Each runs in its own transaction so one
        failure never blocks the others (the bug that broke production)."""
        try:
            cols = [c["name"] for c in inspector.get_columns(table)]
            if column in cols:
                return
            with db.engine.connect() as conn:
                conn.execute(text(ddl))
                conn.commit()
            print(f"[MIGRATION] added {table}.{column}", flush=True)
        except Exception as e:
            print(f"[MIGRATION] FAILED {table}.{column}: {e}", flush=True)

    safe_migrate("package", "club_id", "ALTER TABLE package ADD COLUMN club_id INTEGER DEFAULT NULL")
    safe_migrate("package", "checkout_url", "ALTER TABLE package ADD COLUMN checkout_url VARCHAR(500) DEFAULT ''")

    safe_migrate("customer", "destination",     "ALTER TABLE customer ADD COLUMN destination VARCHAR(100) DEFAULT ''")
    safe_migrate("customer", "status",          "ALTER TABLE customer ADD COLUMN status VARCHAR(20) DEFAULT 'scheduled'")
    safe_migrate("customer", "phones_json",     "ALTER TABLE customer ADD COLUMN phones_json TEXT DEFAULT '[]'")
    safe_migrate("customer", "needs_transport", "ALTER TABLE customer ADD COLUMN needs_transport BOOLEAN DEFAULT 1")
    safe_migrate("customer", "club_status",     "ALTER TABLE customer ADD COLUMN club_status VARCHAR(20) DEFAULT 'coming'")
    safe_migrate("customer", "notified_15km",   "ALTER TABLE customer ADD COLUMN notified_15km BOOLEAN DEFAULT 0")
    safe_migrate("customer", "notified_10km",   "ALTER TABLE customer ADD COLUMN notified_10km BOOLEAN DEFAULT 0")
    safe_migrate("customer", "notified_5km",    "ALTER TABLE customer ADD COLUMN notified_5km BOOLEAN DEFAULT 0")
    safe_migrate("customer", "promoter",        "ALTER TABLE customer ADD COLUMN promoter VARCHAR(80) DEFAULT ''")
    safe_migrate("customer", "dispatch_status", "ALTER TABLE customer ADD COLUMN dispatch_status VARCHAR(20) DEFAULT 'none'")
    safe_migrate("customer", "here_photo",      "ALTER TABLE customer ADD COLUMN here_photo TEXT DEFAULT ''")
    safe_migrate("customer", "here_photo_at",   "ALTER TABLE customer ADD COLUMN here_photo_at DATETIME")
    safe_migrate("customer", "priority",        "ALTER TABLE customer ADD COLUMN priority BOOLEAN DEFAULT 0")
    safe_migrate("customer", "picked_up_at",    "ALTER TABLE customer ADD COLUMN picked_up_at DATETIME")
    safe_migrate("customer", "dropped_off_at",  "ALTER TABLE customer ADD COLUMN dropped_off_at DATETIME")
    safe_migrate("customer", "dropoff_verified","ALTER TABLE customer ADD COLUMN dropoff_verified BOOLEAN DEFAULT 0")
    safe_migrate("customer", "dropoff_distance_mi", "ALTER TABLE customer ADD COLUMN dropoff_distance_mi FLOAT DEFAULT 0")
    safe_migrate("customer", "car_name",        "ALTER TABLE customer ADD COLUMN car_name VARCHAR(100) DEFAULT ''")
    safe_migrate("customer", "car_string_val",  "ALTER TABLE customer ADD COLUMN car_string_val VARCHAR(200) DEFAULT ''")
    safe_migrate("customer", "car_photo",       "ALTER TABLE customer ADD COLUMN car_photo VARCHAR(255) DEFAULT ''")

    safe_migrate("driver", "available", "ALTER TABLE driver ADD COLUMN available BOOLEAN DEFAULT 1")
    safe_migrate("driver", "assigned_car_id", "ALTER TABLE driver ADD COLUMN assigned_car_id INTEGER DEFAULT NULL")
    safe_migrate("driver", "car_model", "ALTER TABLE driver ADD COLUMN car_model VARCHAR(100) DEFAULT ''")
    safe_migrate("driver", "car_color", "ALTER TABLE driver ADD COLUMN car_color VARCHAR(50) DEFAULT ''")
    safe_migrate("driver", "car_plate", "ALTER TABLE driver ADD COLUMN car_plate VARCHAR(30) DEFAULT ''")
    safe_migrate("driver", "car_photo", "ALTER TABLE driver ADD COLUMN car_photo VARCHAR(255) DEFAULT ''")

    safe_migrate("user", "club_id",    'ALTER TABLE "user" ADD COLUMN club_id INTEGER DEFAULT NULL')
    safe_migrate("user", "commission", 'ALTER TABLE "user" ADD COLUMN commission FLOAT DEFAULT 0')
    safe_migrate("user", "email",      'ALTER TABLE "user" ADD COLUMN email VARCHAR(200) DEFAULT \'\'')
    safe_migrate("user", "activation_token", 'ALTER TABLE "user" ADD COLUMN activation_token VARCHAR(64) DEFAULT \'\'')
    safe_migrate("user", "is_active",  'ALTER TABLE "user" ADD COLUMN is_active BOOLEAN DEFAULT 1')

    # ── BACKFILL: create Car records from old Driver car data (one-time) ───────
    if Car.query.count() == 0:
        for d in Driver.query.all():
            if d.car_model or d.car_color or d.car_plate:
                # Use the driver name as the car name only if it matches GPS;
                # otherwise create a car named after the driver as a starting point
                if not Car.query.filter_by(name=d.name).first():
                    db.session.add(Car(
                        name=d.name, model=d.car_model, color=d.car_color,
                        plate=d.car_plate, photo=d.car_photo, active=True
                    ))
        db.session.commit()

    seed_data()

# Start background distance tracker (runs in daemon thread)
# Use WERKZEUG_RUN_MAIN guard to avoid double-starting in Flask debug mode reloader
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
    start_distance_tracker()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
