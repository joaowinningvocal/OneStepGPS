import math
import os
import requests
import urllib.parse
from datetime import datetime, date
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "devverse_secret"
CORS(app)

# ─── DATABASE CONFIG ───────────────────────────────────────────────────────────
database_url = os.environ.get('DATABASE_URL', 'sqlite:///clublifter.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ─── SETTINGS ─────────────────────────────────────────────────────────────────
API_KEY      = "cWpVu8yTfVRytZRt95Tnkv_VmBfUywfg_oT-GkqGzlI"
URL_API      = "https://track.onestepgps.com/v3/api/public/marker"
MAKE_WEBHOOK = "https://hook.us1.make.com/1j3rppk5wufvglcbe23kto5c63uvdt32"

# ─── MODELS ───────────────────────────────────────────────────────────────────
class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), nullable=False, unique=True)
    password_hash = db.Column(db.String(200), nullable=False)
    role          = db.Column(db.String(20), default="promoter")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {"id": self.id, "username": self.username, "role": self.role}

class Package(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255), default="")
    price       = db.Column(db.Float, default=0.0)
    max_guests  = db.Column(db.Integer, default=0)
    active      = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "price": self.price, "max_guests": self.max_guests, "active": self.active
        }

class Driver(db.Model):
    id    = db.Column(db.Integer, primary_key=True)
    name  = db.Column(db.String(100), nullable=False, unique=True)
    phone = db.Column(db.String(20), default="")

    def to_dict(self):
        return {"id": self.id, "name": self.name, "phone": self.phone}

