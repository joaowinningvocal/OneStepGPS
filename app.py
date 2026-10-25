import math
import os
import json
import base64
import threading
from sqlalchemy import func
import time
import requests
import urllib.parse
from datetime import datetime, date, timedelta
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "devverse_secret_CHANGE_ME")
CORS(app)

# ─── ERROR HANDLERS ────────────────────────────────────────────────────────────
# Return clean JSON for API routes instead of leaking the Werkzeug debugger
# (which exposes source code, file paths, and the debugger secret). Non-API
# routes fall through to Flask's normal handling.
import traceback as _traceback

def _is_api_request():
    return request.path.startswith('/api/')

@app.errorhandler(500)
@app.errorhandler(Exception)
def _handle_uncaught(e):
    # Let Flask handle HTTP exceptions (404, 405, etc.) with their own status
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if _is_api_request():
            return jsonify({"success": False, "error": e.description,
                            "status": e.code}), e.code
        return e  # normal HTML for non-API
    # Uncaught exception → log the full trace server-side, return clean JSON
    print(f"[ERROR] Uncaught exception on {request.path}: {e}", flush=True)
    print(_traceback.format_exc(), flush=True)
    try:
        db.session.rollback()
    except Exception:
        pass
    if _is_api_request():
        return jsonify({
            "success": False,
            "error": "An internal error occurred processing your request.",
            "detail": str(e),
            "path": request.path,
        }), 500
    # Non-API: minimal message (no debugger, no source leak)
    return "Internal Server Error", 500

@app.errorhandler(404)
def _handle_404(e):
    if _is_api_request():
        return jsonify({"success": False, "error": "Not found", "path": request.path}), 404
    return e

@app.errorhandler(405)
def _handle_405(e):
    if _is_api_request():
        return jsonify({"success": False, "error": "Method not allowed",
                        "path": request.path}), 405
    return e

# ─── DATABASE CONFIG ───────────────────────────────────────────────────────────
os.makedirs('/data', exist_ok=True)
UPLOAD_FOLDER = '/data/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
database_url = os.environ.get('DATABASE_URL', 'sqlite:////data/clublifter.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Keep logins alive much longer so drivers don't have to sign in repeatedly during
# a shift. Sessions last 30 days and refresh on each request.
from datetime import timedelta as _timedelta
app.config['PERMANENT_SESSION_LIFETIME'] = _timedelta(days=30)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

@app.before_request
def _make_session_permanent():
    session.permanent = True

@app.before_request
def _redirect_to_www():
    """Redirect the bare domain (clublifter.com) to www.clublifter.com, preserving
    the full path and query string. Fixes 404s when someone types
    clublifter.com/app directly. Only redirects the known apex host so it never
    interferes with Railway's own *.up.railway.app domain or local dev."""
    host = (request.host or "").split(":")[0].lower()
    # Only act on the bare apex domain; leave www, railway, and localhost alone.
    if host == "clublifter.com":
        target = "https://www.clublifter.com" + request.full_path
        # full_path can add a trailing "?" when there's no query string — trim it.
        if target.endswith("?"):
            target = target[:-1]
        return redirect(target, code=301)
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
# Voice number for masked driver↔customer calls. Falls back to the SMS number.
TWILIO_VOICE_NUMBER = os.environ.get("TWILIO_VOICE_NUMBER", "").strip() or TWILIO_FROM_NUMBER

# ── Telnyx (SMS provider) ─────────────────────────────────────────────────────
# All outgoing SMS now go through Telnyx. Set these in the environment:
#   TELNYX_API_KEY            — your Telnyx API v2 key (starts with "KEY...")
#   TELNYX_FROM_NUMBER        — the sending number in E.164, e.g. +17252359995
#   TELNYX_MESSAGING_PROFILE_ID (optional) — use a messaging profile instead of/
#                               alongside the from-number for better deliverability
TELNYX_API_KEY             = os.environ.get("TELNYX_API_KEY", "").strip()
TELNYX_FROM_NUMBER         = os.environ.get("TELNYX_FROM_NUMBER", "+17252359995").strip()
TELNYX_MESSAGING_PROFILE_ID = os.environ.get("TELNYX_MESSAGING_PROFILE_ID", "").strip()
# Voice (masked driver↔customer calls) via Telnyx TeXML. Needs a TeXML Application
# whose Voice URL points at this server's /twiml/connect-customer, plus a voice-
# enabled number (defaults to the SMS number). Set:
#   TELNYX_TEXML_APP_ID     — the TeXML Application id (from Telnyx portal)
#   TELNYX_VOICE_NUMBER     — the calling/masked number in E.164 (defaults to SMS #)
TELNYX_TEXML_APP_ID        = os.environ.get("TELNYX_TEXML_APP_ID", "3062886743817586597").strip()
TELNYX_VOICE_NUMBER        = os.environ.get("TELNYX_VOICE_NUMBER", "").strip() or TELNYX_FROM_NUMBER

# Feature flag: the live-tracking SMS link (sent when a driver goes enroute). OFF
# by default for now — set TRACKING_LINK_ENABLED=true to turn it on. The /t/<token>
# page itself stays available (harmless) so any already-sent links keep working.
TRACKING_LINK_ENABLED = os.environ.get("TRACKING_LINK_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
CALLS_ENABLED = os.environ.get("CALLS_ENABLED", "true").strip().lower() not in ("false", "0", "no")
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
    # App / GoBest fields
    state       = db.Column(db.String(40), default="")     # e.g. "Nevada", "California"
    lat         = db.Column(db.Float, nullable=True)
    lng         = db.Column(db.Float, nullable=True)
    photo_url   = db.Column(db.String(400), default="")
    free_drink  = db.Column(db.String(200), default="")    # e.g. "House margarita"
    gobest      = db.Column(db.Boolean, default=True)       # part of the GoBest network
    review_url  = db.Column(db.String(400), default="")     # Google review link for the app prompt

    def to_dict(self):
        return {"id": self.id, "name": self.name, "address": self.address, "active": self.active,
                "state": self.state, "lat": self.lat, "lng": self.lng,
                "photo_url": self.photo_url, "free_drink": self.free_drink,
                "gobest": self.gobest, "review_url": self.review_url or ""}

class Setting(db.Model):
    """Generic key-value store for app settings (e.g. the global API key)."""
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(80), nullable=False, unique=True)
    value = db.Column(db.String(255), default="")

class SavedLocation(db.Model):
    """A reusable pickup location the dispatcher saved (e.g. 'The D' → full address).
    Shows up in the pickup-address dropdown so it can be picked again quickly."""
    id      = db.Column(db.Integer, primary_key=True)
    name    = db.Column(db.String(120), nullable=False, unique=True)   # e.g. "The D"
    address = db.Column(db.String(255), default="")                    # full geocodable address
    active  = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "address": self.address}

class QRCodeItem(db.Model):
    """A managed QR code for the GoBest app. Each one carries a short human code
    that can be typed into the app if scanning fails, and points to a club so the
    app can redeem that venue's promo. Club owners create and manage these."""
    id          = db.Column(db.Integer, primary_key=True)
    code        = db.Column(db.String(20), nullable=False, unique=True)  # typeable code e.g. "HUST-4X9K"
    label       = db.Column(db.String(120), default="")                  # internal name e.g. "Hustler front door"
    club_id     = db.Column(db.Integer, nullable=True)                    # which club it redeems
    club_name   = db.Column(db.String(120), default="")                  # denormalized for display
    drink       = db.Column(db.String(120), default="")                  # promo shown (overrides club default if set)
    active      = db.Column(db.Boolean, default=True)
    scan_count  = db.Column(db.Integer, default=0)                        # how many times redeemed
    created_by  = db.Column(db.String(80), default="")
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "code": self.code, "label": self.label,
            "club_id": self.club_id, "club_name": self.club_name,
            "drink": self.drink, "active": self.active,
            "scan_count": self.scan_count or 0,
            "created_at": self.created_at.strftime("%Y-%m-%d") if self.created_at else "",
        }

class AppUser(db.Model):
    """A consumer who uses the GoBest app. Identified by phone (verified via SMS).
    Carries loyalty progress: check-ins, points, and level."""
    id           = db.Column(db.Integer, primary_key=True)
    phone        = db.Column(db.String(20), nullable=False, unique=True)   # last-10-digit normalized
    name         = db.Column(db.String(120), default="")
    verified     = db.Column(db.Boolean, default=False)
    points       = db.Column(db.Integer, default=0)                        # loyalty points (from check-ins)
    checkin_count = db.Column(db.Integer, default=0)
    auth_token   = db.Column(db.String(64), default="")                    # device session token
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen    = db.Column(db.DateTime, default=datetime.utcnow)

    def level(self):
        """Level derived from points. Every LEVEL_STEP points = 1 level."""
        return app_level_for_points(self.points or 0)

    def to_dict(self):
        lvl = self.level()
        info = APP_LEVELS.get(lvl, APP_LEVELS[max(APP_LEVELS)])
        nxt = APP_LEVELS.get(lvl + 1)
        return {
            "id": self.id, "phone": self.phone, "name": self.name,
            "verified": self.verified, "points": self.points or 0,
            "checkin_count": self.checkin_count or 0,
            "level": lvl, "level_name": info["name"], "level_perk": info["perk"],
            "points_to_next": (nxt["min_points"] - (self.points or 0)) if nxt else 0,
            "next_level_name": nxt["name"] if nxt else None,
            "next_level_at": nxt["min_points"] if nxt else None,
        }

class CheckIn(db.Model):
    """A record of an app user checking in at a club (by scanning its QR)."""
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, nullable=False, index=True)
    club_id    = db.Column(db.Integer, nullable=True)
    club_name  = db.Column(db.String(120), default="")
    points     = db.Column(db.Integer, default=0)          # points awarded for this check-in
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class OTPCode(db.Model):
    """A one-time SMS verification code for app login."""
    id         = db.Column(db.Integer, primary_key=True)
    phone      = db.Column(db.String(20), nullable=False, index=True)
    code       = db.Column(db.String(6), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts   = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AdBanner(db.Model):
    """A paid ad banner shown in the app's home screen. Rented to brand partners
    (e.g. drink brands). Must respect 21+ audience — these run in an app gated to
    users of legal drinking age."""
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(120), default="")     # internal name e.g. "Tito's - March"
    image_url   = db.Column(db.String(500), default="")     # the banner image to show
    link_url    = db.Column(db.String(500), default="")     # where tapping it goes
    active      = db.Column(db.Boolean, default=True)
    weight      = db.Column(db.Integer, default=1)           # relative rotation weight
    impressions = db.Column(db.Integer, default=0)
    clicks      = db.Column(db.Integer, default=0)
    advertiser  = db.Column(db.String(120), default="")     # partner/brand name
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "title": self.title, "image_url": self.image_url,
            "link_url": self.link_url, "active": self.active, "weight": self.weight,
            "impressions": self.impressions or 0, "clicks": self.clicks or 0,
            "advertiser": self.advertiser,
            "created_at": self.created_at.strftime("%Y-%m-%d") if self.created_at else "",
        }

# ─── LOYALTY LEVEL SYSTEM ─────────────────────────────────────────────────────
# Points come from check-ins. Levels unlock periodic rewards. Tune freely.
POINTS_PER_CHECKIN = 10          # each QR check-in at a venue
CHECKIN_COOLDOWN_HOURS = 4       # can't earn again at the SAME club within this window

APP_LEVELS = {
    1: {"name": "Newcomer",  "min_points": 0,   "perk": ""},
    2: {"name": "Regular",   "min_points": 30,  "perk": "Free welcome drink on your next visit"},
    3: {"name": "VIP",       "min_points": 80,  "perk": "Free drink + skip-the-line pass"},
    4: {"name": "Insider",   "min_points": 160, "perk": "Free drink + free dance"},
    5: {"name": "Legend",    "min_points": 300, "perk": "Free bottle service upgrade"},
}

def app_level_for_points(points):
    """Return the numeric level for a points total."""
    lvl = 1
    for n in sorted(APP_LEVELS):
        if points >= APP_LEVELS[n]["min_points"]:
            lvl = n
    return lvl

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
    # Last time this driver's dashboard talked to the server. A driver whose board
    # is open is "on duty" — used so rides aren't auto-assigned to someone who
    # isn't working tonight.
    last_seen_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "phone": self.phone,
            "available": self.available
        }

class RideEvent(db.Model):
    """Audit trail of a ride: who created it (and through what), every driver and
    status change and why, reminders, duplicates. Rows outlive the ride (no FK)
    so a deleted booking can still be explained."""
    id          = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, index=True, nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    actor       = db.Column(db.String(60), default="")     # driver | gps | dispatch:<user> | partner | auto | api
    event       = db.Column(db.String(60), default="")     # created | assigned | status | reschedule | edit | duplicate | deleted …
    detail      = db.Column(db.String(600), default="")

class GeoCache(db.Model):
    """Pickup addresses we already resolved to coordinates. Nominatim (the free map
    service) rate-limits hard; geocoding each address once and remembering it is
    what keeps bookings from losing their driver on busy nights."""
    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(300), unique=True, index=True, nullable=False)
    query_text = db.Column(db.String(500), default="")   # the text that actually resolved
    lat        = db.Column(db.Float, nullable=False)
    lng        = db.Column(db.Float, nullable=False)
    source     = db.Column(db.String(20), default="")    # nominatim | photon | club | manual
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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

class ChatMessage(db.Model):
    """A chat message. channel='general' for the shared channel, or a DM key
    'dm:userA|userB' (usernames sorted) for a direct conversation."""
    id         = db.Column(db.Integer, primary_key=True)
    channel    = db.Column(db.String(120), index=True, default="general")
    sender     = db.Column(db.String(120), index=True)      # username
    body       = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id, "channel": self.channel, "sender": self.sender,
            "body": self.body, "created_at": vegas_time(self.created_at),
            "ts": self.created_at.isoformat() if self.created_at else "",
        }

class ChatRead(db.Model):
    """Tracks the last message id each user has seen per channel (for unread badges)."""
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(120), index=True)
    channel       = db.Column(db.String(120), index=True)
    last_read_id  = db.Column(db.Integer, default=0)

class RideChatMessage(db.Model):
    """A message in the customer<->driver chat for a specific ride. The customer
    sends from the app (identified by the ride); the driver sends from their
    dashboard. Simple polling on both ends."""
    id          = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, index=True, nullable=False)   # the ride this chat belongs to
    sender      = db.Column(db.String(20), default="customer")         # "customer" or "driver"
    body        = db.Column(db.Text, default="")
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    read_by_customer = db.Column(db.Boolean, default=False)
    read_by_driver   = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            "id": self.id, "customer_id": self.customer_id, "sender": self.sender,
            "body": self.body,
            "time": vegas_time(self.created_at),
            "ts": self.created_at.isoformat() if self.created_at else "",
        }

class DriverNotice(db.Model):
    """A notice for a driver about a ride that was reassigned away from them or
    cancelled. Shown as a pop-up when the driver next opens their dashboard, so
    they don't miss it even if they didn't see the SMS."""
    id          = db.Column(db.Integer, primary_key=True)
    driver_name = db.Column(db.String(120), index=True, nullable=False)
    kind        = db.Column(db.String(20), default="reassigned")   # "reassigned" or "cancelled"
    guest_name  = db.Column(db.String(120), default="")
    guest_time  = db.Column(db.String(50), default="")             # the pickup time
    detail      = db.Column(db.String(255), default="")            # e.g. "moved to Alex"
    seen        = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "kind": self.kind, "guest_name": self.guest_name,
            "guest_time": self.guest_time, "detail": self.detail,
            "when": vegas_time(self.created_at),
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
    driver_notes    = db.Column(db.String(500), default="")            # notes the driver adds themselves
    reported        = db.Column(db.Boolean, default=False)             # driver flagged an issue with this guest
    report_reason   = db.Column(db.String(500), default="")           # what the driver reported
    reported_by     = db.Column(db.String(120), default="")           # which driver reported
    reported_at     = db.Column(db.DateTime, nullable=True)
    motorista       = db.Column(db.String(100))
    motorista_phone = db.Column(db.String(20), default="")
    car_name        = db.Column(db.String(100), default="")   # GPS display_name of assigned car
    car_string_val  = db.Column(db.String(200), default="")   # cached "RED HONDA CIVIC (NV-123)"
    car_photo       = db.Column(db.String(255), default="")   # photo filename of assigned car
    distancia       = db.Column(db.Float)
    package         = db.Column(db.String(100))
    price_total     = db.Column(db.Float, nullable=True)                # NEW: total spend (from CartVIP) — drives priority when set
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
    driver_arrived_at = db.Column(db.DateTime, nullable=True)          # when the driver arrived at pickup (tapped "I'm here")
    dropped_off_at  = db.Column(db.DateTime, nullable=True)            # when the guest was dropped at the venue
    dropoff_verified = db.Column(db.Boolean, default=False)            # GPS confirmed arrival at the property
    # Frozen snapshot of WHO actually did the ride (captured at pickup/dropoff time).
    # These never change afterwards, even if the driver later switches cars.
    completed_driver = db.Column(db.String(120), default="")          # driver who actually did it
    completed_car    = db.Column(db.String(200), default="")          # car they were in at the time
    dropoff_distance_mi = db.Column(db.Float, default=0.0)             # car-to-venue distance at drop-off
    promoter        = db.Column(db.String(80), default="")             # NEW: which promoter created this
    # Live tracking link (SMS'd to the guest when the driver goes enroute)
    track_token     = db.Column(db.String(24), unique=True, nullable=True, index=True)  # short public token for /t/<token>
    arrived_at      = db.Column(db.DateTime, nullable=True)            # when club_status became "arrived" (link expires 2h after)
    # Ride Back: links a return ride to the original booking
    is_return_ride  = db.Column(db.Boolean, default=False)             # this ride is a "ride back" (club → hotel)
    return_of_id    = db.Column(db.Integer, nullable=True)             # the original ride this returns for
    has_return_ride = db.Column(db.Boolean, default=False)             # the original has a return already booked
    # Pickup status: 'scheduled', 'picked_up'
    status          = db.Column(db.String(20), default="scheduled")
    # Distance notification flags (so we don't fire 2x)
    notified_15km   = db.Column(db.Boolean, default=False)
    notified_10km   = db.Column(db.Boolean, default=False)
    notified_5km    = db.Column(db.Boolean, default=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    # ── Dispatch engine: auto-assignment + GPS-driven ride status ──
    pickup_lat        = db.Column(db.Float, nullable=True)        # geocoded ONCE at booking, reused everywhere
    pickup_lng        = db.Column(db.Float, nullable=True)
    dest_lat          = db.Column(db.Float, nullable=True)        # venue (or hotel, for return rides)
    dest_lng          = db.Column(db.Float, nullable=True)
    status_source     = db.Column(db.String(20), default="")      # who moved the status last: driver|gps|dispatch|partner
    status_updated_at = db.Column(db.DateTime, nullable=True)
    gps_seen_at       = db.Column(db.DateTime, nullable=True)     # last time the tracker saw this ride's car live
    near_pickup_since = db.Column(db.DateTime, nullable=True)     # dwell timer for GPS "driver arrived at pickup"
    gps_last_pick_mi  = db.Column(db.Float, nullable=True)        # previous car→pickup distance (approach detection)
    assign_note       = db.Column(db.String(200), default="")     # why there's no driver / overbooked / manual
    unassigned_alert_level = db.Column(db.Integer, default=0)     # ops SMS already sent: 1 heads-up, 2 urgent, 3 driver offline
    # ── Provenance: how this booking got here (see RideEvent for the full trail) ──
    source        = db.Column(db.String(40), default="")    # cartvip | api | dashboard | app | return | walkin
    created_by    = db.Column(db.String(160), default="")   # username, or "ip · user-agent" for API callers
    external_ref  = db.Column(db.String(80), default="")    # partner's order/confirmation id (dedupe key)

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
            "driver_notes": self.driver_notes,
            "reported": self.reported, "report_reason": self.report_reason,
            "reported_by": self.reported_by,
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

def telnyx_configured():
    """True when Telnyx can send (API key + either a from-number or a profile)."""
    return bool(TELNYX_API_KEY and (TELNYX_FROM_NUMBER or TELNYX_MESSAGING_PROFILE_ID))

def send_sms(to, body, media_url=""):
    """Send one SMS/MMS through Telnyx. Never raises — messaging failures must not
    break a booking. Keeps the same return shape as before ({"ok":..., "sid":...})
    so all callers (send_sms_bg / send_sms_many) are unchanged.

    The from-number is the Telnyx number (+17252359995 by default). If a Telnyx
    messaging profile id is set, it's sent too for better routing.
    """
    to = clean_phone(to)
    body = (body or "").strip()
    if not SMS_ENABLED:
        print(f"[SMS] muted (SMS_ENABLED=false) to={to}", flush=True)
        return {"ok": False, "skipped": "sms_disabled"}
    if not to or not body:
        print(f"[SMS] skipped: missing to/body (to={to!r})", flush=True)
        return {"ok": False, "skipped": "missing_to_or_body"}
    if not telnyx_configured():
        print("[SMS] skipped: Telnyx env vars not configured (need TELNYX_API_KEY + TELNYX_FROM_NUMBER)", flush=True)
        return {"ok": False, "skipped": "not_configured"}

    # Telnyx wants E.164 with a leading "+". clean_phone strips formatting; add "+"
    # for US 10/11-digit numbers if it isn't already there.
    to_e164 = to if to.startswith("+") else ("+" + to if to.startswith("1") and len(to) == 11
                                             else "+1" + to if len(to) == 10 else "+" + to)

    payload = {"to": to_e164, "text": body[:1550]}
    if TELNYX_MESSAGING_PROFILE_ID:
        payload["messaging_profile_id"] = TELNYX_MESSAGING_PROFILE_ID
    if TELNYX_FROM_NUMBER:
        payload["from"] = TELNYX_FROM_NUMBER

    # Only attach media when it's a valid absolute http(s) URL (MMS)
    media_url = (media_url or "").strip()
    if media_url.startswith("http://") or media_url.startswith("https://"):
        payload["media_urls"] = [media_url]

    try:
        r = requests.post(
            "https://api.telnyx.com/v2/messages",
            json=payload,
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}",
                     "Content-Type": "application/json"},
            timeout=15,
        )
        ok = r.status_code in (200, 201, 202)
        if ok:
            sid = ""
            try: sid = (r.json().get("data", {}) or {}).get("id", "")
            except Exception: pass
            print(f"[SMS] sent via Telnyx to={to_e164} media={'yes' if 'media_urls' in payload else 'no'} id={sid}", flush=True)
            return {"ok": True, "sid": sid}
        print(f"[SMS] FAILED (Telnyx) to={to_e164} status={r.status_code} resp={r.text[:300]}", flush=True)
        return {"ok": False, "status": r.status_code, "error": r.text[:300]}
    except Exception as e:
        print(f"[SMS] EXCEPTION (Telnyx) to={to_e164}: {e}", flush=True)
        return {"ok": False, "error": str(e)}

def _safe_json_get(url, headers=None, timeout=8, default=None):
    """GET a URL and parse JSON, returning `default` on any failure (empty body,
    non-JSON response, rate-limit page, network error). Module-level so background
    jobs like the GPS watcher can use it (it used to only exist nested inside
    cadastrar_cep, which broke [GPS-WATCH] with 'name _safe_json_get is not
    defined')."""
    try:
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        if not r.text or not r.text.strip():
            return default
        return r.json()
    except Exception as _e:
        print(f"[HTTP] request failed ({str(url)[:60]}): {_e}", flush=True)
        return default

def _gen_track_token(n=7):
    """Short, hard-to-guess, URL-safe token for the public /t/<token> tracking
    link. Avoids ambiguous characters (0/O, 1/l/I)."""
    import secrets
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))

def ensure_track_token(c):
    """Give a ride a tracking token if it doesn't have one yet. Returns the token."""
    if not getattr(c, "track_token", None):
        # Make sure it's unique (collision is astronomically unlikely, but cheap to check)
        for _ in range(5):
            tok = _gen_track_token()
            if not Customer.query.filter_by(track_token=tok).first():
                c.track_token = tok
                break
    return c.track_token

def tracking_link(c):
    """Full public tracking URL for a ride, e.g. https://www.clublifter.com/t/aB3xK9."""
    tok = ensure_track_token(c)
    return f"{PUBLIC_BASE_URL}/t/{tok}" if tok else ""

def telnyx_voice_configured():
    """True when Telnyx can place masked calls (API key + TeXML app + number)."""
    return bool(TELNYX_API_KEY and TELNYX_TEXML_APP_ID and TELNYX_VOICE_NUMBER)

def start_masked_call(driver_phone, customer_phone, guest_name="your guest"):
    """Place a masked driver↔customer call through Telnyx TeXML.

    Telnyx rings the DRIVER first (from the masked number). When the driver
    answers, Telnyx fetches TeXML from /twiml/connect-customer, which dials the
    customer and bridges the two legs. Neither party sees the other's real number.

    Returns (ok: bool, error: str|None). Never raises.
    """
    driver_phone = clean_phone(driver_phone)
    customer_phone = clean_phone(customer_phone)
    if not driver_phone:
        return False, "No phone number on file for the driver."
    if not customer_phone:
        return False, "No phone number on file for the customer."
    if not CALLS_ENABLED:
        return False, "Calling is temporarily disabled."
    if not telnyx_voice_configured():
        return False, "Calling is not configured. Contact an admin."

    def _e164(p):
        return p if p.startswith("+") else ("+" + p if p.startswith("1") and len(p) == 11
                                             else "+1" + p if len(p) == 10 else "+" + p)

    import urllib.parse as _up
    connect_url = (f"{PUBLIC_BASE_URL}/twiml/connect-customer"
                   f"?cust={_up.quote(_e164(customer_phone))}&name={_up.quote(guest_name or 'your guest')}")
    # Correct Telnyx TeXML endpoint: connection-scoped, where the connection id IS
    # the TeXML Application id. (The earlier /texml/Accounts/{sid}/Calls form with
    # the API key in the path returned 404 "Resource not found".)
    try:
        resp = requests.post(
            f"https://api.telnyx.com/v2/texml/calls/{TELNYX_TEXML_APP_ID}",
            json={
                "To":   _e164(driver_phone),         # ring the driver first
                "From": _e164(TELNYX_VOICE_NUMBER),  # masked number
                "Url":  connect_url,                 # TeXML fetched when driver answers
            },
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}",
                     "Content-Type": "application/json"},
            timeout=12,
        )
        if resp.status_code >= 400:
            print(f"[CALL] Telnyx error {resp.status_code}: {resp.text[:300]}", flush=True)
            return False, "Couldn't start the call. Try again."
        return True, None
    except Exception as e:
        print(f"[CALL] Telnyx failed: {e}", flush=True)
        return False, "Call service unavailable."

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

def vegas_today():
    """Today's date in Las Vegas (the server runs in UTC, which rolls over ~7h early
    and breaks 'today's pickups' for late-night bookings)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        return (datetime.utcnow() - timedelta(hours=7)).date()

def vegas_naive_now():
    """Current Las Vegas local time as a naive datetime (no tzinfo), so it can be
    compared directly against parsed pickup_datetime values, which are also naive
    Las Vegas local times."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow() - timedelta(hours=7)

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

def normalize_pickup_datetime(dt_str):
    """Normalize a pickup datetime the user typed into the canonical
    'MM/DD/YYYY HH:MM AM/PM' form. Accepts 2- or 4-digit years (26 → 2026),
    single-digit months/days, and a few common separators. Returns the
    normalized string (or the original stripped input if it can't be parsed)."""
    if not dt_str:
        return ""
    s = str(dt_str).strip()
    import re
    # Split date and time (time is the part with a colon and/or AM/PM)
    m = re.match(r'^\s*([\d]{1,2})[/\-.]([\d]{1,2})[/\-.]([\d]{2,4})\s+(.*)$', s)
    if not m:
        return s
    mm, dd, yy, tpart = m.groups()
    mm, dd = int(mm), int(dd)
    yy = int(yy)
    if yy < 100:                      # 26 → 2026
        yy += 2000
    # Parse the time portion (handles "8 PM", "08:00 PM", "20:00")
    tpart = tpart.strip().upper().replace(".", "")
    tm = None
    for fmt in ("%I:%M %p", "%I %p", "%H:%M", "%I:%M%p", "%I%p"):
        try:
            tm = datetime.strptime(tpart, fmt)
            break
        except Exception:
            continue
    if tm is None:
        # keep the time text as-is if unparseable
        return f"{mm:02d}/{dd:02d}/{yy} {tpart}"
    return f"{mm:02d}/{dd:02d}/{yy} {tm.strftime('%I:%M %p')}"

def parse_pickup_datetime(dt_str):
    """
    Parse pickup_datetime string like '04/20/2026 08:00 PM'.
    Tolerant of 2-digit years and loose formats. Returns a datetime or None.
    """
    if not dt_str:
        return None
    normalized = normalize_pickup_datetime(dt_str)
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%y %I:%M %p"):
        try:
            return datetime.strptime(normalized.strip(), fmt)
        except Exception:
            continue
    return None

# A driver counts as busy if they already have a scheduled pickup within this many
# minutes (either side) of the new pickup time. This replaces the old "same
# clock-hour" rule, which missed close pickups that straddled an hour boundary
# (e.g. 10:50 PM and 11:10 PM are 20 min apart but in different hours). Override
# with the DRIVER_BUSY_WINDOW_MIN env var.
DRIVER_BUSY_WINDOW_MIN = int(os.environ.get("DRIVER_BUSY_WINDOW_MIN", "60"))

def driver_is_busy(driver_name: str, pickup_dt: datetime) -> bool:
    """
    A driver is busy if they already have a 'scheduled' customer whose pickup time
    is within DRIVER_BUSY_WINDOW_MIN minutes of the requested pickup_dt (before OR
    after). Measures the actual gap between pickups, so back-to-back rides that are
    too close together are caught regardless of the clock hour.
    """
    if pickup_dt is None:
        return False

    existing = Customer.query.filter_by(
        motorista=driver_name,
        status='scheduled'
    ).all()

    for c in existing:
        existing_dt = parse_pickup_datetime(c.pickup_datetime)
        if existing_dt is None:
            continue
        gap_min = abs((existing_dt - pickup_dt).total_seconds()) / 60.0
        if gap_min < DRIVER_BUSY_WINDOW_MIN:
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
    """Kept for compatibility: who can drive at pickup_dt, as objects with
    .driver/.car. See roster_at() for the rules (overnight shifts belong to the
    evening they start; a Switch-Car shift only overrides that driver/car)."""
    return roster_at(pickup_dt or vegas_naive_now(), include_unavailable=True)

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

        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        # Case-insensitive username match (so "Tj.Madzhary" logs in as "tj.madzhary")
        user = User.query.filter(func.lower(User.username) == username.lower()).first()
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
@app.route('/privacy')
def privacy_policy():
    """Public privacy policy page for the ClubLifter app (required by the App Store
    and Google Play). No login."""
    return render_template('privacy.html')

@app.route('/delete-account', methods=['GET'])
def delete_account_page():
    """Public account-deletion page (required by Google Play). Explains in-app
    deletion and lets a user request deletion by phone without the app. No login."""
    return render_template('delete_account.html')

@app.route('/delete-account/request', methods=['POST'])
def delete_account_request():
    """Handle a web deletion request. If the phone matches an app account, delete it
    immediately (same as the in-app flow). Either way, return a confirmation so the
    page can tell the user it's handled."""
    phone = (request.form.get("phone") or "").strip()
    digits = clean_phone(phone)
    tail = digits[-10:] if len(digits) >= 10 else digits
    deleted = False
    if tail:
        user = AppUser.query.filter(
            (AppUser.phone.like(f"%{tail}"))).first()
        if user:
            try:
                CheckIn.query.filter_by(user_id=user.id).delete()
                try: OTPCode.query.filter_by(phone=user.phone).delete()
                except Exception: pass
                db.session.delete(user)
                db.session.commit()
                deleted = True
            except Exception as e:
                db.session.rollback()
                print(f"[WEB-DELETE] failed for {tail}: {e}", flush=True)
    # Always log the request so staff can honor it even if no account matched yet.
    fire_webhook({
        "type":            "account_deletion_request",
        "phone_last10":    tail,
        "deleted_now":     deleted,
        "source":          "web",
    })
    print(f"[WEB-DELETE] request for ...{tail} — deleted_now={deleted}", flush=True)
    return jsonify({"success": True, "deleted": deleted})

@app.route('/app-preview')
def gobest_app():
    """Serve the ClubLifter consumer app as a web page — a preview of the native
    app for demos/testing. The same HTML is bundled into the native app via
    Capacitor. No login required; it only calls the public /api/app/* endpoints.
    (Moved from /app, which is now the staff dashboard.)"""
    return render_template('gobest_app.html')

# ─── PUBLIC LIVE TRACKING LINK (SMS'd to guests) ──────────────────────────────
TRACK_LINK_EXPIRE_HOURS = int(os.environ.get("TRACK_LINK_EXPIRE_HOURS", "2"))

def _track_link_state(c):
    """Return the lifecycle state of a tracking link:
       'active'  — driver on the way / in progress, show live map
       'arrived' — guest reached the venue, still within the grace window
       'expired' — more than TRACK_LINK_EXPIRE_HOURS past arrival, link dead
    """
    if c.arrived_at:
        if datetime.utcnow() - c.arrived_at > timedelta(hours=TRACK_LINK_EXPIRE_HOURS):
            return "expired"
        return "arrived"
    return "active"

@app.route('/t/<token>')
def track_page(token):
    """Public, no-login live-tracking page for a guest, opened from the SMS link."""
    c = Customer.query.filter_by(track_token=token).first()
    if not c:
        return render_template('track.html', found=False, token=token), 404
    state = _track_link_state(c)
    return render_template('track.html', found=True, token=token, state=state,
                           guest_name=c.nome or "there",
                           driver_name=c.motorista or "Your driver",
                           car=c.car_string_val or "")

@app.route('/t/<token>/data')
def track_data(token):
    """JSON the tracking page polls for live driver position + ETA."""
    c = Customer.query.filter_by(track_token=token).first()
    if not c:
        return jsonify({"found": False}), 404

    state = _track_link_state(c)
    if state == "expired":
        return jsonify({"found": True, "state": "expired"})

    ride_status, ride_status_text = _ride_status_info(c)
    out = {
        "found": True,
        "state": state,
        "driver_name": c.motorista or "Your driver",
        "car": c.car_string_val or "",
        "status": ride_status,
        "status_text": ride_status_text,
        "pickup_address": c.endereco or "",
    }

    # Pickup point (the guest's location)
    plat, plng = _pickup_latlng(c)
    if plat is not None:
        out["pickup_lat"], out["pickup_lng"] = plat, plng

    # Live car position (only while the driver is actually on the move)
    if state == "active" and c.car_name:
        clat, clng = _get_car_gps(c.car_name)
        # Demo fallback so the fake demo car still animates on the page
        if clat is None and c.car_name == "ClubLifter Demo Car":
            import math, time as _t
            t = _t.time() / 30.0
            clat = 36.1072 + 0.012 * math.sin(t)
            clng = -115.1739 + 0.012 * math.cos(t)
        if clat is not None:
            out["car_lat"], out["car_lng"] = clat, clng
            # Simple ETA: straight-line distance ÷ average city speed
            if plat is not None:
                dist_km = _haversine_km(clat, clng, plat, plng)
                avg_kmh = float(os.environ.get("TRACK_ETA_AVG_KMH", "32"))
                eta_min = int(round((dist_km / avg_kmh) * 60)) if avg_kmh > 0 else None
                out["distance_km"] = round(dist_km, 2)
                out["eta_min"] = max(1, eta_min) if eta_min is not None else None

    return jsonify(out)

# ─── DEMO QR CODES ────────────────────────────────────────────────────────────
# Static demo QR codes for the GoBest proof of concept. Each encodes a
# "GOBEST:<club name>" payload the app's scanner reads to redeem that venue's
# free drink. Served as live pages so they can be shared and scanned by anyone.
QR_DEMO = {
    "1": {"club": "Hustler Club Las Vegas", "city": "Las Vegas", "drink": "House Margarita", "glass": "🍹", "accent": "#8a5cf6"},
    "2": {"club": "Kings of Hustler", "city": "Las Vegas", "drink": "House Cocktail", "glass": "🍸", "accent": "#ec4899"},
}

def _qr_png_bytes(data):
    """Generate a QR code PNG as bytes (needs Pillow)."""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H
    import io
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H, box_size=12, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1a1033", back_color="white").convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf.getvalue()

def _qr_svg_bytes(data):
    """Generate a QR code as SVG bytes — pure Python, no Pillow needed. Used as a
    fallback so QR codes always render even if the image libraries aren't present."""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H, box_size=12, border=3)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    box = 12
    size = n * box
    rects = []
    for y, row in enumerate(matrix):
        for x, val in enumerate(row):
            if val:
                rects.append(f'<rect x="{x*box}" y="{y*box}" width="{box}" height="{box}" fill="#1a1033"/>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
           f'viewBox="0 0 {size} {size}"><rect width="{size}" height="{size}" fill="white"/>'
           + "".join(rects) + '</svg>')
    return svg.encode('utf-8')

@app.route('/qr/<qid>.png')
def qr_image(qid):
    """Raw QR for demo code <qid> (encodes GOBEST:<club>). Serves PNG when Pillow
    is available, otherwise falls back to SVG so it always renders."""
    info = QR_DEMO.get(str(qid))
    if not info:
        return "Not found", 404
    from flask import Response
    payload = f"GOBEST:{info['club']}"
    try:
        png = _qr_png_bytes(payload)
        return Response(png, mimetype="image/png")
    except Exception as e:
        print(f"[QR] PNG generation failed ({e}); serving SVG", flush=True)
        svg = _qr_svg_bytes(payload)
        return Response(svg, mimetype="image/svg+xml")

