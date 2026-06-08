import math
import os
import json
import threading
import time
import requests
import urllib.parse
from datetime import datetime, date
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
MAKE_WEBHOOK = os.environ.get("MAKE_WEBHOOK_URL", "https://nightdeskt-production.up.railway.app/webhook/hustler-lv")

# ─── SHOPIFY ──────────────────────────────────────────────────────────────────
SHOPIFY_STORE   = os.environ.get("SHOPIFY_STORE", "vip-packages.myshopify.com")
SHOPIFY_TOKEN   = os.environ.get("SHOPIFY_TOKEN", "")
SHOPIFY_API_VER = "2026-04"
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
    """
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

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "price": self.price, "max_guests": self.max_guests, "active": self.active,
            "checkout_url": self.checkout_url
        }

class Driver(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), nullable=False, unique=True)
    phone      = db.Column(db.String(20), default="")
    # available: False means driver reported a problem and is temporarily disabled
    available  = db.Column(db.Boolean, default=True)
    # NEW: vehicle info
    car_model  = db.Column(db.String(100), default="")
    car_color  = db.Column(db.String(50), default="")
    car_plate  = db.Column(db.String(30), default="")
    car_photo  = db.Column(db.String(255), default="")  # filename in /data/uploads

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "phone": self.phone,
            "available": self.available,
            "car_model": self.car_model, "car_color": self.car_color,
            "car_plate": self.car_plate, "car_photo": self.car_photo
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
    distancia       = db.Column(db.Float)
    package         = db.Column(db.String(100))
    guests          = db.Column(db.Integer)
    pickup_datetime = db.Column(db.String(50), default="")
    destination     = db.Column(db.String(100), default="")
    needs_transport = db.Column(db.Boolean, default=True)              # NEW: walk-in vs transport
    club_status     = db.Column(db.String(20), default="coming")       # NEW: coming | arrived | left
    promoter        = db.Column(db.String(80), default="")             # NEW: which promoter created this
    # Pickup status: 'scheduled', 'picked_up'
    status          = db.Column(db.String(20), default="scheduled")
    # Distance notification flags (so we don't fire 2x)
    notified_15km   = db.Column(db.Boolean, default=False)
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
            "promoter": self.promoter,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else ""
        }

# ─── UTILITY ──────────────────────────────────────────────────────────────────
def calcular_distancia(lat1, lon1, lat2, lon2):
    try:
        R = 6371
        phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
        dlat = math.radians(float(lat2) - float(lat1))
        dlon = math.radians(float(lon2) - float(lon1))
        a = math.sin(dlat/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlon/2)**2
        return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))
    except:
        return float('inf')

def is_master():
    return session.get("role") == "master"

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

def fire_webhook(payload: dict):
    try:
        requests.post(MAKE_WEBHOOK, json=payload, timeout=5)
    except Exception:
        pass

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

    # Car availability — which drivers are free right now (this hour)
    now = datetime.now()
    drivers = Driver.query.filter_by(available=True).all()
    car_status = []
    for d in drivers:
        busy = driver_is_busy(d.name, now)
        car_status.append({
            "name": d.name, "car_model": d.car_model, "car_color": d.car_color,
            "car_plate": d.car_plate, "car_photo": d.car_photo,
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
    details           = request.form.get('details', '').strip()
    package           = request.form.get('package', '').strip()
    guests            = int(request.form.get('guests', 0))
    pickup_datetime   = request.form.get('pickup_datetime', '').strip()
    destination       = request.form.get('destination', '').strip()
    needs_transport   = request.form.get('needs_transport', 'true').lower() == 'true'

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

        # 1. GEOCODING
        encoded = urllib.parse.quote(endereco_completo)
        geo_res = requests.get(
            f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1&addressdetails=1",
            headers={'User-Agent': 'ClubLifter_LasVegas_App'}
        ).json()

        if not geo_res:
            return jsonify({"success": False, "error": "Address not found on global map."})

        lat_cli = float(geo_res[0]['lat'])
        lng_cli = float(geo_res[0]['lon'])

        # 2. GET ALL VEHICLES FROM ONESTEPGPS (sorted by distance)
        headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        res_v = requests.get(
            "https://track.onestepgps.com/v3/api/public/device-info?lat_lng=1",
            headers=headers_api
        ).json()

        lista = res_v if isinstance(res_v, list) else [res_v]

        # Build list of (distance, display_name, coords) sorted nearest first
        candidates = []
        for v in lista:
            v_lat = v.get('lat') or v.get('last_tap', {}).get('lat')
            v_lng = v.get('lng') or v.get('last_tap', {}).get('lng')
            if v_lat and v_lng:
                d = calcular_distancia(lat_cli, lng_cli, v_lat, v_lng)
                candidates.append((d, v.get('display_name', 'Tracker'), {"lat": float(v_lat), "lng": float(v_lng)}))

        candidates.sort(key=lambda x: x[0])

        # 3. PICK THE NEAREST AVAILABLE DRIVER
        melhor_v, menor_d, motorista_coords = "Unavailable", float('inf'), None

        for dist, display_name, coords in candidates:
            # Check if driver exists in DB and is globally available (not disabled)
            driver_profile = Driver.query.filter_by(name=display_name).first()
            if driver_profile and not driver_profile.available:
                continue  # driver reported a problem, skip

            # Check hourly slot availability
            if driver_is_busy(display_name, requested_dt):
                continue  # driver already has a pickup this hour, skip

            melhor_v       = display_name
            menor_d        = dist
            motorista_coords = coords
            break  # found the best available driver

        # 4. LOOK UP DRIVER PHONE + CAR INFO
        driver_profile  = Driver.query.filter_by(name=melhor_v).first()
        motorista_phone = driver_profile.phone if driver_profile else ""
        car_model = driver_profile.car_model if driver_profile else ""
        car_color = driver_profile.car_color if driver_profile else ""
        car_plate = driver_profile.car_plate if driver_profile else ""

        # 5. REGISTER ON ONESTEPGPS
        payload_gps = {
            "display_name": nome, "active": True, "status": "active", "marker_type": "point",
            "detail": {
                "description": f"{endereco_completo} | {details}" if details else endereco_completo,
                "lat_lng": {"lat": lat_cli, "lng": lng_cli}
            }
        }
        requests.post(URL_API, json=payload_gps, headers=headers_api)

        # 6. SAVE TO DATABASE
        distancia_arredondada = round(menor_d, 2) if menor_d != float('inf') else 0
        customer = Customer(
            nome=nome, phone=client_phone, phones_json=json.dumps(extra_phones),
            endereco=endereco_completo, details=details,
            motorista=melhor_v, motorista_phone=motorista_phone,
            distancia=distancia_arredondada, package=package,
            guests=guests, pickup_datetime=pickup_datetime,
            destination=destination,
            needs_transport=True, club_status="coming",
            promoter=session.get('username', ''),
            status='scheduled',
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

        # 8. FIRE MAKE.COM WEBHOOK
        fire_webhook({
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
            "destination":          destination,
            "needs_transport":      True,
            "car_model":            car_model,
            "car_color":            car_color,
            "car_plate":            car_plate,
            "status":               "scheduled",
            "shopify_order_id":     shopify_result.get("shopify_order_id"),
            "shopify_order_number": shopify_result.get("shopify_order_number"),
            "shopify_order_url":    shopify_result.get("shopify_order_url"),
        })

        return jsonify({
            "success": True, "motorista": melhor_v, "motorista_phone": motorista_phone,
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
        return jsonify({"success": False, "error": str(e)})

# ─── ADMIN: TODAY'S SCHEDULE ──────────────────────────────────────────────────
@app.route('/admin/today')
def admin_today():
    if not session.get("logged") or not is_master():
        return redirect(url_for("login"))

    today = date.today()
    today_str = today.strftime("%-m/%-d/%Y") if os.name != 'nt' else today.strftime("%#m/%#d/%Y")

    all_customers = Customer.query.order_by(Customer.pickup_datetime).all()
    today_customers = [c for c in all_customers if today_str in (c.pickup_datetime or "")]

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
@app.route('/api/last-client')
@require_api_key
def last_client():
    c = Customer.query.order_by(Customer.id.desc()).first()
    if not c:
        return jsonify({"error": "No clients found"})
    return jsonify(c.to_dict())

# ─── ADMIN: USER MANAGEMENT ───────────────────────────────────────────────────
@app.route('/admin/users')
def admin_users():
    if not session.get("logged") or not is_master():
        return redirect(url_for("login"))
    users  = User.query.filter(User.role.in_(['promoter', 'driver'])).all()
    clubs  = Club.query.filter_by(active=True).all()
    return render_template('admin_users.html', users=users, clubs=clubs)

@app.route('/admin/users/new', methods=['POST'])
def new_user():
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    role     = request.form.get('role', 'promoter').strip()
    club_ids = request.form.getlist('club_ids')  # multi-select
    commission = request.form.get('commission', '0')
    if not username or not password:
        return jsonify({"success": False, "error": "Username and password are required"})
    if User.query.filter_by(username=username).first():
        return jsonify({"success": False, "error": "Username already exists"})
    if role not in ('promoter', 'driver'):
        role = 'promoter'
    user = User(username=username, role=role)
    user.set_password(password)
    try:
        user.commission = float(commission or 0)
    except ValueError:
        user.commission = 0
    # Assign multiple clubs
    if club_ids:
        user.clubs = Club.query.filter(Club.id.in_([int(c) for c in club_ids])).all()
        user.club_id = int(club_ids[0])  # keep first as legacy primary
    db.session.add(user)
    db.session.commit()
    return jsonify({"success": True, "user": user.to_dict()})

@app.route('/admin/users/edit/<int:user_id>', methods=['POST'])
def edit_user(user_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    user     = User.query.get_or_404(user_id)
    club_ids = request.form.getlist('club_ids')
    role     = request.form.get('role', user.role).strip()
    commission = request.form.get('commission', None)
    if role in ('promoter', 'driver'):
        user.role = role
    if commission is not None:
        try:
            user.commission = float(commission or 0)
        except ValueError:
            pass
    # Update multiple clubs
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
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    user = User.query.get_or_404(user_id)
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
    if not session.get("logged") or not is_master():
        return redirect(url_for("login"))
    return render_template('admin_clubs.html', clubs=Club.query.all())

@app.route('/admin/clubs/new', methods=['POST'])
def new_club():
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
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
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    club = Club.query.get_or_404(club_id)
    club.name    = request.form.get('name', club.name).strip()
    club.address = request.form.get('address', club.address).strip()
    club.active  = request.form.get('active', 'true').lower() == 'true'
    db.session.commit()
    return jsonify({"success": True, "club": club.to_dict()})

@app.route('/admin/clubs/delete/<int:club_id>', methods=['POST'])
def delete_club(club_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    club = Club.query.get_or_404(club_id)
    db.session.delete(club)
    db.session.commit()
    return jsonify({"success": True})

# ─── ADMIN: PACKAGES ──────────────────────────────────────────────────────────
@app.route('/admin/packages')
def admin_packages():
    if not session.get("logged") or not is_master(): return redirect(url_for("login"))
    return render_template('admin_packages.html', packages=Package.query.all())

@app.route('/admin/packages/new', methods=['POST'])
def new_package():
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    name = request.form.get('name', '').strip()
    if not name: return jsonify({"success": False, "error": "Name is required"})
    pkg = Package(name=name, description=request.form.get('description','').strip(),
                  price=float(request.form.get('price',0)), max_guests=int(request.form.get('max_guests',0)),
                  checkout_url=request.form.get('checkout_url','').strip())
    db.session.add(pkg); db.session.commit()
    return jsonify({"success": True, "package": pkg.to_dict()})

@app.route('/admin/packages/edit/<int:pkg_id>', methods=['POST'])
def edit_package(pkg_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    pkg = Package.query.get_or_404(pkg_id)
    pkg.name               = request.form.get('name', pkg.name).strip()
    pkg.description        = request.form.get('description', pkg.description).strip()
    pkg.price              = float(request.form.get('price', pkg.price))
    pkg.max_guests         = int(request.form.get('max_guests', pkg.max_guests))
    pkg.active             = request.form.get('active', 'true').lower() == 'true'
    pkg.checkout_url = request.form.get('checkout_url', pkg.checkout_url).strip()
    db.session.commit()
    return jsonify({"success": True, "package": pkg.to_dict()})

@app.route('/admin/packages/delete/<int:pkg_id>', methods=['POST'])
def delete_package(pkg_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
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

@app.route('/admin/drivers')
def admin_drivers():
    if not session.get("logged") or not is_master(): return redirect(url_for("login"))
    return render_template('admin_drivers.html', drivers=Driver.query.all())

@app.route('/admin/drivers/new', methods=['POST'])
def new_driver():
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    name = request.form.get('name', '').strip()
    if not name: return jsonify({"success": False, "error": "Name is required"})
    if Driver.query.filter_by(name=name).first():
        return jsonify({"success": False, "error": "A driver with this name already exists"})
    photo = save_car_photo(request.files.get('car_photo'))
    driver = Driver(
        name=name,
        phone=request.form.get('phone','').strip(),
        car_model=request.form.get('car_model','').strip(),
        car_color=request.form.get('car_color','').strip(),
        car_plate=request.form.get('car_plate','').strip(),
        car_photo=photo
    )
    db.session.add(driver); db.session.commit()
    return jsonify({"success": True, "driver": driver.to_dict()})

@app.route('/admin/drivers/edit/<int:driver_id>', methods=['POST'])
def edit_driver(driver_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    driver = Driver.query.get_or_404(driver_id)
    driver.name      = request.form.get('name', driver.name).strip()
    driver.phone     = request.form.get('phone', driver.phone).strip()
    driver.car_model = request.form.get('car_model', driver.car_model).strip()
    driver.car_color = request.form.get('car_color', driver.car_color).strip()
    driver.car_plate = request.form.get('car_plate', driver.car_plate).strip()
    new_photo = save_car_photo(request.files.get('car_photo'))
    if new_photo:
        driver.car_photo = new_photo
    db.session.commit()
    return jsonify({"success": True, "driver": driver.to_dict()})

@app.route('/admin/drivers/delete/<int:driver_id>', methods=['POST'])
def delete_driver(driver_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    driver = Driver.query.get_or_404(driver_id)
    db.session.delete(driver); db.session.commit()
    return jsonify({"success": True})

# ─── DRIVER PORTAL ────────────────────────────────────────────────────────────
@app.route('/driver')
def driver_dashboard():
    if not session.get("logged"):
        return redirect(url_for("login"))
    if session.get("role") != "driver":
        return redirect(url_for("index"))

    driver_name = session.get("username")

    today = date.today()
    today_str = today.strftime("%-m/%-d/%Y") if os.name != 'nt' else today.strftime("%#m/%#d/%Y")

    # Get today's scheduled customers for this driver, ordered by pickup time
    all_customers = Customer.query.filter_by(motorista=driver_name).order_by(Customer.pickup_datetime).all()
    my_customers  = [c for c in all_customers if today_str in (c.pickup_datetime or "")]

    # Get driver availability status
    driver_profile = Driver.query.filter_by(name=driver_name).first()
    driver_available = driver_profile.available if driver_profile else True

    return render_template('driver_dashboard.html',
        customers=my_customers,
        driver_name=driver_name,
        driver_available=driver_available,
        today_str=today_str
    )

@app.route('/driver/pickup/<int:customer_id>', methods=['POST'])
def mark_picked_up(customer_id):
    """Driver marks a customer as picked up."""
    if not session.get("logged") or session.get("role") != "driver":
        return jsonify({"success": False, "error": "Unauthorized"})
    customer = Customer.query.get_or_404(customer_id)
    # Verify the driver owns this customer
    if customer.motorista != session.get("username"):
        return jsonify({"success": False, "error": "Not your customer"})
    customer.status = "picked_up"
    db.session.commit()
    return jsonify({"success": True})

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

# ─── PUBLIC API ───────────────────────────────────────────────────────────────
@app.route('/api/customers', methods=['GET'])
@require_api_key
def api_customers():
    return jsonify([c.to_dict() for c in Customer.query.order_by(Customer.id.desc()).all()])

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

# ─── GUEST LIST (admin only) ──────────────────────────────────────────────────
@app.route('/admin/guestlist')
def admin_guestlist():
    if not session.get("logged") or not is_master():
        return redirect(url_for("login"))

    # Filters
    filter_type = request.args.get('type', 'all')  # all | transport | walkin
    filter_club = request.args.get('club', 'all')
    filter_date = request.args.get('date', date.today().strftime("%-m/%-d/%Y") if os.name != 'nt' else date.today().strftime("%#m/%#d/%Y"))

    q = Customer.query
    if filter_type == 'transport':
        q = q.filter_by(needs_transport=True)
    elif filter_type == 'walkin':
        q = q.filter_by(needs_transport=False)
    if filter_club != 'all':
        q = q.filter_by(destination=filter_club)

    all_customers = q.order_by(Customer.pickup_datetime).all()

    # Filter by date in pickup_datetime
    if filter_date:
        all_customers = [c for c in all_customers if filter_date in (c.pickup_datetime or "")]

    clubs = Club.query.filter_by(active=True).all()

    # Stats
    total    = len(all_customers)
    coming   = sum(1 for c in all_customers if c.club_status == 'coming')
    arrived  = sum(1 for c in all_customers if c.club_status == 'arrived')
    left_    = sum(1 for c in all_customers if c.club_status == 'left')

    return render_template('admin_guestlist.html',
        customers=all_customers, clubs=clubs,
        filter_type=filter_type, filter_club=filter_club, filter_date=filter_date,
        total=total, coming=coming, arrived=arrived, left=left_
    )

@app.route('/admin/guestlist/status/<int:customer_id>', methods=['POST'])
def update_club_status(customer_id):
    if not is_master():
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
    if not is_master():
        return jsonify({"success": False, "error": "Unauthorized"})
    customer = Customer.query.get_or_404(customer_id)
    db.session.delete(customer)
    db.session.commit()
    return jsonify({"success": True})

# ─── BACKGROUND DISTANCE TRACKER (15km/5km notifications) ─────────────────────
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
                today = date.today()
                today_str = today.strftime("%-m/%-d/%Y") if os.name != 'nt' else today.strftime("%#m/%#d/%Y")

                # Only check today's scheduled customers that still need transport tracking
                scheduled = Customer.query.filter_by(
                    status='scheduled',
                    needs_transport=True
                ).all()
                scheduled = [c for c in scheduled if today_str in (c.pickup_datetime or "")
                             and (not c.notified_5km or not c.notified_15km)]

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
                # (we cache by ID in memory? for now, re-query)
                for c in scheduled:
                    if c.motorista not in driver_pos:
                        continue
                    d_lat, d_lng = driver_pos[c.motorista]

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

                    dist_km = calcular_distancia(c_lat, c_lng, d_lat, d_lng)
                    print(f"[TRACKER] {c.nome}: driver {c.motorista} is {dist_km:.2f} km away", flush=True)

                    # 15 km notification
                    if dist_km <= 15 and not c.notified_15km:
                        c.notified_15km = True
                        db.session.commit()
                        fire_webhook({
                            "event":           "distance_15km",
                            "customer_id":     c.id,
                            "customer_name":   c.nome,
                            "customer_phone":  c.phone,
                            "customer_phones": c.get_phones(),
                            "driver_name":     c.motorista,
                            "driver_phone":    c.motorista_phone,
                            "distance_km":     round(dist_km, 2),
                            "destination":     c.destination,
                            "pickup_datetime": c.pickup_datetime,
                        })

                    # 5 km notification
                    if dist_km <= 5 and not c.notified_5km:
                        c.notified_5km = True
                        db.session.commit()
                        fire_webhook({
                            "event":           "distance_5km",
                            "customer_id":     c.id,
                            "customer_name":   c.nome,
                            "customer_phone":  c.phone,
                            "customer_phones": c.get_phones(),
                            "driver_name":     c.motorista,
                            "driver_phone":    c.motorista_phone,
                            "distance_km":     round(dist_km, 2),
                            "destination":     c.destination,
                            "pickup_datetime": c.pickup_datetime,
                        })
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

with app.app_context():
    db.create_all()

    # ── MIGRATIONS: add new columns to existing databases ─────────────────────
    with db.engine.connect() as conn:
        from sqlalchemy import text, inspect
        inspector = inspect(db.engine)

        existing_package_cols = [c["name"] for c in inspector.get_columns("package")]
        if "checkout_url" not in existing_package_cols:
            conn.execute(text("ALTER TABLE package ADD COLUMN checkout_url VARCHAR(500) DEFAULT ''"))
            conn.commit()

        existing_customer_cols = [c["name"] for c in inspector.get_columns("customer")]
        if "destination" not in existing_customer_cols:
            conn.execute(text("ALTER TABLE customer ADD COLUMN destination VARCHAR(100) DEFAULT ''"))
            conn.commit()
        if "status" not in existing_customer_cols:
            conn.execute(text("ALTER TABLE customer ADD COLUMN status VARCHAR(20) DEFAULT 'scheduled'"))
            conn.commit()
        if "phones_json" not in existing_customer_cols:
            conn.execute(text("ALTER TABLE customer ADD COLUMN phones_json TEXT DEFAULT '[]'"))
            conn.commit()
        if "needs_transport" not in existing_customer_cols:
            conn.execute(text("ALTER TABLE customer ADD COLUMN needs_transport BOOLEAN DEFAULT 1"))
            conn.commit()
        if "club_status" not in existing_customer_cols:
            conn.execute(text("ALTER TABLE customer ADD COLUMN club_status VARCHAR(20) DEFAULT 'coming'"))
            conn.commit()
        if "notified_15km" not in existing_customer_cols:
            conn.execute(text("ALTER TABLE customer ADD COLUMN notified_15km BOOLEAN DEFAULT 0"))
            conn.commit()
        if "notified_5km" not in existing_customer_cols:
            conn.execute(text("ALTER TABLE customer ADD COLUMN notified_5km BOOLEAN DEFAULT 0"))
            conn.commit()
        if "promoter" not in existing_customer_cols:
            conn.execute(text("ALTER TABLE customer ADD COLUMN promoter VARCHAR(80) DEFAULT ''"))
            conn.commit()

        existing_driver_cols = [c["name"] for c in inspector.get_columns("driver")]
        if "available" not in existing_driver_cols:
            conn.execute(text("ALTER TABLE driver ADD COLUMN available BOOLEAN DEFAULT 1"))
            conn.commit()
        if "car_model" not in existing_driver_cols:
            conn.execute(text("ALTER TABLE driver ADD COLUMN car_model VARCHAR(100) DEFAULT ''"))
            conn.commit()
        if "car_color" not in existing_driver_cols:
            conn.execute(text("ALTER TABLE driver ADD COLUMN car_color VARCHAR(50) DEFAULT ''"))
            conn.commit()
        if "car_plate" not in existing_driver_cols:
            conn.execute(text("ALTER TABLE driver ADD COLUMN car_plate VARCHAR(30) DEFAULT ''"))
            conn.commit()
        if "car_photo" not in existing_driver_cols:
            conn.execute(text("ALTER TABLE driver ADD COLUMN car_photo VARCHAR(255) DEFAULT ''"))
            conn.commit()

        existing_user_cols = [c["name"] for c in inspector.get_columns("user")]
        if "club_id" not in existing_user_cols:
            conn.execute(text('ALTER TABLE "user" ADD COLUMN club_id INTEGER DEFAULT NULL'))
            conn.commit()
        if "commission" not in existing_user_cols:
            conn.execute(text('ALTER TABLE "user" ADD COLUMN commission FLOAT DEFAULT 0'))
            conn.commit()

    seed_data()

# Start background distance tracker (runs in daemon thread)
# Use WERKZEUG_RUN_MAIN guard to avoid double-starting in Flask debug mode reloader
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
    start_distance_tracker()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