class Customer(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    nome            = db.Column(db.String(100))
    phone           = db.Column(db.String(20), default="")
    endereco        = db.Column(db.String(500))
    details         = db.Column(db.String(500), default="")
    motorista       = db.Column(db.String(100))
    motorista_phone = db.Column(db.String(20), default="")
    distancia       = db.Column(db.Float)
    package         = db.Column(db.String(100))
    guests          = db.Column(db.Integer)
    pickup_datetime = db.Column(db.String(50), default="")
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "nome": self.nome, "phone": self.phone,
            "endereco": self.endereco, "details": self.details,
            "motorista": self.motorista, "motorista_phone": self.motorista_phone,
            "distancia": self.distancia, "package": self.package,
            "guests": self.guests, "pickup_datetime": self.pickup_datetime,
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

def fire_webhook(payload: dict):
    try:
        requests.post(MAKE_WEBHOOK, json=payload, timeout=5)
    except Exception:
        pass

# ─── AUTH ─────────────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session['logged']   = True
            session['username'] = user.username
            session['role']     = user.role
            return redirect(url_for('index'))
        return render_template('login.html', error="Invalid credentials")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── MAIN ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    if not session.get("logged"):
        return redirect(url_for("login"))
    packages  = Package.query.filter_by(active=True).all()
    customers = Customer.query.order_by(Customer.id.desc()).all()
    return render_template('index.html', clientes=customers, packages=packages)

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
    endereco_completo = request.form.get('endereco_completo', '').strip()
    details           = request.form.get('details', '').strip()
    package           = request.form.get('package', '').strip()
    guests            = int(request.form.get('guests', 0))
    pickup_datetime   = request.form.get('pickup_datetime', '').strip()

    try:
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

        # 2. FIND NEAREST DRIVER (OneStepGPS)
        headers_api = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        res_v = requests.get(
            "https://track.onestepgps.com/v3/api/public/device-info?lat_lng=1",
            headers=headers_api
        ).json()

        melhor_v, menor_d, motorista_coords = "Unavailable", float('inf'), None
        lista = res_v if isinstance(res_v, list) else [res_v]

        for v in lista:
            v_lat = v.get('lat') or v.get('last_tap', {}).get('lat')
            v_lng = v.get('lng') or v.get('last_tap', {}).get('lng')
            if v_lat and v_lng:
                d = calcular_distancia(lat_cli, lng_cli, v_lat, v_lng)
                if d < menor_d:
                    menor_d  = d
                    melhor_v = v.get('display_name', 'Tracker')
                    motorista_coords = {"lat": float(v_lat), "lng": float(v_lng)}

        # 3. LOOK UP DRIVER PHONE
        driver_profile  = Driver.query.filter_by(name=melhor_v).first()
        motorista_phone = driver_profile.phone if driver_profile else ""

        # 4. REGISTER ON ONESTEPGPS
        payload_gps = {
            "display_name": nome, "active": True, "status": "active", "marker_type": "point",
            "detail": {
                "description": f"{endereco_completo} | {details}" if details else endereco_completo,
                "lat_lng": {"lat": lat_cli, "lng": lng_cli}
            }
        }
        requests.post(URL_API, json=payload_gps, headers=headers_api)

        # 5. SAVE TO DATABASE
        distancia_arredondada = round(menor_d, 2) if menor_d != float('inf') else 0
        customer = Customer(
            nome=nome, phone=client_phone, endereco=endereco_completo, details=details,
            motorista=melhor_v, motorista_phone=motorista_phone,
            distancia=distancia_arredondada, package=package,
            guests=guests, pickup_datetime=pickup_datetime,
            created_at=datetime.utcnow()
        )
        db.session.add(customer)
        db.session.commit()

        # 6. FIRE MAKE.COM WEBHOOK
        fire_webhook({
            "driver_name":     melhor_v,
            "driver_phone":    motorista_phone,
            "customer_name":   nome,
            "customer_phone":  client_phone,
            "pickup_address":  endereco_completo,
            "details":         details,
            "pickup_datetime": pickup_datetime,
            "package":         package,
            "guests":          guests,
            "distance_km":     distancia_arredondada
        })

        return jsonify({
            "success": True, "motorista": melhor_v, "motorista_phone": motorista_phone,
            "distancia": distancia_arredondada,
            "cliente_coords": {"lat": lat_cli, "lng": lng_cli},
            "motorista_coords": motorista_coords,
            "package": package, "guests": guests, "pickup_datetime": pickup_datetime
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

    # Filter customers scheduled for today by pickup_datetime containing today's date
    all_customers = Customer.query.order_by(Customer.pickup_datetime).all()
    today_customers = [c for c in all_customers if today_str in (c.pickup_datetime or "")]

    # Monthly stats
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
def last_client():
    if not session.get("logged") or not is_master():
        return redirect(url_for("login"))
    c = Customer.query.order_by(Customer.id.desc()).first()
    if not c:
        return jsonify({"error": "No clients found"})
    return jsonify(c.to_dict())

# ─── ADMIN: USER MANAGEMENT ───────────────────────────────────────────────────
@app.route('/admin/users')
def admin_users():
    if not session.get("logged") or not is_master():
        return redirect(url_for("login"))
    users = User.query.filter_by(role='promoter').all()
    return render_template('admin_users.html', users=users)

@app.route('/admin/users/new', methods=['POST'])
def new_user():
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    if not username or not password:
        return jsonify({"success": False, "error": "Username and password are required"})
    if User.query.filter_by(username=username).first():
        return jsonify({"success": False, "error": "Username already exists"})
    user = User(username=username, role='promoter')
    user.set_password(password)
    db.session.add(user); db.session.commit()
    return jsonify({"success": True, "user": user.to_dict()})

@app.route('/admin/users/reset/<int:user_id>', methods=['POST'])
def reset_password(user_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    user = User.query.get_or_404(user_id)
    new_password = request.form.get('password', '').strip()
    if not new_password: return jsonify({"success": False, "error": "Password cannot be empty"})
    user.set_password(new_password); db.session.commit()
    return jsonify({"success": True})

@app.route('/admin/users/delete/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    user = User.query.get_or_404(user_id)
    if user.role == 'master': return jsonify({"success": False, "error": "Cannot delete master account"})
    db.session.delete(user); db.session.commit()
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
                  price=float(request.form.get('price',0)), max_guests=int(request.form.get('max_guests',0)))
    db.session.add(pkg); db.session.commit()
    return jsonify({"success": True, "package": pkg.to_dict()})

@app.route('/admin/packages/edit/<int:pkg_id>', methods=['POST'])
def edit_package(pkg_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    pkg = Package.query.get_or_404(pkg_id)
    pkg.name = request.form.get('name', pkg.name).strip()
    pkg.description = request.form.get('description', pkg.description).strip()
    pkg.price = float(request.form.get('price', pkg.price))
    pkg.max_guests = int(request.form.get('max_guests', pkg.max_guests))
    pkg.active = request.form.get('active', 'true').lower() == 'true'
    db.session.commit()
    return jsonify({"success": True, "package": pkg.to_dict()})

@app.route('/admin/packages/delete/<int:pkg_id>', methods=['POST'])
def delete_package(pkg_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    pkg = Package.query.get_or_404(pkg_id)
    db.session.delete(pkg); db.session.commit()
    return jsonify({"success": True})

# ─── ADMIN: DRIVERS ───────────────────────────────────────────────────────────
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
    driver = Driver(name=name, phone=request.form.get('phone','').strip())
    db.session.add(driver); db.session.commit()
    return jsonify({"success": True, "driver": driver.to_dict()})

@app.route('/admin/drivers/edit/<int:driver_id>', methods=['POST'])
def edit_driver(driver_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    driver = Driver.query.get_or_404(driver_id)
    driver.name = request.form.get('name', driver.name).strip()
    driver.phone = request.form.get('phone', driver.phone).strip()
    db.session.commit()
    return jsonify({"success": True, "driver": driver.to_dict()})

@app.route('/admin/drivers/delete/<int:driver_id>', methods=['POST'])
def delete_driver(driver_id):
    if not is_master(): return jsonify({"success": False, "error": "Unauthorized"})
    driver = Driver.query.get_or_404(driver_id)
    db.session.delete(driver); db.session.commit()
    return jsonify({"success": True})

# ─── PUBLIC API ───────────────────────────────────────────────────────────────
@app.route('/api/customers', methods=['GET'])
def api_customers():
    return jsonify([c.to_dict() for c in Customer.query.order_by(Customer.id.desc()).all()])

@app.route('/api/packages', methods=['GET'])
def api_packages():
    return jsonify([p.to_dict() for p in Package.query.filter_by(active=True).all()])

@app.route('/api/drivers', methods=['GET'])
def api_drivers():
    return jsonify([d.to_dict() for d in Driver.query.all()])

# ─── INIT ─────────────────────────────────────────────────────────────────────
def seed_data():
    if not User.query.filter_by(username='admin').first():
        admin = User(username='admin', role='master')
        admin.set_password('1234')
        db.session.add(admin)
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
    seed_data()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