@app.route('/qr/<qid>')
def qr_poster(qid):
    """A shareable poster page for one demo QR code."""
    info = QR_DEMO.get(str(qid))
    if not info:
        return "Not found", 404
    return render_template('qr_poster.html', qid=qid, info=info, single=True, all_qrs=None)

@app.route('/qr')
def qr_posters_all():
    """Both demo QR codes side by side."""
    return render_template('qr_poster.html', qid=None, info=None, single=False, all_qrs=QR_DEMO)

# ─── APP MANAGER (QR codes, later: promotions + banners) ──────────────────────
import random as _random, string as _string

def _gen_qr_code(club_name=""):
    """Generate a short, human-typeable, unique code like 'HUST-4X9K'. Uses a club
    prefix when possible so codes are recognizable. Avoids ambiguous chars."""
    prefix = "".join(ch for ch in (club_name or "GB").upper() if ch.isalpha())[:4] or "GB"
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I,O,0,1 (ambiguous)
    for _ in range(50):
        suffix = "".join(_random.choice(alphabet) for _ in range(4))
        code = f"{prefix}-{suffix}"
        if not QRCodeItem.query.filter_by(code=code).first():
            return code
    # Fallback: longer random
    return "GB-" + "".join(_random.choice(alphabet) for _ in range(6))

def _can_manage_app():
    """Master and club owners can manage the app (QR codes, promos, banners)."""
    return session.get("logged") and session.get("role") in ("master", "club_owner")

def _build_flyer_pdf(item):
    """Build a printable flyer PDF (bytes) for a managed QR code. Uses the Hustler
    neon branding when it's a Hustler venue; otherwise a clean GoBest flyer.
    Shows the QR plus the typeable code underneath."""
    import qrcode as _qrcode
    from qrcode.constants import ERROR_CORRECT_H
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as _canvas
    from reportlab.lib.units import inch
    from reportlab.lib.utils import ImageReader
    import io as _io

    club = item.club_name or "ClubLifter Venue"
    drink = item.drink or "a complimentary welcome drink"
    code = item.code
    payload = f"GOBEST:{code}"

    # QR image
    qr = _qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H, box_size=16, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="#1a0033", back_color="white").convert("RGB")
    qb = _io.BytesIO(); qr_img.save(qb, format="PNG"); qb.seek(0)
    qr_reader = ImageReader(qb)

    is_hustler = "hustler" in club.lower()

    # Palette
    if is_hustler:
        BG_TOP=(0.043,0.024,0.094); BG_BOT=(0.024,0.008,0.063)
        ACCENT=(0.925,0.145,0.686); ACCENT_LT=(0.988,0.541,0.851); ACCENT2=(0.298,0.788,0.941)
    else:
        BG_TOP=(0.055,0.043,0.098); BG_BOT=(0.031,0.024,0.059)
        ACCENT=(0.541,0.361,0.965); ACCENT_LT=(0.655,0.545,0.980); ACCENT2=(0.925,0.282,0.600)
    GOLD=(0.984,0.749,0.141); WHITE=(0.965,0.953,0.980); MUTED=(0.639,0.612,0.729)

    buf = _io.BytesIO()
    c = _canvas.Canvas(buf, pagesize=letter)
    W, H = letter

    # Background gradient
    steps = 100
    for i in range(steps):
        t = i/steps
        c.setFillColorRGB(BG_TOP[0]+(BG_BOT[0]-BG_TOP[0])*t,
                          BG_TOP[1]+(BG_BOT[1]-BG_TOP[1])*t,
                          BG_TOP[2]+(BG_BOT[2]-BG_TOP[2])*t)
        c.rect(0, H*(1-(i+1)/steps), W, H/steps+1, fill=1, stroke=0)

    # Glow blobs
    c.setFillColorRGB(*ACCENT); c.setFillAlpha(0.10); c.circle(W*0.15, H*0.82, 140, fill=1, stroke=0)
    c.setFillColorRGB(*ACCENT2); c.setFillAlpha(0.08); c.circle(W*0.88, H*0.30, 130, fill=1, stroke=0)
    c.setFillAlpha(1)

    # Try to place the Hustler logo if available
    top_y = H - 0.9*inch
    logo_path = "/mnt/user-data/uploads/1787766136000_image.png"
    placed_logo = False
    if is_hustler and os.path.exists(logo_path):
        try:
            from PIL import Image as _PILImage
            logo = _PILImage.open(logo_path).convert("RGBA")
            lb = _io.BytesIO(); logo.save(lb, format="PNG"); lb.seek(0)
            logo_reader = ImageReader(lb)
            logo_w = 5.4*inch; logo_h = logo_w / (logo.width/logo.height)
            c.drawImage(logo_reader, (W-logo_w)/2, H-0.5*inch-logo_h, logo_w, logo_h, mask='auto')
            top_y = H - 0.5*inch - logo_h - 0.4*inch
            placed_logo = True
        except Exception:
            placed_logo = False

    if not placed_logo:
        # ClubLifter brand text
        c.setFillColorRGB(*ACCENT_LT); c.setFont("Helvetica-Bold", 40)
        c.drawCentredString(W/2 - 0.55*inch, H-1.15*inch, "Club")
        c.setFillColorRGB(*WHITE)
        gw = c.stringWidth("Club", "Helvetica-Bold", 40)
        c.drawString(W/2 - 0.55*inch + gw, H-1.15*inch, "Lifter")
        # Club name
        c.setFillColorRGB(*WHITE); c.setFont("Helvetica-Bold", 26)
        c.drawCentredString(W/2, H-1.7*inch, club)
        top_y = H - 2.1*inch

    # Headline
    c.setFillColorRGB(*GOLD); c.setFont("Helvetica-Bold", 17)
    c.drawCentredString(W/2, top_y, "★  SCAN FOR A FREE WELCOME DRINK  ★")

    # QR card
    qr_size = 2.8*inch
    qr_x = (W-qr_size)/2
    qr_y = top_y - 0.45*inch - qr_size
    pad = 0.26*inch
    c.setFillColorRGB(*ACCENT)
    c.roundRect(qr_x-pad-5, qr_y-pad-5, qr_size+2*pad+10, qr_size+2*pad+10, 22, fill=1, stroke=0)
    c.setFillColorRGB(1,1,1)
    c.roundRect(qr_x-pad, qr_y-pad, qr_size+2*pad, qr_size+2*pad, 18, fill=1, stroke=0)
    c.drawImage(qr_reader, qr_x, qr_y, qr_size, qr_size)

    # Drink
    c.setFillColorRGB(*ACCENT_LT); c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(W/2, qr_y-0.5*inch, "YOUR COMPLIMENTARY DRINK")
    c.setFillColorRGB(*WHITE); c.setFont("Helvetica-Bold", 26)
    c.drawCentredString(W/2, qr_y-0.92*inch, drink)

    # Typeable code
    c.setFillColorRGB(*MUTED); c.setFont("Helvetica", 10)
    c.drawCentredString(W/2, qr_y-1.32*inch, "Can't scan? Enter this code in the app:")
    c.setFillColorRGB(*GOLD); c.setFont("Courier-Bold", 22)
    c.drawCentredString(W/2, qr_y-1.68*inch, code)

    # Steps
    c.setFillColorRGB(*ACCENT2); c.setFont("Helvetica-Bold", 12)
    steps_txt = ["1.  Download the ClubLifter app", "2.  Tap Scan (or enter the code)", "3.  Show your phone + photo ID at the bar"]
    sy = qr_y - 2.15*inch
    for s in steps_txt:
        c.drawCentredString(W/2, sy, s); sy -= 0.25*inch

    # Footer
    c.setStrokeColorRGB(*ACCENT); c.setStrokeAlpha(0.4); c.setLineWidth(1)
    c.line(1.4*inch, 0.6*inch, W-1.4*inch, 0.6*inch); c.setStrokeAlpha(1)
    c.setFillColorRGB(*MUTED); c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(W/2, 0.4*inch, "Powered by ClubLifter  ·  clublifter.com/app")

    c.showPage(); c.save()
    buf.seek(0)
    return buf.getvalue()

@app.route('/admin/app-manager')
def app_manager():
    if not _can_manage_app():
        return redirect(url_for("login"))
    clubs = Club.query.filter_by(active=True).order_by(Club.name).all()
    qrs = QRCodeItem.query.order_by(QRCodeItem.created_at.desc()).all()
    banners = AdBanner.query.order_by(AdBanner.created_at.desc()).all()
    return render_template('admin_app_manager.html', clubs=clubs, qrs=qrs, banners=banners)

@app.route('/admin/app-manager/qr/create', methods=['POST'])
def app_qr_create():
    if not _can_manage_app():
        return jsonify({"success": False, "error": "Unauthorized"})
    f = request.form
    label = (f.get('label') or "").strip()
    club_id = _safe_int(f.get('club_id'))
    drink = (f.get('drink') or "").strip()
    club = Club.query.get(club_id) if club_id else None
    if not club:
        return jsonify({"success": False, "error": "Pick a club for this QR code."})
    item = QRCodeItem(
        code=_gen_qr_code(club.name),
        label=label or f"{club.name} QR",
        club_id=club.id, club_name=club.name,
        drink=drink or (club.free_drink or ""),
        active=True, created_by=session.get("username", ""),
    )
    db.session.add(item)
    db.session.commit()
    return jsonify({"success": True, "qr": item.to_dict()})

@app.route('/admin/app-manager/qr/toggle/<int:qr_id>', methods=['POST'])
def app_qr_toggle(qr_id):
    if not _can_manage_app():
        return jsonify({"success": False, "error": "Unauthorized"})
    item = QRCodeItem.query.get_or_404(qr_id)
    item.active = not item.active
    db.session.commit()
    return jsonify({"success": True, "active": item.active})

@app.route('/admin/app-manager/qr/delete/<int:qr_id>', methods=['POST'])
def app_qr_delete(qr_id):
    if not _can_manage_app():
        return jsonify({"success": False, "error": "Unauthorized"})
    item = QRCodeItem.query.get_or_404(qr_id)
    db.session.delete(item)
    db.session.commit()
    return jsonify({"success": True})

@app.route('/admin/app-manager/qr/<int:qr_id>.png')
def app_qr_png(qr_id):
    """QR image for a managed code. Encodes the typeable code so the app resolves
    it the same whether scanned or typed."""
    item = QRCodeItem.query.get_or_404(qr_id)
    from flask import Response
    payload = f"GOBEST:{item.code}"
    try:
        return Response(_qr_png_bytes(payload), mimetype="image/png")
    except Exception:
        return Response(_qr_svg_bytes(payload), mimetype="image/svg+xml")

@app.route('/admin/app-manager/qr/<int:qr_id>/flyer')
def app_qr_flyer(qr_id):
    """Generate a printable flyer PDF for a managed QR code (Hustler-style branding
    when it's a Hustler venue, otherwise a clean GoBest flyer)."""
    if not _can_manage_app():
        return redirect(url_for("login"))
    item = QRCodeItem.query.get_or_404(qr_id)
    pdf_bytes = _build_flyer_pdf(item)
    from flask import Response
    safe = "".join(ch for ch in (item.club_name or "venue") if ch.isalnum() or ch in " -_").strip().replace(" ", "_")
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename=ClubLifter_Flyer_{safe}.pdf"})

@app.route('/admin/app-manager/banner/create', methods=['POST'])
def app_banner_create():
    if not _can_manage_app():
        return jsonify({"success": False, "error": "Unauthorized"})
    f = request.form
    title = (f.get('title') or "").strip()
    image_url = (f.get('image_url') or "").strip()
    link_url = (f.get('link_url') or "").strip()
    advertiser = (f.get('advertiser') or "").strip()
    if not image_url:
        return jsonify({"success": False, "error": "A banner image URL is required."})
    b = AdBanner(title=title or "Untitled ad", image_url=image_url, link_url=link_url,
                 advertiser=advertiser, active=True, weight=_safe_int(f.get('weight')) or 1)
    db.session.add(b)
    db.session.commit()
    return jsonify({"success": True, "banner": b.to_dict()})

@app.route('/admin/app-manager/banner/toggle/<int:banner_id>', methods=['POST'])
def app_banner_toggle(banner_id):
    if not _can_manage_app():
        return jsonify({"success": False, "error": "Unauthorized"})
    b = AdBanner.query.get_or_404(banner_id)
    b.active = not b.active
    db.session.commit()
    return jsonify({"success": True, "active": b.active})

@app.route('/admin/app-manager/banner/delete/<int:banner_id>', methods=['POST'])
def app_banner_delete(banner_id):
    if not _can_manage_app():
        return jsonify({"success": False, "error": "Unauthorized"})
    b = AdBanner.query.get_or_404(banner_id)
    db.session.delete(b)
    db.session.commit()
    return jsonify({"success": True})

@app.route('/')
def front_page():
    """Public promotional front page for ClubLifter. Has Log In and Sign Up
    (Sign Up = coming soon). No login required. The dashboard now lives at /app."""
    # If someone who's already logged in hits the landing, send them to their app.
    if session.get("logged"):
        if session.get("role") == "driver":
            return redirect(url_for("driver_dashboard"))
        return redirect(url_for("index"))
    return render_template('landing.html')

@app.route('/app')
def index():
    if not session.get("logged"):
        return redirect(url_for("login"))
    # Drivers should not access the main dashboard
    if session.get("role") == "driver":
        return redirect(url_for("driver_dashboard"))

    packages = Package.query.filter_by(active=True).all()
    # Group order: real club packages first (by club), generic/no-club ones last
    packages = sorted(packages, key=lambda p: (p.club_id is None, p.club_id or 0, p.price or 0))
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

    saved_locations = SavedLocation.query.filter_by(active=True).order_by(SavedLocation.name).all()
    return render_template('index.html', clientes=customers, packages=packages, clubs=clubs,
                           saved_locations=saved_locations)

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
    pickup_datetime   = normalize_pickup_datetime(request.form.get('pickup_datetime', '').strip())
    destination       = request.form.get('destination', '').strip()
    needs_transport   = request.form.get('needs_transport', 'true').lower() == 'true'
    force_waitlist    = request.form.get('force_waitlist', 'false').lower() == 'true'
    is_priority       = request.form.get('priority', '0') in ('1', 'true', 'yes', 'on')

    # Parse extra phones JSON array
    try:
        extra_phones = json.loads(extra_phones_raw) if extra_phones_raw else []
        extra_phones = [p.strip() for p in extra_phones if p and p.strip()]
    except Exception:
        extra_phones = []
    all_phones = ([client_phone] if client_phone else []) + extra_phones

    # ── DUPLICATE RIDE CHECK ──────────────────────────────────────────────
    # Warn (once) if this name OR phone already has a ride, unless the user
    # confirms. Skip if force_duplicate=true is sent.
    force_duplicate = request.form.get('force_duplicate', 'false').lower() == 'true'
    if not force_duplicate:
        norm_new_phone = clean_phone(client_phone)
        name_l = nome.lower()
        dup = None
        # Look at recent rides for a name or phone match ON THE SAME DAY (or future).
        # Past rides (e.g. the guest rode last night) must NOT trigger a warning.
        def _ride_day(dt_txt):
            """Best-effort date extraction. Tries the normal parser, then a
            DD/MM fallback (some legacy rides were stored day-first), then a
            raw date-token compare. Returns a date or None."""
            if not dt_txt:
                return None
            d = parse_pickup_datetime(dt_txt)
            if d:
                return d.date()
            # Fallback: pull the first token and try both orders
            token = dt_txt.strip().split(" ")[0]
            import re as _re
            m = _re.match(r'^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$', token)
            if m:
                x, y, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if yy < 100:
                    yy += 2000
                from datetime import date as _date
                # If first number > 12 it must be the day (DD/MM)
                try:
                    if x > 12:
                        return _date(yy, y, x)
                    return _date(yy, x, y)   # assume MM/DD
                except ValueError:
                    try:
                        return _date(yy, y, x)
                    except ValueError:
                        return None
            return None

        new_day = _ride_day(pickup_datetime)

        for existing in Customer.query.order_by(Customer.id.desc()).limit(500).all():
            # Skip rides already completed
            if existing.status in ("dropped_off",):
                continue
            ex_day = _ride_day(existing.pickup_datetime)
            # Only treat as a possible duplicate when we can CONFIRM the same day.
            # If either date is unknown, or they differ, do NOT warn (avoids
            # flagging month-old rides just because the name matches).
            if not new_day or not ex_day or ex_day != new_day:
                continue
            name_match = existing.nome and existing.nome.lower() == name_l
            phone_match = False
            if norm_new_phone:
                ex_phones = [clean_phone(existing.phone)] + [clean_phone(p) for p in existing.get_phones()]
                phone_match = norm_new_phone in [p for p in ex_phones if p]
            if name_match or phone_match:
                dup = existing
                break
        if dup:
            return jsonify({
                "success": False,
                "duplicate": True,
                "message": (
                    f"A ride for {dup.nome} already exists"
                    + (f" on {dup.pickup_datetime}" if dup.pickup_datetime else "")
                    + (f" to {dup.destination}" if dup.destination else "")
                    + f" (matched by {'name' if dup.nome.lower()==name_l else 'phone'}). "
                    "Schedule this one anyway?"
                ),
                "existing": {
                    "name": dup.nome,
                    "phone": dup.phone,
                    "pickup_datetime": dup.pickup_datetime,
                    "destination": dup.destination,
                    "driver": dup.completed_driver or dup.motorista,
                    "status": dup.status,
                },
            })

    try:
        # ── WALK-IN PATH (no transport): skip geocoding, GPS, driver assignment ──
        if not needs_transport:
            customer = Customer(
                nome=nome, phone=client_phone, phones_json=json.dumps(extra_phones),
                endereco="(walk-in)", details=details,
                motorista="(walk-in)", motorista_phone="",
                distancia=0, package=package, priority=is_priority,
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

        def _safe_json_get(url, headers=None, timeout=8, default=None):
            """GET a URL and parse JSON, returning `default` on any failure
            (empty body, non-JSON response, rate-limit page, network error)."""
            try:
                r = requests.get(url, headers=headers or {}, timeout=timeout)
                if not r.text or not r.text.strip():
                    return default
                return r.json()
            except Exception as _e:
                print(f"[BOOKING] request failed ({url[:60]}): {_e}", flush=True)
                return default

        # 1. GEOCODING — clean street address first, then the full text. Cached and
        # throttled (see DISPATCH ENGINE) so busy nights don't get us rate-limited.
        # (the form field is also called geocode_address — use the wrapper here)
        lat_cli, lng_cli = _geocode_pickup_flexible(geocode_address)
        if lat_cli is None and geocode_address != endereco_completo:
            lat_cli, lng_cli = _geocode_pickup_flexible(endereco_completo)
        if lat_cli is None:
            return jsonify({"success": False, "error": "Address not found on global map."})

        # 2. Live GPS (shared cache) — used for the manual-driver distance below
        headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        gps_by_name = {}
        for v in (gps_fleet() or []):
            _vl, _vg = _device_latlng(v) if isinstance(v, dict) else (None, None)
            if _vl is not None:
                gps_by_name[v.get('display_name', '')] = {"lat": _vl, "lng": _vg}

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
                # Find their car: 1:1 assignment first, then today's shift,
                # then a Car whose name matches a live GPS vehicle for this driver.
                chosen_car = driver_car_at(mdrv, requested_dt)
                if chosen_car:
                    coords = gps_by_name.get(chosen_car.name)
                    if coords:
                        motorista_coords = coords
                        menor_d = calcular_distancia(lat_cli, lng_cli, coords["lat"], coords["lng"])

        assign_note = "manual" if (driver_mode == 'manual' and melhor_v != "Unavailable") else ""
        if melhor_v == "Unavailable":
            # Shared picker: a real person who is set up (and, for tonight, online),
            # free if possible — never a car's GPS name.
            _pick, _why = pick_driver(lat_cli, lng_cli, requested_dt)
            if _pick:
                melhor_v = _pick.driver.name
                chosen_car = _pick.car
                menor_d = _pick.dist_mi if _pick.dist_mi is not None else float('inf')
                _p = car_position(_pick.car.name) if _pick.car else None
                motorista_coords = {"lat": _p["lat"], "lng": _p["lng"]} if _p else None
                assign_note = _pick.note or ""
            else:
                assign_note = _why or ""

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
                "message": (f"No driver can take this ride right now ({assign_note or 'none available'}). "
                            "Add it to the waitlist? The system keeps retrying every minute and "
                            "texts dispatch until someone has it.")
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
        try:
            requests.post(URL_API, json=payload_gps, headers=headers_api, timeout=8)
        except Exception as _e:
            print(f"[BOOKING] OneStepGPS marker skipped: {_e}", flush=True)

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
            priority=is_priority,
            guests=guests, pickup_datetime=pickup_datetime,
            destination=destination,
            needs_transport=True, club_status="coming",
            promoter=session.get('username', ''),
            status=('waitlist' if is_waitlist else 'scheduled'),
            created_at=datetime.utcnow(),
            pickup_lat=lat_cli, pickup_lng=lng_cli,
            assign_note=(assign_note or "")[:200],
            source="dashboard", created_by=session.get('username', ''),
        )
        _club = club_for_destination(destination)
        if _club and _club.lat is not None:
            customer.dest_lat, customer.dest_lng = _club.lat, _club.lng
        db.session.add(customer)
        db.session.commit()
        log_event(customer, "created",
                  f"by {session.get('username', '?')} on the dashboard · {pickup_datetime} · {endereco_completo} → {destination}"
                  + (f" · driver {melhor_v} ({driver_mode})" if melhor_v != "Unavailable" else " · NO DRIVER")
                  + (" · DUPLICATE CONFIRMED by user" if force_duplicate else ""),
                  actor=f"dispatch:{session.get('username', '')}")

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
            _drv_obj = Driver.query.filter_by(name=melhor_v).first()
            if _drv_obj:
                notify_driver_of_ride(customer, _drv_obj, "assigned")
            else:
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
                f"{chr(10)}Heads up: your driver may call from this number to reach you — "
                f"please keep an eye out and answer so they can find you."
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
@app.route('/admin/analytics')
def admin_analytics():
    if not session.get("logged") or not can_dispatch():
        return redirect(url_for("login"))

    from collections import Counter
    today = vegas_today()
    now = datetime.utcnow()
    week_ago  = now - timedelta(days=7)
    month_start = datetime(today.year, today.month, 1)
    day_start = datetime(today.year, today.month, today.day) - timedelta(hours=7)  # ~Vegas midnight in UTC

    all_c = Customer.query.all()
    pkg_price = {p.name: (p.price or 0) for p in Package.query.all()}

    def revenue(rows):
        return sum(pkg_price.get(c.package, 0) for c in rows)

    day_rows   = [c for c in all_c if c.created_at and c.created_at >= day_start]
    week_rows  = [c for c in all_c if c.created_at and c.created_at >= week_ago]
    month_rows = [c for c in all_c if c.created_at and c.created_at >= month_start]

    # Averages: completion + time-to-venue
    completed = [c for c in all_c if c.picked_up_at and c.dropped_off_at]
    ride_minutes = []
    for c in completed:
        try:
            mins = (c.dropped_off_at - c.picked_up_at).total_seconds() / 60
            if 0 < mins < 240:   # ignore absurd outliers
                ride_minutes.append(mins)
        except Exception:
            pass
    avg_ride_min = round(sum(ride_minutes) / len(ride_minutes), 1) if ride_minutes else 0

    total_pickups = len([c for c in all_c if c.picked_up_at])
    total_dropoffs = len([c for c in all_c if c.dropped_off_at])

    # Peak hours (by pickup time)
    hour_counts = Counter()
    for c in all_c:
        dt = parse_pickup_datetime(c.pickup_datetime)
        if dt:
            hour_counts[dt.hour] += 1
    # Build a 24h series (as label + count), nightlife-friendly ordering 6PM→5AM
    order = list(range(18, 24)) + list(range(0, 18))
    peak_series = []
    for h in order:
        label = f"{(h%12) or 12}{'AM' if h<12 else 'PM'}"
        peak_series.append({"hour": label, "count": hour_counts.get(h, 0)})
    busiest_hour = max(hour_counts.items(), key=lambda x: x[1])[0] if hour_counts else None
    busiest_label = (f"{(busiest_hour%12) or 12}{'AM' if busiest_hour<12 else 'PM'}"
                     if busiest_hour is not None else "—")

    # Top destinations
    dest_counts = Counter(c.destination for c in all_c if c.destination)
    top_dests = [{"name": k, "count": v} for k, v in dest_counts.most_common(6)]

    # Top drivers (by completed rides)
    drv_counts = Counter((c.completed_driver or c.motorista) for c in completed
                         if (c.completed_driver or c.motorista))
    top_drivers = [{"name": k, "count": v} for k, v in drv_counts.most_common(6)]

    # Reports
    reported_count = len([c for c in all_c if c.reported])

    stats = {
        "day_count": len(day_rows), "day_revenue": revenue(day_rows),
        "day_guests": sum(c.guests or 0 for c in day_rows),
        "week_count": len(week_rows), "week_revenue": revenue(week_rows),
        "week_guests": sum(c.guests or 0 for c in week_rows),
        "month_count": len(month_rows), "month_revenue": revenue(month_rows),
        "month_guests": sum(c.guests or 0 for c in month_rows),
        "total_pickups": total_pickups, "total_dropoffs": total_dropoffs,
        "avg_ride_min": avg_ride_min,
        "completion_rate": (round(100 * total_dropoffs / total_pickups) if total_pickups else 0),
        "busiest_hour": busiest_label,
        "reported_count": reported_count,
        "total_rides": len(all_c),
    }
    return render_template('admin_analytics.html',
        stats=stats, peak_series=peak_series,
        top_dests=top_dests, top_drivers=top_drivers)

@app.route('/admin/today')
def admin_today():
    if not session.get("logged") or not can_dispatch():
        return redirect(url_for("login"))

    today = vegas_today()
    # Match both non-padded (7/1/2026) and padded (07/01/2026) date formats,
    # since pickups may be stored either way.
    today_np = f"{today.month}/{today.day}/{today.year}"           # 7/1/2026
    today_p  = f"{today.month:02d}/{today.day:02d}/{today.year}"   # 07/01/2026
    today_str = today_np

    all_customers = Customer.query.order_by(Customer.pickup_datetime).all()
    today_customers = [c for c in all_customers
                       if today_np in (c.pickup_datetime or "")
                       or today_p in (c.pickup_datetime or "")]

    # "Today" shows only pickups scheduled by the LOGGED-IN user by default.
    # ?scope=all shows everyone's (handy for managers).
    me = session.get("username", "")
    scope = request.args.get('scope', 'mine')
    if scope != 'all':
        today_customers = [c for c in today_customers if (c.promoter or "") == me]

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
        today=today,
        scope=scope
    )

# ─── API: LAST CLIENT (for AI voice calls) ────────────────────────────────────
# ─── CHAT ─────────────────────────────────────────────────────────────────────
def _dm_channel(a_user, b_user):
    """Deterministic channel key for a DM between two usernames."""
    a, b = sorted([a_user, b_user])
    return f"dm:{a}|{b}"

def _extract_mentions(body):
    import re
    return set(re.findall(r'@([A-Za-z0-9._-]+)', body or ""))

@app.route('/api/chat/messages')
def chat_messages():
    """Fetch messages for a channel. ?channel=general or ?dm=<username>.
    ?after=<id> returns only newer messages (for polling)."""
    if not session.get("logged"):
        return jsonify({"error": "Unauthorized"}), 401
    me = session.get("username", "")
    dm_user = (request.args.get('dm', '') or "").strip()
    if dm_user:
        if not User.query.filter_by(username=dm_user).first():
            return jsonify({"error": "User not found"}), 404
        channel = _dm_channel(me, dm_user)
    else:
        channel = "general"
    try:
        after = int(request.args.get('after', 0))
    except ValueError:
        after = 0

    q = ChatMessage.query.filter_by(channel=channel)
    if after:
        q = q.filter(ChatMessage.id > after)
    msgs = q.order_by(ChatMessage.id).limit(200).all()

    # Mark as read up to the latest message in this channel
    if msgs:
        last_id = msgs[-1].id
        rd = ChatRead.query.filter_by(username=me, channel=channel).first()
        if not rd:
            rd = ChatRead(username=me, channel=channel, last_read_id=last_id)
            db.session.add(rd)
        elif last_id > rd.last_read_id:
            rd.last_read_id = last_id
        db.session.commit()

    return jsonify({"messages": [m.to_dict() for m in msgs], "channel": channel, "me": me})

@app.route('/api/chat/send', methods=['POST'])
def chat_send():
    if not session.get("logged"):
        return jsonify({"success": False, "error": "Unauthorized"})
    me = session.get("username", "")
    body = (request.form.get('body', '') or "").strip()
    dm_user = (request.form.get('dm', '') or "").strip()
    if not body:
        return jsonify({"success": False, "error": "Message is empty"})
    if len(body) > 2000:
        body = body[:2000]
    if dm_user:
        if not User.query.filter_by(username=dm_user).first():
            return jsonify({"success": False, "error": "User not found"})
        channel = _dm_channel(me, dm_user)
    else:
        channel = "general"

    msg = ChatMessage(channel=channel, sender=me, body=body)
    db.session.add(msg)
    db.session.commit()

    # Notify @mentioned users via webhook (Make can route to SMS/email if desired)
    mentions = _extract_mentions(body)
    if mentions:
        valid = [u.username for u in User.query.filter(User.username.in_(mentions)).all()]
        if valid:
            fire_webhook({
                "type":       "chat_mention",
                "from":       me,
                "mentioned":  valid,
                "channel":    "general" if channel == "general" else "dm",
                "message":    body[:300],
            })
    return jsonify({"success": True, "message": msg.to_dict()})

@app.route('/api/chat/dm-overview')
def chat_dm_overview():
    """Master / club owner oversight: list every DM conversation with its participants
    and last message. Does not touch read state."""
    if not session.get("logged") or session.get("role") not in ("master", "club_owner"):
        return jsonify({"error": "Unauthorized"}), 401
    # Distinct DM channels
    channels = (db.session.query(ChatMessage.channel)
                .filter(ChatMessage.channel.like("dm:%"))
                .distinct().all())
    convos = []
    for (ch,) in channels:
        try:
            pair = ch[3:].split("|")
        except Exception:
            continue
        last = (ChatMessage.query.filter_by(channel=ch)
                .order_by(ChatMessage.id.desc()).first())
        count = ChatMessage.query.filter_by(channel=ch).count()
        convos.append({
            "channel": ch,
            "participants": pair,
            "count": count,
            "last_sender": last.sender if last else "",
            "last_body": (last.body[:80] if last else ""),
            "last_at": vegas_time(last.created_at) if last else "",
            "last_id": last.id if last else 0,
        })
    convos.sort(key=lambda c: c["last_id"], reverse=True)
    return jsonify({"conversations": convos})

@app.route('/api/chat/dm-view')
def chat_dm_view():
    """Master / club owner: read a specific DM conversation (?channel=dm:a|b)."""
    if not session.get("logged") or session.get("role") not in ("master", "club_owner"):
        return jsonify({"error": "Unauthorized"}), 401
    channel = (request.args.get('channel', '') or "").strip()
    if not channel.startswith("dm:"):
        return jsonify({"error": "Invalid channel"}), 400
    msgs = (ChatMessage.query.filter_by(channel=channel)
            .order_by(ChatMessage.id).limit(500).all())
    pair = channel[3:].split("|")
    return jsonify({"messages": [m.to_dict() for m in msgs], "participants": pair})

@app.route('/api/chat/contacts')
def chat_contacts():
    """List users to DM, plus unread counts per channel for the badge."""
    if not session.get("logged"):
        return jsonify({"error": "Unauthorized"}), 401
    me = session.get("username", "")
    users = [u.username for u in User.query.order_by(User.username).all() if u.username != me]

    # Unread in general
    def unread_for(channel):
        rd = ChatRead.query.filter_by(username=me, channel=channel).first()
        last_read = rd.last_read_id if rd else 0
        return (ChatMessage.query.filter(ChatMessage.channel == channel,
                                         ChatMessage.id > last_read,
                                         ChatMessage.sender != me).count())

    general_unread = unread_for("general")
    dm_unread = {}
    total_dm = 0
    for u in users:
        ch = _dm_channel(me, u)
        n = unread_for(ch)
        if n:
            dm_unread[u] = n
            total_dm += n

    return jsonify({
        "me": me, "users": users,
        "general_unread": general_unread,
        "dm_unread": dm_unread,
        "total_unread": general_unread + total_dm,
    })

@app.route('/api/last-client')
@require_api_key
def last_client():
    c = Customer.query.order_by(Customer.id.desc()).first()
    if not c:
        return jsonify({"error": "No clients found"})
    return jsonify(c.to_dict())

# ─── GOBEST APP APIs ──────────────────────────────────────────────────────────
# US state centroids (lat, lng) — used as a fallback to figure out which state a
# coordinate is in without an external call, and to sanity-check reverse geocoding.
US_STATES = {
    "Nevada": (39.5, -116.9), "California": (36.7, -119.7), "Arizona": (34.2, -111.6),
    "Utah": (39.3, -111.7), "Texas": (31.5, -99.3), "New Mexico": (34.5, -106.1),
    "Florida": (27.8, -81.7), "New York": (42.9, -75.5),
}

def _haversine_mi(lat1, lng1, lat2, lng2):
    from math import radians, sin, cos, sqrt, atan2
    R = 3958.8
    dlat = radians(lat2 - lat1); dlng = radians(lng2 - lng1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlng/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))

def _state_from_coords(lat, lng):
    """Best-effort: reverse geocode to a US state. Falls back to nearest-centroid
    if the geocoder is unavailable, so the app still works offline-ish."""
    try:
        data = _nominatim_get({"lat": lat, "lon": lng, "format": "json", "zoom": 5},
                              path="reverse", timeout=8)
        st = ((data or {}).get('address', {}) or {}).get('state', '')
        if st:
            return st
    except Exception as e:
        print(f"[GOBEST] reverse geocode failed: {e}", flush=True)
    # Fallback: nearest known state centroid
    best, bestd = None, 1e9
    for name, (slat, slng) in US_STATES.items():
        d = _haversine_mi(lat, lng, slat, slng)
        if d < bestd:
            best, bestd = name, d
    return best or ""

@app.route('/api/app/detect-state', methods=['GET', 'POST'])
def app_detect_state():
    """The app sends the user's coordinates; we return which state they're in and
    whether that state has GoBest clubs (so the app knows to show the welcome)."""
    if request.method == 'POST':
        data = request.get_json(silent=True) or request.form
    else:
        data = request.args
    try:
        lat = float(data.get('lat'))
        lng = float(data.get('lng'))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "lat and lng are required"})
    state = _state_from_coords(lat, lng)
    club_count = Club.query.filter(Club.active == True, Club.gobest == True,
                                   func.lower(Club.state) == (state or "").lower()).count()
    return jsonify({
        "success": True,
        "state": state,
        "has_clubs": club_count > 0,
        "club_count": club_count,
        "welcome_message": f"Welcome to {state}! Be sure to check out our clubs and scan the QR code for a free drink!" if club_count else "",
    })

@app.route('/api/app/clubs', methods=['GET'])
def app_clubs():
    """Return GoBest clubs, optionally filtered by state, with distance from the
    user's coordinates (if provided), sorted nearest-first."""
    state = (request.args.get('state') or "").strip()
    try:
        u_lat = float(request.args.get('lat')) if request.args.get('lat') else None
        u_lng = float(request.args.get('lng')) if request.args.get('lng') else None
    except (TypeError, ValueError):
        u_lat = u_lng = None

    q = Club.query.filter(Club.active == True, Club.gobest == True)
    if state:
        q = q.filter(func.lower(Club.state) == state.lower())
    clubs = q.all()

    out = []
    for c in clubs:
        item = c.to_dict()
        if u_lat is not None and u_lng is not None and c.lat is not None and c.lng is not None:
            item["distance_mi"] = round(_haversine_mi(u_lat, u_lng, c.lat, c.lng), 1)
        else:
            item["distance_mi"] = None
        out.append(item)

    # Sort by distance when we have it, else by name
    if u_lat is not None and u_lng is not None:
        out.sort(key=lambda x: (x["distance_mi"] is None, x["distance_mi"] or 0))
    else:
        out.sort(key=lambda x: x["name"])
    return jsonify({"success": True, "state": state, "count": len(out), "clubs": out})

# ─── APP AUTH (SMS login) + PROFILE ───────────────────────────────────────────
import secrets as _secrets

# Demo account for App Store / Play Store reviewers. Entering this phone number
# skips the SMS step and accepts the fixed code below. Can be overridden via env.
DEMO_PHONE = clean_phone(os.environ.get("DEMO_PHONE", "7025550100"))
DEMO_CODE  = os.environ.get("DEMO_CODE", "424242")

def _app_user_from_token():
    """Resolve the logged-in AppUser from the Authorization header or ?token=."""
    tok = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        tok = auth[7:].strip()
    if not tok:
        tok = (request.args.get("token") or "").strip()
    if not tok:
        data = request.get_json(silent=True) or {}
        tok = (data.get("token") or "").strip()
    if not tok:
        return None
    return AppUser.query.filter_by(auth_token=tok).first()

