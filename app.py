from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import requests

app = Flask(__name__)
app.secret_key = "devverse_secret"  # necessário para sessão

API_KEY = "cWpVu8yTfVRytZRt95Tnkv_VmBfUywfg_oT-GkqGzlI"
URL_API = "https://track.onestepgps.com/v3/api/public/marker"

# =========================
# LOGIN FIXO (SIMPLES)
# =========================
USER = {
    "username": "admin",
    "password": "1234"
}

# =========================
# ROTAS
# =========================

@app.route('/')
def index():
    if not session.get("logged"):
        return redirect(url_for("login"))
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form.get('username')
        password = request.form.get('password')

        if user == USER['username'] and password == USER['password']:
            session['logged'] = True
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error="Login inválido")

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# =========================
# API (PROTEGIDA)
# =========================
@app.route('/cadastrar_cep', methods=['POST'])
def cadastrar_cep():
    if not session.get("logged"):
        return jsonify({"success": False, "error": "Não autorizado"})

    nome = request.form.get('nome')
    cep = request.form.get('cep')
    numero = request.form.get('numero', '')

    try:
        viacep_res = requests.get(f"https://viacep.com.br/ws/{cep}/json/").json()
        if "erro" in viacep_res:
            return jsonify({"success": False, "error": "CEP não encontrado."})

        rua = viacep_res['logradouro']
        bairro = viacep_res['bairro']
        cidade = viacep_res['localidade']
        uf = viacep_res['uf']

        full_address = f"{rua}, {numero}, {bairro}, {cidade}, {uf}, Brasil"

        geo_url = f"https://nominatim.openstreetmap.org/search?q={full_address}&format=json&limit=1"
        geo_res = requests.get(geo_url, headers={'User-Agent': 'DevVerse_App'}).json()

        if not geo_res:
            return jsonify({"success": False, "error": "Sem coordenadas."})

        lat = float(geo_res[0]['lat'])
        lng = float(geo_res[0]['lon'])

        payload = {
            "display_name": nome,
            "active": True,
            "status": "active",
            "marker_type": "point",
            "detail": {
                "description": full_address,
                "marker_icon": "/v3/ui/assets/map-places-icons/location-dot.svg",
                "marker_visible": True,
                "lat_lng": {"lat": lat, "lng": lng}
            }
        }

        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json"
        }

        res = requests.post(URL_API, json=payload, headers=headers)

        if res.status_code in [200, 201]:
            data = res.json()
            return jsonify({
                "success": True,
                "id": data.get("marker_id"),
                "address": full_address
            })
        else:
            return jsonify({"success": False, "error": res.text})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == '__main__':
    app.run(debug=True)