const csrfToken = document.querySelector('meta[name="csrf-token"]').getAttribute("content");

async function fetchJSON(url, options = {}) {
    const resp = await fetch(url, {
        ...options,
        headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
            ...(options.headers || {}),
        },
    });
    if (resp.status === 204) return null;
    return resp.json();
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function renderKeys(keys) {
    const list = document.getElementById("keys-list");
    if (!list) return;

    if (!keys || keys.length === 0) {
        list.innerHTML = "<li>No SSH keys found.</li>";
        return;
    }

    let html = "";
    for (const key of keys) {
        const name = escapeHtml(key.name || "");
        const alg = escapeHtml(key.algorithm || "unknown");
        const fp = key.fingerprint ? escapeHtml(key.fingerprint) : "";
        html += `
            <li data-name="${name}">
                <span class="key-name">${name}</span>
                <span class="key-algorithm">${alg}</span>
                <span class="key-fingerprint">${fp}</span>
                <button class="show-key" data-name="${name}">Show Public</button>
                <button class="delete-key danger" data-name="${name}">Delete</button>
            </li>
        `;
    }
    list.innerHTML = html;

    list.querySelectorAll(".show-key").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const name = btn.dataset.name;
            const out = document.getElementById("generated-key");
            out.textContent = `Loading ${name}...`;
            try {
                const resp = await fetch(`/api/ssh/keys/${encodeURIComponent(name)}.pub`);
                if (!resp.ok) {
                    out.textContent = `Failed: ${resp.status}`;
                    return;
                }
                out.textContent = await resp.text();
            } catch (err) {
                out.textContent = `Failed: ${err}`;
            }
        });
    });

    list.querySelectorAll(".delete-key").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const name = btn.dataset.name;
            if (!confirm(`Delete key ${name}? This cannot be undone.`)) return;
            const resp = await fetchJSON(
                `/api/ssh/keys/${encodeURIComponent(name)}`,
                { method: "DELETE" },
            );
            if (resp === null) {
                loadKeys();
            } else if (resp && resp.error) {
                document.getElementById("generated-key").textContent =
                    `Delete failed: ${resp.error}`;
            }
        });
    });
}

async function loadKeys() {
    const list = document.getElementById("keys-list");
    if (!list) return;
    try {
        const data = await fetchJSON("/api/ssh/keys");
        if (data && data.keys) {
            renderKeys(data.keys);
        }
    } catch (err) {
        list.innerHTML = `<li>Failed to load keys: ${escapeHtml(String(err))}</li>`;
    }
}

async function generateKey() {
    const out = document.getElementById("generated-key");
    out.textContent = "Generating...";
    try {
        const data = await fetchJSON("/api/ssh/keys", { method: "POST" });
        if (data && data.error) {
            out.innerHTML = `Generation failed: ${escapeHtml(data.error)}<br><pre>${escapeHtml(data.detail || "")}</pre>`;
            return;
        }
        const name = data && data.name ? data.name : "new key";
        out.innerHTML = `<strong>${escapeHtml(name)}</strong> generated.<pre>${escapeHtml(data.public_key || "")}</pre>`;
        loadKeys();
    } catch (err) {
        out.textContent = `Generation failed: ${err}`;
    }
}

const genBtn = document.getElementById("generate-key");
if (genBtn) {
    genBtn.addEventListener("click", (e) => {
        e.preventDefault();
        generateKey();
    });
}

loadKeys();

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
        loadKeys();
    }
});