@app.route('/api/app/auth/send-code', methods=['POST'])
def app_auth_send_code():
    """Step 1 of login: the user enters their phone; we text them a 6-digit code."""
    data = request.get_json(silent=True) or request.form
    phone = clean_phone(data.get("phone") or "")
    if not phone or len(phone) < 10:
        return jsonify({"success": False, "error": "Enter a valid phone number."})
    # Demo/review account (for App Store / Play Store reviewers): the magic number
    # skips SMS entirely and accepts a fixed code. Never sends a real text.
    if phone == DEMO_PHONE:
        return jsonify({"success": True, "message": "Code sent."})
    # Rate limit: max 1 code per 30s per phone
    recent = OTPCode.query.filter_by(phone=phone).order_by(OTPCode.created_at.desc()).first()
    if recent and (datetime.utcnow() - recent.created_at).total_seconds() < 30:
        return jsonify({"success": False, "error": "Please wait a moment before requesting another code."})
    code = "".join(_secrets.choice("0123456789") for _ in range(6))
    otp = OTPCode(phone=phone, code=code, expires_at=datetime.utcnow() + timedelta(minutes=10))
    db.session.add(otp)
    db.session.commit()
    # Send the SMS
    msg = f"Your ClubLifter verification code is {code}. It expires in 10 minutes."
    sent = send_sms(phone, msg)
    if not (sent and sent.get("ok")):
        # Tell the user the truth instead of "Code sent." (the old behaviour hid
        # a dead sending number for weeks). The Telnyx error is in the server log.
        print(f"[APP-AUTH] SMS send failed for {phone}: {sent}", flush=True)
        return jsonify({"success": False,
                        "error": "We couldn't send the text right now. Please try again in a minute."})
    return jsonify({"success": True, "message": "Code sent."})

@app.route('/api/app/auth/verify', methods=['POST'])
def app_auth_verify():
    """Step 2 of login: the user enters the code. On success we create/return the
    AppUser and a device token."""
    data = request.get_json(silent=True) or request.form
    phone = clean_phone(data.get("phone") or "")
    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()
    if not phone or not code:
        return jsonify({"success": False, "error": "Phone and code are required."})

    # Demo/review account: the magic number accepts the fixed demo code directly.
    if phone == DEMO_PHONE:
        if code != DEMO_CODE:
            return jsonify({"success": False, "error": "Incorrect code. Try again."})
        user = AppUser.query.filter_by(phone=phone).first()
        if not user:
            user = AppUser(phone=phone, name=name or "Reviewer", verified=True,
                           points=95, checkin_count=8)  # seeded so reviewers see the loyalty UI populated
            db.session.add(user)
        user.auth_token = _secrets.token_hex(24)
        user.last_seen = datetime.utcnow()
        db.session.commit()
        return jsonify({"success": True, "token": user.auth_token, "user": user.to_dict()})

    otp = OTPCode.query.filter_by(phone=phone).order_by(OTPCode.created_at.desc()).first()
    if not otp:
        return jsonify({"success": False, "error": "Request a code first."})
    if datetime.utcnow() > otp.expires_at:
        return jsonify({"success": False, "error": "That code expired. Request a new one."})
    if otp.attempts >= 5:
        return jsonify({"success": False, "error": "Too many attempts. Request a new code."})
    otp.attempts += 1
    db.session.commit()
    if code != otp.code:
        return jsonify({"success": False, "error": "Incorrect code. Try again."})

    # Success — find or create the user
    user = AppUser.query.filter_by(phone=phone).first()
    if not user:
        user = AppUser(phone=phone, name=name, verified=True)
        db.session.add(user)
    else:
        user.verified = True
        if name and not user.name:
            user.name = name
    user.auth_token = _secrets.token_hex(24)
    user.last_seen = datetime.utcnow()
    db.session.commit()
    # Clean up used codes for this phone
    OTPCode.query.filter_by(phone=phone).delete()
    db.session.commit()
    return jsonify({"success": True, "token": user.auth_token, "user": user.to_dict()})

@app.route('/api/app/profile', methods=['GET', 'POST'])
def app_profile():
    """Return the logged-in user's profile (level, points, check-ins). POST can
    update the name."""
    user = _app_user_from_token()
    if not user:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    if request.method == 'POST':
        data = request.get_json(silent=True) or request.form
        new_name = (data.get("name") or "").strip()
        if new_name:
            user.name = new_name
            db.session.commit()
    user.last_seen = datetime.utcnow()
    db.session.commit()
    # Recent check-ins
    checkins = (CheckIn.query.filter_by(user_id=user.id)
                .order_by(CheckIn.created_at.desc()).limit(10).all())
    recent = [{"club_name": c.club_name, "points": c.points,
               "date": c.created_at.strftime("%b %d") if c.created_at else ""} for c in checkins]
    out = user.to_dict()
    out["recent_checkins"] = recent
    # Include the full level ladder so the app can show progress
    out["levels"] = [{"level": n, "name": APP_LEVELS[n]["name"],
                      "min_points": APP_LEVELS[n]["min_points"], "perk": APP_LEVELS[n]["perk"]}
                     for n in sorted(APP_LEVELS)]
    return jsonify({"success": True, "user": out})

@app.route('/api/app/history', methods=['GET'])
def app_history():
    """Full visit history for the logged-in user: every check-in (most recent
    first) plus a per-venue summary (how many times they've been to each club)."""
    user = _app_user_from_token()
    if not user:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    checkins = (CheckIn.query.filter_by(user_id=user.id)
                .order_by(CheckIn.created_at.desc()).all())
    visits = [{
        "club_name": c.club_name,
        "points": c.points,
        "date": c.created_at.strftime("%b %d, %Y") if c.created_at else "",
        "time": c.created_at.strftime("%-I:%M %p") if c.created_at else "",
    } for c in checkins]
    # Per-venue summary
    by_club = {}
    for c in checkins:
        key = c.club_name or "Unknown"
        if key not in by_club:
            by_club[key] = {"club_name": key, "visits": 0, "points": 0, "last": ""}
        by_club[key]["visits"] += 1
        by_club[key]["points"] += (c.points or 0)
        if not by_club[key]["last"] and c.created_at:
            by_club[key]["last"] = c.created_at.strftime("%b %d, %Y")
    venues = sorted(by_club.values(), key=lambda v: v["visits"], reverse=True)
    return jsonify({
        "success": True,
        "total_visits": len(checkins),
        "total_points": sum(c.points or 0 for c in checkins),
        "venues_count": len(by_club),
        "visits": visits,
        "venues": venues,
    })

@app.route('/api/app/account/delete', methods=['POST'])
def app_account_delete():
    """Permanently delete the logged-in user's account and personal data. Required
    by App Store guidelines for any app with account creation. Removes the AppUser
    and their check-in history; the auth token is invalidated immediately."""
    user = _app_user_from_token()
    if not user:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    uid = user.id
    try:
        # Delete the user's check-in history
        CheckIn.query.filter_by(user_id=uid).delete()
        # Delete any pending OTP codes for this phone
        try:
            OTPCode.query.filter_by(phone=user.phone).delete()
        except Exception:
            pass
        # Delete the account itself
        db.session.delete(user)
        db.session.commit()
        return jsonify({"success": True, "message": "Your account and personal data have been permanently deleted."})
    except Exception as e:
        db.session.rollback()
        print(f"[ACCOUNT-DELETE] failed for user {uid}: {e}", flush=True)
        return jsonify({"success": False, "error": "Could not delete account. Please try again."})

@app.route('/api/app/chat/report', methods=['POST'])
def app_chat_report():
    """Report an inappropriate chat message (App Store content-moderation
    requirement). Flags the message for staff review. Body: {message_id, reason}."""
    user = _app_user_from_token()
    if not user:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    data = request.get_json(silent=True) or request.form
    msg_id = _safe_int(data.get("message_id"))
    reason = (data.get("reason") or "").strip()[:300]
    msg = RideChatMessage.query.get(msg_id) if msg_id else None
    if not msg:
        return jsonify({"success": False, "error": "Message not found."})
    # Record the report as a webhook + server log so staff can act on it.
    fire_webhook({
        "type":          "chat_message_reported",
        "message_id":    msg.id,
        "customer_id":   msg.customer_id,
        "message_body":  msg.body,
        "message_sender": msg.sender,
        "reported_by_phone": user.phone,
        "reason":        reason,
    })
    print(f"[CHAT-REPORT] msg {msg.id} reported by {user.phone}: {reason}", flush=True)
    return jsonify({"success": True, "message": "Thanks — this message has been reported to our team for review."})

@app.route('/api/app/chat/block', methods=['POST'])
def app_chat_block():
    """Block further chat on the user's active ride (App Store blocking
    requirement). Stops the customer from seeing/sending more messages on it."""
    user = _app_user_from_token()
    if not user:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    c = _app_active_ride(user)
    if not c:
        return jsonify({"success": False, "error": "No active ride."})
    fire_webhook({
        "type":          "ride_chat_blocked",
        "customer_id":   c.id,
        "blocked_by_phone": user.phone,
    })
    print(f"[CHAT-BLOCK] ride {c.id} chat blocked by {user.phone}", flush=True)
    return jsonify({"success": True, "message": "You won't receive further messages on this ride."})

@app.route('/api/app/checkin', methods=['POST'])
def app_checkin():
    """Record a check-in when a logged-in user scans a club QR. Awards points
    (with a per-club cooldown) and returns the updated level + any new unlock."""
    user = _app_user_from_token()
    if not user:
        return jsonify({"success": False, "error": "Not logged in", "need_login": True}), 401
    data = request.get_json(silent=True) or request.form
    raw = (data.get("code") or "").strip()
    code = raw.split(":", 1)[1].strip() if raw.upper().startswith("GOBEST:") else raw

    # Resolve the club from the QR code (managed code or club name/id)
    club = None
    item = QRCodeItem.query.filter(func.upper(QRCodeItem.code) == code.upper()).first()
    if item and item.active:
        club = Club.query.get(item.club_id) if item.club_id else None
        club_name = item.club_name
    else:
        if code.isdigit():
            club = Club.query.get(int(code))
        if not club:
            club = Club.query.filter(func.lower(Club.name) == code.lower()).first()
        club_name = club.name if club else ""
    if not club_name:
        return jsonify({"success": False, "error": "That code isn't a valid venue."})

    # Cooldown: no repeat points at the SAME club within the window
    club_id = club.id if club else None
    cutoff = datetime.utcnow() - timedelta(hours=CHECKIN_COOLDOWN_HOURS)
    recent_same = (CheckIn.query.filter_by(user_id=user.id, club_name=club_name)
                   .filter(CheckIn.created_at >= cutoff).first())
    old_level = user.level()
    if recent_same:
        awarded = 0
    else:
        awarded = POINTS_PER_CHECKIN
        user.points = (user.points or 0) + awarded
        user.checkin_count = (user.checkin_count or 0) + 1
        db.session.add(CheckIn(user_id=user.id, club_id=club_id, club_name=club_name, points=awarded))
    db.session.commit()
    new_level = user.level()
    leveled_up = new_level > old_level

    return jsonify({
        "success": True,
        "club_name": club_name,
        "points_awarded": awarded,
        "already_checked_in": awarded == 0,
        "leveled_up": leveled_up,
        "new_level": new_level,
        "new_level_name": APP_LEVELS.get(new_level, {}).get("name", ""),
        "new_perk": APP_LEVELS.get(new_level, {}).get("perk", "") if leveled_up else "",
        "user": user.to_dict(),
    })

@app.route('/api/app/club-review-link', methods=['GET'])
def app_club_review_link():
    """Return the Google review link for a club (for the 'Enjoying your stay?'
    prompt). Uses the club's saved review URL, or builds a Google search fallback."""
    club_id = _safe_int(request.args.get("club_id"))
    club = Club.query.get(club_id) if club_id else None
    if not club:
        name = (request.args.get("club_name") or "").strip()
        if name:
            club = Club.query.filter(func.lower(Club.name) == name.lower()).first()
    if not club:
        return jsonify({"success": False, "error": "Club not found"})
    # Prefer an explicit review URL if the club has one; else Google search
    link = (club.review_url or "").strip() if hasattr(club, "review_url") else ""
    if not link:
        import urllib.parse as _up
        q = _up.quote(f"{club.name} {club.address or ''} reviews")
        link = f"https://www.google.com/search?q={q}"
    return jsonify({"success": True, "club_name": club.name, "review_link": link})

@app.route('/api/app/banner', methods=['GET'])
def app_banner():
    """Return an active ad banner to show in the app (rotates by weight). Returns
    empty when there are none, so the app shows its 'Your ad here' placeholder."""
    banners = AdBanner.query.filter(AdBanner.active == True, AdBanner.image_url != "").all()
    if not banners:
        return jsonify({"success": True, "banner": None})
    # Weighted random pick
    import random as _r
    pool = []
    for b in banners:
        pool.extend([b] * max(1, b.weight or 1))
    chosen = _r.choice(pool)
    chosen.impressions = (chosen.impressions or 0) + 1
    db.session.commit()
    return jsonify({"success": True, "banner": {
        "id": chosen.id, "image_url": chosen.image_url, "link_url": chosen.link_url,
    }})

@app.route('/api/app/banner/click/<int:banner_id>', methods=['POST'])
def app_banner_click(banner_id):
    """Record a tap on a banner (for the advertiser's click count)."""
    b = AdBanner.query.get(banner_id)
    if b:
        b.clicks = (b.clicks or 0) + 1
        db.session.commit()
    return jsonify({"success": True})

@app.route('/api/app/redeem', methods=['POST'])
def app_redeem():
    """Called when a user scans a QR code OR types a code into the app. Resolves
    either a managed QR code (QRCodeItem) or, for the original demo codes, a club
    id/name. Returns the free-drink details to display."""
    data = request.get_json(silent=True) or request.form
    raw = (data.get('code') or "").strip()
    code = raw
    if code.upper().startswith("GOBEST:"):
        code = code.split(":", 1)[1].strip()

    # 1) Managed QR code (also what typed codes match) — case-insensitive
    item = QRCodeItem.query.filter(func.upper(QRCodeItem.code) == code.upper()).first()
    if item:
        if not item.active:
            return jsonify({"success": False, "error": "This code is no longer active."})
        club = Club.query.get(item.club_id) if item.club_id else None
        item.scan_count = (item.scan_count or 0) + 1
        db.session.commit()
        return jsonify({
            "success": True,
            "club_name": item.club_name or (club.name if club else "ClubLifter"),
            "free_drink": item.drink or (club.free_drink if club else "") or "a complimentary welcome drink",
            "instructions": "Show this screen and a valid photo ID to the bartender to claim your free drink.",
        })

    # 2) Legacy/demo: club id or name
    club = None
    if code.isdigit():
        club = Club.query.get(int(code))
    if not club:
        club = Club.query.filter(func.lower(Club.name) == code.lower()).first()
    if not club or not club.gobest:
        return jsonify({"success": False, "error": "That code isn't valid. Check the code and try again."})
    return jsonify({
        "success": True,
        "club_name": club.name,
        "free_drink": club.free_drink or "a complimentary welcome drink",
        "instructions": "Show this screen and a valid photo ID to the bartender to claim your free drink.",
    })

# ─── AI AGENT APIs ────────────────────────────────────────────────────────────
def _safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def _find_customer_by_phone(phone):
    """Find the most recent ride whose main or extra phone matches (ignoring
    formatting, country code, and invisible characters). Returns a Customer or None."""
    def _last10(p):
        d = ''.join(ch for ch in clean_phone(p) if ch.isdigit())
        return d[-10:] if len(d) >= 10 else d
    target = _last10(phone or "")
    if not target:
        return None
    for c in Customer.query.order_by(Customer.id.desc()).limit(1000).all():
        candidates = [_last10(c.phone)] + [_last10(p) for p in c.get_phones()]
        if target in [p for p in candidates if p]:
            return c
    return None

def _ride_status_info(c):
    """Single, clear ride status for partners, the AI agent, the app and the guest
    list. Returns (code, human_text). Codes:
      unassigned  → booked, no driver yet (NEW — used to be hidden as "scheduled")
      scheduled   → driver assigned, not on the way yet
      enroute     → driver on the way to the pickup (driver tap or GPS)
      arrived     → driver at the pickup (driver tap or GPS dwell)
      picked_up   → guest on board (driver tap or GPS: car left the pickup)
      dropped_off → guest delivered (driver tap or GPS at the venue)
      at_venue    → guest at the venue (dispatcher/partner/GPS)
      left        → guest left the venue (dispatcher/partner, or their ride back started)
      waitlist    → waitlisted
    Time-based guesses are marked "(estimated)" / status_estimated=true, and the
    background failsafe (auto_timeouts) turns them into real completions."""
    code, text, _est = _ride_status_full(c)
    return code, text

@app.route('/api/ai/lookup', methods=['GET'])
@require_api_key
def ai_lookup():
    """AI agent: look up a booking by the caller's phone number.
    GET /api/ai/lookup?phone=+17025307390
    Returns the most recent matching ride, or found=false."""
    phone = request.args.get('phone', '') or request.args.get('from', '')
    c = _find_customer_by_phone(phone)
    if not c:
        return jsonify({"found": False, "message": "No booking found for that phone number."})
    ride_status, ride_status_text = _ride_status_info(c)
    extras = ride_status_extras(c)
    return jsonify({
        "found": True,
        "customer_id":     c.id,
        "name":            c.nome,
        "phone":           c.phone,
        "pickup_datetime": c.pickup_datetime,
        "pickup_location": c.endereco,
        "destination":     c.destination,
        # ── Ride progression status (scheduled → picked_up → dropped_off → at_venue → left) ──
        "status":          ride_status,        # the clear, unified status (use this)
        "ride_status":     ride_status,         # alias (same value)
        "ride_status_text": ride_status_text,   # human-readable
        "transport_status": c.status,           # raw transport field: scheduled|picked_up|dropped_off
        "club_status":     c.club_status,       # raw club field: coming|arrived|left
        "picked_up":       bool(c.picked_up_at),
        "picked_up_at":    vegas_datetime(c.picked_up_at) if c.picked_up_at else "",
        "driver_arrived":  bool(c.driver_arrived_at),
        "driver_arrived_at": vegas_datetime(c.driver_arrived_at) if c.driver_arrived_at else "",
        "dropped_off":     bool(c.dropped_off_at),
        "dropped_off_at":  vegas_datetime(c.dropped_off_at) if c.dropped_off_at else "",
        "dropoff_verified": c.dropoff_verified,
        "status_source":    extras["status_source"],      # driver | gps | dispatch | partner | estimated
        "status_estimated": extras["status_estimated"],   # true = guessed from the clock, not confirmed
        "status_updated_at": extras["status_updated_at"],
        "driver_eta_min":   extras.get("driver_eta_min"),
        "driver_distance_mi": extras.get("driver_distance_mi"),
        # ── Driver + booking details ──
        "driver_name":     extras["driver_name"],
        "driver_phone":    extras["driver_phone"],
        "driver_assigned": extras["driver_assigned"],
        "car":             c.completed_car or c.car_string_val,
        "guests":          c.guests,
        "package":         c.package,
        "notes":           c.details,
        "driver_notes":    c.driver_notes,
        "reported":        c.reported,
        "report_reason":   c.report_reason,
        "priority_level":  priority_level(c),
        "package_price":   _package_price(c.package),
        "price_total":     c.price_total,
    })

@app.route('/api/ai/update-notes', methods=['POST'])
@require_api_key
def ai_update_notes():
    """AI agent: add or replace the notes/details on an existing ride.
    POST JSON:
      { "phone": "+17025307390", "notes": "Guest is at the north entrance" }
      or { "customer_id": 123, "notes": "...", "append": true }
    append=true adds to existing notes instead of replacing."""
    data = request.get_json(silent=True) or request.form
    notes = (data.get("notes") or data.get("details") or "").strip()
    if not notes:
        return jsonify({"success": False, "error": "notes is required"})
    cust = None
    if data.get("customer_id"):
        cust = Customer.query.get(_safe_int(data.get("customer_id")))
    if not cust and data.get("phone"):
        cust = _find_customer_by_phone(data.get("phone"))
    if not cust:
        return jsonify({"success": False, "error": "No booking found for that customer."})
    append = str(data.get("append", "")).lower() in ("true", "1", "yes")
    if append and cust.details:
        cust.details = f"{cust.details} | {notes}"[:500]
    else:
        cust.details = notes[:500]
    db.session.commit()
    return jsonify({"success": True, "customer_id": cust.id, "notes": cust.details})

@app.route('/api/v1/reschedule', methods=['POST'])
@app.route('/api/ai/reschedule', methods=['POST'])
@require_api_key
def api_reschedule():
    """Reschedule a booking to a new pickup date/time. Reuses the same logic as
    the dispatcher reschedule: normalizes the date, re-arms proximity alerts,
    fires the pickup_rescheduled webhook, and texts the assigned driver about the
    new time.

    POST JSON or form:
      { "phone": "+17025307390", "pickup_datetime": "09/15/2026 11:00 PM" }
      or { "customer_id": 462, "pickup_datetime": "09/15/2026 11:00 PM" }
    """
    data = request.get_json(silent=True) or request.form
    raw = (data.get("pickup_datetime") or data.get("datetime") or "").strip()
    if not raw:
        return jsonify({"success": False, "error": "pickup_datetime is required"})

    # Resolve the booking (by customer_id or phone)
    cust = None
    if data.get("customer_id"):
        cust = Customer.query.get(_safe_int(data.get("customer_id")))
    if not cust and data.get("phone"):
        cust = _find_customer_by_phone(data.get("phone"))
    if not cust:
        return jsonify({"success": False, "error": "No booking found for that customer."})

    # Normalize + validate the new date/time (same as dispatcher reschedule)
    normalized = normalize_pickup_datetime(raw)
    if parse_pickup_datetime(normalized) is None:
        return jsonify({"success": False,
                        "error": "Couldn't read that date/time. Use MM/DD/YYYY HH:MM AM/PM."})

    old = cust.pickup_datetime
    cust.pickup_datetime = normalized
    # Re-arm proximity alerts + GPS detection for the new time
    reset_ride_tracking(cust)
    db.session.commit()
    log_event(cust, "reschedule", f"{old} → {normalized} via API from {_request_origin()}", actor="api")

    # Fire the same webhook the dispatcher reschedule uses
    fire_webhook({
        "type":            "pickup_rescheduled",
        "customer_id":     cust.id,
        "customer_name":   cust.nome,
        "customer_phone":  cust.phone,
        "old_datetime":    old,
        "new_datetime":    normalized,
        "driver_name":     cust.motorista,
        "destination":     cust.destination,
        "changed_by":      "api",
    })

    # Notify the assigned driver about the new time (reuses the SMS system)
    driver_notified = False
    _dname, _dphone = ride_driver_display(cust)
    has_driver = bool(_dname)
    if has_driver:
        driver_phone = clean_phone(_dphone or "")
        if driver_phone:
            send_sms_bg(driver_phone,
                f"ClubLifter: pickup time changed.\n"
                f"Guest: {cust.nome}\nPickup: {cust.endereco}\n"
                f"New time: {normalized}\nDrop-off: {cust.destination or 'N/A'}")
            driver_notified = True

    return jsonify({
        "success": True,
        "customer_id": cust.id,
        "old_datetime": old,
        "new_datetime": normalized,
        "driver_name": _dname if has_driver else None,
        "driver_notified": driver_notified,
    })

@app.route('/api/v1/status', methods=['GET', 'POST'])
@app.route('/api/ai/status', methods=['GET', 'POST'])
@require_api_key
def api_set_status():
    """Update a booking's club status (and, optionally, mark the ride redeemed).
    Designed to be called by a partner system — accepts GET (query string) or POST
    (JSON/form).

    Identify the booking by ONE of:
      phone=+16044188302   OR   customer_id=630

    Set the status with:
      club_status=arrived     (coming | arrived | left)

    Convenience aliases (any of these also work):
      status=arrived                 (same as club_status)
      redeemed=1  /  redeemed=true   → marks club_status=arrived
                                       (guest showed up / used the ride)

    Examples:
      GET /api/v1/status?phone=%2B16044188302&club_status=arrived
      GET /api/v1/status?customer_id=630&redeemed=1
      POST {"customer_id":630,"club_status":"left"}
    """
    data = request.values if request.method == "GET" else (request.get_json(silent=True) or request.form or request.values)

    # Find the booking
    cust = None
    cid = data.get("customer_id")
    if cid:
        cust = Customer.query.get(_safe_int(cid))
    if not cust and data.get("phone"):
        cust = _find_customer_by_phone(data.get("phone"))
    if not cust:
        return jsonify({"success": False, "error": "No booking found for that customer."}), 404

    # Work out the requested club_status
    new_status = (data.get("club_status") or data.get("status") or "").strip().lower()
    redeemed = str(data.get("redeemed", "")).strip().lower() in ("1", "true", "yes", "redeemed")
    if not new_status and redeemed:
        new_status = "arrived"   # "redeemed" means the guest showed up at the venue

    valid = {"coming", "arrived", "left"}
    if new_status not in valid:
        return jsonify({
            "success": False,
            "error": f"Provide club_status as one of {sorted(valid)} (or redeemed=1).",
            "current_club_status": cust.club_status,
        }), 400

    old_status = cust.club_status
    _before = _ride_status_info(cust)[0]
    cust.club_status = new_status
    mark_status(cust, "partner")
    # Keep timestamps sensible when moving to arrived/left so the rest of the
    # system (which infers ride progress from these) stays consistent.
    now = datetime.utcnow()
    if new_status in ("arrived", "left"):
        if not cust.dropped_off_at:
            cust.dropped_off_at = now
        if not cust.picked_up_at:
            cust.picked_up_at = cust.dropped_off_at
        if cust.status == "scheduled":
            cust.status = "dropped_off"
        if new_status == "arrived" and not cust.arrived_at:
            cust.arrived_at = now
    db.session.commit()

    fire_webhook({
        "type":           "club_status_updated",
        "customer_id":    cust.id,
        "customer_name":  cust.nome,
        "customer_phone": cust.phone,
        "old_club_status": old_status,
        "new_club_status": new_status,
        "source":         "api",
    })
    ride_status_changed(cust, _before, "partner")

    code, text = _ride_status_info(cust)
    return jsonify({
        "success": True,
        "customer_id": cust.id,
        "name": cust.nome,
        "old_club_status": old_status,
        "club_status": cust.club_status,
        "ride_status": code,
        "ride_status_text": text,
    })

@app.route('/api/v1/ride-back', methods=['POST'])
@app.route('/api/ai/ride-back', methods=['POST'])
@require_api_key
def api_ride_back():
    """Book a return ride (club → original pickup) for an existing booking.
    POST JSON:
      { "phone": "+17025307390", "pickup_datetime": "09/16/2026 2:00 AM" }
      or { "customer_id": 462, "pickup_datetime": "..." }
    pickup_datetime is optional; omit for an ASAP/unspecified return.
    """
    data = request.get_json(silent=True) or request.form
    cust = None
    if data.get("customer_id"):
        cust = Customer.query.get(_safe_int(data.get("customer_id")))
    if not cust and data.get("phone"):
        cust = _find_customer_by_phone(data.get("phone"))
    if not cust:
        return jsonify({"success": False, "error": "No booking found for that customer."})
    pickup_dt = (data.get("pickup_datetime") or data.get("datetime") or "").strip()
    if pickup_dt and parse_pickup_datetime(normalize_pickup_datetime(pickup_dt)) is None:
        return jsonify({"success": False, "error": "Couldn't read that date/time."})
    ride, err = _create_return_ride(cust, pickup_dt, created_by="api")
    if err:
        return jsonify({"success": False, "error": err})
    return jsonify({
        "success": True,
        "return_ride_id": ride.id,
        "original_id": cust.id,
        "pickup": ride.endereco,
        "destination": ride.destination,
        "pickup_datetime": ride.pickup_datetime,
    })

@app.route('/api/app/ride-back', methods=['POST'])
def app_ride_back():
    """App: a logged-in user books their own return ride (club → hotel/origin)."""
    user = _app_user_from_token()
    if not user:
        return jsonify({"success": False, "error": "Not logged in", "need_login": True}), 401
    # Find the user's most recent ORIGINAL ride (not a return ride) by phone
    digits = clean_phone(user.phone)
    target = digits[-10:] if len(digits) >= 10 else digits
    cust = None
    if target:
        cust = (Customer.query
                .filter(Customer.is_return_ride == False)
                .filter((Customer.phone.like(f"%{target}")) | (Customer.phones_json.like(f"%{target}%")))
                .order_by(Customer.id.desc()).first())
    if not cust:
        # fall back to the generic finder
        cust = _find_customer_by_phone(user.phone)
    if not cust:
        return jsonify({"success": False, "error": "No active ride found to return from."})
    if cust.is_return_ride:
        return jsonify({"success": False, "error": "Your most recent ride is already a return ride."})
    data = request.get_json(silent=True) or request.form
    pickup_dt = (data.get("pickup_datetime") or "").strip()
    if pickup_dt and parse_pickup_datetime(normalize_pickup_datetime(pickup_dt)) is None:
        return jsonify({"success": False, "error": "Couldn't read that date/time."})
    ride, err = _create_return_ride(cust, pickup_dt, created_by=f"app:{user.phone}")
    if err:
        return jsonify({"success": False, "error": err})
    return jsonify({
        "success": True,
        "return_ride_id": ride.id,
        "pickup": ride.endereco,
        "destination": ride.destination,
        "pickup_datetime": ride.pickup_datetime or "As soon as possible",
    })

def _app_active_ride(user):
    """Find the logged-in app user's current active ride (scheduled or in progress,
    not dropped off/left). Returns the most recent one."""
    digits = clean_phone(user.phone)
    target = digits[-10:] if len(digits) >= 10 else digits
    if not target:
        return None
    return (Customer.query
            .filter((Customer.phone.like(f"%{target}")) | (Customer.phones_json.like(f"%{target}%")))
            .filter(Customer.club_status != "left")
            .filter(Customer.status != "dropped_off")
            .order_by(Customer.id.desc()).first())

@app.route('/api/debug/demo-ride', methods=['GET'])
def debug_demo_ride():
    """Diagnostic: show every ride matching a phone's last 10 digits and which one
    the app would pick. Call with ?phone=7025307390. TEMPORARY — no auth so it can
    be opened directly in a browser for debugging; remove after."""
    phone = request.args.get("phone", "") or os.environ.get("DEMO_RIDE_PHONE", "")
    digits = clean_phone(phone)
    tail = digits[-10:] if len(digits) >= 10 else digits
    all_rides = (Customer.query
                 .filter((Customer.phone.like(f"%{tail}")) | (Customer.phones_json.like(f"%{tail}%")))
                 .order_by(Customer.id.desc()).all())
    def _info(c):
        code, text = _ride_status_info(c)
        return {
            "id": c.id, "nome": c.nome, "phone": c.phone,
            "is_return_ride": c.is_return_ride,
            "motorista": c.motorista, "car_name": c.car_name,
            "status": c.status, "club_status": c.club_status,
            "dispatch_status": c.dispatch_status,
            "computed_status": code, "computed_text": text,
            "needs_transport": c.needs_transport,
        }
    # What _app_active_ride would pick:
    picked = (Customer.query
              .filter((Customer.phone.like(f"%{tail}")) | (Customer.phones_json.like(f"%{tail}%")))
              .filter(Customer.club_status != "left")
              .filter(Customer.status != "dropped_off")
              .order_by(Customer.id.desc()).first())
    return jsonify({
        "success": True,
        "searched_phone": phone,
        "tail_10_digits": tail,
        "demo_ride_env": os.environ.get("DEMO_RIDE", "(unset)"),
        "demo_ride_phone_env": os.environ.get("DEMO_RIDE_PHONE", "(unset)"),
        "total_matching_rides": len(all_rides),
        "app_would_pick_id": picked.id if picked else None,
        "app_would_pick": _info(picked) if picked else None,
        "all_rides": [_info(c) for c in all_rides],
    })

@app.route('/api/app/ride-status', methods=['GET'])
def app_ride_status():
    """App: live status of the user's current ride, including the driver's car
    position (from OneStepGPS) once the driver is on the way. Powers the map."""
    user = _app_user_from_token()
    if not user:
        return jsonify({"success": False, "error": "Not logged in", "need_login": True}), 401
    c = _app_active_ride(user)
    if not c:
        return jsonify({"success": True, "has_ride": False})

    ride_status, ride_status_text = _ride_status_info(c)
    # Guest-facing wording for the two dispatcher-side codes
    if ride_status == "unassigned":
        ride_status, ride_status_text = "scheduled", "Your ride is booked — driver details coming soon"
    elif ride_status == "unconfirmed":
        ride_status_text = "We couldn't confirm your pickup — message us if you still need a ride"
    drv_name, _drv_phone = ride_driver_display(c) if c.needs_transport else ("", "")
    has_driver = bool(drv_name)
    # Chat is available for any active (not-yet-finished) ride that has a driver
    # assigned — including while it's still "scheduled" — so the guest can reach
    # their driver before the driver marks themselves en route.
    chat_available = has_driver and ride_status in ("scheduled", "enroute", "arrived", "picked_up")

    # The assigned car's live GPS position (shared, cached fleet lookup)
    car_lat = car_lng = None
    if has_driver and c.car_name and ride_status in ("enroute", "arrived", "picked_up"):
        pos = car_position(c.car_name)
        if pos:
            car_lat, car_lng = pos["lat"], pos["lng"]

    # Demo fallback: only if this is the FAKE demo car (no real GPS device),
    # simulate a moving position near the Strip so the map isn't empty. A real
    # car (e.g. a Sprinter in OneStepGPS) uses its live GPS above.
    if car_lat is None and c.car_name == "ClubLifter Demo Car" and ride_status in ("enroute", "arrived", "picked_up"):
        import math, time as _t
        base_lat, base_lng = 36.1072, -115.1739   # near Aria
        t = _t.time() / 30.0
        car_lat = base_lat + 0.004 * math.sin(t)
        car_lng = base_lng + 0.004 * math.cos(t)

    # Unread driver messages for this customer
    unread = 0
    if chat_available:
        unread = RideChatMessage.query.filter_by(
            customer_id=c.id, sender="driver", read_by_customer=False).count()

    return jsonify({
        "success": True,
        "has_ride": True,
        "customer_id": c.id,
        "status": ride_status,
        "status_text": ride_status_text,
        "destination": c.destination,
        "pickup_location": c.endereco,
        "pickup_datetime": c.pickup_datetime,
        "driver_name": drv_name if has_driver else None,
        "driver_assigned": has_driver,
        "car": c.car_string_val if has_driver else "",
        "car_lat": car_lat,
        "car_lng": car_lng,
        "chat_available": chat_available,
        "unread_messages": unread,
        "is_return_ride": c.is_return_ride,
    })

@app.route('/api/app/chat', methods=['GET'])
def app_chat_get():
    """App: fetch the customer<->driver chat messages for the user's active ride.
    Optional ?after=<id> to get only newer messages (for polling)."""
    user = _app_user_from_token()
    if not user:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    c = _app_active_ride(user)
    if not c:
        return jsonify({"success": True, "messages": [], "customer_id": None})
    after = _safe_int(request.args.get("after", 0))
    q = RideChatMessage.query.filter_by(customer_id=c.id)
    if after:
        q = q.filter(RideChatMessage.id > after)
    msgs = q.order_by(RideChatMessage.id.asc()).all()
    # Mark driver messages as read by the customer
    for m in msgs:
        if m.sender == "driver" and not m.read_by_customer:
            m.read_by_customer = True
    db.session.commit()
    return jsonify({
        "success": True,
        "customer_id": c.id,
        "driver_name": c.motorista or "Your driver",
        "messages": [m.to_dict() for m in msgs],
    })

@app.route('/api/app/chat/send', methods=['POST'])
def app_chat_send():
    """App: the customer sends a message to their driver."""
    user = _app_user_from_token()
    if not user:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    c = _app_active_ride(user)
    if not c:
        return jsonify({"success": False, "error": "No active ride."})
    data = request.get_json(silent=True) or request.form
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"success": False, "error": "Message is empty."})
    m = RideChatMessage(customer_id=c.id, sender="customer", body=body[:1000], read_by_driver=False)
    db.session.add(m)
    db.session.commit()
    return jsonify({"success": True, "message": m.to_dict()})

@app.route('/api/ai/request-driver-call', methods=['POST'])
@require_api_key
def ai_request_driver_call():
    """AI agent: the customer asked to speak with their driver. This starts a
    MASKED Twilio call — Twilio rings the DRIVER first, then bridges the customer.
    Neither party sees the other's real number.

    POST JSON or form:
      { "phone": "+17025307390" }           (the customer's number)
      or { "customer_id": 123 }
    """
    data = request.get_json(silent=True) or request.form
    cust = None
    if data.get("customer_id"):
        cust = Customer.query.get(_safe_int(data.get("customer_id")))
    if not cust and data.get("phone"):
        cust = _find_customer_by_phone(data.get("phone"))
    if not cust:
        return jsonify({"success": False, "error": "No booking found for that customer."})

    # Resolve the real driver + phone (handles old rows holding a car name)
    _dname, _dphone = ride_driver_display(cust)
    driver_phone = clean_phone(_dphone or "")
    customer_phone = clean_phone(cust.phone or "")

    if not _dname:
        return jsonify({"success": False, "error": "No driver is assigned to this ride yet."})

    ok, err = start_masked_call(driver_phone, customer_phone, cust.nome or "your guest")
    if not ok:
        return jsonify({"success": False, "error": err})

    fire_webhook({
        "type":          "masked_call_started",
        "source":        "ai_agent",
        "customer_id":   cust.id,
        "customer_name": cust.nome,
        "driver_name":   cust.motorista,
    })
    return jsonify({
        "success": True,
        "message": f"We're asking {_dname} to call you right now. "
                   "Please keep your phone nearby and answer when it rings.",
        "driver_name": _dname,
    })


