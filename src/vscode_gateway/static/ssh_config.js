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

function renderAliases(data) {
    const list = document.getElementById("alias-list");
    if (!list) return;

    if (data && data.error) {
        list.innerHTML = `<li class="error-msg">Catalog error: ${escapeHtml(data.error)}</li>`;
        return;
    }
    const aliases = (data && data.aliases) || [];
    if (aliases.length === 0) {
        list.innerHTML = "<li>No Host aliases found in the config.</li>";
        return;
    }
    list.innerHTML = aliases
        .map((a) => `<li class="alias-item">${escapeHtml(a)}</li>`)
        .join("");
}

async function loadAliases() {
    const list = document.getElementById("alias-list");
    if (!list) return;
    list.innerHTML = "<li>Loading aliases...</li>";
    try {
        const data = await fetchJSON("/api/ssh/catalog");
        renderAliases(data);
    } catch (err) {
        list.innerHTML = `<li>Failed to load aliases: ${escapeHtml(String(err))}</li>`;
    }
}

async function saveConfig() {
    const errs = document.getElementById("config-errors");
    const btn = document.getElementById("save-config");
    if (!btn) return;

    const textEl = document.getElementById("config-text");
    if (!textEl) return;
    const text = textEl.value;
    const revisionEl = document.getElementById("config-revision");
    const expectedRevision = revisionEl ? revisionEl.value : null;

    btn.disabled = true;
    errs.textContent = "Saving...";

    try {
        const data = await fetchJSON("/api/ssh/config", {
            method: "PUT",
            body: JSON.stringify({
                text,
                expected_revision: expectedRevision,
            }),
        });
        if (data && data.type && data.type.startsWith("urn:vscode-gateway:error:")) {
            const detail = data.detail || data.title || "Save failed";
            errs.innerHTML = `<span class="error-msg">${escapeHtml(detail)}</span>`;
            return;
        }
        if (data && data.error) {
            errs.innerHTML = `<span class="error-msg">${escapeHtml(data.error)}</span>`;
            return;
        }
        errs.innerHTML = `<span class="success-msg">Saved.</span>`;
        if (data && data.revision && revisionEl) {
            revisionEl.value = data.revision;
        }
        await loadAliases();
    } catch (err) {
        errs.innerHTML = `<span class="error-msg">Save failed: ${escapeHtml(String(err))}</span>`;
    } finally {
        btn.disabled = false;
    }
}

const saveBtn = document.getElementById("save-config");
if (saveBtn) {
    saveBtn.addEventListener("click", (e) => {
        e.preventDefault();
        saveConfig();
    });
}

loadAliases();