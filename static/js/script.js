document.getElementById('markerForm').onsubmit = async (e) => {
    e.preventDefault();

    const status = document.getElementById('status');
    status.innerHTML = "🔍 Processando...";

    const formData = new FormData(e.target);

    const res = await fetch('/cadastrar_cep', {
        method: 'POST',
        body: formData
    });

    const data = await res.json();

    if (data.success) {
        status.innerHTML = `
            <div class="alert alert-success">
                ✅ Criado!<br>
                ID: ${data.id}<br>
                ${data.address}
            </div>`;
    } else {
        status.innerHTML = `
            <div class="alert alert-danger">
                ❌ ${data.error}
            </div>`;
    }
};