@app.route('/api/sms-test', methods=['GET', 'POST'])
def sms_test():
    """Check Twilio config and optionally send a test SMS.
    GET  /api/sms-test            → shows config status (no SMS sent)
    POST /api/sms-test  to=+1...  → sends a test message to that number
    """
    if not (session.get("logged") and is_master()):
        return jsonify({"error": "Unauthorized"}), 401
    status = {
        "provider":              "telnyx",
        "configured":            telnyx_configured(),
        "sms_enabled":           SMS_ENABLED,
        "telnyx_api_key_set":    bool(TELNYX_API_KEY),
        "telnyx_from_number":    TELNYX_FROM_NUMBER or None,
        "telnyx_profile_set":    bool(TELNYX_MESSAGING_PROFILE_ID),
        "public_base_url":       PUBLIC_BASE_URL,
    }
    to = request.form.get('to', '').strip() or request.args.get('to', '').strip()
    if not to:
        return jsonify({"hint": "add ?to=+1702... to send a test text and see Telnyx's answer", "status": status})
    result = send_sms(to, "ClubLifter test message — your Telnyx integration is working.")
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
        visible = list(allowed)
        # Club owners can also see (and manage) their peers
        if session.get("role") == "club_owner" and "club_owner" not in visible:
            visible.append("club_owner")
        users = User.query.filter(User.role.in_(visible)).order_by(User.role, User.username).all()
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
        return jsonify({"success": False, "error": "Your role can't reset passwords."})
    user = User.query.get_or_404(user_id)
    allowed = creatable_roles()
    # You can reset anyone in your creatable set. Club owners can also reset each
    # other (peers). Only master can reset a master.
    role = session.get("role")
    can_manage = (
        is_master()
        or user.role in allowed
        or (role == "club_owner" and user.role == "club_owner")
    )
    if user.role == "master" and not is_master():
        return jsonify({"success": False, "error": "Only a master can reset a master account's password."})
    if not can_manage:
        return jsonify({"success": False, "error": f"You can't manage a '{user.role}' account."})
    new_password = request.form.get('password', '').strip()
    if not new_password:
        return jsonify({"success": False, "error": "Password cannot be empty"})
    if len(new_password) < 6:
        return jsonify({"success": False, "error": "Password must be at least 6 characters."})
    user.set_password(new_password)
    user.is_active = True   # in case it was an un-activated invite
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

def _geocode_club(address):
    """Best-effort geocode an address to (lat, lng, state). Returns (None,None,'')
    on failure so club creation never breaks."""
    if not address:
        return None, None, ""
    try:
        data = _nominatim_get({"q": address, "format": "json", "limit": 1, "addressdetails": 1})
        if data:
            lat = float(data[0]['lat']); lng = float(data[0]['lon'])
            state = (data[0].get('address', {}) or {}).get('state', '')
            return lat, lng, state
    except Exception as e:
        print(f"[CLUB] geocode failed: {e}", flush=True)
    return None, None, ""

def _apply_gobest_fields(club, f):
    """Apply GoBest/app fields from a form to a club, auto-geocoding when the
    address is present but coordinates weren't supplied."""
    club.free_drink = (f.get('free_drink', club.free_drink) or "").strip()
    club.photo_url  = (f.get('photo_url', club.photo_url) or "").strip()
    if 'gobest' in f:
        club.gobest = str(f.get('gobest')).lower() in ('true', '1', 'yes', 'on')
    # State + coords: use provided values, else geocode from the address
    state_in = (f.get('state') or "").strip()
    lat_in = f.get('lat'); lng_in = f.get('lng')
    if lat_in and lng_in:
        try:
            club.lat = float(lat_in); club.lng = float(lng_in)
        except (TypeError, ValueError):
            pass
    if state_in:
        club.state = state_in
    # Auto-geocode if we still don't have coords (or state) and there's an address
    if (club.lat is None or club.lng is None or not club.state) and club.address:
        lat, lng, state = _geocode_club(club.address)
        if lat is not None and (club.lat is None or club.lng is None):
            club.lat = lat; club.lng = lng
        if state and not club.state:
            club.state = state

@app.route('/admin/clubs/new', methods=['POST'])
def new_club():
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    name = request.form.get('name', '').strip()
    if not name: return jsonify({"success": False, "error": "Name is required"})
    if Club.query.filter_by(name=name).first():
        return jsonify({"success": False, "error": "Club already exists"})
    club = Club(name=name, address=request.form.get('address', '').strip())
    _apply_gobest_fields(club, request.form)
    db.session.add(club)
    db.session.commit()
    return jsonify({"success": True, "club": club.to_dict()})

@app.route('/admin/clubs/edit/<int:club_id>', methods=['POST'])
def edit_club(club_id):
    if not is_admin_level(): return jsonify({"success": False, "error": "Unauthorized"})
    club = Club.query.get_or_404(club_id)
    old_address = club.address
    club.name    = request.form.get('name', club.name).strip()
    club.address = request.form.get('address', club.address).strip()
    club.active  = request.form.get('active', 'true').lower() == 'true'
    # If the address changed, clear coords so they re-geocode
    if club.address != old_address:
        club.lat = None; club.lng = None
    _apply_gobest_fields(club, request.form)
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
    if not is_master(): return jsonify({"success": False, "error": "Only the master account can manage cars"})
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
    if not is_master(): return jsonify({"success": False, "error": "Only the master account can manage cars"})
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
    if not is_master(): return jsonify({"success": False, "error": "Only the master account can manage cars"})
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
def _driver_board_rides(driver_name):
    """The rides a logged-in driver should see on their board."""
    today = vegas_today()
    today_np = f"{today.month}/{today.day}/{today.year}"
    today_p  = f"{today.month:02d}/{today.day:02d}/{today.year}"
    # Grace window: an unfinished ride from a previous day stays visible until
    # GRACE_HOURS after its scheduled pickup time (handles ~midnight pickups),
    # then it drops off the driver's board so stale rides don't pile up.
    GRACE_HOURS = 4
    now_vegas = vegas_naive_now()

    def _visible_to_driver(c):
        dt_txt = c.pickup_datetime or ""
        is_today = (today_np in dt_txt or today_p in dt_txt)
        # Completed rides only show for today (history clears next day)
        if c.status == "dropped_off":
            return is_today
        if is_today:
            return True
        pdt = parse_pickup_datetime(dt_txt)
        if not pdt:
            return False
        # Future rides (tomorrow, next week) must NOT show today.
        if pdt > now_vegas:
            return False
        # Past & unfinished: keep only within the grace window.
        return now_vegas <= (pdt + timedelta(hours=GRACE_HOURS))

    # Matched by any of the driver's identities (username / Driver record name)
    # OR the car they're in today.
    names, car_names = driver_scope(driver_name)
    all_customers = Customer.query.order_by(Customer.pickup_datetime).all()
    return [c for c in all_customers
            if ((c.motorista and c.motorista in names)
                or (c.car_name and c.car_name in car_names))
            and _visible_to_driver(c)]

def _driver_board_sig(rides):
    """A short fingerprint of the driver's board: changes when a ride is added,
    removed, moved, or changes status — the page reloads itself when it does."""
    import hashlib
    raw = "|".join(f"{c.id}:{c.status}:{c.dispatch_status}:{c.pickup_datetime}:{c.endereco}"
                   for c in sorted(rides, key=lambda x: x.id))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]

@app.before_request
def _track_driver_activity():
    """Any request from a logged-in driver = they're on duty (dashboard open)."""
    try:
        if session.get("logged") and session.get("role") == "driver":
            mark_driver_seen(session.get("username"))
    except Exception:
        pass

@app.route('/driver/board-state', methods=['GET'])
def driver_board_state():
    """Polled by the driver dashboard; when `sig` changes the page refreshes."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False}), 401
    rides = _driver_board_rides(session.get("username"))
    return jsonify({"success": True, "sig": _driver_board_sig(rides), "count": len(rides)})

@app.route('/driver')
def driver_dashboard():
    if not session.get("logged"):
        return redirect(url_for("login"))
    if session.get("role") != "driver":
        return redirect(url_for("index"))

    driver_name = session.get("username")
    today = vegas_today()
    today_str = f"{today.month}/{today.day}/{today.year}"
    names, car_names = driver_scope(driver_name)
    my_customers = _driver_board_rides(driver_name)
    board_sig = _driver_board_sig(my_customers)

    # Get driver availability status (loose match on the Driver record)
    driver_profile = Driver.query.filter_by(name=driver_name).first()
    if not driver_profile:
        for n in names:
            driver_profile = Driver.query.filter_by(name=n).first()
            if driver_profile:
                break
    driver_available = driver_profile.available if driver_profile else True

    # The driver's currently assigned car (1:1), shown next to Switch Car
    current_car = None
    _car = driver_car_at(driver_profile) if driver_profile else None
    if _car:
        current_car = _car.car_string() if hasattr(_car, 'car_string') else _car.name

    # Driver's live car GPS (for the map) — look up by the car on their pickups or shift
    car_lat = car_lng = None
    car_name = None
    _my_car = driver_car_at(driver_profile) if driver_profile else None
    if _my_car:
        car_name = _my_car.name
    for c in my_customers:
        if not car_name and c.car_name:
            car_name = c.car_name
            break
    _pos = car_position(car_name) if car_name else None
    if _pos:
        car_lat, car_lng = _pos["lat"], _pos["lng"]

    # Build pickup list with coords + distance from car. Coordinates come from the
    # booking (or the geocode cache) — never a live geocoder call per page load.
    pickups = []
    for c in my_customers:
        p_lat, p_lng = _pickup_latlng(c)
        dist_mi = None
        if car_lat and car_lng and p_lat and p_lng:
            dist_mi = round(calcular_distancia(float(car_lat), float(car_lng), p_lat, p_lng), 1)
        time_part = ""
        if c.pickup_datetime and len(c.pickup_datetime.split(' ')) >= 3:
            parts = c.pickup_datetime.split(' '); time_part = f"{parts[1]} {parts[2]}"
        pickups.append({
            "id": c.id, "nome": c.nome, "endereco": c.endereco, "destination": c.destination,
            "package": c.package, "guests": c.guests, "details": c.details,
            "driver_notes": c.driver_notes,
            "reported": c.reported, "report_reason": c.report_reason,
            "reported_by": c.reported_by,
            "priority": bool(getattr(c, 'priority', False)),
            "priority_level": priority_level(c),
            "time": time_part, "status": c.status,
            "dispatch_status": c.dispatch_status,
            "dropped_at": vegas_time(c.dropped_off_at),
            "picked_at": vegas_time(c.picked_up_at),
            "picked_min": (int((datetime.utcnow() - c.picked_up_at).total_seconds() // 60)
                           if (c.status == "picked_up" and c.picked_up_at) else 0),
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
        board_sig=board_sig,
        driver_name=driver_name,
        driver_available=driver_available,
        today_str=today_str,
        current_car=current_car,
        car_lat=car_lat, car_lng=car_lng
    )

@app.route('/driver/report/<int:customer_id>', methods=['POST'])
def driver_report_customer(customer_id):
    """Driver flags an issue with a guest (no-show, rude, wrong address, etc).
    Stored on the ride and surfaced in the guest list + API."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    if not _driver_owns(c):
        return jsonify({"success": False, "error": "Not your pickup"})
    reason = (request.form.get('reason', '') or "").strip()[:500]
    clear = str(request.form.get('clear', '')).lower() in ('true', '1', 'yes')
    if clear:
        c.reported = False
        c.report_reason = ""
        c.reported_by = ""
        c.reported_at = None
        db.session.commit()
        return jsonify({"success": True, "reported": False})
    if not reason:
        return jsonify({"success": False, "error": "Please describe the issue."})
    c.reported = True
    c.report_reason = reason
    c.reported_by = session.get("username", "")
    c.reported_at = datetime.utcnow()
    db.session.commit()
    fire_webhook({
        "type":          "customer_reported",
        "customer_id":   c.id,
        "customer_name": c.nome,
        "reason":        reason,
        "reported_by":   c.reported_by,
        "pickup_datetime": c.pickup_datetime,
        "destination":   c.destination,
    })
    return jsonify({"success": True, "reported": True, "reason": reason})

@app.route('/driver/notes/<int:customer_id>', methods=['POST'])
def driver_save_notes(customer_id):
    """Driver adds/updates their own notes on a pickup (separate from dispatch notes)."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    if not _driver_owns(c):
        return jsonify({"success": False, "error": "Not your pickup"})
    notes = (request.form.get('driver_notes', '') or "").strip()[:500]
    c.driver_notes = notes
    db.session.commit()
    return jsonify({"success": True, "driver_notes": c.driver_notes})

@app.route('/driver/pickup/<int:customer_id>', methods=['POST'])
def mark_picked_up(customer_id):
    """Driver marks a customer as picked up."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    customer = Customer.query.get_or_404(customer_id)
    # Verify the driver owns this customer
    if not _driver_owns(customer):
        return jsonify({"success": False, "error": "Not your customer"})
    _before = _ride_status_info(customer)[0]
    customer.status = "picked_up"
    customer.picked_up_at = datetime.utcnow()
    mark_status(customer, "driver")
    # Freeze who actually did this ride (never changes if they switch cars later)
    if not customer.completed_driver:
        customer.completed_driver = customer.motorista or session.get("username", "")
    if not customer.completed_car:
        customer.completed_car = customer.car_string_val or customer.car_name or ""
    db.session.commit()
    fire_webhook({
        "type":           "picked_up",
        "customer_id":    customer.id,
        "customer_name":  customer.nome,
        "driver_name":    customer.motorista,
        "destination":    customer.destination,
        "picked_up_at":   vegas_datetime(customer.picked_up_at),
    })
    ride_status_changed(customer, _before, "driver")
    _mark_original_left(customer, "driver")
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

    _before = _ride_status_info(c)[0]
    c.status = "dropped_off"
    c.dropped_off_at = datetime.utcnow()
    mark_status(c, "driver")
    # Freeze who did the ride (in case pickup wasn't explicitly marked first)
    if not c.completed_driver:
        c.completed_driver = c.motorista or session.get("username", "")
    if not c.completed_car:
        c.completed_car = c.car_string_val or c.car_name or ""
    if not c.picked_up_at:
        c.picked_up_at = c.dropped_off_at
    if not c.is_return_ride:
        c.club_status = "arrived"
        if not c.arrived_at:
            c.arrived_at = datetime.utcnow()

    # GPS verification: how far is the car from the destination right now?
    verified, dist_mi = False, 0.0
    try:
        pos = car_position(c.car_name) if c.car_name else None
        pts = venue_points(c, allow_network=False)
        if pos and pts:
            dist_mi = round(min(calcular_distancia(pos["lat"], pos["lng"], p[0], p[1]) for p in pts), 2)
            verified = dist_mi <= VENUE_RADIUS_MI
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
    ride_status_changed(c, _before, "driver")
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
            # Record a notice for the OLD driver so they see it on next login
            try:
                db.session.add(DriverNotice(
                    driver_name=old_driver, kind="reassigned",
                    guest_name=customer.nome, guest_time=customer.pickup_datetime,
                    detail=f"moved to {new_driver.name}"))
                db.session.commit()
            except Exception:
                pass

    # Give the driver who went unavailable a short heads-up of what left them
    if reassigned and driver_profile and driver_profile.phone:
        names = ", ".join(r["customer"] for r in reassigned[:5])
        more = f" +{len(reassigned)-5} more" if len(reassigned) > 5 else ""
        send_sms_bg(driver_profile.phone,
            f"ClubLifter: {len(reassigned)} pickup(s) were moved to other drivers "
            f"and are no longer yours ({names}{more}).")

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
        today = vegas_today()
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
        try:
            now_car = driver_car_at(drv)
            if now_car:
                car_names.add(now_car.name)
                names.add(now_car.name)
        except Exception:
            pass
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

@app.route('/driver/notices', methods=['GET'])
def driver_notices():
    """Unseen notices (reassigned/cancelled rides) for the logged-in driver — shown
    as a pop-up when they open the dashboard."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    driver_name = session.get("username")
    names, _cars = driver_scope(driver_name)
    notices = (DriverNotice.query
               .filter(DriverNotice.driver_name.in_(list(names) or [driver_name]))
               .filter(DriverNotice.seen == False)
               .order_by(DriverNotice.created_at.desc()).limit(20).all())
    return jsonify({"success": True, "notices": [n.to_dict() for n in notices]})

@app.route('/driver/notices/seen', methods=['POST'])
def driver_notices_seen():
    """Mark the driver's notices as seen (after they dismiss the pop-up)."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    driver_name = session.get("username")
    names, _cars = driver_scope(driver_name)
    DriverNotice.query.filter(DriverNotice.driver_name.in_(list(names) or [driver_name]))\
                      .filter(DriverNotice.seen == False)\
                      .update({"seen": True}, synchronize_session=False)
    db.session.commit()
    return jsonify({"success": True})

@app.route('/driver/chat/<int:customer_id>', methods=['GET'])
def driver_chat_get(customer_id):
    """Driver dashboard: fetch the customer<->driver chat for one of the driver's
    rides. ?after=<id> for polling only newer messages."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    c = Customer.query.get_or_404(customer_id)
    if not _driver_owns(c):
        return jsonify({"success": False, "error": "Not your ride"}), 403
    after = _safe_int(request.args.get("after", 0))
    q = RideChatMessage.query.filter_by(customer_id=c.id)
    if after:
        q = q.filter(RideChatMessage.id > after)
    msgs = q.order_by(RideChatMessage.id.asc()).all()
    # Mark customer messages as read by the driver
    for m in msgs:
        if m.sender == "customer" and not m.read_by_driver:
            m.read_by_driver = True
    db.session.commit()
    return jsonify({
        "success": True,
        "customer_id": c.id,
        "customer_name": c.nome,
        "messages": [m.to_dict() for m in msgs],
    })

@app.route('/driver/chat/<int:customer_id>/send', methods=['POST'])
def driver_chat_send(customer_id):
    """Driver dashboard: send a message to the customer."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    c = Customer.query.get_or_404(customer_id)
    if not _driver_owns(c):
        return jsonify({"success": False, "error": "Not your ride"}), 403
    body = (request.form.get("body") or "").strip()
    if not body:
        return jsonify({"success": False, "error": "Message is empty."})
    m = RideChatMessage(customer_id=c.id, sender="driver", body=body[:1000], read_by_customer=False)
    db.session.add(m)
    db.session.commit()
    return jsonify({"success": True, "message": m.to_dict()})

@app.route('/driver/chat-unread', methods=['GET'])
def driver_chat_unread():
    """Driver dashboard: unread customer message counts per ride, for badges."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    driver_name = session.get("username")
    # Rides that belong to this driver
    my_rides = Customer.query.filter(Customer.motorista == driver_name).all()
    counts = {}
    total = 0
    for c in my_rides:
        n = RideChatMessage.query.filter_by(
            customer_id=c.id, sender="customer", read_by_driver=False).count()
        if n:
            counts[str(c.id)] = n
            total += n
    return jsonify({"success": True, "unread_by_ride": counts, "total": total})

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
    _before = _ride_status_info(c)[0]
    c.dispatch_status = "enroute"
    mark_status(c, "driver")
    car_str = c.car_string_val or "your ride"
    # The live-tracking SMS link is behind a feature flag so it can be turned off
    # without a code change. Set TRACKING_LINK_ENABLED=true to turn it back on.
    link = ""
    if TRACKING_LINK_ENABLED:
        link = tracking_link(c)            # generates/stores the token
        enroute_msg = (f"Hi {c.nome}! Your ClubLifter driver {c.motorista} is on the way "
                       f"in a {car_str}. Track them live: {link}")
    else:
        enroute_msg = (f"Hi {c.nome}! Your ClubLifter driver {c.motorista} is on the way "
                       f"in a {car_str}. See you soon!")
    db.session.commit()
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
        "tracking_link":   link,
        "message":         enroute_msg,
    })
    send_sms_many(c.get_phones() or [c.phone], enroute_msg)
    ride_status_changed(c, _before, "driver")
    return jsonify({"success": True, "tracking_link": link})

@app.route('/driver/running-late/<int:customer_id>', methods=['POST'])
def driver_running_late(customer_id):
    """Driver is running late but still coming → text the customer a heads-up.
    Optional 'reason' and 'minutes' in the form."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    if not _driver_owns(c):
        return jsonify({"success": False, "error": "Not your pickup"})
    reason = (request.form.get("reason") or "").strip()
    minutes = _safe_int(request.form.get("minutes"))
    # Build a friendly, reassuring message
    late_msg = f"Hi {c.nome}, this is your ClubLifter driver {c.motorista}. "
    if minutes:
        late_msg += f"I'm running about {minutes} minutes late"
    else:
        late_msg += "I'm running a little behind"
    if reason:
        late_msg += f" ({reason})"
    late_msg += ", but I'm on my way — thanks for your patience!"

    fire_webhook({
        "type":            "running_late",
        "customer_id":     c.id,
        "customer_name":   c.nome,
        "customer_phone":  c.phone,
        "customer_phones": c.get_phones(),
        "driver_name":     c.motorista,
        "reason":          reason,
        "minutes_late":    minutes,
        "message":         late_msg,
    })
    send_sms_many(c.get_phones() or [c.phone], late_msg)
    # Also drop a note in the ride chat so it's visible in-app
    try:
        db.session.add(RideChatMessage(customer_id=c.id, sender="driver",
            body=late_msg, read_by_customer=False))
        db.session.commit()
    except Exception:
        pass
    return jsonify({"success": True, "message": late_msg})

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
    # Record that the driver arrived at the pickup (so the AI/API can report it)
    _before = _ride_status_info(c)[0]
    c.dispatch_status = "arrived"
    if not c.driver_arrived_at:
        c.driver_arrived_at = datetime.utcnow()
    # The guest is texted below — the GPS "your driver has arrived" text is skipped.
    c.notified_5km = c.notified_10km = c.notified_15km = True
    mark_status(c, "driver")
    db.session.commit()
    ride_status_changed(c, _before, "driver")
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
    """Start a MASKED call between driver and customer via Twilio.

    Flow: Twilio calls the DRIVER's phone first. When they answer, the TwiML at
    /twiml/connect-customer dials the CUSTOMER and bridges them. The customer sees
    only the Twilio number; the driver never sees the customer's real number.
    """
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    if not _driver_owns(c):
        return jsonify({"success": False, "error": "Not your pickup"})

    driver_phone = clean_phone(c.motorista_phone or "")
    if not driver_phone:
        # Fall back to the driver's own profile phone
        drv = get_driver_record(session.get("username"))
        driver_phone = clean_phone(drv.phone) if drv and drv.phone else ""
    customer_phone = clean_phone(c.phone or "")

    ok, err = start_masked_call(driver_phone, customer_phone, c.nome or "your guest")
    if not ok:
        return jsonify({"success": False, "error": err})

    fire_webhook({
        "type":            "masked_call_started",
        "customer_id":     c.id,
        "customer_name":   c.nome,
        "driver_name":     c.motorista,
        "pickup_datetime": c.pickup_datetime,
    })
    return jsonify({"success": True, "message": "Calling your phone now — answer to connect."})

@app.route('/twiml/connect-customer', methods=['GET', 'POST'])
def twiml_connect_customer():
    """TwiML returned when the driver answers: greet, then dial the customer.
    The customer's number is passed as a query param (from our own server), never
    exposed to the driver's device."""
    cust = clean_phone(request.args.get('cust', ''))
    name = request.args.get('name', 'your guest')
    # Basic XML escaping for the spoken name
    safe_name = (name.replace('&', 'and').replace('<', '').replace('>', '').replace('"', ''))
    if not cust:
        xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<Response><Say>Sorry, we could not find the guest number. Goodbye.</Say>'
               '<Hangup/></Response>')
        return app.response_class(xml, mimetype='text/xml')
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Say voice="alice">Connecting you to {safe_name}. Please hold.</Say>'
        f'<Dial callerId="{TELNYX_VOICE_NUMBER}" timeout="30">'
        f'<Number>{cust}</Number>'
        '</Dial>'
        '<Say voice="alice">The call could not be completed. Goodbye.</Say>'
        '<Hangup/>'
        '</Response>'
    )
    return app.response_class(xml, mimetype='text/xml')

@app.route('/driver/cars')
def driver_cars():
    """List available cars for the driver's car-switch picker, flagging which are
    currently assigned to another driver (via today's shifts)."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    driver_name = session.get("username")
    me = get_driver_record(driver_name)
    today = vegas_today()
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

    today = vegas_today()
    dow = today.weekday()
    today_p = f"{today.month:02d}/{today.day:02d}/{today.year}"

    now_v = vegas_naive_now()
    # Who has this car right now (shift OR Driver Management)?
    holder = car_driver_at(car, now_v)
    other_driver = holder.name if holder and holder.id != me.id else None

    if other_driver and not confirm:
        # Ask the frontend to confirm the takeover
        return jsonify({"success": False, "needs_confirm": True,
                        "assigned_to": other_driver,
                        "car_name": car.car_string()})

    # Take the car for TODAY only. We used to deactivate weekly shifts here, which
    # silently deleted them for every future week — and any date-specific shift
    # also hid every other driver's weekly shift that day. Now a date-specific
    # shift simply overrides that driver and that car for the day (roster_at).
    old_start, old_end = '18:00', '05:30'
    for sh in (Shift.query.filter_by(active=True)
               .filter(Shift.specific_date == today_p)
               .filter((Shift.driver_id == me.id) | (Shift.car_id == car.id)).all()):
        if sh.driver_id == me.id:
            old_start, old_end = sh.start_time, sh.end_time
        sh.active = False          # date-specific → only affects today
    for sh in (Shift.query.filter_by(driver_id=me.id, active=True)
               .filter(Shift.day_of_week == dow).all()):
        old_start, old_end = sh.start_time, sh.end_time   # keep my usual hours

    # From now until the end of my usual shift (crossing midnight if needed), so a
    # switch at 1 AM is in force immediately — not from 6 PM.
    db.session.add(Shift(driver_id=me.id, car_id=car.id, specific_date=today_p,
                         day_of_week=None, start_time=now_v.strftime("%H:%M"),
                         end_time=(old_end or '05:30'), active=True))
    db.session.commit()

    # Move my not-yet-picked-up rides for today onto the new car (GPS follows it)
    names, _cars = driver_scope(driver_name)
    updated = 0
    for c in Customer.query.filter(Customer.motorista.in_(list(names) or [driver_name]),
                                   Customer.status == 'scheduled').all():
        if today_p in (c.pickup_datetime or "") or f"{today.month}/{today.day}/{today.year}" in (c.pickup_datetime or ""):
            c.car_name = car.name
            c.car_string_val = car.car_string()
            c.car_photo = car.photo or ""
            c.near_pickup_since = None
            c.gps_last_pick_mi = None
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

def _package_price(pkg_name):
    """Look up a package's price by name (0 if unknown / free)."""
    if not pkg_name:
        return 0
    p = Package.query.filter_by(name=pkg_name).first()
    return (p.price or 0) if p else 0

def _effective_price(customer):
    """The price that drives priority. Uses the ride's total spend (price_total,
    sent by CartVIP) when set; otherwise falls back to the package's price."""
    pt = getattr(customer, 'price_total', None)
    if pt is not None and pt > 0:
        return pt
    return _package_price(customer.package)

def priority_level(customer):
    """Human-readable priority tier for a ride, by total spend (price_total) when
    set, otherwise by package price.
      manual star  → 'starred' (always wins)
      $1000+       → 'vip'
      $300–$999    → 'high'
      $1–$299      → 'medium'
      free ($0)    → 'low'  (no tag shown)
    """
    if getattr(customer, 'priority', False):
        return 'starred'
    price = _effective_price(customer)
    if price >= 1000:
        return 'vip'
    if price >= 300:
        return 'high'
    if price > 0:
        return 'medium'
    return 'low'

# Anti-starvation: a ride waiting longer than this many minutes is bumped up so
# cheap/free guests are never stuck behind an endless stream of pricier ones.
PRIORITY_WAIT_ESCALATE_MIN = 15

def priority_sort_key(customer, now=None):
    """Sort key for scheduling order. Lower tuple = served first.
    Order: (1) manual star, (2) package price tier, (3) how long they've waited.
    But once a ride has waited > PRIORITY_WAIT_ESCALATE_MIN, it jumps to the top
    tier so it can't be starved by a stream of higher-priced bookings."""
    if now is None:
        now = datetime.utcnow()
    starred = 0 if getattr(customer, 'priority', False) else 1
    price = _effective_price(customer)

    waited_min = 0
    if customer.created_at:
        try:
            waited_min = max(0, (now - customer.created_at).total_seconds() / 60)
        except Exception:
            waited_min = 0
    escalated = waited_min > PRIORITY_WAIT_ESCALATE_MIN
    pdt = parse_pickup_datetime(customer.pickup_datetime) or datetime.max
    created = customer.created_at or datetime.max

    # band 0 = escalated long-waiters (served first, longest wait first)
    # band 1 = everyone else, ordered by package price (higher = earlier)
    # All tuples are the same shape (5 elements) so they compare cleanly.
    if escalated:
        return (starred, 0, -waited_min, pdt, created)
    return (starred, 1, -price, pdt, created)

def _assign_driver_and_car(lat_cli, lng_cli, requested_dt):
    """Shared driver/car assignment (compat wrapper around pick_driver). Always a
    real person — or driver_name "" with a reason when nobody can take it."""
    pick, why = pick_driver(lat_cli, lng_cli, requested_dt)
    if not pick:
        return {"driver_name": "", "driver_phone": "", "distance_km": 0,
                "driver_coords": None, "car": None, "note": why, "pick": None}
    pos = car_position(pick.car.name) if pick.car else None
    dist = pick.dist_mi if pick.dist_mi not in (None, float("inf")) else 0
    return {"driver_name": pick.driver.name, "driver_phone": pick.driver.phone or "",
            "distance_km": round(dist, 2),
            "driver_coords": {"lat": pos["lat"], "lng": pos["lng"]} if pos else None,
            "car": pick.car, "note": pick.note, "pick": pick}

def _normalize_street_address(address):
    """Clean up common address quirks that make Nominatim fail even on valid,
    real addresses:
      - Non-standard street-type abbreviations ("bl" -> "Blvd", "st" stays, etc.)
      - Apartment/unit/suite parts ("apt 2057", "#12", "unit 4") which throw off
        the geocoder — the building geocodes fine without them.
    Returns (cleaned_address, cleaned_without_unit)."""
    import re
    a = " ".join((address or "").split())  # collapse whitespace

    # Normalize some street-type abbreviations that Nominatim doesn't accept.
    # Word-boundary, case-insensitive. "blvd" it understands; "bl"/"blv" it doesn't.
    repl = [
        (r'\bbl\b',   'Blvd'),
        (r'\bblv\b',  'Blvd'),
        (r'\bblvd\b', 'Blvd'),
        (r'\bav\b',   'Ave'),
        (r'\bave\b',  'Ave'),
        (r'\bst\b',   'St'),
        (r'\brd\b',   'Rd'),
        (r'\bdr\b',   'Dr'),
        (r'\bln\b',   'Ln'),
        (r'\bpkwy\b', 'Pkwy'),
        (r'\bhwy\b',  'Hwy'),
        (r'\bctr\b',  'Center'),
    ]
    for pat, rep in repl:
        a = re.sub(pat, rep, a, flags=re.IGNORECASE)

    # Build a version with the apartment/unit/suite chunk removed. Matches things
    # like "apt 2057", "apt. 2057", "unit 4", "ste 300", "suite 300", "# 12", "#12".
    unit_pat = r'\s*(?:,\s*)?(?:apt\.?|apartment|unit|ste\.?|suite|#)\s*[\w-]+'
    without_unit = re.sub(unit_pat, '', a, flags=re.IGNORECASE)
    without_unit = " ".join(without_unit.split()).rstrip(',').strip()

    return a, without_unit

