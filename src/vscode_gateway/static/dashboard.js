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

function statusClass(state) {
    const map = {
        ready: "status-ready",
        starting: "status-starting",
        stopping: "status-stopping",
        error: "status-error",
        closed: "status-closed",
    };
    return map[state] || "status-closed";
}

function renderWorkspaces(workspaces) {
    const container = document.getElementById("workspaces-container");
    if (!container) return;

    if (!workspaces || workspaces.length === 0) {
        container.innerHTML = "<p>No workspaces found. Add Host aliases to your SSH config.</p>";
        return;
    }

    let html = "";
    for (const ws of workspaces) {
        let actionsHtml = "";
        if (ws.canOpen) {
            actionsHtml += `<button class="open-btn" data-alias="${ws.alias}">Open</button>`;
        }
        if (ws.canClose) {
            actionsHtml += `<button class="close-btn danger" data-alias="${ws.alias}">Close</button>`;
        }
        if (ws.canRetry) {
            actionsHtml += `<button class="retry-btn" data-alias="${ws.alias}">Retry</button>`;
        }
        if (ws.state === "ready" && ws.editorUrl) {
            actionsHtml += `<a href="${ws.editorUrl}" target="_blank"><button>Open Editor</button></a>`;
        }

        let countdownHtml = "";
        if (ws.disconnectDeadline) {
            const deadline = new Date(ws.disconnectDeadline);
            const remaining = Math.max(0, Math.ceil((deadline - new Date()) / 1000));
            countdownHtml = `<span class="countdown">Auto-close in ${remaining}s</span>`;
        }

        let errorHtml = "";
        if (ws.errorMessage) {
            errorHtml = `<div class="error-msg">${ws.errorMessage}</div>`;
        }

        html += `
            <div class="workspace-card" data-alias="${ws.alias}">
                <span class="alias">${ws.alias}${ws.catalogMissing ? ' (removed from config)' : ''}</span>
                <span class="status ${statusClass(ws.state)}">${ws.state}</span>
                <span class="stage">${ws.stage || ""}</span>
                <span class="clients">${ws.connectedClients ? ws.connectedClients + " connected" : ""}</span>
                ${countdownHtml}
                <span class="actions">${actionsHtml}</span>
                ${errorHtml}
            </div>
        `;
    }
    container.innerHTML = html;

    container.querySelectorAll(".open-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
            btn.disabled = true;
            const alias = btn.dataset.alias;
            await fetchJSON(`/api/sessions/${encodeURIComponent(alias)}/open`, { method: "POST" });
            loadWorkspaces();
        });
    });

    container.querySelectorAll(".close-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
            btn.disabled = true;
            const alias = btn.dataset.alias;
            await fetchJSON(`/api/sessions/${encodeURIComponent(alias)}/close`, { method: "POST" });
            loadWorkspaces();
        });
    });

    container.querySelectorAll(".retry-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
            btn.disabled = true;
            const alias = btn.dataset.alias;
            await fetchJSON(`/api/sessions/${encodeURIComponent(alias)}/retry`, { method: "POST" });
            loadWorkspaces();
        });
    });
}

async function loadWorkspaces() {
    try {
        const data = await fetchJSON("/api/sessions");
        if (data && data.workspaces) {
            renderWorkspaces(data.workspaces);
        }
    } catch (err) {
        console.error("Failed to load workspaces", err);
    }
}

// Poll every 2 seconds
loadWorkspaces();
setInterval(loadWorkspaces, 2000);

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
        loadWorkspaces();
    }
});