def _geocode_pickup_flexible(address):
    """Best-effort (lat, lng) for a pickup address. Now backed by the cached,
    throttled geocoder (see DISPATCH ENGINE → geocode_address)."""
    return geocode_address(address)

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
    if client_phone and not _looks_like_phone(client_phone):
        # e.g. {"customer_name": "Kellen", "customer_phone": "Roti"} — a split name,
        # not a number. Creating that ride would strand the guest.
        return jsonify({"success": False,
                        "error": f"customer_phone must be a phone number (got '{client_phone[:40]}')"}), 400
    extra_phones = data.get("extra_phones") or []
    if not isinstance(extra_phones, list):
        extra_phones = []
    extra_phones = [str(p).strip() for p in extra_phones if p and _looks_like_phone(p)]
    all_phones = ([client_phone] if client_phone else []) + [p for p in extra_phones if p]
    details     = (data.get("details") or data.get("notes") or "").strip()
    package     = (data.get("package") or "").strip()
    try:
        guests = int(data.get("guests") or 0)
    except (TypeError, ValueError):
        guests = 0
    destination = (data.get("destination") or "").strip()
    # CartVIP can send the total spend, which drives ride priority
    try:
        price_total = float(data.get("price_total")) if data.get("price_total") not in (None, "") else None
    except (TypeError, ValueError):
        price_total = None
    # Provenance: who is calling (header or payload), their order id, ip + user-agent
    source_label = (request.headers.get("X-Source") or data.get("source") or "cartvip").strip()[:40]
    external_ref = str(data.get("order_id") or data.get("external_id") or data.get("confirmation")
                       or data.get("booking_id") or "").strip()[:80]
    origin = _request_origin()
    allow_dup = str(data.get("allow_duplicate", "")).lower() in ("1", "true", "yes")

    requested_dt = parse_pickup_datetime(pickup_datetime)
    if requested_dt is None:
        return jsonify({"success": False,
                        "error": "pickup_datetime must look like MM/DD/YYYY HH:MM AM/PM"}), 400
    # Normalize the stored date so 2-digit years / loose formats are consistent
    pickup_datetime = normalize_pickup_datetime(pickup_datetime) or pickup_datetime

    # ── Duplicate protection ─────────────────────────────────────────────────
    # The same guest around the same time already has an open ride (typically one
    # dispatch typed in by hand before the partner booking arrived) → fold this
    # booking into it and return THAT ride's id, so there's one ride, one driver,
    # and the partner tracks the right record. Two bookings with different partner
    # order ids are both kept (e.g. one guest, two orders via an affiliate).
    if not allow_dup:
        existing, why = find_duplicate_ride(name, all_phones, requested_dt, external_ref=external_ref)
        if existing is not None:
            changes = merge_booking_into(existing, {
                "external_ref": external_ref, "price_total": price_total, "package": package,
                "guests": guests, "phones": all_phones, "details": details,
                "pickup_datetime": pickup_datetime,
            }, source_label, origin)
            dn, dp = ride_driver_display(existing)
            print(f"[API/schedule] duplicate of ride {existing.id} ({why}) from {origin} → merged", flush=True)
            return jsonify({
                "success": True, "duplicate": True, "merged_into": existing.id,
                "customer_id": existing.id, "driver_name": dn, "driver_phone": dp,
                "driver_assigned": bool(dn), "car": existing.car_string_val or "",
                "distance_km": existing.distancia or 0, "status": "scheduled",
                "ride_status": _ride_status_info(existing)[0],
                "note": f"matched existing ride #{existing.id} by {why}; "
                        + (", ".join(changes) if changes else "no new details") + " merged",
            })

    # Geocode the pickup (cached + throttled; see DISPATCH ENGINE). Best-effort:
    # the booking is NEVER lost, and a driver is assigned even without coordinates
    # (they navigate by the address text) — only the distance ranking is skipped.
    lat_cli, lng_cli = geocode_address(pickup_address)
    geocoded = lat_cli is not None and lng_cli is not None
    if not geocoded:
        print(f"[API/schedule] could not geocode '{pickup_address}' — assigning without distance", flush=True)
    pick, no_driver_reason = pick_driver(lat_cli, lng_cli, requested_dt)
    chosen_car = pick.car if pick else None
    car_string = chosen_car.car_string() if chosen_car else ""
    car_photo  = chosen_car.photo if chosen_car else ""
    car_photo_url = f"{PUBLIC_BASE_URL}/uploads/{car_photo}" if car_photo else ""
    driver_name  = pick.driver.name if pick else ""
    driver_phone = (pick.driver.phone or "") if pick else ""
    dist_mi = pick.dist_mi if (pick and pick.dist_mi not in (None, float("inf"))) else 0

    # Register marker on OneStepGPS (best-effort, only if we have coordinates)
    if geocoded:
        try:
            headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
            requests.post(URL_API, json={
                "display_name": name, "active": True, "status": "active", "marker_type": "point",
                "detail": {"description": pickup_address, "lat_lng": {"lat": lat_cli, "lng": lng_cli}}
            }, headers=headers_api, timeout=8)
        except Exception:
            pass

    # If we couldn't geocode, prepend a clear note so dispatch sets the pickup
    # location manually.
    stored_details = details
    if not geocoded:
        flag = "⚠️ PICKUP NEEDS REVIEW (address couldn't be mapped)"
        stored_details = (flag + (" — " + details if details else ""))

    club = club_for_destination(destination)
    customer = Customer(
        nome=name, phone=client_phone, phones_json=json.dumps([p for p in extra_phones if p]),
        endereco=pickup_address, details=stored_details[:500],
        motorista=driver_name, motorista_phone=driver_phone,
        car_name=(chosen_car.name if chosen_car else ""),
        car_string_val=car_string, car_photo=car_photo,
        distancia=round(dist_mi, 2), package=package, guests=guests,
        price_total=price_total,
        pickup_datetime=pickup_datetime, destination=destination,
        needs_transport=True, club_status="coming",
        promoter="cartvip", status='scheduled', created_at=datetime.utcnow(),
        pickup_lat=lat_cli, pickup_lng=lng_cli,
        dest_lat=(club.lat if club else None), dest_lng=(club.lng if club else None),
        assign_note=((pick.note if pick else no_driver_reason) or "")[:200],
        source=source_label.lower(), created_by=origin, external_ref=external_ref,
    )
    db.session.add(customer); db.session.commit()
    log_event(customer, "created",
              f"via API ({source_label}) from {origin}"
              + (f" · order {external_ref}" if external_ref else "")
              + f" · {pickup_datetime} · {pickup_address} → {destination}"
              + (f" · driver {driver_name}" if driver_name else f" · NO DRIVER ({no_driver_reason})"),
              actor="api")

    time_part = _time_part(pickup_datetime)
    if pick:
        sms_text = (f"Hi {name}! Your ClubLifter ride is booked. {driver_name} will pick you up"
                    f"{' at ' + time_part if time_part else ''}"
                    f"{' in a ' + car_string if car_string else ''}. See you soon!"
                    f"{chr(10)}Heads up: your driver may call from this number to reach you — "
                    f"please keep an eye out and answer so they can find you.")
    else:
        # Booked, but nobody can take it yet. The background dispatcher keeps
        # retrying every minute and alerts ops; the guest gets the driver's details
        # as soon as one is assigned. Never name a driver that doesn't exist.
        sms_text = (f"Hi {name}! Your ClubLifter ride is booked"
                    f"{' for ' + time_part if time_part else ''}. "
                    f"We're confirming your driver and their details will follow shortly. "
                    f"See you soon!")
    fire_webhook({
        "type": "scheduled", "source": "cartvip",
        "customer_name": name, "customer_phone": client_phone,
        "driver_name": driver_name, "driver_car": car_string or "N/A",
        "driver_car_photo_url": car_photo_url,
        "pickup_datetime": pickup_datetime, "pickup_time": time_part,
        "destination": destination, "message": sms_text, "customer_id": customer.id,
    })
    if pick:
        # The driver hears about it right away (SMS + pop-up on their dashboard)
        notify_driver_of_ride(customer, pick.driver, "assigned")
    else:
        fire_webhook({
            "event": "no_driver_available", "source": "cartvip", "customer_id": customer.id,
            "customer_name": name, "customer_phone": client_phone,
            "pickup_address": pickup_address, "pickup_datetime": pickup_datetime,
            "destination": destination, "reason": no_driver_reason,
        })
    # Direct SMS to the customer for API-sourced bookings as well
    _api_phones = [client_phone] + [p for p in (extra_phones or []) if p]
    send_sms_many(_api_phones, sms_text, car_photo_url)

    return jsonify({
        "success": True,
        "customer_id": customer.id,
        "driver_name": driver_name,
        "driver_phone": driver_phone,
        "driver_assigned": bool(pick),
        "car": car_string,
        "distance_km": round(dist_mi, 2),
        "status": "scheduled",                       # booking status (unchanged contract)
        "ride_status": "scheduled" if pick else "unassigned",
        "note": customer.assign_note,
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
    try:
        price_total = float(data.get("price_total")) if data.get("price_total") not in (None, "") else None
    except (TypeError, ValueError):
        price_total = None

    customer = Customer(
        nome=name, phone=client_phone, phones_json=json.dumps([p for p in extra_phones if p]),
        endereco="(walk-in)", details=(data.get("details") or "").strip(),
        motorista="(walk-in)", motorista_phone="",
        distancia=0, package=(data.get("package") or "").strip(),
        price_total=price_total,
        guests=int(data.get("guests") or 0),
        pickup_datetime=(data.get("pickup_datetime") or "").strip(),
        destination=(data.get("destination") or "").strip(),
        needs_transport=False, club_status="coming",
        promoter="cartvip", status='scheduled', created_at=datetime.utcnow(),
        source="walkin", created_by=_request_origin(),
    )
    db.session.add(customer); db.session.commit()
    log_event(customer, "created", f"walk-in via API from {customer.created_by}", actor="api")

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
    ride_status, ride_status_text = _ride_status_info(c)
    extras = ride_status_extras(c)
    return jsonify({
        "success": True,
        "customer_id": c.id, "customer_name": c.nome,
        "needs_transport": c.needs_transport,
        "driver_name": extras["driver_name"], "driver_phone": extras["driver_phone"],
        "driver_assigned": extras["driver_assigned"],
        "status_source": extras["status_source"],
        "status_estimated": extras["status_estimated"],
        "status_updated_at": extras["status_updated_at"],
        "driver_eta_min": extras.get("driver_eta_min"),
        "driver_distance_mi": extras.get("driver_distance_mi"),
        "car": c.completed_car or c.car_string_val, "distance_km": c.distancia,
        "pickup_datetime": c.pickup_datetime, "destination": c.destination,
        # ── Clear, unified ride status (scheduled → enroute → arrived → picked_up → dropped_off → at_venue → left) ──
        "status":          ride_status,        # the clear, unified status (use this)
        "ride_status":     ride_status,         # alias (same value)
        "ride_status_text": ride_status_text,   # human-readable
        "pickup_status": ride_status,           # updated: now reflects the full progression (was raw c.status)
        "transport_status": c.status,           # raw transport field: scheduled|picked_up|dropped_off
        "club_status": c.club_status,           # raw club field: coming|arrived|left
        "picked_up":      bool(c.picked_up_at),
        "picked_up_at":   vegas_datetime(c.picked_up_at) if c.picked_up_at else "",
        "driver_arrived": bool(c.driver_arrived_at),
        "dropped_off":    bool(c.dropped_off_at),
        "dropped_off_at": vegas_datetime(c.dropped_off_at) if c.dropped_off_at else "",
        "package": c.package, "guests": c.guests,
    })

@app.route('/api/v1/customer/<int:customer_id>/cancel', methods=['POST'])
@require_api_key
def api_v1_cancel(customer_id):
    """Cancel/remove a booking."""
    c = Customer.query.get(customer_id)
    if not c:
        return jsonify({"success": False, "error": "Customer not found"}), 404
    drv = resolve_ride_driver(c) if c.needs_transport and c.status in ("scheduled", "picked_up") else None
    if drv:
        t = _time_part(c.pickup_datetime)
        if drv.phone:
            send_sms_bg(clean_phone(drv.phone),
                        f"ClubLifter: {c.nome}{(' (' + t + ')') if t else ''} "
                        f"was cancelled and is no longer on your list.")
        try:
            db.session.add(DriverNotice(driver_name=drv.name, kind="cancelled", guest_name=c.nome,
                                        guest_time=c.pickup_datetime, detail="cancelled by partner"))
        except Exception:
            pass
    log_event(c, "deleted", f"cancelled via API from {_request_origin()}"
              + (f" · driver {drv.name} told" if drv else ""), actor="api")
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

def _get_car_gps(car_name):
    """Return (lat, lng) for a car's current OneStepGPS position, or (None, None).
    Shared by the app tracking and the public SMS tracking page."""
    if not car_name:
        return None, None
    match = _match_car(car_name)
    pos = car_position(match.name if match else car_name)
    if pos:
        return pos["lat"], pos["lng"]
    return None, None

def _haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km between two lat/lng points."""
    import math
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def _pickup_latlng(c):
    """(lat, lng) of a ride's pickup: the coordinates stored at booking, else the
    geocode cache. Never calls a geocoder on the spot (pages poll this)."""
    if c.pickup_lat is not None and c.pickup_lng is not None:
        return c.pickup_lat, c.pickup_lng
    addr = (c.endereco or "").strip()
    if not addr or addr == "(walk-in)":
        return None, None
    return geocode_address(addr, allow_network=False)

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

def _driver_for_car_today(car, today_p=None):
    """Driver in this car now (roster: Switch Car > Driver Management > weekly)."""
    return car_driver_at(car) if car else None


def can_see_driver_tracking():
    """Driver Tracking (stop history) is limited to master and club owners."""
    return session.get("role") in ("master", "club_owner")

@app.route('/admin/driver-tracking')
def admin_driver_tracking():
    if not session.get("logged") or not can_see_driver_tracking():
        return redirect(url_for("login"))
    return render_template('admin_driver_tracking.html')

@app.route('/api/driver-stops/rename-car', methods=['POST'])
def api_driver_stops_rename_car():
    """Rename the car on historical stops — e.g. after a vehicle was renamed in
    the Cars screen, move its old stops onto the new name so they group together.
    Body: {"from": "2019 Mercedes sprinter", "to": "2016 Mercedes Sprinter 2500"}"""
    if not session.get("logged") or not is_master():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    src = (data.get("from") or "").strip()
    dst = (data.get("to") or "").strip()
    if not src or not dst:
        return jsonify({"success": False, "error": "from and to are required"})
    stops = DriverStop.query.filter(DriverStop.car_name == src).all()
    for s in stops:
        s.car_name = dst
    db.session.commit()
    return jsonify({"success": True, "renamed": len(stops), "from": src, "to": dst})

@app.route('/api/driver-stops/car-names', methods=['GET'])
def api_driver_stops_car_names():
    """List the distinct car names that appear in stop history, with counts —
    so the UI can offer them for renaming."""
    if not session.get("logged") or not is_master():
        return jsonify({"error": "Unauthorized"}), 401
    from collections import Counter
    counts = Counter(s.car_name for s in DriverStop.query.all() if s.car_name)
    names = [{"name": n, "stops": c} for n, c in counts.most_common()]
    return jsonify({"car_names": names})

@app.route('/api/driver-stops/diagnose')
def api_driver_stops_diagnose():
    """Diagnostic: shows every distinct car_name in the stop history, how many
    stops each has, whether a driver_name is attached, and which driver each car
    currently maps to. Helps explain why a driver shows no stops."""
    if not session.get("logged") or not can_see_driver_tracking():
        return jsonify({"error": "Unauthorized"}), 401
    today_p = f"{vegas_today().month:02d}/{vegas_today().day:02d}/{vegas_today().year}"
    from collections import defaultdict
    by_car = defaultdict(lambda: {"stops": 0, "with_driver": 0, "drivers": set()})
    for s in DriverStop.query.all():
        b = by_car[s.car_name or "(no car)"]
        b["stops"] += 1
        if s.driver_name:
            b["with_driver"] += 1
            b["drivers"].add(s.driver_name)
    out = []
    for car_name, b in by_car.items():
        car = _match_car(car_name)
        maps_to = None
        if car:
            drv = _driver_for_car_today(car, today_p)
            maps_to = drv.name if drv else None
        out.append({
            "car_name": car_name,
            "matched_car": car.name if car else None,
            "stops": b["stops"],
            "stops_with_driver": b["with_driver"],
            "stops_missing_driver": b["stops"] - b["with_driver"],
            "driver_names_on_stops": sorted(b["drivers"]),
            "currently_maps_to_driver": maps_to,
        })
    out.sort(key=lambda x: x["stops"], reverse=True)
    # Also list drivers and their assigned cars for reference
    drivers = []
    for d in Driver.query.all():
        car = Car.query.get(d.assigned_car_id) if d.assigned_car_id else None
        drivers.append({"driver": d.name, "assigned_car": car.name if car else None})
    return jsonify({"cars_in_stop_history": out, "drivers": drivers})

@app.route('/api/driver-stops/backfill', methods=['POST'])
def api_driver_stops_backfill():
    """Repair pass: for stops that have no driver_name (or a stale one), set the
    driver based on which driver the stop's car currently maps to. Fixes the case
    where stops were recorded before a car was assigned to a driver.

    Body (optional): {"car_name": "...", "driver_name": "...", "date": "YYYY-MM-DD"}
    - If car_name + driver_name given, force that mapping for that car's stops.
    - Otherwise auto-map every car to its current driver.
    - date limits the repair to one Vegas day; omit for all history."""
    if not session.get("logged") or not is_master():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    force_car = (data.get("car_name") or "").strip()
    force_driver = (data.get("driver_name") or "").strip()
    date_iso = (data.get("date") or "").strip()
    today_p = f"{vegas_today().month:02d}/{vegas_today().day:02d}/{vegas_today().year}"

    q = DriverStop.query
    # Optional date window
    if date_iso:
        try:
            y, m, d = [int(x) for x in date_iso.split("-")]
            start_utc = datetime(y, m, d) + timedelta(hours=7)
            end_utc = start_utc + timedelta(days=1)
            q = q.filter(DriverStop.started_at >= start_utc, DriverStop.started_at < end_utc)
        except Exception:
            return jsonify({"error": "bad date"}), 400
    if force_car:
        q = q.filter(DriverStop.car_name == force_car)

    stops = q.all()
    fixed = 0
    for s in stops:
        if force_car and force_driver:
            if s.driver_name != force_driver:
                s.driver_name = force_driver
                fixed += 1
            continue
        # Auto-map: only fill when empty, using the car's current driver
        if not s.driver_name:
            car = _match_car(s.car_name)
            drv = _driver_for_car_today(car, today_p) if car else None
            if drv:
                s.driver_name = drv.name
                fixed += 1
    db.session.commit()
    return jsonify({"success": True, "stops_examined": len(stops), "stops_updated": fixed})

@app.route('/api/driver-stops')
def api_driver_stops():
    """Stop history with flexible windows:
      Single day:   ?date=YYYY-MM-DD
      Range:        ?from=YYYY-MM-DDTHH:MM&to=YYYY-MM-DDTHH:MM  (Vegas local time)
      Also:         ?driver=name  ?min=minutes
    The range form is handy for overnight shifts, e.g. from 8 PM to 6 AM next day."""
    if not session.get("logged") or not can_see_driver_tracking():
        return jsonify({"error": "Unauthorized"}), 401
    driver_f = (request.args.get('driver', '') or "").strip()
    try:
        min_min = int(request.args.get('min', 5))
    except ValueError:
        min_min = 5

    from_s = (request.args.get('from', '') or "").strip()
    to_s   = (request.args.get('to', '') or "").strip()
    day_iso = request.args.get('date', vegas_today().strftime("%Y-%m-%d"))

    def _vegas_to_utc(dt_local):
        """Convert a naive Vegas-local datetime to naive UTC."""
        try:
            from zoneinfo import ZoneInfo
            from datetime import timezone as _tz
            return dt_local.replace(tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(_tz.utc).replace(tzinfo=None)
        except Exception:
            return dt_local + timedelta(hours=7)

    def _parse_local(s):
        """Accept 'YYYY-MM-DDTHH:MM', 'YYYY-MM-DD HH:MM', or 'YYYY-MM-DD'."""
        s = s.replace("T", " ").strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    label = day_iso
    if from_s and to_s:
        d1 = _parse_local(from_s); d2 = _parse_local(to_s)
        if not d1 or not d2:
            return jsonify({"error": "bad from/to datetime"}), 400
        start_utc = _vegas_to_utc(d1)
        end_utc = _vegas_to_utc(d2)
        label = f"{from_s} → {to_s}"
    else:
        # Single Vegas day
        d1 = _parse_local(day_iso)
        if not d1:
            return jsonify({"error": "bad date"}), 400
        start_utc = _vegas_to_utc(datetime(d1.year, d1.month, d1.day, 0, 0))
        end_utc = _vegas_to_utc(datetime(d1.year, d1.month, d1.day, 0, 0) + timedelta(days=1))

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
    return jsonify({"groups": out, "date": label, "min": min_min,
                    "known_drivers": drivers,
                    "total_stops": len(stops),
                    "total_minutes": sum(s.duration_min or 0 for s in stops)})

@app.route('/api/driver-stops/export')
def api_driver_stops_export():
    """CSV export of the stop history for the same window/filters as the feed."""
    if not session.get("logged") or not can_see_driver_tracking():
        return "Unauthorized", 401
    driver_f = (request.args.get('driver', '') or "").strip()
    try:
        min_min = int(request.args.get('min', 5))
    except ValueError:
        min_min = 5
    from_s = (request.args.get('from', '') or "").strip()
    to_s   = (request.args.get('to', '') or "").strip()
    day_iso = request.args.get('date', vegas_today().strftime("%Y-%m-%d"))

    def _vegas_to_utc(dt_local):
        try:
            from zoneinfo import ZoneInfo
            from datetime import timezone as _tz
            return dt_local.replace(tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(_tz.utc).replace(tzinfo=None)
        except Exception:
            return dt_local + timedelta(hours=7)

    def _parse_local(s):
        s = s.replace("T", " ").strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    if from_s and to_s:
        d1 = _parse_local(from_s); d2 = _parse_local(to_s)
        if not d1 or not d2:
            return "bad from/to", 400
        start_utc = _vegas_to_utc(d1); end_utc = _vegas_to_utc(d2)
        window_label = f"{from_s}_to_{to_s}".replace(":", "").replace(" ", "_")
    else:
        d1 = _parse_local(day_iso) or datetime.utcnow()
        start_utc = _vegas_to_utc(datetime(d1.year, d1.month, d1.day, 0, 0))
        end_utc = _vegas_to_utc(datetime(d1.year, d1.month, d1.day, 0, 0) + timedelta(days=1))
        window_label = day_iso

    q = (DriverStop.query
         .filter(DriverStop.started_at >= start_utc, DriverStop.started_at < end_utc)
         .filter(DriverStop.duration_min >= min_min))
    if driver_f:
        q = q.filter((DriverStop.driver_name == driver_f) | (DriverStop.car_name == driver_f))
    stops = q.order_by(DriverStop.started_at.asc()).all()

    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Driver", "Car", "Address", "Started", "Ended", "Duration (min)",
                "Still there", "Latitude", "Longitude"])
    for s in stops:
        w.writerow([
            s.driver_name or "", s.car_name or "", s.address or "",
            vegas_datetime(s.started_at), vegas_datetime(s.ended_at) if s.ended_at else "",
            s.duration_min or 0, "Yes" if s.ongoing else "No",
            s.lat if s.lat is not None else "", s.lng if s.lng is not None else "",
        ])
    from flask import Response
    fname = f"driver_stops_{window_label}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


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
    """Drivers a dispatcher can give a pickup to, with the car they're in, whether
    they're online, and whether they already have a pickup near that time."""
    if not can_dispatch():
        return jsonify({"error": "Unauthorized"}), 401
    at = None
    cid = request.args.get("customer_id")
    if cid:
        _c = Customer.query.get(_safe_int(cid))
        at = parse_pickup_datetime(_c.pickup_datetime) if _c else None
    positions = gps_positions(allow_fetch=False) or {}
    out = []
    for d in Driver.query.order_by(Driver.name).all():
        car = driver_car_at(d, at)
        out.append({
            "id": d.id, "name": d.name, "phone": d.phone or "",
            "available": bool(d.available),
            "online": driver_on_duty(d, car=car, positions=positions),
            "busy": driver_is_busy(d.name, at) if at else False,
            "car_name": car.name if car else "",
            "car_string": car.car_string() if car else "",
        })
    # Online + available + free first
    out.sort(key=lambda x: (not x["available"], not x["online"], x["busy"], x["name"].lower()))
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
    _before = _ride_status_info(c)[0]
    car = driver_car_at(drv, parse_pickup_datetime(c.pickup_datetime))

    c.motorista = drv.name
    c.motorista_phone = drv.phone or ""
    if car:
        c.car_name = car.name
        c.car_string_val = car.car_string()
        c.car_photo = car.photo or ""
    if c.status == "waitlist":
        c.status = "scheduled"
    # Re-arm proximity alerts / GPS tracking for the new driver
    c.notified_15km = False
    c.notified_10km = False
    c.notified_5km = False
    c.near_pickup_since = None
    c.gps_last_pick_mi = None
    c.unassigned_alert_level = 0
    # A dispatcher's choice is never auto-moved by the offline check
    c.assign_note = f"manual by {session.get('username', '')}"[:200]
    db.session.commit()
    log_event(c, "assigned", f"{previous or '(none)'} → {drv.name}" + (f" / {car.name}" if car else ""),
              actor=f"dispatch:{session.get('username', '')}")
    ride_status_changed(c, _before, "dispatch")

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
    # Let the new driver know (SMS + dashboard pop-up)
    if previous != drv.name:
        notify_driver_of_ride(c, drv, "reassigned_to")

    # Let the PREVIOUS driver know the ride was taken off them
    if previous and previous != drv.name:
        _time_txt = ""
        _parts = (c.pickup_datetime or "").split(" ")
        if len(_parts) >= 3:
            _time_txt = f"{_parts[1]} {_parts[2]}"
        prev_drv = Driver.query.filter_by(name=previous).first() or get_driver_record(previous)
        prev_phone = prev_drv.phone if prev_drv and prev_drv.phone else ""
        if prev_phone:
            send_sms_bg(prev_phone,
                f"ClubLifter: {c.nome}"
                f"{(' (' + _time_txt + ')') if _time_txt else ''} "
                f"was reassigned to another driver and is no longer yours.")
        # Record a notice for the previous driver
        try:
            db.session.add(DriverNotice(
                driver_name=previous, kind="reassigned",
                guest_name=c.nome, guest_time=c.pickup_datetime,
                detail=f"moved to {drv.name}"))
            db.session.commit()
        except Exception:
            pass

    return jsonify({"success": True, "driver": drv.name,
                    "car": c.car_string_val or "", "previous": previous})

@app.route('/admin/tracking/pickup-done/<int:customer_id>', methods=['POST'])
def tracking_mark_done(customer_id):
    """Manager marks a pickup as completed (or reopens it)."""
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    _before = _ride_status_info(c)[0]
    c.status = 'scheduled' if c.status == 'picked_up' else 'picked_up'
    if c.status == 'picked_up' and not c.picked_up_at:
        c.picked_up_at = datetime.utcnow()
    elif c.status == 'scheduled':
        c.picked_up_at = None
    mark_status(c, "dispatch")
    db.session.commit()
    ride_status_changed(c, _before, "dispatch")
    return jsonify({"success": True, "status": c.status})

@app.route('/api/live-drivers', methods=['GET'])
def api_live_drivers():
    """Every vehicle OneStepGPS reports, plus that driver's full pickup queue for today.
    Pickups are linked by DRIVER (via today's shift) as well as by car name, so a
    driver switching cars — or a car name that doesn't exactly match GPS — still works."""
    if not session.get("logged") or not can_dispatch():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        lista = gps_fleet() or []   # GPS down → every pickup shows under "no GPS"

        today = vegas_today()
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
                "priority_level":  priority_level(c),
                "is_current":      False,
            }

        def _build(matched):
            # Order by priority (manual star → package price → wait time) so the
            # driver's "next pickup" reflects who should be served first.
            _now = datetime.utcnow()
            matched.sort(key=lambda c: priority_sort_key(c, _now))
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
            if not isinstance(v, dict):
                continue
            v_lat, v_lng = _device_latlng(v)
            if v_lat is None:
                continue
            gps_name = v.get('display_name', 'Unknown')
            car = _match_car(gps_name)
            drv = car_driver_at(car) if car else None
            driver_name = drv.name if drv else ""

            # All the names a pickup's car_name could plausibly hold for THIS vehicle
            car_names = {gps_name}
            if car:
                car_names.add(car.name)

            # Names a pickup's motorista field could hold for this vehicle:
            # the resolved driver, plus the GPS/car name itself (fallback assignment
            # stores the car name in motorista when no driver is on the car).
            driver_names = set(car_names)
            if driver_name:
                driver_names.add(driver_name)

            matched = [c for c in todays
                       if (c.motorista and c.motorista in driver_names)
                       or (c.car_name and c.car_name in car_names)]
            for c in matched:
                claimed.add(c.id)
            if not driver_name and matched:
                # Prefer a real person name over a car-name fallback
                for c in matched:
                    if c.motorista and c.motorista not in car_names:
                        driver_name = c.motorista
                        break
                if not driver_name:
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

def _guest_row_info(c, dup_of=None):
    """What the guest list shows in the Ride column for one row."""
    code, text, est = _ride_status_full(c)
    name = ""
    if c.needs_transport:
        name = (c.completed_driver or "").strip()
        if not name and code not in ("unassigned", "waitlist"):
            name, _p = ride_driver_display(c)
    return {"code": code, "text": text, "estimated": est,
            "source": "est." if est else (c.status_source or ""),
            "driver": name, "note": c.assign_note or "",
            "origin": (c.source or ("cartvip" if c.promoter == "cartvip" else "")) or "",
            "dup_of": dup_of,
            "unassigned": bool(c.needs_transport and code in ("unassigned", "waitlist"))}

@app.route('/admin/dispatch-health')
def dispatch_health():
    """Plain-JSON health check of the dispatch engine — open it in the browser to
    see GPS, who's on duty, rides without a driver, and geocoder status."""
    if not session.get("logged") or not can_dispatch():
        return jsonify({"error": "Unauthorized"}), 401
    now_v = vegas_naive_now()
    positions = gps_positions()
    roster_ids = {e.driver.id for e in roster_at(now_v, include_unavailable=True)}
    drivers = []
    for d in Driver.query.order_by(Driver.name).all():
        car = driver_car_at(d, now_v)
        pos = car_position(car.name, positions) if (car and positions) else None
        drivers.append({
            "name": d.name, "phone": d.phone or "", "available": bool(d.available),
            "on_roster_now": d.id in roster_ids,
            "car": car.name if car else None,
            "dashboard_last_seen": vegas_datetime(d.last_seen_at) if d.last_seen_at else None,
            "on_duty": driver_on_duty(d, car=car, positions=positions),
            "gps": ({"age_min": pos["age_min"], "fresh": pos["fresh"],
                     "moved_recently": car_recently_moved(pos["name"])} if pos else None),
        })
    devices = []
    for k, p in (positions or {}).items():
        car = _match_car(p["name"])
        drv = car_driver_at(car, now_v) if car else None
        devices.append({"gps_name": p["name"], "age_min": p["age_min"], "fresh": p["fresh"],
                        "car_record": car.name if car else None,
                        "driver_now": drv.name if drv else None})
    rides = []
    for c in Customer.query.filter(Customer.needs_transport == True,
                                   Customer.status.in_(("scheduled", "waitlist", "picked_up"))).all():
        pdt = parse_pickup_datetime(c.pickup_datetime)
        if not pdt or not (-180 <= (pdt - now_v).total_seconds() / 60 <= 12 * 60):
            continue
        code, text, est = _ride_status_full(c)
        name, _p = ride_driver_display(c)
        _car = _car_by_name(c.car_name) if c.car_name else None
        _pos = car_position(c.car_name, positions, car=_car) if positions else None
        _pts = venue_points(c)
        rides.append({"id": c.id, "guest": c.nome, "pickup_time": c.pickup_datetime,
                      "pickup": c.endereco, "destination": c.destination,
                      "status": code, "source": "est." if est else (c.status_source or ""),
                      "driver": name or None, "note": c.assign_note or "",
                      "car_name": c.car_name, "car_gps": (_pos["name"] if _pos else None),
                      "car_gps_fresh": (_pos["fresh"] if _pos else None),
                      "car_gps_age_min": (_pos["age_min"] if _pos else None),
                      "pickup_coords": c.pickup_lat is not None,
                      "miles_to_pickup": (round(calcular_distancia(_pos["lat"], _pos["lng"], c.pickup_lat, c.pickup_lng), 2)
                                          if (_pos and c.pickup_lat is not None) else None),
                      "miles_to_venue": (round(min(calcular_distancia(_pos["lat"], _pos["lng"], p[0], p[1]) for p in _pts), 2)
                                         if (_pos and _pts) else None),
                      "venue_points": len(_pts)})
    rides.sort(key=lambda r: parse_pickup_datetime(r["pickup_time"]) or now_v)
    venues = []
    for cl in Club.query.filter_by(active=True).all():
        if "NV" not in (cl.address or "").upper():
            continue
        g = geocode_address(cl.address, allow_network=False) if cl.address else (None, None)
        gap = (round(calcular_distancia(cl.lat, cl.lng, g[0], g[1]), 2)
               if (cl.lat is not None and g[0] is not None) else None)
        venues.append({"club": cl.name, "stored": [cl.lat, cl.lng], "address_geocoded": list(g),
                       "gap_mi": gap, "check": bool(gap and gap > 0.3)})
    payload = {
        "now_vegas": now_v.strftime("%m/%d/%Y %I:%M %p"),
        "gps": {"reachable": positions is not None, "devices": len(positions or {}),
                "fresh": sum(1 for p in (positions or {}).values() if p["fresh"]), **GPS_STATS},
        "drivers": drivers,
        "gps_devices": devices,
        "upcoming_rides": rides,
        "rides_without_driver": [r for r in rides if r["status"] in ("unassigned", "waitlist")],
        "venues": venues,
        "geocoder": {**GEO_STATS, "cached_addresses": GeoCache.query.count(),
                     "nominatim_paused": time.time() < _nomi_state["backoff_until"]},
        "inactive_weekly_shifts": Shift.query.filter(Shift.active == False,
                                                     Shift.day_of_week.isnot(None)).count(),
        "possible_duplicates": [{"ride": k, "duplicate_of": v} for k, v in
                                flag_duplicates(Customer.query.filter(Customer.status.in_(("scheduled", "waitlist", "picked_up")))
                                                .order_by(Customer.id.desc()).limit(400).all()).items()],
        "recent_events": [{"at": vegas_datetime(e.created_at), "ride": e.customer_id, "actor": e.actor,
                           "event": e.event, "detail": e.detail}
                          for e in RideEvent.query.order_by(RideEvent.id.desc()).limit(60).all()],
        "settings": {"AUTO_STATUS_ENABLED": AUTO_STATUS_ENABLED, "AUTO_ASSIGN_ENABLED": AUTO_ASSIGN_ENABLED,
                     "AUTO_REASSIGN_OFFLINE": AUTO_REASSIGN_OFFLINE, "UNASSIGNED_ALERTS": UNASSIGNED_ALERTS,
                     "OPS_ALERT_NUMBERS": _ops_numbers(), "DRIVER_BUSY_WINDOW_MIN": DRIVER_BUSY_WINDOW_MIN,
                     "TRACKER_INTERVAL_SEC": TRACKER_INTERVAL_SEC, "PICKUP_RADIUS_MI": PICKUP_RADIUS_MI,
                     "VENUE_RADIUS_MI": VENUE_RADIUS_MI, "DRIVER_ACTIVE_MIN": DRIVER_ACTIVE_MIN,
                     "PARTNER_STATUS_WEBHOOK_URL": bool(PARTNER_STATUS_WEBHOOK_URL)},
    }
    return app.response_class(json.dumps(payload, indent=2, default=str), mimetype="application/json")

@app.route('/admin/guestlist/statuses')
def guestlist_statuses():
    """Lightweight JSON of current statuses for the guest list, so the page can
    auto-refresh badges (coming/arrived/left + transport status) every few seconds
    without a full reload. Honors the same type/club/date filters as the page."""
    if not session.get("logged") or not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    filter_type = request.args.get('type', 'all')
    filter_club = request.args.get('club', 'all')
    view_all    = request.args.get('view', '') == 'all'
    filter_date_iso = request.args.get('date', vegas_today().strftime("%Y-%m-%d"))

    q = Customer.query
    if filter_type == 'transport':
        q = q.filter_by(needs_transport=True)
    elif filter_type == 'walkin':
        q = q.filter_by(needs_transport=False)
    if filter_club != 'all':
        q = q.filter_by(destination=filter_club)

    if view_all:
        rows = q.order_by(Customer.id.desc()).all()
    else:
        rows = q.order_by(Customer.pickup_datetime).all()
        try:
            y, m, d = filter_date_iso.split("-")
            match = f"{int(m)}/{int(d)}/{y}"
            padded = f"{int(m):02d}/{int(d):02d}/{y}"
            rows = [c for c in rows
                    if match in (c.pickup_datetime or "") or padded in (c.pickup_datetime or "")]
        except Exception:
            pass

    items = []
    coming = arrived = left = unassigned = 0
    dups = flag_duplicates(rows)
    for c in rows:
        cs = c.club_status or "coming"
        if cs == "arrived": arrived += 1
        elif cs == "left":  left += 1
        else:               coming += 1
        info = _guest_row_info(c, dup_of=dups.get(c.id))
        if info["unassigned"]:
            unassigned += 1
        items.append({
            "id": c.id,
            "club_status": cs,
            "ride_status": info["code"],
            "ride_text": info["text"],
            "ride_source": info["source"],
            "driver": info["driver"],
            "note": info["note"],
            "origin": info["origin"],
            "dup_of": info["dup_of"],
            "unassigned": info["unassigned"],
            "needs_transport": bool(c.needs_transport),
            "dispatch_status": c.dispatch_status,
            "transport_status": c.status,
            "picked_at": vegas_time(c.picked_up_at) if c.picked_up_at else "",
            "dropped_at": vegas_time(c.dropped_off_at) if c.dropped_off_at else "",
            "verified": bool(c.dropoff_verified),
        })
    return jsonify({
        "success": True,
        "total": len(rows),
        "coming": coming, "arrived": arrived, "left": left,
        "unassigned": unassigned,
        "duplicates": len(dups),
        "items": items,
    })

@app.route('/admin/guestlist')
def admin_guestlist():
    if not session.get("logged") or not can_dispatch():
        return redirect(url_for("login"))

    # Filters
    filter_type = request.args.get('type', 'all')   # all | transport | walkin
    filter_club = request.args.get('club', 'all')
    view_all    = request.args.get('view', '') == 'all'
    # date arrives as ISO (YYYY-MM-DD) from the date picker; default = today
    filter_date_iso = request.args.get('date', vegas_today().strftime("%Y-%m-%d"))

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

    _dups = flag_duplicates(all_customers)
    ride_info = {c.id: _guest_row_info(c, dup_of=_dups.get(c.id)) for c in all_customers}
    unassigned = sum(1 for v in ride_info.values() if v["unassigned"])
    return render_template('admin_guestlist.html',
        customers=all_customers, clubs=clubs,
        ride_info=ride_info, unassigned=unassigned,
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
    filter_date_iso = request.args.get('date', vegas_today().strftime("%Y-%m-%d"))
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
                "Distance To Venue (mi)", "Status", "Priority", "Promoter",
                "Notes", "Driver Notes", "Reported", "Report Reason", "Reported By"])
    for c in rows:
        parts = (c.pickup_datetime or "").split(" ")
        d_part = parts[0] if parts else ""
        t_part = " ".join(parts[1:3]) if len(parts) >= 3 else ""
        w.writerow([
            d_part, t_part, c.nome, c.phone, c.guests, c.package,
            c.endereco, c.destination,
            (c.completed_driver or c.motorista), (c.completed_car or c.car_string_val),
            vegas_datetime(c.picked_up_at), vegas_datetime(c.dropped_off_at),
            ("Yes" if c.dropoff_verified else ("No" if c.dropped_off_at else "")),
            (c.dropoff_distance_mi or "") if c.dropped_off_at else "",
            c.status, ("Yes" if getattr(c, 'priority', False) else "No"), c.promoter or "",
            c.details or "", c.driver_notes or "",
            ("Yes" if c.reported else "No"), c.report_reason or "", c.reported_by or "",
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
    _before = _ride_status_info(customer)[0]
    customer.club_status = new_status
    mark_status(customer, "dispatch")
    # Stamp arrival time the first time they reach the venue — the live-tracking
    # link expires 2h after this.
    if new_status == "arrived" and not customer.arrived_at:
        customer.arrived_at = datetime.utcnow()
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
    ride_status_changed(customer, _before, "dispatch")
    return jsonify({"success": True, "club_status": new_status})

@app.route('/admin/guestlist/delete/<int:customer_id>', methods=['POST'])
def delete_guest(customer_id):
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    customer = Customer.query.get_or_404(customer_id)

    # If a driver still had this pickup pending, let them know it's cancelled
    if customer.motorista and customer.status in ("scheduled", "picked_up") and customer.needs_transport:
        drv = Driver.query.filter_by(name=customer.motorista).first()
        prev_phone = drv.phone if drv and drv.phone else (customer.motorista_phone or "")
        if prev_phone:
            _time_txt = ""
            _parts = (customer.pickup_datetime or "").split(" ")
            if len(_parts) >= 3:
                _time_txt = f"{_parts[1]} {_parts[2]}"
            send_sms_bg(clean_phone(prev_phone),
                f"ClubLifter: {customer.nome}"
                f"{(' (' + _time_txt + ')') if _time_txt else ''} "
                f"was cancelled and is no longer on your list.")
        # Record a cancel notice for the assigned driver (if any)
        assigned = customer.motorista
        if assigned and assigned not in ("", "Unavailable", "Waitlist", "(walk-in)"):
            try:
                db.session.add(DriverNotice(
                    driver_name=assigned, kind="cancelled",
                    guest_name=customer.nome, guest_time=customer.pickup_datetime,
                    detail="ride cancelled"))
                db.session.commit()
            except Exception:
                pass

    log_event(customer, "deleted", f"deleted by {session.get('username', '?')} on the guest list "
              f"({customer.nome} · {customer.pickup_datetime} · driver {customer.motorista or '-'})",
              actor=f"dispatch:{session.get('username', '')}")
    db.session.delete(customer)
    db.session.commit()
    return jsonify({"success": True})

@app.route('/admin/guestlist/<int:customer_id>/events')
def guest_events(customer_id):
    """The ride's audit trail (how it was created, every change and why)."""
    if not can_dispatch():
        return jsonify({"error": "Unauthorized"}), 401
    c = Customer.query.get(customer_id)
    rows = (RideEvent.query.filter_by(customer_id=customer_id)
            .order_by(RideEvent.created_at.asc()).limit(200).all())
    return jsonify({
        "success": True,
        "ride": ({"id": c.id, "source": c.source or "", "created_by": c.created_by or "",
                  "external_ref": c.external_ref or "", "created_at": vegas_datetime(c.created_at) if c.created_at else "",
                  "promoter": c.promoter or ""} if c else None),
        "events": [{"at": vegas_datetime(e.created_at), "actor": e.actor, "event": e.event, "detail": e.detail}
                   for e in rows],
    })

@app.route('/admin/guestlist/merge/<int:dup_id>/<int:keep_id>', methods=['POST'])
def merge_guest(dup_id, keep_id):
    """Fold a duplicate ride into the original (phones/notes/package copied over,
    the duplicate deleted, its driver told it's gone)."""
    if not can_dispatch():
        return jsonify({"error": "Unauthorized"}), 401
    dup, keep = Customer.query.get(dup_id), Customer.query.get(keep_id)
    if not dup or not keep or dup.id == keep.id:
        return jsonify({"success": False, "error": "Ride not found"}), 404
    who = session.get('username', '')
    changes = merge_booking_into(keep, {
        "external_ref": dup.external_ref, "price_total": dup.price_total, "package": dup.package,
        "guests": dup.guests, "phones": [dup.phone] + dup.get_phones(), "details": dup.details,
        "pickup_datetime": dup.pickup_datetime,
    }, f"duplicate #{dup.id}", f"merge by {who}")
    dup_drv = resolve_ride_driver(dup) if dup.needs_transport else None
    keep_drv = resolve_ride_driver(keep) if keep.needs_transport else None
    if dup_drv and (not keep_drv or dup_drv.id != keep_drv.id):
        t = _time_part(dup.pickup_datetime)
        if dup_drv.phone:
            send_sms_bg(clean_phone(dup_drv.phone),
                        f"ClubLifter: {dup.nome}{(' (' + t + ')') if t else ''} was a duplicate booking "
                        f"and is no longer on your list.")
        try:
            db.session.add(DriverNotice(driver_name=dup_drv.name, kind="cancelled", guest_name=dup.nome,
                                        guest_time=dup.pickup_datetime, detail=f"duplicate of #{keep.id}"))
        except Exception:
            pass
    log_event(dup, "deleted", f"merged into #{keep.id} by {who} (duplicate)", actor=f"dispatch:{who}")
    log_event(keep, "duplicate-merged", f"#{dup.id} ({dup.nome}, {dup.pickup_datetime}) merged in by {who}",
              actor=f"dispatch:{who}")
    db.session.delete(dup)
    db.session.commit()
    return jsonify({"success": True, "kept": keep.id, "changes": changes})

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

@app.route('/admin/locations', methods=['GET'])
def list_saved_locations():
    """Return saved pickup locations for the dropdown."""
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    locs = SavedLocation.query.filter_by(active=True).order_by(SavedLocation.name).all()
    return jsonify({"success": True, "locations": [l.to_dict() for l in locs]})

@app.route('/admin/locations/save', methods=['POST'])
def save_location():
    """Save a custom pickup location under a friendly name for reuse."""
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    name = (request.form.get('name') or "").strip()
    address = (request.form.get('address') or "").strip()
    if not name or not address:
        return jsonify({"success": False, "error": "Name and address are required."})
    existing = SavedLocation.query.filter(func.lower(SavedLocation.name) == name.lower()).first()
    if existing:
        existing.address = address
        existing.active = True
    else:
        db.session.add(SavedLocation(name=name, address=address))
    db.session.commit()
    return jsonify({"success": True, "name": name, "address": address})

@app.route('/admin/locations/delete/<int:loc_id>', methods=['POST'])
def delete_location(loc_id):
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    loc = SavedLocation.query.get_or_404(loc_id)
    db.session.delete(loc)
    db.session.commit()
    return jsonify({"success": True})

def _create_return_ride(orig, pickup_datetime=None, created_by="", geocode=True, custom_dropoff=None):
    """Create a NEW ride that's the reverse of an existing one: the guest goes
    from the original destination (the club) back to their original pickup
    location (hotel/address), OR to a custom drop-off if provided.
    Enters the normal queue. Returns (new_customer, error).
    """
    if not orig:
        return None, "Original ride not found."
    if orig.is_return_ride:
        return None, "This is already a return ride."
    # The return ride swaps pickup <-> destination:
    #   pickup  = the club (orig.destination)
    #   dropoff = the original pickup address (orig.endereco), or a custom one
    return_pickup_addr = (orig.destination or "").strip()
    return_dropoff     = (custom_dropoff or "").strip() or (orig.endereco or "").strip()
    if not return_pickup_addr:
        return None, "The original ride has no destination to return from."

    # Resolve the club's address for geocoding (destination is a club name)
    pickup_geocode_addr = return_pickup_addr
    club = club_for_destination(return_pickup_addr)
    if club and club.address:
        pickup_geocode_addr = club.address

    # No time given = "as soon as possible" → now. An empty time used to hide the
    # ride from every driver's board and from today's guest list.
    new_dt = (normalize_pickup_datetime(pickup_datetime) if pickup_datetime
              else vegas_naive_now().strftime("%m/%d/%Y %I:%M %p"))
    # Human-readable note of who booked this return, shown on the ride.
    if (created_by or "").startswith("app:"):
        booked_note = "Return ride — scheduled by client via the app"
    elif created_by == "api":
        booked_note = "Return ride — scheduled via API"
    else:
        who = created_by or "manager"
        booked_note = f"Return ride — scheduled by {who}"
    ride = Customer(
        nome=orig.nome,
        phone=orig.phone,
        phones_json=orig.phones_json,
        endereco=pickup_geocode_addr,       # pickup is now the club/venue address
        details=(booked_note + (f" — {orig.details}" if orig.details else "")),
        destination=return_dropoff,          # dropoff is the original pickup (hotel)
        package=orig.package,
        guests=orig.guests,
        pickup_datetime=new_dt,
        needs_transport=True,
        promoter=created_by or orig.promoter,
        is_return_ride=True,
        return_of_id=orig.id,
        status="scheduled",
        club_status="coming",
    )
    # Coordinates: pickup = the club, drop-off = where the guest came from.
    if club and club.lat is not None:
        ride.pickup_lat, ride.pickup_lng = club.lat, club.lng
    elif geocode and pickup_geocode_addr:
        ride.pickup_lat, ride.pickup_lng = geocode_address(pickup_geocode_addr)
    if not custom_dropoff and orig.pickup_lat is not None:
        ride.dest_lat, ride.dest_lng = orig.pickup_lat, orig.pickup_lng
    elif geocode and return_dropoff:
        ride.dest_lat, ride.dest_lng = geocode_address(return_dropoff)

    # Assign a driver right away (before, return rides never got one).
    pick, why = pick_driver(ride.pickup_lat, ride.pickup_lng, parse_pickup_datetime(new_dt))
    if pick:
        ride.motorista = pick.driver.name
        ride.motorista_phone = pick.driver.phone or ""
        if pick.car:
            ride.car_name = pick.car.name
            ride.car_string_val = pick.car.car_string()
            ride.car_photo = pick.car.photo or ""
        if pick.dist_mi not in (None, float("inf")):
            ride.distancia = round(pick.dist_mi, 2)
        ride.assign_note = (pick.note or "")[:200]
    else:
        ride.motorista = ""
        ride.assign_note = (why or "")[:200]

    ride.source = "return"
    ride.created_by = (created_by or "")[:160]
    db.session.add(ride)
    # Mark the original as having a return
    orig.has_return_ride = True
    db.session.commit()
    log_event(ride, "created", f"ride back of #{orig.id} by {created_by or '?'} · {new_dt} · {return_pickup_addr} → {return_dropoff}"
              + (f" · driver {ride.motorista}" if ride.motorista else f" · NO DRIVER ({ride.assign_note})"),
              actor="auto")
    log_event(orig, "ride-back", f"ride back #{ride.id} created ({new_dt})", actor="auto")
    if pick:
        notify_driver_of_ride(ride, pick.driver, "assigned")
        _sms_guest_driver_details(ride)

    fire_webhook({
        "type":            "return_ride_created",
        "customer_id":     ride.id,
        "original_id":     orig.id,
        "customer_name":   ride.nome,
        "customer_phone":  ride.phone,
        "pickup_address":  ride.endereco,
        "destination":     ride.destination,
        "pickup_datetime": ride.pickup_datetime,
        "created_by":      created_by,
    })
    return ride, None

@app.route('/admin/guestlist/ride-back/<int:customer_id>', methods=['POST'])
def admin_ride_back(customer_id):
    """Book a return ride (club → original pickup, or a custom drop-off) for an
    existing booking."""
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    orig = Customer.query.get_or_404(customer_id)
    pickup_dt = (request.form.get('pickup_datetime', '') or "").strip()
    custom_dropoff = (request.form.get('dropoff', '') or "").strip()
    if pickup_dt and parse_pickup_datetime(normalize_pickup_datetime(pickup_dt)) is None:
        return jsonify({"success": False, "error": "Couldn't read that date/time."})
    ride, err = _create_return_ride(orig, pickup_dt, created_by=session.get("username", ""),
                                    custom_dropoff=custom_dropoff or None)
    if err:
        return jsonify({"success": False, "error": err})
    return jsonify({
        "success": True,
        "return_ride_id": ride.id,
        "pickup": ride.endereco,
        "destination": ride.destination,
        "pickup_datetime": ride.pickup_datetime,
    })

@app.route('/admin/rideback/today-guests', methods=['GET'])
def rideback_today_guests():
    """List today's guests (most recent first) for the Ride Back picker on the
    dashboard. Only rides that have a destination and aren't themselves returns."""
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    vt = vegas_today()
    today_variants = [f"{vt.month:02d}/{vt.day:02d}/{vt.year}", f"{vt.month:02d}/{vt.day:02d}/{str(vt.year)[2:]}"]
    guests = []
    seen = set()
    # Pull recent rides, filter to today by the stored pickup date
    rides = (Customer.query
             .filter(Customer.is_return_ride == False)
             .filter(Customer.destination != "")
             .order_by(Customer.id.desc()).limit(200).all())
    for c in rides:
        dt = (c.pickup_datetime or "")
        date_part = dt.split(" ")[0] if dt else ""
        is_today = any(date_part == tv for tv in today_variants)
        # also include very recent ones (created today) even if no date parsed
        if not is_today and c.created_at and c.created_at.date() != datetime.utcnow().date():
            continue
        if c.id in seen:
            continue
        seen.add(c.id)
        guests.append({
            "id": c.id,
            "name": c.nome,
            "phone": c.phone,
            "club": c.destination,           # where they are now (their club)
            "original_pickup": c.endereco,   # where they came from (default return dropoff)
            "pickup_datetime": c.pickup_datetime,
            "has_return": c.has_return_ride,
        })
        if len(guests) >= 50:
            break
    return jsonify({"success": True, "guests": guests})

@app.route('/admin/guestlist/edit/<int:customer_id>', methods=['POST'])
def edit_customer(customer_id):
    """Edit a ride's details — name, phone, pickup address, destination, guests,
    package, notes, and date/time. Only sends the fields that changed."""
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    f = request.form

    # Simple text fields
    if 'nome' in f:
        v = (f.get('nome') or "").strip()
        if v:
            c.nome = v
    if 'phone' in f:
        c.phone = clean_phone(f.get('phone') or "")
    _old_addr, _old_dest = c.endereco, c.destination
    if 'endereco' in f:
        c.endereco = (f.get('endereco') or "").strip()
    if 'destination' in f:
        c.destination = (f.get('destination') or "").strip()
    _addr_changed = (c.endereco or "") != (_old_addr or "")
    _dest_changed = (c.destination or "") != (_old_dest or "")
    if 'package' in f:
        c.package = (f.get('package') or "").strip()
    if 'details' in f:
        c.details = (f.get('details') or "").strip()[:500]
    if 'guests' in f:
        try:
            c.guests = int(f.get('guests') or 0)
        except (TypeError, ValueError):
            pass

    # Date/time (optional here — reschedule route also exists)
    if f.get('pickup_datetime'):
        normalized = normalize_pickup_datetime(f.get('pickup_datetime').strip())
        if parse_pickup_datetime(normalized) is None:
            return jsonify({"success": False, "error": "Couldn't read that date/time."})
        if normalized != c.pickup_datetime:
            c.pickup_datetime = normalized
            # Re-arm proximity alerts for the new time
            c.notified_15km = c.notified_10km = c.notified_5km = False
            reset_ride_tracking(c)

    if _addr_changed or _dest_changed:
        reset_ride_tracking(c, address_changed=_addr_changed, dest_changed=_dest_changed)
    db.session.commit()
    log_event(c, "edit", "fields: " + ", ".join(k for k in ('nome', 'phone', 'endereco', 'destination',
                                                           'pickup_datetime', 'guests', 'package', 'details') if k in f),
              actor=f"dispatch:{session.get('username', '')}")
    fire_webhook({
        "type":          "pickup_edited",
        "customer_id":   c.id,
        "customer_name": c.nome,
        "changed_by":    session.get("username", ""),
    })
    return jsonify({
        "success": True,
        "customer": {
            "id": c.id, "nome": c.nome, "phone": c.phone, "endereco": c.endereco,
            "destination": c.destination, "package": c.package, "guests": c.guests,
            "details": c.details, "pickup_datetime": c.pickup_datetime,
        }
    })

@app.route('/admin/guestlist/reschedule/<int:customer_id>', methods=['POST'])
def reschedule_pickup(customer_id):
    """Change the pickup date/time of an existing ride. Accepts loose date formats
    (2- or 4-digit year) and re-arms proximity alerts so they fire correctly."""
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    c = Customer.query.get_or_404(customer_id)
    raw = (request.form.get('pickup_datetime', '') or "").strip()
    if not raw:
        return jsonify({"success": False, "error": "Pickup date/time is required"})
    normalized = normalize_pickup_datetime(raw)
    if parse_pickup_datetime(normalized) is None:
        return jsonify({"success": False, "error": "Couldn't read that date/time. Use MM/DD/YYYY HH:MM AM/PM."})
    old = c.pickup_datetime
    c.pickup_datetime = normalized
    # Re-arm proximity alerts + GPS detection for the new time
    reset_ride_tracking(c)
    db.session.commit()
    log_event(c, "reschedule", f"{old} → {normalized}", actor=f"dispatch:{session.get('username', '')}")
    fire_webhook({
        "type":            "pickup_rescheduled",
        "customer_id":     c.id,
        "customer_name":   c.nome,
        "customer_phone":  c.phone,
        "old_datetime":    old,
        "new_datetime":    normalized,
        "driver_name":     c.motorista,
        "destination":     c.destination,
        "changed_by":      session.get("username", ""),
    })
    return jsonify({"success": True, "pickup_datetime": normalized})

@app.route('/admin/guestlist/dispatch/<int:customer_id>', methods=['POST'])
def update_dispatch_status(customer_id):
    if not can_dispatch():
        return jsonify({"success": False, "error": "Unauthorized"})
    customer = Customer.query.get_or_404(customer_id)
    new_status = request.form.get('dispatch_status', '').strip()
    if new_status not in ('none', 'sent', 'confirmed', 'enroute', 'arrived'):
        return jsonify({"success": False, "error": "Invalid status"})
    _before = _ride_status_info(customer)[0]
    customer.dispatch_status = new_status
    if new_status == "arrived" and not customer.driver_arrived_at:
        customer.driver_arrived_at = datetime.utcnow()
    mark_status(customer, "dispatch")
    db.session.commit()
    ride_status_changed(customer, _before, "dispatch")

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
        pos = car_position(customer.car_name)
        if not pos:
            return jsonify({"eta_min": None, "distance_km": None, "reason": "car not reporting GPS"})
        pts = venue_points(customer, allow_network=True)
        if not pts:
            return jsonify({"eta_min": None, "distance_km": None, "reason": "could not locate club"})
        dist = min(calcular_distancia(pos["lat"], pos["lng"], p[0], p[1]) for p in pts)
        eta_min = round((dist / 25.0) * 60)
        return jsonify({"eta_min": eta_min, "distance_mi": round(dist, 1), "distance_km": round(dist, 1),
                        "gps_age_min": pos.get("age_min")})
    except Exception as e:
        return jsonify({"eta_min": None, "distance_km": None, "reason": str(e)}), 500

# ═══════════════════════════════════════════════════════════════════════════════
#  DISPATCH ENGINE
#  One place for everything the bookings, the dashboards and the background
#  tracker share: geocoding (cached + throttled), the OneStepGPS fleet (cached),
#  who is actually driving tonight, auto-assignment, and GPS-driven ride status.
# ═══════════════════════════════════════════════════════════════════════════════
import re as _re

def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)

def _env_bool(name, default=True):
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")

# ── Tunables (every one can be overridden in Railway) ──
AUTO_STATUS_ENABLED       = _env_bool("AUTO_STATUS_ENABLED", True)    # GPS moves the ride status
AUTO_ASSIGN_ENABLED       = _env_bool("AUTO_ASSIGN_ENABLED", True)    # retry rides with no driver
AUTO_REASSIGN_OFFLINE     = _env_bool("AUTO_REASSIGN_OFFLINE", True)  # move rides off drivers who aren't online
UNASSIGNED_ALERTS         = _env_bool("UNASSIGNED_ALERTS", True)      # SMS ops about rides with no driver
TRACKER_INTERVAL_SEC      = max(20, int(_env_float("TRACKER_INTERVAL_SEC", 60)))
PICKUP_RADIUS_MI          = _env_float("PICKUP_RADIUS_MI", 0.25)   # car this close to the pickup = "at pickup"
PICKUP_DWELL_SEC          = _env_float("PICKUP_DWELL_SEC", 90)     # ...for at least this long (not just driving by)
PICKUP_LEAVE_MI           = _env_float("PICKUP_LEAVE_MI", 0.45)    # car left the pickup = guest picked up
VENUE_RADIUS_MI           = _env_float("VENUE_RADIUS_MI", 0.35)    # car this close to the venue = dropped off
STOP_DROP_MIN             = _env_float("STOP_DROP_MIN", 4)         # picked-up car parked this long, away from the pickup = dropped off
STOP_DROP_MIN_FROM_PICKUP_MI = _env_float("STOP_DROP_MIN_FROM_PICKUP_MI", 1.0)
DROP_REMINDER_MIN         = _env_float("DROP_REMINDER_MIN", 25)    # text the driver if still "picked up" after this
PICKED_UP_TIMEOUT_MIN     = _env_float("PICKED_UP_TIMEOUT_MIN", 75) # auto-complete a picked-up ride after this
SCHEDULED_TIMEOUT_MIN     = _env_float("SCHEDULED_TIMEOUT_MIN", 180) # auto-complete an assigned ride this long after pickup
ENROUTE_RADIUS_MI         = _env_float("ENROUTE_RADIUS_MI", 3.0)
ARRIVAL_WINDOW_MIN        = _env_float("ARRIVAL_WINDOW_MIN", 30)   # only detect "at pickup" this close to pickup time
GPS_FRESH_MIN             = _env_float("GPS_FRESH_MIN", 20)        # ignore GPS fixes older than this
DRIVER_ACTIVE_MIN         = _env_float("DRIVER_ACTIVE_MIN", 30)    # dashboard seen within → on duty
CAR_MOVED_MIN             = _env_float("CAR_MOVED_MIN", 90)        # car moved within → on duty
REASSIGN_OFFLINE_LEAD_MIN = _env_float("REASSIGN_OFFLINE_LEAD_MIN", 40)
UNASSIGNED_URGENT_MIN     = _env_float("UNASSIGNED_URGENT_MIN", 45)
OPS_ALERT_NUMBERS = [n.strip() for n in os.environ.get("OPS_ALERT_NUMBERS", "").split(",") if n.strip()]
PARTNER_STATUS_WEBHOOK_URL = os.environ.get("PARTNER_STATUS_WEBHOOK_URL", "").strip()
PARTNER_STATUS_WEBHOOK_KEY = os.environ.get("PARTNER_STATUS_WEBHOOK_KEY", "").strip()
PARTNER_STATUS_PROMOTERS   = [p.strip().lower() for p in
                              os.environ.get("PARTNER_STATUS_PROMOTERS", "cartvip").split(",") if p.strip()]

NO_DRIVER_VALUES = ("", "Unavailable", "Waitlist", "(walk-in)")
# The App Store / Google Play review account (seed_demo_ride). It may be linked to
# a REAL car for the demo map, so it must never be picked for real rides — and the
# demo ride must never be moved by real GPS, reassigned, or trigger alerts.
DEMO_DRIVER_NAMES = {"demo.driver"}

def _is_demo_driver(drv):
    return bool(drv) and (drv.name or "").strip().lower() in DEMO_DRIVER_NAMES

def _is_demo_ride(c):
    """The review/demo ride: 'Demo Guest', or any ride on the demo phone while
    DEMO_RIDE is on (seed_demo_ride keeps those in a fixed 'driver en route' state)."""
    if (c.nome or "").strip() == "Demo Guest":
        return True
    if os.environ.get("DEMO_RIDE", "0").strip().lower() in ("0", "false", "no", ""):
        return False
    demo = clean_phone(os.environ.get("DEMO_RIDE_PHONE", "") or globals().get("DEMO_PHONE", "") or "")
    mine = clean_phone(c.phone or "")
    return len(demo) >= 10 and len(mine) >= 10 and demo[-10:] == mine[-10:]
_ENGINE_BOOT = time.time()
BOOT_GRACE_SEC = 600   # don't call a driver "offline" in the first 10 min after a deploy

def log_event(c_or_id, event, detail="", actor=""):
    """Append one line to a ride's audit trail. Never raises, never affects the
    caller's transaction (own connection)."""
    try:
        cid = c_or_id if isinstance(c_or_id, int) else getattr(c_or_id, "id", None)
        if not cid:
            return
        with db.engine.begin() as conn:
            conn.execute(RideEvent.__table__.insert().values(
                customer_id=int(cid), created_at=datetime.utcnow(),
                actor=str(actor or "")[:60], event=str(event or "")[:60],
                detail=str(detail or "")[:600]))
    except Exception as e:
        print(f"[EVENT] not logged ({event}): {e}", flush=True)

def _request_origin():
    """'ip · user-agent' of the current request, for API bookings."""
    try:
        ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "").split(",")[0].strip()
        ua = (request.headers.get("User-Agent", "") or "")[:90]
        return f"{ip} · {ua}".strip(" ·")[:160]
    except Exception:
        return ""

def _phone_tail(p):
    d = "".join(ch for ch in str(p or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else d

def _norm_person(name):
    return " ".join(_re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())

def _looks_like_phone(p):
    return len("".join(ch for ch in str(p or "") if ch.isdigit())) >= 7

def find_duplicate_ride(name, phones, pickup_dt, external_ref="", exclude_id=None, window_min=180):
    """An open, non-return ride for the same person around the same time.
    Matches by phone (last 10 digits) first, then by name. A different partner
    order id (external_ref) on both sides means two real bookings → no match."""
    if pickup_dt is None:
        return None, ""
    tails = {_phone_tail(p) for p in (phones or []) if _phone_tail(p)}
    nm = _norm_person(name)
    lo, hi = pickup_dt - timedelta(hours=18), pickup_dt + timedelta(hours=18)
    best = None
    for ex in (Customer.query.filter(Customer.status.in_(("scheduled", "waitlist", "picked_up")))
               .order_by(Customer.id.desc()).limit(600).all()):
        if ex.id == exclude_id or ex.is_return_ride or not ex.needs_transport:
            continue
        pdt = parse_pickup_datetime(ex.pickup_datetime)
        if pdt is None or not (lo <= pdt <= hi):
            continue
        if external_ref and (ex.external_ref or "") and ex.external_ref != external_ref:
            continue        # two different partner orders → both real (e.g. two bookings via an affiliate)
        if external_ref and ex.external_ref == external_ref:
            return ex, "same order id"
        gap = abs((pdt - pickup_dt).total_seconds()) / 60.0
        if gap > window_min:
            continue
        ex_tails = {_phone_tail(ex.phone)} | {_phone_tail(p) for p in ex.get_phones()}
        ex_tails.discard("")
        if tails & ex_tails:
            cand = (0, gap, ex, "same phone")
        elif nm and _norm_person(ex.nome) == nm:
            cand = (1, gap, ex, "same name")
        else:
            continue
        if best is None or cand[:2] < best[:2]:
            best = cand
    return (best[2], best[3]) if best else (None, "")

def flag_duplicates(rows):
    """{ride id: original ride id} for rides that look like copies of an earlier
    one in the same listing (same person, pickup within 3 h). The earliest-created
    ride is the original."""
    out = {}
    open_rows = [c for c in rows if c.needs_transport and not c.is_return_ride
                 and c.status in ("scheduled", "waitlist", "picked_up")]
    open_rows.sort(key=lambda c: c.id)
    seen = []   # (tails, norm name, pdt, id)
    for c in open_rows:
        pdt = parse_pickup_datetime(c.pickup_datetime)
        if pdt is None:
            continue
        tails = {_phone_tail(c.phone)} | {_phone_tail(p) for p in c.get_phones()}
        tails.discard("")
        nm = _norm_person(c.nome)
        for t, n, p, oid in seen:
            if abs((pdt - p).total_seconds()) <= 180 * 60 and ((tails & t) or (nm and nm == n)):
                out[c.id] = oid
                break
        else:
            seen.append((tails, nm, pdt, c.id))
    return out

def merge_booking_into(existing, data, source_label, origin):
    """A partner booking that matches a ride dispatch already created by hand:
    fold the partner's details into that ride instead of making a second one."""
    changes = []
    if not existing.external_ref and data.get("external_ref"):
        existing.external_ref = str(data["external_ref"])[:80]; changes.append("order id")
    if data.get("price_total") is not None and existing.price_total is None:
        existing.price_total = data["price_total"]; changes.append("price")
    if data.get("package") and not existing.package:
        existing.package = data["package"]; changes.append("package")
    if data.get("guests") and not existing.guests:
        existing.guests = data["guests"]; changes.append("guests")
    phones = existing.get_phones()
    tails = {_phone_tail(p) for p in phones}
    for p in data.get("phones") or []:
        if p and _phone_tail(p) not in tails:
            phones.append(p); tails.add(_phone_tail(p)); changes.append("phone")
    if len(phones) > len(existing.get_phones()):
        existing.phones_json = json.dumps([p for p in phones if p and p != existing.phone])
    if data.get("details") and (data["details"] not in (existing.details or "")):
        existing.details = ((existing.details or "") + f" | {source_label}: {data['details']}")[:500]
        changes.append("notes")
    if data.get("pickup_datetime") and data["pickup_datetime"] != existing.pickup_datetime:
        note = f"{source_label} booking says {data['pickup_datetime']}"
        if note not in (existing.details or ""):
            existing.details = ((existing.details or "") + f" | ⚠ {note}")[:500]
        changes.append(f"time differs ({data['pickup_datetime']})")
    if existing.promoter != "cartvip" and source_label.lower() == "cartvip":
        existing.promoter = "cartvip"      # so partner status pushes cover it
    existing.source = (existing.source or "") + ("+" if existing.source else "") + source_label.lower()
    db.session.commit()
    log_event(existing, "duplicate-merged",
              f"{source_label} sent the same booking; merged ({', '.join(changes) or 'no new info'}). From {origin}",
              actor="api")
    return changes

def _memo():
    """A dict that lives for one request (or one tracker tick). Lookups like
    'which Driver is called X' or 'who's on the roster at 10 PM' are repeated
    for every row of the guest list — this keeps them to one query each."""
    from flask import g, has_app_context
    if not has_app_context():
        return {}
    m = getattr(g, "_dispatch_memo", None)
    if m is None:
        m = {}
        g._dispatch_memo = m
    return m

def _clear_memo_after_commit(_session):
    """Any write invalidates the memo, so nothing stale is ever served."""
    try:
        from flask import g, has_app_context
        if has_app_context() and getattr(g, "_dispatch_memo", None):
            g._dispatch_memo.clear()
    except Exception:
        pass

from sqlalchemy import event as _sa_event
from sqlalchemy.orm import Session as _SASession
_sa_event.listen(_SASession, "after_commit", _clear_memo_after_commit)

def _driver_by_name(name):
    name = (name or "").strip()
    if not name:
        return None
    m = _memo().setdefault("drv", {})
    if name not in m:
        m[name] = Driver.query.filter_by(name=name).first() or get_driver_record(name)
    return m[name]

def _car_by_name(name):
    name = (name or "").strip()
    if not name:
        return None
    m = _memo().setdefault("car", {})
    if name not in m:
        m[name] = _match_car(name)
    return m[name]

def _time_part(dt_txt):
    parts = (dt_txt or "").split(" ")
    return f"{parts[1]} {parts[2]}" if len(parts) >= 3 else ""

# ─────────────────────────────────────────────────────────────────────────────
#  GEOCODING — cached forever per address, Nominatim throttled to 1 req/s
# ─────────────────────────────────────────────────────────────────────────────
VEGAS_CENTER = (36.1147, -115.1728)
GEO_MAX_MI_FROM_VEGAS = _env_float("GEO_MAX_MI_FROM_VEGAS", 80)
NOMINATIM_MIN_INTERVAL = _env_float("NOMINATIM_MIN_INTERVAL", 1.1)
NOMINATIM_UA = os.environ.get("NOMINATIM_UA", "ClubLifter/1.0 (+https://www.clublifter.com)")
_NOMI_VIEWBOX = "-115.65,36.45,-114.75,35.85"     # Nominatim: left,top,right,bottom
_PHOTON_BBOX  = "-115.65,35.85,-114.75,36.45"     # Photon:    minLon,minLat,maxLon,maxLat

_geo_mem = {}                    # address key -> (lat, lng)
_geo_neg = {}                    # address key -> time of last total failure
_nomi_lock = threading.Lock()
_nomi_state = {"last": 0.0, "backoff_until": 0.0}
GEO_STATS = {"nominatim_ok": 0, "nominatim_fail": 0, "nominatim_rate_limited": 0,
             "photon_ok": 0, "photon_fail": 0, "landmark_fallback": 0, "cache_hits": 0,
             "last_error": "", "last_error_at": ""}

def _geo_error(msg):
    GEO_STATS["last_error"] = str(msg)[:200]
    GEO_STATS["last_error_at"] = vegas_datetime(datetime.utcnow())

def _nominatim_get(params, path="search", timeout=10):
    """The ONLY way the app talks to Nominatim: at most one request every
    NOMINATIM_MIN_INTERVAL seconds across the whole server, and a 2-minute pause
    if they rate-limit us. Returns parsed JSON, or None."""
    if time.time() < _nomi_state["backoff_until"]:
        return None
    url = f"https://nominatim.openstreetmap.org/{path}?" + urllib.parse.urlencode(params)
    with _nomi_lock:
        wait = _nomi_state["last"] + NOMINATIM_MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(url, headers={"User-Agent": NOMINATIM_UA, "Accept-Language": "en"},
                             timeout=timeout)
        except Exception as e:
            _nomi_state["last"] = time.time()
            GEO_STATS["nominatim_fail"] += 1
            _geo_error(f"nominatim: {e}")
            return None
        _nomi_state["last"] = time.time()
    if r.status_code in (403, 429, 509):
        _nomi_state["backoff_until"] = time.time() + 120
        GEO_STATS["nominatim_rate_limited"] += 1
        _geo_error(f"nominatim rate-limited us (HTTP {r.status_code}) — pausing 2 min")
        print(f"[GEO] Nominatim rate-limited (HTTP {r.status_code}); pausing 2 min", flush=True)
        return None
    if r.status_code != 200 or not (r.text or "").strip():
        GEO_STATS["nominatim_fail"] += 1
        _geo_error(f"nominatim HTTP {r.status_code}")
        return None
    try:
        return r.json()
    except Exception:
        GEO_STATS["nominatim_fail"] += 1
        _geo_error("nominatim returned non-JSON")
        return None

def _photon_search(q):
    """Second geocoder (photon.komoot.io, also OpenStreetMap data, typo-tolerant).
    Used when Nominatim can't find the address or is rate-limiting us."""
    try:
        r = requests.get("https://photon.komoot.io/api/",
                         params={"q": q, "limit": 1, "lat": VEGAS_CENTER[0],
                                 "lon": VEGAS_CENTER[1], "bbox": _PHOTON_BBOX},
                         headers={"User-Agent": NOMINATIM_UA}, timeout=8)
        if r.status_code != 200:
            GEO_STATS["photon_fail"] += 1
            return None
        feats = (r.json() or {}).get("features") or []
        if feats:
            lon, lat = feats[0]["geometry"]["coordinates"][:2]
            GEO_STATS["photon_ok"] += 1
            return float(lat), float(lon)
    except Exception as e:
        GEO_STATS["photon_fail"] += 1
        _geo_error(f"photon: {e}")
    return None

# Big Strip resorts. The canonical name is tried FIRST against the geocoders
# ("Caesars Palace Augustus Valet" → "Caesars Palace, Las Vegas"). The coordinates
# are approximate building centres and are only used as a LAST resort, when both
# geocoders fail, and never for addresses with a house number (e.g. "W Sahara Ave").
_VEGAS_LANDMARKS = [
    (r"\bcaesars\s+palace\b",                    "Caesars Palace, Las Vegas",          36.1162, -115.1745),
    (r"\bbellagio\b",                             "Bellagio, Las Vegas",                36.1129, -115.1765),
    (r"\baria\b",                                 "ARIA Resort & Casino, Las Vegas",    36.1072, -115.1766),
    (r"\bvdara\b",                                "Vdara Hotel, Las Vegas",             36.1093, -115.1778),
    (r"\bcosmopolitan\b|\bthe\s+cosmo\b",         "The Cosmopolitan of Las Vegas",      36.1097, -115.1740),
    (r"\bvenetian\b",                             "The Venetian Resort, Las Vegas",     36.1212, -115.1696),
    (r"\bpalazzo\b",                              "The Palazzo, Las Vegas",             36.1240, -115.1680),
    (r"\bencore\b",                               "Encore Las Vegas",                   36.1293, -115.1646),
    (r"\bwynn\b",                                 "Wynn Las Vegas",                     36.1265, -115.1659),
    (r"\bmgm\s+grand\b",                          "MGM Grand, Las Vegas",               36.1024, -115.1699),
    (r"\bpark\s+mgm\b",                           "Park MGM, Las Vegas",                36.1045, -115.1745),
    (r"\bnew\s+york[\s-]+new\s+york\b|\bnyny\b",  "New York-New York, Las Vegas",       36.1021, -115.1744),
    (r"\bexcalibur\b",                            "Excalibur, Las Vegas",               36.0988, -115.1754),
    (r"\bluxor\b",                                "Luxor, Las Vegas",                   36.0955, -115.1761),
    (r"\bmandalay\s+bay\b",                       "Mandalay Bay, Las Vegas",            36.0919, -115.1747),
    (r"\bdelano\b",                               "Delano Las Vegas",                   36.0928, -115.1764),
    (r"\bparis\s+(las\s+vegas|hotel|casino)\b|^paris\b", "Paris Las Vegas",             36.1125, -115.1707),
    (r"\bplanet\s+hollywood\b",                   "Planet Hollywood, Las Vegas",        36.1100, -115.1718),
    (r"\bhorseshoe\b|\bbally'?s\b",               "Horseshoe Las Vegas",                36.1143, -115.1701),
    (r"\bflamingo\s+(las\s+vegas|hotel|casino)\b|^flamingo$", "Flamingo Las Vegas",     36.1161, -115.1708),
    (r"\bthe\s+linq\b|\blinq\s+hotel\b",          "The LINQ Hotel, Las Vegas",          36.1178, -115.1707),
    (r"\bharrah'?s\b",                            "Harrah's Las Vegas",                 36.1196, -115.1712),
    (r"\bcromwell\b",                             "The Cromwell, Las Vegas",            36.1155, -115.1717),
    (r"\btreasure\s+island\b",                    "Treasure Island, Las Vegas",         36.1247, -115.1721),
    (r"\bresorts\s+world\b",                      "Resorts World Las Vegas",            36.1340, -115.1662),
    (r"\bfontaine\s*bleau\b",                     "Fontainebleau Las Vegas",            36.1373, -115.1615),
    (r"\bcircus\s+circus\b",                      "Circus Circus, Las Vegas",           36.1374, -115.1638),
    (r"\bsahara\s+(las\s+vegas|hotel|casino)\b",  "SAHARA Las Vegas",                   36.1422, -115.1567),
    (r"\bthe\s+strat\b|\bstratosphere\b",         "The STRAT, Las Vegas",               36.1475, -115.1566),
    (r"\bvirgin\s+hotels?\b",                     "Virgin Hotels Las Vegas",            36.1094, -115.1547),
    (r"\bwestgate\b",                             "Westgate Las Vegas",                 36.1356, -115.1520),
]

def _landmark_for(text):
    t = (text or "").strip().lower()
    if not t:
        return None
    for pat, canonical, lat, lng in _VEGAS_LANDMARKS:
        if _re.search(pat, t):
            return canonical, lat, lng
    return None

def _has_house_number(text):
    return bool(_re.match(r"^\s*\d+[a-z]?\s+\S", text or "", flags=_re.I))

def _geo_key(address):
    s = _re.sub(r"[^a-z0-9#]+", " ", (address or "").lower())
    return " ".join(s.split())[:300]

def _near_vegas(lat, lng):
    try:
        return calcular_distancia(VEGAS_CENTER[0], VEGAS_CENTER[1], lat, lng) <= GEO_MAX_MI_FROM_VEGAS
    except Exception:
        return False

def _strip_pickup_descriptors(a):
    """'Caesars Palace Augustus Valet' → 'Caesars Palace Augustus'. Words that
    describe WHERE at a hotel to meet (valet, lobby, rideshare…) confuse geocoders."""
    s = _re.sub(r"\b(valet|self[\s-]?park(ing)?|lobby|main\s+entrance|entrance|porte[\s-]?coch[eè]re|"
                r"ride[\s-]?share|pick[\s-]?up(\s+area)?|drop[\s-]?off|front\s+desk|front|towers?|"
                r"doors?|uber|lyft|taxi(\s+line)?|area|zone|garage|level\s+\d+)\b", " ", a or "", flags=_re.I)
    return " ".join(s.replace(" ,", ",").split()).strip(" ,-")

def _geo_candidates(address):
    a = " ".join((address or "").split())
    normalized, no_unit = _normalize_street_address(a)
    stripped = _strip_pickup_descriptors(no_unit)

    def _city(s):
        low = s.lower()
        if any(tok in low for tok in (" nv", "nevada", "las vegas", "henderson", ", usa")):
            return s
        return f"{s}, Las Vegas, NV"

    out, seen = [], set()
    lm = _landmark_for(a)
    if lm:
        out.append(lm[0]); seen.add(lm[0].lower())
    for b in (no_unit, stripped, normalized, a):
        if not b:
            continue
        q = _city(b)
        if q.lower() not in seen:
            seen.add(q.lower()); out.append(q)
    return out[:4]

def _geo_save(key, query, lat, lng, source):
    _geo_mem[key] = (lat, lng)
    try:
        # Own connection/transaction so a geocode never commits (or rolls back)
        # whatever the calling request has pending in its session.
        with db.engine.begin() as conn:
            conn.execute(GeoCache.__table__.insert().values(
                key=key, query_text=(query or "")[:500], lat=lat, lng=lng,
                source=source, created_at=datetime.utcnow()))
    except Exception as e:
        if "UNIQUE" not in str(e).upper():
            print(f"[GEO] cache write skipped: {e}", flush=True)

def _club_coords_for_text(text):
    """A pickup/destination typed as a known club's exact name or address."""
    t = (text or "").strip().lower()
    if not t:
        return None
    try:
        for cl in Club.query.all():
            if cl.lat is None or cl.lng is None:
                continue
            if (cl.name or "").strip().lower() == t:
                return cl.lat, cl.lng
    except Exception:
        pass
    return None

def geocode_address(address, allow_network=True):
    """(lat, lng) for an address, or (None, None).
    Order: memory → saved cache → exact club name → Nominatim (throttled,
    Las Vegas–bounded) → Photon → approximate landmark. Successful lookups are
    stored so each address hits the network once, ever."""
    a = (address or "").strip()
    if not a or a.lower() in ("(walk-in)", "walk-in", "n/a", "na", "tbd", "-"):
        return None, None
    key = _geo_key(a)
    if not key:
        return None, None
    hit = _geo_mem.get(key)
    if hit:
        GEO_STATS["cache_hits"] += 1
        return hit
    try:
        row = GeoCache.query.filter_by(key=key).first()
    except Exception:
        row = None
    if row:
        _geo_mem[key] = (row.lat, row.lng)
        GEO_STATS["cache_hits"] += 1
        return row.lat, row.lng
    club_ll = _club_coords_for_text(a)
    if club_ll:
        return club_ll
    if not allow_network:
        return None, None

    neg = _geo_neg.get(key)
    tried_network = False
    if not (neg and time.time() - neg < 600):
        tried_network = True
        cands = _geo_candidates(a)
        for q in cands:
            data = _nominatim_get({"q": q, "format": "json", "limit": 1,
                                   "viewbox": _NOMI_VIEWBOX, "bounded": 1})
            if data is None:
                break        # network error / rate-limited: other spellings won't help
            if data:
                try:
                    lat, lng = float(data[0]["lat"]), float(data[0]["lon"])
                    if _near_vegas(lat, lng):
                        GEO_STATS["nominatim_ok"] += 1
                        _geo_save(key, q, lat, lng, "nominatim")
                        if q != a:
                            print(f"[GEO] '{a}' resolved via '{q}'", flush=True)
                        return lat, lng
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
        for q in cands[:2]:
            p = _photon_search(q)
            if p and _near_vegas(*p):
                _geo_save(key, q, p[0], p[1], "photon")
                print(f"[GEO] '{a}' resolved by Photon via '{q}'", flush=True)
                return p
        _geo_neg[key] = time.time()

    lm = _landmark_for(a)
    if lm and not _has_house_number(a):
        if tried_network:
            GEO_STATS["landmark_fallback"] += 1
            print(f"[GEO] '{a}' → approximate landmark '{lm[0]}'", flush=True)
        return lm[1], lm[2]
    if tried_network:
        print(f"[GEO] could not geocode '{a}' (retry in 10 min)", flush=True)
    return None, None

# ─────────────────────────────────────────────────────────────────────────────
#  VENUES
# ─────────────────────────────────────────────────────────────────────────────
_CLUB_STOPWORDS = {"club", "the", "of", "nightclub", "gentlemens", "gentlemen", "s", "and", "lv"}

def _name_tokens(s):
    return {t for t in _re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split() if t} - _CLUB_STOPWORDS

def club_for_destination(dest):
    """The Club a ride is going to. Tolerates name variations, e.g. CartVIP's
    'Hustler Las Vegas' → 'Hustler Club Las Vegas'."""
    d = (dest or "").strip()
    if not d:
        return None
    c = Club.query.filter(func.lower(Club.name) == d.lower()).first()
    if c:
        return c
    dt = _name_tokens(d)
    if not dt:
        return None
    best = None
    for cl in Club.query.all():
        ct = _name_tokens(cl.name)
        if not ct:
            continue
        if dt <= ct or ct <= dt:
            score = (len(ct ^ dt), 0 if cl.lat is not None else 1, 0 if cl.active else 1)
            if best is None or score < best[0]:
                best = (score, cl)
    return best[1] if best else None

def venue_points(c, allow_network=False):
    """Every point that counts as 'arrived at the destination' for a ride. For a
    club we use its stored coordinates AND its geocoded address AND any other club
    at the same address — so one bad coordinate can't break drop-off detection."""
    pts = []
    def _add(lat, lng):
        if lat is None or lng is None:
            return
        for p in pts:
            if calcular_distancia(p[0], p[1], lat, lng) < 0.03:
                return
        pts.append((float(lat), float(lng)))
    _add(c.dest_lat, c.dest_lng)
    if c.is_return_ride:
        if not pts:
            _add(*geocode_address(c.destination, allow_network=allow_network))
        return pts
    club = club_for_destination(c.destination)
    if club:
        _add(club.lat, club.lng)
        if club.address:
            _add(*geocode_address(club.address, allow_network=allow_network))
            for other in Club.query.filter(func.lower(Club.address) == club.address.lower()).all():
                _add(other.lat, other.lng)
    return pts

def fix_known_bad_venue_coords():
    """Kings of Hustler shares Hustler Club Las Vegas's address (6007 Dean Martin
    Dr) but was seeded with Luxor's coordinates, ~2 mi away — so arrivals there
    could never be detected. Repair rows that still hold that exact bad value."""
    try:
        bad = Club.query.filter(Club.lat == 36.0955, Club.lng == -115.1761).all()
        for cl in bad:
            if "dean martin" not in (cl.address or "").lower():
                continue
            twin = (Club.query.filter(func.lower(Club.address) == (cl.address or "").lower())
                    .filter(Club.id != cl.id).filter(Club.lat.isnot(None)).first())
            if twin and twin.lat != 36.0955:
                cl.lat, cl.lng = twin.lat, twin.lng
                print(f"[SEED] fixed coordinates of {cl.name} → {cl.lat},{cl.lng}", flush=True)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[SEED] venue coordinate fix skipped: {e}", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
#  GPS FLEET — one OneStepGPS call shared by everyone (cached ~20 s)
# ─────────────────────────────────────────────────────────────────────────────
GPS_CACHE_SEC = _env_float("GPS_CACHE_SEC", 20)
GPS_DEVICE_URL = "https://track.onestepgps.com/v3/api/public/device-info?lat_lng=1"
_gps_cache = {"t": 0.0, "data": None}
_gps_lock = threading.Lock()
GPS_STATS = {"ok": 0, "fail": 0, "last_ok_at": "", "last_error": ""}

def gps_fleet(max_age=None, allow_fetch=True):
    """Every OneStepGPS device (raw dicts), or None if the GPS API is down.
    Cached so the dashboards, the APIs and the tracker don't each hit OneStepGPS."""
    age = GPS_CACHE_SEC if max_age is None else max_age
    cached = _gps_cache["data"]
    if cached is not None and time.time() - _gps_cache["t"] <= age:
        return cached
    if not allow_fetch:
        return cached if (cached is not None and time.time() - _gps_cache["t"] <= 300) else None
    with _gps_lock:
        cached = _gps_cache["data"]
        if cached is not None and time.time() - _gps_cache["t"] <= age:
            return cached
        res = _safe_json_get(GPS_DEVICE_URL,
                             headers={"Authorization": f"Bearer {API_KEY}",
                                      "Content-Type": "application/json"},
                             timeout=10, default=None)
        if res is None or (isinstance(res, dict) and (res.get("error") or res.get("message"))
                           and not res.get("display_name")):
            GPS_STATS["fail"] += 1
            GPS_STATS["last_error"] = str(res)[:200] if res else "no response"
            return None
        lista = res if isinstance(res, list) else [res]
        _gps_cache["t"] = time.time()
        _gps_cache["data"] = lista
        GPS_STATS["ok"] += 1
        GPS_STATS["last_ok_at"] = vegas_datetime(datetime.utcnow())
        return lista

def _device_latlng(v):
    for src in (v, v.get("last_tap") or {}, v.get("latest_device_point") or {}):
        lat, lng = src.get("lat"), src.get("lng")
        if lat and lng:
            try:
                return float(lat), float(lng)
            except (TypeError, ValueError):
                continue
    return None, None

def gps_positions(max_age=None, allow_fetch=True):
    """{normalized device name: {name, lat, lng, age_min, fresh}} or None if GPS is down."""
    fleet = gps_fleet(max_age=max_age, allow_fetch=allow_fetch)
    if fleet is None:
        return None
    now = datetime.utcnow()
    seen = _gps_cache.setdefault("seen", {})     # name -> (lat, lng, last time the position changed)
    out = {}
    for v in fleet:
        if not isinstance(v, dict):
            continue
        name = v.get("display_name") or ""
        lat, lng = _device_latlng(v)
        if not name or lat is None:
            continue
        ts = _parse_gps_timestamp(v)
        age = (now - ts).total_seconds() / 60.0 if ts else None
        prev = seen.get(name)
        if prev is None or abs(prev[0] - lat) > 1e-5 or abs(prev[1] - lng) > 1e-5:
            seen[name] = (lat, lng, now)
        moved_min = (now - seen[name][2]).total_seconds() / 60.0
        # Fresh if the device clock says so, OR we watched the position change
        # recently (a device whose timestamp we can't read right is still alive).
        fresh = age is None or age <= GPS_FRESH_MIN or moved_min <= GPS_FRESH_MIN or age < -5
        out[_norm_name(name)] = {"name": name, "lat": lat, "lng": lng,
                                 "age_min": round(age, 1) if age is not None else None,
                                 "moved_min": round(moved_min, 1), "fresh": fresh}
    return out

def car_position(car_name, positions=None, car=None):
    """Live position for a car: by its OneStepGPS display name (exact, then loose),
    then by plate — so a renamed GPS device still matches its Car record."""
    if positions is None:
        positions = gps_positions()
    if not positions:
        return None
    names = [car_name]
    if car is not None:
        names += [car.name, car.model]
    for n in names:
        key = _norm_name(n or "")
        if not key:
            continue
        if key in positions:
            return positions[key]
        for k, p in positions.items():
            if k and (key in k or k in key):
                return p
    plate = _norm_name((car.plate if car else "") or "")
    if plate and len(plate) >= 4:
        for k, p in positions.items():
            if plate in k:
                return p
    return None

def car_recently_moved(gps_name):
    """True if the car moved within CAR_MOVED_MIN, False if parked longer, None if unknown."""
    anchor = _stop_anchors.get(gps_name) if gps_name else None
    if not anchor or anchor.get("first_seen"):
        return None   # we haven't watched it move since the server started
    return (datetime.utcnow() - anchor["since"]).total_seconds() <= CAR_MOVED_MIN * 60

# ─────────────────────────────────────────────────────────────────────────────
#  ROSTER — who is driving which car at a given time
# ─────────────────────────────────────────────────────────────────────────────
class RosterEntry:
    __slots__ = ("driver", "car", "source")
    def __init__(self, driver, car, source):
        self.driver, self.car, self.source = driver, car, source

def _hm_to_min(s):
    try:
        h, m = (s or "").strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None

def _parse_shift_date(s):
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def _shift_service_date(sh, dt):
    """The day a shift STARTED on, if it covers `dt`. Overnight shifts belong to
    the evening they start: a Fri 18:00→05:30 shift covers Sat 1:00 AM."""
    s, e = _hm_to_min(sh.start_time), _hm_to_min(sh.end_time)
    t = dt.hour * 60 + dt.minute
    d = dt.date()
    if s is None or e is None:
        return d
    if s <= e:
        return d if s <= t <= e else None
    if t >= s:
        return d
    if t <= e:
        return d - timedelta(days=1)
    return None

def _shift_on_date(sh, d):
    if (sh.specific_date or "").strip():
        return _parse_shift_date(sh.specific_date) == d
    return sh.day_of_week is not None and sh.day_of_week == d.weekday()

def roster_at(dt=None, include_unavailable=False):
    """Drivers (with their car) who can drive at `dt` (Las Vegas local time).
    Per driver: a date-specific shift (e.g. from Switch Car) beats their
    Driver-Management car, which beats a weekly shift. A car belongs to one driver
    at a time — a date-specific shift takes it away from whoever else had it."""
    if dt is None:
        dt = vegas_naive_now()
    _mk = (dt.replace(second=0, microsecond=0), bool(include_unavailable))
    _rm = _memo().setdefault("roster", {})
    if _mk in _rm:
        return list(_rm[_mk])
    specific, weekly = [], []
    for sh in Shift.query.filter_by(active=True).all():
        sd = _shift_service_date(sh, dt)
        if sd is None or not _shift_on_date(sh, sd):
            continue
        (specific if (sh.specific_date or "").strip() else weekly).append(sh)
    specific.sort(key=lambda s: s.id, reverse=True)    # newest switch wins
    entries, used_cars = {}, set()
    specific = [s for s in specific if not _is_demo_driver(s.driver)]
    weekly = [s for s in weekly if not _is_demo_driver(s.driver)]
    for sh in specific:
        if sh.driver and sh.car and sh.driver_id not in entries and sh.car_id not in used_cars:
            entries[sh.driver_id] = RosterEntry(sh.driver, sh.car, "shift-date")
            used_cars.add(sh.car_id)
    for d in Driver.query.filter(Driver.assigned_car_id.isnot(None)).order_by(Driver.id).all():
        if d.id in entries or d.assigned_car_id in used_cars or _is_demo_driver(d):
            continue
        car = Car.query.get(d.assigned_car_id)
        if car:
            entries[d.id] = RosterEntry(d, car, "assigned")
            used_cars.add(car.id)
    for sh in weekly:
        if sh.driver and sh.car and sh.driver_id not in entries and sh.car_id not in used_cars:
            entries[sh.driver_id] = RosterEntry(sh.driver, sh.car, "shift-weekly")
            used_cars.add(sh.car_id)
    out = []
    for e in entries.values():
        if e.car.active is False:
            continue
        if not include_unavailable and not e.driver.available:
            continue
        out.append(e)
    _rm[_mk] = list(out)
    return out

def driver_car_at(drv, dt=None):
    """The car a driver is in at `dt` (falls back to their car for the day)."""
    if not drv:
        return None
    for e in roster_at(dt, include_unavailable=True):
        if e.driver.id == drv.id:
            return e.car
    if drv.assigned_car_id:
        car = Car.query.get(drv.assigned_car_id)
        if car:
            return car
    d = (dt or vegas_naive_now()).date()
    for sh in Shift.query.filter_by(driver_id=drv.id, active=True).order_by(Shift.id.desc()).all():
        if _shift_on_date(sh, d) or _shift_on_date(sh, d - timedelta(days=1)):
            car = Car.query.get(sh.car_id)
            if car:
                return car
    return None

def car_driver_at(car, dt=None):
    """The driver in a given car at `dt`."""
    if not car:
        return None
    for e in roster_at(dt, include_unavailable=True):
        if e.car.id == car.id:
            return e.driver
    for direct in Driver.query.filter_by(assigned_car_id=car.id).all():
        if not _is_demo_driver(direct):
            return direct
    d = (dt or vegas_naive_now()).date()
    for sh in Shift.query.filter_by(car_id=car.id, active=True).order_by(Shift.id.desc()).all():
        if _shift_on_date(sh, d) or _shift_on_date(sh, d - timedelta(days=1)):
            _d = Driver.query.get(sh.driver_id)
            if _d and not _is_demo_driver(_d):
                return _d
    return None

def resolve_ride_driver(c, at=None):
    """The Driver (person) responsible for a ride, or None. Handles old rows where
    the driver field holds a car's GPS name instead of a person."""
    name = (c.motorista or "").strip()
    if name and name not in NO_DRIVER_VALUES:
        drv = _driver_by_name(name)
        if drv:
            # A real ride that landed on the review account has nobody driving it.
            if _is_demo_driver(drv) and not _is_demo_ride(c):
                return None
            return drv
    car = _car_by_name(c.car_name) if c.car_name else None
    if car is None and name and name not in NO_DRIVER_VALUES:
        car = _car_by_name(name)
    if car:
        at = at or parse_pickup_datetime(c.pickup_datetime) or vegas_naive_now()
        return car_driver_at(car, at)
    return None

def ride_has_driver(c):
    if (c.completed_driver or "").strip():
        return True
    return resolve_ride_driver(c) is not None

def ride_driver_display(c):
    """(name, phone) to show partners/guests — always a person, never a car."""
    drv = resolve_ride_driver(c)
    name = (c.completed_driver or "").strip() or (drv.name if drv else "")
    if not name:
        return "", ""
    phone = (drv.phone if drv and drv.phone else "") or (c.motorista_phone or "")
    return name, phone

# ─────────────────────────────────────────────────────────────────────────────
#  ON DUTY — is this driver actually working right now?
# ─────────────────────────────────────────────────────────────────────────────
_seen_throttle = {}

def mark_driver_seen(username):
    """Called on every driver-portal request (throttled to one DB write a minute)."""
    if not username:
        return
    now = time.time()
    if now - _seen_throttle.get(username, 0) < 60:
        return
    _seen_throttle[username] = now
    try:
        drv = get_driver_record(username)
        if drv:
            drv.last_seen_at = datetime.utcnow()
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[DUTY] could not record driver activity: {e}", flush=True)

def driver_on_duty(drv, car=None, positions=None):
    """On duty = their dashboard was open in the last DRIVER_ACTIVE_MIN minutes, OR
    their car is reporting GPS and has moved in the last CAR_MOVED_MIN minutes."""
    if not drv:
        return False
    now = datetime.utcnow()
    if drv.last_seen_at and (now - drv.last_seen_at).total_seconds() <= DRIVER_ACTIVE_MIN * 60:
        return True
    car = car or driver_car_at(drv)
    if car:
        pos = car_position(car.name, positions)
        if pos and pos["fresh"] and car_recently_moved(pos["name"]):
            return True
    return False

def _driver_load(name, around_dt):
    """How many open rides this driver has within ±6 h (tie-breaker: spread the work)."""
    if not around_dt:
        return 0
    n = 0
    for c in Customer.query.filter(Customer.motorista == name,
                                   Customer.status.in_(("scheduled", "picked_up"))).all():
        pdt = parse_pickup_datetime(c.pickup_datetime)
        if pdt and abs((pdt - around_dt).total_seconds()) <= 6 * 3600:
            n += 1
    return n

# ─────────────────────────────────────────────────────────────────────────────
#  AUTO-ASSIGNMENT
# ─────────────────────────────────────────────────────────────────────────────
class DriverPick:
    __slots__ = ("driver", "car", "dist_mi", "overbooked", "note")
    def __init__(self, driver, car, dist_mi, overbooked, note):
        self.driver, self.car, self.dist_mi = driver, car, dist_mi
        self.overbooked, self.note = overbooked, note

def pick_driver(lat, lng, pickup_dt, exclude_ids=(), require_on_duty=False):
    """Choose a real person for a pickup. Returns (DriverPick, None) or (None, reason).
      1. Drivers set up for that time (Driver Management car / shifts), Available.
      2. Pickups within 3 h: prefer drivers who are on duty right now.
      3. Prefer drivers without another pickup within DRIVER_BUSY_WINDOW_MIN; if
         everyone is busy, double-book the best one (flagged) — never a car name.
      4. Nearest car (live GPS) first; then whoever has fewer rides tonight."""
    now_v = vegas_naive_now()
    at = pickup_dt or now_v
    entries = [e for e in roster_at(at) if e.driver.id not in set(exclude_ids or ())]
    if not entries:
        return None, "no available driver with a car for that time (Driver Management)"
    positions = gps_positions(allow_fetch=True) or {}
    note = ""
    pool = entries
    near_term = (at - now_v) <= timedelta(hours=3)
    if near_term or require_on_duty:
        duty = [e for e in entries if driver_on_duty(e.driver, car=e.car, positions=positions)]
        if duty:
            pool = duty
        elif require_on_duty:
            return None, "no driver is online right now"
        else:
            note = "no driver online when assigned"
    free = [e for e in pool if not driver_is_busy(e.driver.name, pickup_dt)]
    overbooked = not free
    tier = free or pool

    def _dist(e):
        if lat is None or lng is None:
            return float("inf")
        p = car_position(e.car.name, positions)
        return calcular_distancia(lat, lng, p["lat"], p["lng"]) if p else float("inf")

    ranked = sorted(tier, key=lambda e: (_dist(e), _driver_load(e.driver.name, pickup_dt), e.driver.name))
    best = ranked[0]
    if overbooked:
        note = (f"double-booked: every driver already has a pickup within "
                f"{DRIVER_BUSY_WINDOW_MIN} min" + (f"; {note}" if note else ""))
    return DriverPick(best.driver, best.car, _dist(best), overbooked, note), None

def notify_driver_of_ride(c, drv, kind="assigned"):
    """SMS + dashboard pop-up so the driver can't miss a new pickup."""
    try:
        t = _time_part(c.pickup_datetime)
        g = c.guests or 0
        title = "New ClubLifter pickup" if kind == "assigned" else "ClubLifter: pickup moved to you"
        sv = streetview_for(c.endereco)
        body = (f"{title}{' at ' + t if t else ''}\n"
                f"Guest: {c.nome} ({g} guest{'s' if g != 1 else ''})\n"
                f"Pickup: {c.endereco}\nDrop-off: {c.destination or 'N/A'}"
                + (f"\nNotes: {c.details}" if c.details else "")
                + (f"\nPickup spot: {sv}" if sv else ""))
        phone = clean_phone(drv.phone or "")
        if phone:
            send_sms_bg(phone, body)
        log_event(c, "driver-notified", f"{drv.name} ({phone or 'no phone'}): SMS + dashboard pop-up", actor="auto")
        db.session.add(DriverNotice(driver_name=drv.name, kind=kind, guest_name=c.nome or "",
                                    guest_time=c.pickup_datetime or "",
                                    detail=f"{c.endereco or ''} → {c.destination or ''}"[:255]))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[ASSIGN] driver notice failed: {e}", flush=True)

def _sms_guest_driver_details(c, updated=False):
    name, _ = ride_driver_display(c)
    if not name:
        return
    t = _time_part(c.pickup_datetime)
    car = c.car_string_val or ""
    lead = "Update: your ClubLifter driver is now" if updated else "Your ClubLifter driver is"
    msg = (f"Hi {c.nome}! {lead} {name}"
           + (f" in a {car}" if car else "")
           + (f", picking you up at {t}" if t else "") + ".\n"
           "Heads up: your driver may call from this number — please answer so they can find you.")
    photo = f"{PUBLIC_BASE_URL}/uploads/{c.car_photo}" if c.car_photo else ""
    send_sms_many(c.get_phones() or [c.phone], msg, photo)

def apply_assignment(c, pick, source="auto", notify_driver=True, notify_guest=False, note=None):
    """Put a picked driver on a ride, re-arm GPS tracking, tell everyone."""
    before = _ride_status_info(c)[0]
    prev = c.motorista or ""
    drv, car = pick.driver, pick.car
    c.motorista = drv.name
    c.motorista_phone = drv.phone or ""
    if car:
        c.car_name = car.name
        c.car_string_val = car.car_string()
        c.car_photo = car.photo or ""
    if pick.dist_mi is not None and pick.dist_mi != float("inf"):
        c.distancia = round(pick.dist_mi, 2)
    c.assign_note = ((note if note is not None else pick.note) or "")[:200]
    if c.status == "waitlist":
        c.status = "scheduled"
    c.notified_15km = c.notified_10km = c.notified_5km = False
    c.near_pickup_since = None
    c.gps_last_pick_mi = None
    c.unassigned_alert_level = 0
    db.session.commit()
    log_event(c, "assigned", f"{prev or '(none)'} → {drv.name}"
              + (f" / {car.name}" if car else "") + (f" — {c.assign_note}" if c.assign_note else ""),
              actor=source)
    if notify_driver:
        notify_driver_of_ride(c, drv, "assigned")
    if notify_guest:
        _sms_guest_driver_details(c, updated=bool(prev and prev not in NO_DRIVER_VALUES))
    fire_webhook({
        "type": "driver_assigned", "source": source, "customer_id": c.id,
        "customer_name": c.nome, "customer_phone": c.phone,
        "driver_name": drv.name, "driver_phone": drv.phone or "",
        "driver_car": c.car_string_val or "", "pickup_address": c.endereco,
        "pickup_datetime": c.pickup_datetime, "destination": c.destination,
        "previous_driver": prev, "note": c.assign_note,
    })
    ride_status_changed(c, before, source)

def _ops_numbers():
    return OPS_ALERT_NUMBERS or list(GPS_DISCONNECT_ALERT_NUMBERS)

def _send_ops_alerts(alerts):
    if not alerts or not UNASSIGNED_ALERTS:
        return
    label = {"new": "NO DRIVER", "urgent": "URGENT – NO DRIVER", "offline": "DRIVER NOT ONLINE"}
    lines = ["ClubLifter dispatch ⚠"]
    for c, kind in alerts[:12]:
        t = _time_part(c.pickup_datetime) or c.pickup_datetime or "?"
        extra = f" ({c.motorista})" if kind == "offline" else ""
        lines.append(f"{label.get(kind, kind)}: {c.nome} {t}{extra} · {c.endereco or '?'} → {c.destination or '?'}")
    if len(alerts) > 12:
        lines.append(f"…and {len(alerts) - 12} more")
    lines.append(f"{PUBLIC_BASE_URL}/admin/guestlist")
    body = "\n".join(lines)[:1500]
    for num in _ops_numbers():
        send_sms_bg(num, body)
    for c, kind in alerts:
        log_event(c, "ops-alert", f"{label.get(kind, kind)} — texted {', '.join(_ops_numbers())}", actor="auto")
    print(f"[ASSIGN] ops alerted about {len(alerts)} ride(s)", flush=True)

def auto_assign_pending():
    """Every tick: give a driver to rides that have none, fix old car-name rows, and
    move rides off drivers who aren't online when the pickup is close."""
    if not AUTO_ASSIGN_ENABLED:
        return
    now_v = vegas_naive_now()
    alerts = []
    rows = Customer.query.filter(Customer.needs_transport == True,
                                 Customer.status.in_(("scheduled", "waitlist"))).all()
    for c in rows:
        if _is_demo_ride(c):
            continue
        pdt = parse_pickup_datetime(c.pickup_datetime)
        if pdt is None:
            continue
        mins = (pdt - now_v).total_seconds() / 60.0
        if mins < -120 or mins > 18 * 60:
            continue
        drv = resolve_ride_driver(c, pdt)
        if drv is None:
            pick, why = pick_driver(c.pickup_lat, c.pickup_lng, pdt)
            if pick:
                apply_assignment(c, pick, source="auto-retry", notify_driver=True, notify_guest=mins > -15)
                print(f"[ASSIGN] ride {c.id} ({c.nome}) → {pick.driver.name}", flush=True)
                continue
            if (c.assign_note or "") != why:
                c.assign_note = why[:200]
            lvl = c.unassigned_alert_level or 0
            if mins <= UNASSIGNED_URGENT_MIN and lvl < 2:
                alerts.append((c, "urgent")); c.unassigned_alert_level = 2
            elif mins <= 12 * 60 and lvl < 1:
                alerts.append((c, "new")); c.unassigned_alert_level = 1
            db.session.commit()
            continue

        # Old rows whose driver field holds a car's GPS name → the real person.
        if c.motorista != drv.name and mins > -60:
            prev = c.motorista
            c.motorista = drv.name
            c.motorista_phone = drv.phone or c.motorista_phone or ""
            c.assign_note = f"driver resolved from car '{prev}'"[:200]
            db.session.commit()
            notify_driver_of_ride(c, drv, "assigned")

        # Assigned driver not working? Hand the ride to someone who is.
        if (AUTO_REASSIGN_OFFLINE and time.time() - _ENGINE_BOOT > BOOT_GRACE_SEC
                and -30 <= mins <= REASSIGN_OFFLINE_LEAD_MIN and c.status == "scheduled"
                and c.dispatch_status not in ("enroute", "arrived") and not c.driver_arrived_at
                and not driver_on_duty(drv)):
            manual = (c.assign_note or "").startswith(("manual", "auto-reassigned"))
            if not manual:
                pick, _why = pick_driver(c.pickup_lat, c.pickup_lng, pdt,
                                         exclude_ids={drv.id}, require_on_duty=True)
                if pick:
                    reassign_ride(c, pick, prev_driver=drv,
                                  note=f"auto-reassigned from {drv.name} (not online)")
                    continue
            if (c.unassigned_alert_level or 0) < 3:
                alerts.append((c, "offline")); c.unassigned_alert_level = 3
                db.session.commit()
    _send_ops_alerts(alerts)

def reassign_ride(c, pick, prev_driver=None, note="", source="auto"):
    """Move a ride to another driver and tell the old one it's gone."""
    prev_name = prev_driver.name if prev_driver else (c.motorista or "")
    apply_assignment(c, pick, source=source, notify_driver=True, notify_guest=True, note=note)
    if prev_driver and prev_driver.id != pick.driver.id:
        t = _time_part(c.pickup_datetime)
        if prev_driver.phone:
            send_sms_bg(clean_phone(prev_driver.phone),
                        f"ClubLifter: {c.nome}{(' (' + t + ')') if t else ''} was moved to "
                        f"another driver and is no longer on your list.")
        try:
            db.session.add(DriverNotice(driver_name=prev_driver.name, kind="reassigned",
                                        guest_name=c.nome, guest_time=c.pickup_datetime,
                                        detail=f"moved to {pick.driver.name}"))
            db.session.commit()
        except Exception:
            db.session.rollback()
    print(f"[ASSIGN] ride {c.id} moved {prev_name} → {pick.driver.name} ({note})", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
#  RIDE STATUS (shared by the API, the app, the guest list)
# ─────────────────────────────────────────────────────────────────────────────
def _ride_status_full(c):
    """(code, text, estimated). Real events (driver taps, GPS, dispatcher, partner)
    always win; time-based guesses are only used when the ride's car has no GPS,
    and are flagged as estimated. A ride with no driver says so — it is never
    reported as 'at the venue'."""
    ret = bool(getattr(c, "is_return_ride", False))
    if c.club_status == "left":
        return "left", "Guest has left the venue", False
    if c.club_status == "arrived":
        return "at_venue", "Guest has arrived at the venue", False
    if c.status == "dropped_off" or c.dropped_off_at:
        return "dropped_off", ("Guest was dropped off at their destination" if ret
                               else "Guest was dropped off at the venue"), False
    if c.status == "picked_up" or c.picked_up_at:
        return "picked_up", ("Guest picked up, on the way back" if ret
                             else "Guest picked up, en route to the venue"), False
    if c.status == "waitlist":
        return "waitlist", "Ride is waitlisted — no driver yet", False
    if c.needs_transport and not ride_has_driver(c):
        return "unassigned", "Ride is booked — no driver assigned yet", False
    if c.dispatch_status == "arrived" or c.driver_arrived_at:
        return "arrived", "Driver has arrived at the pickup location", False
    if c.dispatch_status == "enroute":
        return "enroute", "Driver is on the way to pick up the guest", False
    try:
        pdt = parse_pickup_datetime(c.pickup_datetime) if c.pickup_datetime else None
        if pdt is not None:
            mins_past = (vegas_naive_now() - pdt).total_seconds() / 60.0
            # Nothing recorded yet — estimate from the clock, clearly labeled. (The
            # background failsafe writes the real completion at SCHEDULED_TIMEOUT_MIN.)
            if mins_past >= 180:
                return "at_venue", "Guest has arrived at the venue (estimated)", True
            if mins_past >= 30:
                return "picked_up", "Guest picked up, en route to the venue (estimated)", True
    except Exception:
        pass
    return "scheduled", "Ride is scheduled, awaiting pickup", False

def ride_status_extras(c, with_eta=True):
    """Extra, partner-facing fields: who/what set the status, the real driver, ETA."""
    code, _text, est = _ride_status_full(c)
    if (c.status_source or "") == "auto-timeout":
        est = True
    name, phone = ride_driver_display(c)
    out = {
        "status_source": "estimated" if est and not c.status_source else (c.status_source or ""),
        "status_estimated": est,
        "status_updated_at": vegas_datetime(c.status_updated_at) if c.status_updated_at else "",
        "driver_assigned": bool(name),
        "driver_name": name,
        "driver_phone": phone,
        "assign_note": c.assign_note or "",
    }
    if with_eta and code in ("scheduled", "enroute") and name and c.pickup_lat is not None:
        try:
            pos = car_position(c.car_name, gps_positions(max_age=180, allow_fetch=False))
            if pos and pos["fresh"]:
                d = calcular_distancia(pos["lat"], pos["lng"], c.pickup_lat, c.pickup_lng)
                out["driver_distance_mi"] = round(d, 2)
                out["driver_eta_min"] = max(1, int(round(d / 25.0 * 60)))
        except Exception:
            pass
    return out

def mark_status(c, source):
    c.status_source = source
    c.status_updated_at = datetime.utcnow()

def _post_partner(payload):
    try:
        headers = {"Content-Type": "application/json"}
        if PARTNER_STATUS_WEBHOOK_KEY:
            headers["X-API-Key"] = PARTNER_STATUS_WEBHOOK_KEY
        r = requests.post(PARTNER_STATUS_WEBHOOK_URL, json=payload, headers=headers, timeout=10)
        print(f"[PARTNER] status push {payload.get('customer_id')} → {payload.get('status')} "
              f"HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"[PARTNER] status push failed: {e}", flush=True)

def ride_status_changed(c, before_code, source):
    """Call after any status change. Pushes to the partner (CartVIP) when
    PARTNER_STATUS_WEBHOOK_URL is set, so they don't have to wait for their poll."""
    try:
        code, text, est = _ride_status_full(c)
        if code == before_code:
            return
        log_event(c, "status", f"{before_code} → {code} ({c.status}/{c.dispatch_status}/{c.club_status})",
                  actor=source)
        if not PARTNER_STATUS_WEBHOOK_URL:
            return
        if (c.promoter or "").lower() not in PARTNER_STATUS_PROMOTERS:
            return
        name, phone = ride_driver_display(c)
        payload = {"customer_id": c.id, "status": code, "ride_status": code,
                   "ride_status_text": text, "previous_status": before_code,
                   "status_source": "estimated" if est else source,
                   "driver_name": name, "driver_phone": phone,
                   "car": c.completed_car or c.car_string_val or "",
                   "club_status": c.club_status, "updated_at": vegas_datetime(datetime.utcnow())}
        threading.Thread(target=_post_partner, args=(payload,), daemon=True).start()
    except Exception as e:
        print(f"[PARTNER] status push skipped: {e}", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
#  GPS-DRIVEN RIDE PROGRESS
#  scheduled → enroute → arrived (at pickup) → picked_up → dropped_off/at venue
# ─────────────────────────────────────────────────────────────────────────────
def _drop_token(cid):
    import hmac, hashlib
    return hmac.new(app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key,
                    f"drop:{cid}".encode(), hashlib.sha256).hexdigest()[:20]

@app.route('/d/<int:cid>/<token>', methods=['GET', 'POST'])
def driver_drop_link(cid, token):
    """One-tap 'guest dropped off' confirmation from the reminder SMS."""
    c = Customer.query.get_or_404(cid)
    if not hmac_compare(token, _drop_token(cid)):
        return "Invalid link", 403
    if c.status != "dropped_off":
        before = _ride_status_info(c)[0]
        _complete_dropoff(c, datetime.utcnow(), None, "driver")
        c.dropoff_verified = False
        db.session.commit()
        fire_webhook({"type": "dropped_off", "source": "driver-link", "customer_id": c.id,
                      "customer_name": c.nome, "driver_name": c.completed_driver,
                      "destination": c.destination})
        ride_status_changed(c, before, "driver")
    return (f"<html><body style='font-family:sans-serif;background:#08060f;color:#e8eaf0;"
            f"padding:40px;text-align:center'><h2>✅ Marked dropped off</h2>"
            f"<p>{c.nome} → {c.destination or ''}</p>"
            f"<p><a style='color:#a78bfa' href='/driver'>Open my board</a></p></body></html>")

def hmac_compare(a, b):
    import hmac
    return hmac.compare_digest(str(a or ""), str(b or ""))

def _remind_driver_dropoff(c, drv):
    t = _time_part(c.pickup_datetime)
    link = f"{PUBLIC_BASE_URL}/d/{c.id}/{_drop_token(c.id)}"
    body = (f"ClubLifter: did you drop off {c.nome}{(' (' + t + ')') if t else ''} at "
            f"{c.destination or 'the venue'}? Tap to confirm: {link}\n"
            f"(It will auto-complete in {int(PICKED_UP_TIMEOUT_MIN)} min if you don't.)")
    if drv and drv.phone:
        send_sms_bg(clean_phone(drv.phone), body)
    try:
        db.session.add(DriverNotice(driver_name=drv.name if drv else (c.motorista or ""), kind="dropoff_reminder",
                                    guest_name=c.nome, guest_time=c.pickup_datetime,
                                    detail=f"Still marked picked up — tap Dropped off ({c.destination or ''})"[:255]))
        db.session.commit()
    except Exception:
        db.session.rollback()

def auto_timeouts():
    """Failsafe that needs no GPS at all. Rides never stay 'picked up' or
    'en route' forever again:
      • picked up for DROP_REMINDER_MIN  → remind the driver (SMS with a one-tap link)
      • picked up for PICKED_UP_TIMEOUT_MIN → auto-complete (at venue, flagged auto)
      • assigned, pickup time + SCHEDULED_TIMEOUT_MIN, nothing recorded → auto-complete"""
    now_u = datetime.utcnow()
    now_v = vegas_naive_now()
    rows = Customer.query.filter(Customer.needs_transport == True,
                                 Customer.status.in_(("scheduled", "picked_up"))).all()
    for c in rows:
        if _is_demo_ride(c):
            continue
        pdt = parse_pickup_datetime(c.pickup_datetime)
        if pdt is None:
            continue
        mins_past = (now_v - pdt).total_seconds() / 60.0
        if mins_past < 0 or mins_past > 36 * 60:
            continue
        before = _ride_status_info(c)[0]
        if c.status == "picked_up":
            ref = c.picked_up_at or (pdt + timedelta(hours=7))   # pdt is Vegas; picked_up_at is UTC
            up_min = (now_u - ref).total_seconds() / 60.0 if c.picked_up_at else mins_past
            if up_min >= PICKED_UP_TIMEOUT_MIN:
                _complete_dropoff(c, now_u, None, "auto-timeout")
                c.dropoff_verified = False
                db.session.commit()
                print(f"[FAILSAFE] ride {c.id} ({c.nome}) picked up {int(up_min)} min ago → auto-completed", flush=True)
                fire_webhook({"type": "dropped_off", "source": "auto-timeout", "customer_id": c.id,
                              "customer_name": c.nome, "driver_name": c.completed_driver,
                              "destination": c.destination, "verified_at_venue": False})
                ride_status_changed(c, before, "auto-timeout")
            elif up_min >= DROP_REMINDER_MIN and (c.unassigned_alert_level or 0) < 10:
                c.unassigned_alert_level = 10          # reminder sent (one per ride)
                db.session.commit()
                _remind_driver_dropoff(c, resolve_ride_driver(c))
                log_event(c, "reminder", f"still picked up after {int(up_min)} min — driver texted a one-tap drop-off link", actor="auto")
                print(f"[FAILSAFE] ride {c.id} ({c.nome}) still picked up after {int(up_min)} min → driver reminded", flush=True)
        elif c.status == "scheduled" and mins_past >= SCHEDULED_TIMEOUT_MIN:
            drv = resolve_ride_driver(c, pdt)
            if drv is None:
                continue                                 # stays 'unassigned' — never invent a trip
            if not c.picked_up_at:
                c.picked_up_at = now_u
            _complete_dropoff(c, now_u, None, "auto-timeout")
            c.dropoff_verified = False
            db.session.commit()
            print(f"[FAILSAFE] ride {c.id} ({c.nome}) {int(mins_past)} min past pickup with no updates → auto-completed", flush=True)
            fire_webhook({"type": "dropped_off", "source": "auto-timeout", "customer_id": c.id,
                          "customer_name": c.nome, "driver_name": c.completed_driver,
                          "destination": c.destination, "verified_at_venue": False})
            ride_status_changed(c, before, "auto-timeout")

def _complete_dropoff(c, now_u, dist_mi, source):
    c.status = "dropped_off"
    c.dropped_off_at = now_u
    c.dropoff_verified = True
    c.dropoff_distance_mi = round(dist_mi, 2) if dist_mi is not None else 0.0
    if not c.picked_up_at:
        c.picked_up_at = c.driver_arrived_at or now_u
    if not c.completed_driver:
        c.completed_driver = c.motorista or ""
    if not c.completed_car:
        c.completed_car = c.car_string_val or c.car_name or ""
    if not c.is_return_ride:
        c.club_status = "arrived"
        if not c.arrived_at:
            c.arrived_at = now_u
    mark_status(c, source)

def _mark_original_left(ret_ride, source):
    """Guest got picked up for their ride back → they've left the venue."""
    if not ret_ride.is_return_ride or not ret_ride.return_of_id:
        return
    orig = Customer.query.get(ret_ride.return_of_id)
    if orig and orig.club_status != "left":
        before = _ride_status_info(orig)[0]
        orig.club_status = "left"
        mark_status(orig, source)
        db.session.commit()
        ride_status_changed(orig, before, source)

def _proximity_sms(c, label, body):
    car_photo_url = f"{PUBLIC_BASE_URL}/uploads/{c.car_photo}" if c.car_photo else ""
    fire_webhook({"type": "distance", "threshold": label, "customer_id": c.id,
                  "customer_name": c.nome, "customer_phone": c.phone,
                  "customer_phones": c.get_phones(), "driver_name": c.motorista,
                  "driver_car": c.car_string_val or "N/A",
                  "driver_car_photo_url": car_photo_url, "destination": c.destination,
                  "pickup_datetime": c.pickup_datetime, "distance_unit": "mi"})
    send_sms_many(c.get_phones() or [c.phone], body, car_photo_url)

def _note_gps_problem(c, why):
    """Log (once per ride) why GPS can't follow it — the answer to 'why didn't it close?'."""
    try:
        if RideEvent.query.filter_by(customer_id=c.id, event="gps-problem").first():
            return
    except Exception:
        return
    log_event(c, "gps-problem", why, actor="gps")

def auto_progress_rides():
    """Move each live ride forward using its car's GPS. Never moves a ride
    backwards and never overrides what a driver/dispatcher already set."""
    if not AUTO_STATUS_ENABLED:
        return
    positions = gps_positions()
    if positions is None:
        return
    now_v = vegas_naive_now()
    now_u = datetime.utcnow()
    rows = Customer.query.filter(Customer.needs_transport == True,
                                 Customer.status.in_(("scheduled", "picked_up"))).all()
    for c in rows:
        if _is_demo_ride(c):
            continue
        pdt = parse_pickup_datetime(c.pickup_datetime)
        if pdt is None:
            continue
        mins_to = (pdt - now_v).total_seconds() / 60.0
        if mins_to > 90 or mins_to < (-600 if c.status == "picked_up" else -240):
            continue
        drv = resolve_ride_driver(c, pdt)
        if drv is None:
            continue
        car = _car_by_name(c.car_name) if c.car_name else None
        if car is None:
            car = driver_car_at(drv, pdt)
        pos = car_position(c.car_name or (car.name if car else ""), positions, car=car)
        if not pos or not pos["fresh"]:
            if -30 <= mins_to <= 30:
                _note_gps_problem(c, ("no GPS device matches car '%s'" % (c.car_name or (car.name if car else "?")))
                                  if not pos else f"GPS fix for {pos['name']} is {pos['age_min']} min old")
            continue

        before = _ride_status_info(c)[0]
        if c.pickup_lat is not None:
            c.gps_seen_at = now_u     # we could have seen the pickup happen
        clat, clng = pos["lat"], pos["lng"]
        d_pick = (calcular_distancia(clat, clng, c.pickup_lat, c.pickup_lng)
                  if c.pickup_lat is not None and c.pickup_lng is not None else None)
        pts = venue_points(c)
        d_dest = min((calcular_distancia(clat, clng, p[0], p[1]) for p in pts), default=None)
        event = None
        sms = None

        if c.status == "scheduled":
            arrived = bool(c.driver_arrived_at) or c.dispatch_status == "arrived"
            # "At pickup" is only inferred from ARRIVAL_WINDOW_MIN before to 90 min after
            # the pickup time — a car parked near the address hours later isn't a pickup.
            if (d_pick is not None and d_pick <= PICKUP_RADIUS_MI
                    and -90 <= mins_to <= ARRIVAL_WINDOW_MIN):
                if c.near_pickup_since is None:
                    c.near_pickup_since = now_u
                elif (not arrived and
                      (now_u - c.near_pickup_since).total_seconds() >= PICKUP_DWELL_SEC):
                    c.dispatch_status = "arrived"
                    c.driver_arrived_at = c.near_pickup_since
                    mark_status(c, "gps")
                    arrived = True
                    event = "arrived"
                if arrived and not c.notified_5km and mins_to <= 15:
                    c.notified_5km = c.notified_10km = c.notified_15km = True
                    sms = ("arrived",
                           f"Hi {c.nome}! Your ClubLifter driver {c.motorista} has arrived"
                           + (f" in a {c.car_string_val}" if c.car_string_val else "")
                           + ". Please head out to meet your driver now!")
            elif arrived and c.near_pickup_since is not None and d_pick is not None \
                    and d_pick >= PICKUP_LEAVE_MI:
                # The car was seen at the pickup and has now left → guest on board.
                c.status = "picked_up"
                c.picked_up_at = now_u
                if not c.completed_driver:
                    c.completed_driver = c.motorista or ""
                if not c.completed_car:
                    c.completed_car = c.car_string_val or c.car_name or ""
                mark_status(c, "gps")
                event = "picked_up"
            elif ((arrived or c.dispatch_status == "enroute") and c.near_pickup_since is None
                  and d_dest is not None and d_dest <= VENUE_RADIUS_MI and mins_to <= 0):
                # Driver was engaged (en route / at pickup — but the pickup pin is off),
                # then the car reached the venue → the ride happened.
                _complete_dropoff(c, now_u, d_dest, "gps")
                event = "dropped_off"
            elif not arrived and d_pick is not None:
                if d_pick > PICKUP_RADIUS_MI:
                    c.near_pickup_since = None
                if (d_pick <= ENROUTE_RADIUS_MI and mins_to <= 45
                        and c.dispatch_status != "enroute"
                        and c.gps_last_pick_mi is not None and d_pick < c.gps_last_pick_mi - 0.05):
                    c.dispatch_status = "enroute"
                    mark_status(c, "gps")
                    event = "enroute"
                if mins_to <= 20:
                    who = c.motorista
                    car_txt = f" in a {c.car_string_val}" if c.car_string_val else ""
                    if d_pick <= 0.311 and not c.notified_10km:
                        c.notified_10km = c.notified_15km = True
                        sms = ("500m", f"Hi {c.nome}! Your ClubLifter driver {who} is about "
                                       f"500 meters away{car_txt}. Please start heading out!")
                    elif d_pick <= 0.621 and not c.notified_15km:
                        c.notified_15km = True
                        sms = ("1km", f"Hi {c.nome}! Your ClubLifter driver {who} is about "
                                      f"1 km away{car_txt}. Get ready!")
            if d_pick is not None:
                c.gps_last_pick_mi = round(d_pick, 3)

        elif c.status == "picked_up":
            if d_dest is not None and d_dest <= VENUE_RADIUS_MI:
                _complete_dropoff(c, now_u, d_dest, "gps")
                event = "dropped_off"
            else:
                # Venue pin may be wrong: a picked-up car that has been PARKED for a
                # few minutes, well away from the pickup, has delivered the guest.
                anchor = _stop_anchors.get(pos["name"])
                up_min = ((now_u - c.picked_up_at).total_seconds() / 60.0) if c.picked_up_at else -mins_to
                if anchor and not anchor.get("first_seen") and up_min >= 8:
                    parked_min = (now_u - anchor["since"]).total_seconds() / 60.0
                    far = (d_pick is None) or (d_pick >= STOP_DROP_MIN_FROM_PICKUP_MI)
                    if parked_min >= STOP_DROP_MIN and far:
                        _complete_dropoff(c, now_u, d_dest, "gps")
                        c.dropoff_verified = False       # stop-based, not at the venue pin
                        event = "dropped_off"

        db.session.commit()

        if sms:
            _proximity_sms(c, sms[0], sms[1])
        if event:
            print(f"[AUTO-STATUS] ride {c.id} ({c.nome}) → {event} by GPS "
                  f"(pickup {d_pick if d_pick is None else round(d_pick, 2)} mi, "
                  f"venue {d_dest if d_dest is None else round(d_dest, 2)} mi)", flush=True)
            if event == "picked_up":
                fire_webhook({"type": "picked_up", "source": "auto", "customer_id": c.id,
                              "customer_name": c.nome, "driver_name": c.motorista,
                              "destination": c.destination,
                              "picked_up_at": vegas_datetime(c.picked_up_at)})
                _mark_original_left(c, "gps")
            elif event == "dropped_off":
                fire_webhook({"type": "dropped_off", "source": "auto", "customer_id": c.id,
                              "customer_name": c.nome, "driver_name": c.completed_driver,
                              "destination": c.destination, "verified_at_venue": True,
                              "distance_to_venue_mi": c.dropoff_distance_mi})
            else:
                fire_webhook({"type": "ride_status_auto", "status": event, "customer_id": c.id,
                              "customer_name": c.nome, "driver_name": c.motorista,
                              "pickup_address": c.endereco, "destination": c.destination})
            ride_status_changed(c, before, "gps")

def auto_dropoff_at_venue():
    """Kept for compatibility — drop-off detection now lives in auto_progress_rides()."""
    auto_progress_rides()

def backfill_ride_coords(limit=4, budget_sec=10):
    """Geocode (once) the pickups/destinations of rides that are coming up, and the
    Las Vegas clubs, so GPS matching never has to call a geocoder on the spot.
    Bounded per tick (count + time) so a slow geocoder never delays GPS status."""
    now_v = vegas_naive_now()
    t0 = time.time()
    done = 0
    rows = Customer.query.filter(Customer.needs_transport == True,
                                 Customer.status.in_(("scheduled", "waitlist", "picked_up"))).all()
    for c in rows:
        if done >= limit or time.time() - t0 > budget_sec:
            break
        pdt = parse_pickup_datetime(c.pickup_datetime)
        if pdt is None or not (-240 <= (pdt - now_v).total_seconds() / 60.0 <= 24 * 60):
            continue
        if c.pickup_lat is None and c.endereco and c.endereco != "(walk-in)":
            lat, lng = geocode_address(c.endereco)
            done += 1
            if lat is not None:
                c.pickup_lat, c.pickup_lng = lat, lng
                db.session.commit()
        if c.dest_lat is None and c.destination:
            if c.is_return_ride:
                lat, lng = geocode_address(c.destination)
                done += 1
            else:
                club = club_for_destination(c.destination)
                lat, lng = (club.lat, club.lng) if club and club.lat is not None else (None, None)
            if lat is not None:
                c.dest_lat, c.dest_lng = lat, lng
                db.session.commit()
    # Vegas clubs: make sure each address has been geocoded once (2nd venue point)
    for cl in Club.query.filter_by(active=True).all():
        if done >= limit + 2 or time.time() - t0 > budget_sec:
            break
        if not cl.address or "NV" not in cl.address.upper():
            continue
        _k = _geo_key(cl.address)
        if _k in _geo_mem or (time.time() - _geo_neg.get(_k, 0) < 600):
            continue
        geocode_address(cl.address)
        done += 1

def run_tracker_tick():
    """One pass of all background work. Each step is isolated so one failure
    never stops the others."""
    for label, fn in (("STOPS", detect_driver_stops),
                      ("GPS-WATCH", check_gps_disconnects),
                      ("ASSIGN", auto_assign_pending),
                      ("AUTO-STATUS", auto_progress_rides),
                      ("FAILSAFE", auto_timeouts),
                      ("GEO", backfill_ride_coords),        # slow part last
                      ("CLEANUP", _purge_old_here_photos)):
        try:
            fn()
        except Exception as e:
            db.session.rollback()
            print(f"[{label}] error: {e}", flush=True)
            _traceback.print_exc()

def reset_ride_tracking(c, address_changed=False, dest_changed=False):
    """After an edit/reschedule: re-arm alerts and GPS detection, and refresh the
    stored coordinates when the pickup or destination changed."""
    c.notified_15km = c.notified_10km = c.notified_5km = False
    c.near_pickup_since = None
    c.gps_last_pick_mi = None
    c.gps_seen_at = None
    c.unassigned_alert_level = 0
    if address_changed:
        c.pickup_lat, c.pickup_lng = geocode_address(c.endereco)
    if dest_changed:
        club = club_for_destination(c.destination) if not c.is_return_ride else None
        if club and club.lat is not None:
            c.dest_lat, c.dest_lng = club.lat, club.lng
        else:
            c.dest_lat, c.dest_lng = (geocode_address(c.destination) if c.is_return_ride
                                      else (None, None))

def _purge_old_here_photos():
    cutoff = datetime.utcnow() - timedelta(hours=24)
    stale = Customer.query.filter(Customer.here_photo != "", Customer.here_photo_at != None,
                                  Customer.here_photo_at < cutoff).all()
    for sc in stale:
        sc.here_photo = ""
        sc.here_photo_at = None
    if stale:
        db.session.commit()
        print(f"[CLEANUP] Purged {len(stale)} expired here-photo(s)", flush=True)

# ─── BACKGROUND DISTANCE TRACKER (15km/5km notifications) ─────────────────────
# ─── DRIVER STOP DETECTION ────────────────────────────────────────────────────
STOP_RADIUS_MI = 0.05    # ~80 m — within this, the vehicle counts as "not moving"
MIN_STOP_MIN   = 5       # only record a stop once it lasts this long
_stop_anchors  = {}      # car_name -> {"lat","lng","since","stop_id"}

def _reverse_geocode(lat, lng):
    r = _nominatim_get({"lat": lat, "lon": lng, "format": "json", "zoom": 18}, path="reverse", timeout=8)
    try:
        return ((r or {}).get('display_name') or "")[:300]
    except Exception:
        return ""

def detect_driver_stops():
    """Compare each vehicle's current position with its anchor point.
    Still there → the stop grows. Moved away → the stop is closed."""
    lista = gps_fleet()
    if lista is None:
        print("[STOPS] GPS fetch failed", flush=True)
        return

    now = datetime.utcnow()
    today = vegas_today()
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
            # first_seen: we don't know how long it was parked before the server
            # started, so it doesn't count as "recently moved" until it moves.
            _stop_anchors[name] = {"lat": lat, "lng": lng, "since": now, "stop_id": None,
                                   "first_seen": True}
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
                # Keep the driver in sync: fill when empty, and also update if the
                # car's driver changed while the stop is still open (e.g. the car
                # was just assigned to someone in Driver Management).
                if driver_name and st.driver_name != driver_name:
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

GPS_DISCONNECT_ALERT_NUMBERS = ["+17253121048", "+13109307413"]
GPS_DISCONNECT_MINUTES = 60          # consider a device "disconnected" after this long with no update
_gps_disconnected = {}               # display_name -> True while we've already alerted (reset when it returns)

def _parse_gps_timestamp(v):
    """Pull the 'last seen' time from a OneStepGPS device record and return a naive
    UTC datetime, or None if it can't be read. OneStepGPS uses 'dt_tracker' (and
    sometimes 'dt_server') as ISO-8601 timestamps."""
    raw = v.get('dt_tracker') or v.get('dt_server') or (v.get('last_tap', {}) or {}).get('dt_tracker')
    if not raw:
        return None
    try:
        s = str(raw).replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            from datetime import timezone as _tz
            dt = dt.astimezone(_tz.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None

def check_gps_disconnects():
    """Alert by SMS when any OneStepGPS device stops reporting for over an hour.
    Fires once per disconnection; re-arms when the device comes back so a later
    drop alerts again."""
    lista = gps_fleet()
    if lista is None:
        return  # GPS API unreachable — don't false-alarm

    now = datetime.utcnow()
    for v in lista:
        name = v.get('display_name', '') or "Unknown device"
        last_seen = _parse_gps_timestamp(v)
        if last_seen is None:
            continue  # can't tell age → skip rather than false-alarm
        minutes_since = (now - last_seen).total_seconds() / 60.0

        if minutes_since >= GPS_DISCONNECT_MINUTES:
            if not _gps_disconnected.get(name):
                _gps_disconnected[name] = True
                hrs = minutes_since / 60.0
                ago = f"{hrs:.1f} hours" if hrs >= 2 else f"{int(minutes_since)} minutes"
                msg = (f"ClubLifter GPS alert: \"{name}\" has stopped reporting "
                       f"(no signal for {ago}). The device may be disconnected or powered off.")
                for num in GPS_DISCONNECT_ALERT_NUMBERS:
                    send_sms_bg(num, msg)
                print(f"[GPS-WATCH] {name} disconnected ({int(minutes_since)} min) — alerted", flush=True)
        else:
            if _gps_disconnected.get(name):
                _gps_disconnected[name] = False
                print(f"[GPS-WATCH] {name} is back online", flush=True)

def distance_tracker_loop():
    """Background worker: every TRACKER_INTERVAL_SEC runs run_tracker_tick() —
    stop detection, GPS watch, geocode backfill, auto-assignment and GPS ride
    status (see DISPATCH ENGINE)."""
    while True:
        time.sleep(TRACKER_INTERVAL_SEC)
        try:
            with app.app_context():
                run_tracker_tick()
        except Exception as e:
            print(f"[TRACKER] Loop error: {e}", flush=True)

def start_distance_tracker():
    """Start the tracker in a background thread (only once)."""
    thread = threading.Thread(target=distance_tracker_loop, daemon=True)
    thread.start()
    print(f"[TRACKER] Dispatch tracker started (every {TRACKER_INTERVAL_SEC}s)", flush=True)

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
        "gobest": {"state": "Nevada", "lat": 36.0665, "lng": -115.1830,
                   "free_drink": "House margarita"},
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
        "address": HUSTLER_ADDRESS,   # same venue address → same coordinates as Hustler LV
        "gobest": {"state": "Nevada", "lat": 36.0665, "lng": -115.1830,
                   "free_drink": "Well cocktail"},
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

            # ── GoBest / app fields — set coordinates, state, and free drink so
            # the venues show up in the GoBest app. Only fills when empty so any
            # manual edits on the Clubs screen are preserved.
            gb = info.get("gobest")
            if gb:
                if club.lat is None or club.lng is None:
                    club.lat = gb["lat"]; club.lng = gb["lng"]
                if not club.state:
                    club.state = gb["state"]
                if not club.free_drink:
                    club.free_drink = gb["free_drink"]
                if club.gobest is None:
                    club.gobest = True

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

def seed_gobest_clubs():
    """Idempotently create/update the GoBest network clubs with their real
    addresses, coordinates, and state so they populate the app. Only fills
    fields that are empty so manual edits on the Clubs screen are preserved.

    This is the official CartVIP venue list (Jason's starting set)."""
    clubs = [
        # name, address, state, lat, lng, free_drink
        ("Hustler Club Las Vegas", "6007 Dean Martin Dr, Las Vegas, NV 89118", "Nevada", 36.0665, -115.1830, "House Margarita"),
        ("Hustler Club New York", "641 W 51st St, New York, NY 10019", "New York", 40.7672, -73.9954, "House cocktail"),
        ("Kings of Hustler", "6007 Dean Martin Dr, Las Vegas, NV 89118", "Nevada", 36.0665, -115.1830, "House cocktail"),
        ("Little Darlings Las Vegas", "1514 Western Ave, Las Vegas, NV 89102", "Nevada", 36.1585, -115.1618, "Well Drink"),
        ("Erotic Heritage Museum Las Vegas", "3275 S Sammy Davis Jr Dr, Las Vegas, NV 89109", "Nevada", 36.1367, -115.1720, "Well drink"),
        ("Barely Legal New Orleans", "423 Bourbon St, New Orleans, LA 70130", "Louisiana", 29.9591, -90.0645, "House cocktail"),
        ("Cats Meow New Orleans", "701 Bourbon St, New Orleans, LA 70116", "Louisiana", 29.9604, -90.0631, "Well drink"),
        ("Hustler Club New Orleans", "225 Bourbon St, New Orleans, LA 70130", "Louisiana", 29.9575, -90.0668, "House cocktail"),
        ("Deja Vu Showgirls Las Vegas", "3247 S Sammy Davis Jr Dr, Las Vegas, NV 89109", "Nevada", 36.1361, -115.1723, "Well drink"),
    ]
    try:
        for name, addr, state, lat, lng, drink in clubs:
            club = Club.query.filter(func.lower(Club.name) == name.lower()).first()
            if not club:
                club = Club(name=name, address=addr, active=True,
                            state=state, lat=lat, lng=lng, free_drink=drink, gobest=True)
                db.session.add(club)
                print(f"[SEED] GoBest club created: {name}", flush=True)
            else:
                if not club.address: club.address = addr
                if not club.state:   club.state = state
                if club.lat is None: club.lat = lat
                if club.lng is None: club.lng = lng
                if not club.free_drink: club.free_drink = drink
                if club.gobest is None: club.gobest = True
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[SEED] GoBest clubs seed failed: {e}", flush=True)

def seed_demo_ride():
    """Set up a ready-to-demo scenario: a driver who is enroute, a car with a
    (simulated) GPS position, an active ride, and a couple of chat messages. Lets
    you show Ride Back + Tracking + Chat after logging into the app.

    The ride is tied to DEMO_RIDE_PHONE (defaults to DEMO_PHONE if unset). Set
    DEMO_RIDE_PHONE to your own number to demo with a normal SMS login while still
    seeing the ride. Idempotent. Set DEMO_RIDE=0 to skip."""
    # Demo scenario is OFF by default now that the project is live. Set
    # DEMO_RIDE=1 to re-enable the demo ride/driver/chat for a walkthrough.
    if os.environ.get("DEMO_RIDE", "0").lower() in ("0", "false", "no"):
        return
    # Whose number the demo ride belongs to — independent of the login demo phone
    ride_phone = clean_phone(os.environ.get("DEMO_RIDE_PHONE", "") or DEMO_PHONE)
    try:
        # 1. Demo driver's car. Defaults to a REAL OneStepGPS vehicle so the
        #    tracking map shows live GPS. Override with DEMO_RIDE_CAR if needed.
        demo_car_name = (os.environ.get("DEMO_RIDE_CAR", "") or "2013 Mercedes Sprinter 3500").strip()
        car = Car.query.filter_by(name=demo_car_name).first()
        if not car:
            car = Car(name=demo_car_name, model="", color="", plate="DEMO", active=True)
            db.session.add(car); db.session.commit()

        # 2. Demo driver account (role=driver) + Driver record, marked available
        demo_driver_user = "demo.driver"
        du = User.query.filter_by(username=demo_driver_user).first()
        if not du:
            du = User(username=demo_driver_user,
                      password_hash=generate_password_hash("demodriver123"),
                      role="driver", is_active=True)
            db.session.add(du); db.session.commit()
        drv = Driver.query.filter_by(name=demo_driver_user).first()
        if not drv:
            drv = Driver(name=demo_driver_user, phone="7025550123", available=True)
            db.session.add(drv); db.session.commit()
        # Only claim the car for the demo driver if no other driver already owns it
        # (a real GPS car like the Sprinter stays assigned to its real driver — the
        # demo ride references it by car_name for tracking, which is enough).
        other_owner = Driver.query.filter(Driver.assigned_car_id == car.id,
                                           Driver.name != demo_driver_user).first()
        if not other_owner and not drv.assigned_car_id:
            drv.assigned_car_id = car.id; db.session.commit()

        # 3. The demo ride, tied to ride_phone, driver enroute, so tracking+chat
        # show. Match by the LAST 10 DIGITS (same as the app's _app_active_ride),
        # because a stored number may be "+17025307390" while DEMO_RIDE_PHONE is
        # "7025307390" — an exact match would miss it and we'd end up with a stale
        # ride still showing. We fix ALL of the user's active rides so none of them
        # is left in a "scheduled, no driver" state that the app might pick.
        _pd = clean_phone(ride_phone)
        tail = _pd[-10:] if len(_pd) >= 10 else _pd
        # Remove any leftover TEST ride-backs on the demo number — those are created
        # when you tap "Book a ride back" while testing, and they pile up as newer
        # rides that the app then picks instead of the real demo ride (that's why it
        # showed "scheduled" with no driver). They're demo junk, safe to delete.
        junk = (Customer.query
                .filter(Customer.is_return_ride == True)
                .filter((Customer.phone.like(f"%{tail}")) |
                        (Customer.phones_json.like(f"%{tail}%")))
                .all())
        if junk:
            for j in junk:
                # Also clear their chat messages
                RideChatMessage.query.filter_by(customer_id=j.id).delete()
                db.session.delete(j)
            db.session.commit()
            print(f"[SEED] removed {len(junk)} leftover test ride-back(s) on demo number", flush=True)

        existing_rides = (Customer.query
                          .filter(Customer.is_return_ride == False)
                          .filter((Customer.phone.like(f"%{tail}")) |
                                  (Customer.phones_json.like(f"%{tail}%")))
                          .filter(Customer.club_status != "left")
                          .filter(Customer.status != "dropped_off")
                          .order_by(Customer.id.desc()).all())
        demo_ride = existing_rides[0] if existing_rides else None
        vt = vegas_today()
        today = f"{vt.month:02d}/{vt.day:02d}/{vt.year}"
        car_str = car.car_string() if hasattr(car, 'car_string') else demo_car_name
        if not demo_ride:
            demo_ride = Customer(
                nome="Demo Guest",
                phone=ride_phone,
                needs_transport=True,
                endereco="Aria, 3730 S Las Vegas Blvd, Las Vegas, NV 89158",
                destination="Hustler Club Las Vegas",
                package="Free Ride, Entry & Drink",
                guests=2,
                pickup_datetime=f"{today} 11:00 PM",
                motorista=demo_driver_user,
                motorista_phone="7025550123",
                car_name=demo_car_name,
                car_string_val=car_str,
                status="scheduled",
                club_status="coming",
                dispatch_status="enroute",   # ← enroute so chat is available + tracking shows the car
                driver_arrived_at=None,
                distancia=3.2,
            )
            db.session.add(demo_ride); db.session.commit()
        else:
            # One or more demo rides already exist for this number (from earlier
            # runs). Put EACH of them into the right state — driver assigned + en
            # route — so the app never picks a stale "scheduled, no driver" one
            # (which would show no chat and no live status).
            changed = False
            for r in existing_rides:
                if r.motorista != demo_driver_user:
                    r.motorista = demo_driver_user
                    r.motorista_phone = "7025550123"; changed = True
                if r.car_name != demo_car_name:
                    r.car_name = demo_car_name
                    r.car_string_val = car_str; changed = True
                if r.dispatch_status != "enroute":
                    r.dispatch_status = "enroute"; changed = True
                if r.club_status == "left" or r.status == "dropped_off":
                    r.club_status = "coming"; r.status = "scheduled"; changed = True
            if changed:
                db.session.commit()
                print(f"[SEED] refreshed {len(existing_rides)} existing demo ride(s) to enroute+driver", flush=True)

        # 4. Starter chat messages so the chat isn't empty (only if none exist yet)
        if RideChatMessage.query.filter_by(customer_id=demo_ride.id).count() == 0:
            db.session.add(RideChatMessage(customer_id=demo_ride.id, sender="driver",
                body="Hey! I'm on my way to pick you up, about 5 minutes out.",
                read_by_customer=False))
            db.session.add(RideChatMessage(customer_id=demo_ride.id, sender="customer",
                body="Perfect, I'll be at the north valet.", read_by_driver=True))
            db.session.commit()
            print(f"[SEED] demo ride created for {ride_phone} (driver enroute)", flush=True)
    except Exception as e:
        db.session.rollback()
        print(f"[SEED] demo ride seed failed: {e}", flush=True)

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
    safe_migrate("customer", "driver_arrived_at", "ALTER TABLE customer ADD COLUMN driver_arrived_at DATETIME")
    safe_migrate("customer", "is_return_ride",  "ALTER TABLE customer ADD COLUMN is_return_ride BOOLEAN DEFAULT 0")
    safe_migrate("customer", "return_of_id",    "ALTER TABLE customer ADD COLUMN return_of_id INTEGER")
    safe_migrate("customer", "has_return_ride", "ALTER TABLE customer ADD COLUMN has_return_ride BOOLEAN DEFAULT 0")
    safe_migrate("customer", "price_total",    "ALTER TABLE customer ADD COLUMN price_total FLOAT")
    safe_migrate("customer", "track_token",    "ALTER TABLE customer ADD COLUMN track_token VARCHAR(24)")
    safe_migrate("customer", "arrived_at",     "ALTER TABLE customer ADD COLUMN arrived_at DATETIME")
    safe_migrate("customer", "dropped_off_at",  "ALTER TABLE customer ADD COLUMN dropped_off_at DATETIME")
    safe_migrate("customer", "dropoff_verified","ALTER TABLE customer ADD COLUMN dropoff_verified BOOLEAN DEFAULT 0")
    safe_migrate("customer", "completed_driver","ALTER TABLE customer ADD COLUMN completed_driver VARCHAR(120) DEFAULT ''")
    safe_migrate("customer", "completed_car",   "ALTER TABLE customer ADD COLUMN completed_car VARCHAR(200) DEFAULT ''")
    safe_migrate("customer", "driver_notes",     "ALTER TABLE customer ADD COLUMN driver_notes VARCHAR(500) DEFAULT ''")
    safe_migrate("club", "state",       "ALTER TABLE club ADD COLUMN state VARCHAR(40) DEFAULT ''")
    safe_migrate("club", "lat",         "ALTER TABLE club ADD COLUMN lat FLOAT")
    safe_migrate("club", "lng",         "ALTER TABLE club ADD COLUMN lng FLOAT")
    safe_migrate("club", "photo_url",   "ALTER TABLE club ADD COLUMN photo_url VARCHAR(400) DEFAULT ''")
    safe_migrate("club", "free_drink",  "ALTER TABLE club ADD COLUMN free_drink VARCHAR(200) DEFAULT ''")
    safe_migrate("club", "gobest",      "ALTER TABLE club ADD COLUMN gobest BOOLEAN DEFAULT 1")
    safe_migrate("club", "review_url",  "ALTER TABLE club ADD COLUMN review_url VARCHAR(400) DEFAULT ''")
    safe_migrate("customer", "reported",         "ALTER TABLE customer ADD COLUMN reported BOOLEAN DEFAULT 0")
    safe_migrate("customer", "report_reason",    "ALTER TABLE customer ADD COLUMN report_reason VARCHAR(500) DEFAULT ''")
    safe_migrate("customer", "reported_by",      "ALTER TABLE customer ADD COLUMN reported_by VARCHAR(120) DEFAULT ''")
    safe_migrate("customer", "reported_at",      "ALTER TABLE customer ADD COLUMN reported_at DATETIME")
    safe_migrate("customer", "dropoff_distance_mi", "ALTER TABLE customer ADD COLUMN dropoff_distance_mi FLOAT DEFAULT 0")
    safe_migrate("customer", "car_name",        "ALTER TABLE customer ADD COLUMN car_name VARCHAR(100) DEFAULT ''")
    safe_migrate("customer", "car_string_val",  "ALTER TABLE customer ADD COLUMN car_string_val VARCHAR(200) DEFAULT ''")
    safe_migrate("customer", "car_photo",       "ALTER TABLE customer ADD COLUMN car_photo VARCHAR(255) DEFAULT ''")
    # Dispatch engine (auto-assign + GPS status)
    safe_migrate("customer", "pickup_lat",        "ALTER TABLE customer ADD COLUMN pickup_lat FLOAT")
    safe_migrate("customer", "pickup_lng",        "ALTER TABLE customer ADD COLUMN pickup_lng FLOAT")
    safe_migrate("customer", "dest_lat",          "ALTER TABLE customer ADD COLUMN dest_lat FLOAT")
    safe_migrate("customer", "dest_lng",          "ALTER TABLE customer ADD COLUMN dest_lng FLOAT")
    safe_migrate("customer", "status_source",     "ALTER TABLE customer ADD COLUMN status_source VARCHAR(20) DEFAULT ''")
    safe_migrate("customer", "status_updated_at", "ALTER TABLE customer ADD COLUMN status_updated_at DATETIME")
    safe_migrate("customer", "gps_seen_at",       "ALTER TABLE customer ADD COLUMN gps_seen_at DATETIME")
    safe_migrate("customer", "near_pickup_since", "ALTER TABLE customer ADD COLUMN near_pickup_since DATETIME")
    safe_migrate("customer", "gps_last_pick_mi",  "ALTER TABLE customer ADD COLUMN gps_last_pick_mi FLOAT")
    safe_migrate("customer", "assign_note",       "ALTER TABLE customer ADD COLUMN assign_note VARCHAR(200) DEFAULT ''")
    safe_migrate("customer", "unassigned_alert_level", "ALTER TABLE customer ADD COLUMN unassigned_alert_level INTEGER DEFAULT 0")
    safe_migrate("driver", "last_seen_at",        "ALTER TABLE driver ADD COLUMN last_seen_at DATETIME")
    safe_migrate("customer", "source",            "ALTER TABLE customer ADD COLUMN source VARCHAR(40) DEFAULT ''")
    safe_migrate("customer", "created_by",        "ALTER TABLE customer ADD COLUMN created_by VARCHAR(160) DEFAULT ''")
    safe_migrate("customer", "external_ref",      "ALTER TABLE customer ADD COLUMN external_ref VARCHAR(80) DEFAULT ''")

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
    seed_gobest_clubs()
    fix_known_bad_venue_coords()
    seed_demo_ride()

# Start background distance tracker (runs in daemon thread)
# Use WERKZEUG_RUN_MAIN guard to avoid double-starting in Flask debug mode reloader
# DISABLE_TRACKER=1 skips it (local tests / one-off scripts).
if os.environ.get("DISABLE_TRACKER", "").lower() not in ("1", "true", "yes") and \
        (os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug):
    start_distance_tracker()

if __name__ == '__main__':
    # Debug is OFF by default (production). Enable locally with FLASK_DEBUG=1.
    # Never run with debug=True in production — it exposes the Werkzeug debugger.
    debug_mode = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=debug_mode)
