const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const csrfToken = csrfMeta ? csrfMeta.getAttribute("content") || "" : "";

const pendingActions = new Map();
const actionFeedback = new Map();
let currentWorkspaces = [];
let workspaceByAlias = new Map();
let hasRenderedWorkspaces = false;
let loadSequence = 0;
let pollTimer = null;

class ApiError extends Error {
    constructor(status, code, detail) {
        super(detail || `Request failed with status ${status}`);
        this.name = "ApiError";
        this.status = status;
        this.code = code || "";
        this.detail = detail || "";
    }
}

function valueText(value, fallback = "") {
    if (value === null || value === undefined) return fallback;
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") {
        return String(value);
    }
    try {
        return JSON.stringify(value);
    } catch (_err) {
        return fallback;
    }
}

function formatProblemDetail(detail) {
    if (detail === null || detail === undefined) return "";
    if (typeof detail === "string") return detail;
    if (typeof detail === "number" || typeof detail === "boolean") {
        return String(detail);
    }
    if (Array.isArray(detail)) {
        return detail.map(formatProblemDetail).filter(Boolean).join("; ");
    }
    if (typeof detail === "object") {
        const message = formatProblemDetail(detail.msg || detail.message);
        if (message) {
            const location = Array.isArray(detail.loc)
                ? detail.loc.map((part) => valueText(part)).join(".")
                : "";
            return location ? `${location}: ${message}` : message;
        }
        if (detail.detail) return formatProblemDetail(detail.detail);
        try {
            return JSON.stringify(detail);
        } catch (_err) {
            return "";
        }
    }
    return "";
}

async function fetchJSON(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (
        !headers.has("Content-Type") &&
        !(typeof FormData !== "undefined" && options.body instanceof FormData)
    ) {
        headers.set("Content-Type", "application/json");
    }
    headers.set("X-CSRF-Token", csrfToken);

    let response;
    try {
        response = await fetch(url, { ...options, headers });
    } catch (err) {
        const message = err instanceof Error ? err.message : "Network request failed";
        throw new Error(`Network request failed: ${message}`);
    }

    let payload = null;
    const contentType = response.headers
        ? response.headers.get("content-type") || ""
        : "";
    if (contentType.includes("json")) {
        try {
            payload = await response.json();
        } catch (_err) {
            payload = null;
        }
    } else {
        try {
            payload = await response.text();
        } catch (_err) {
            payload = null;
        }
    }

    if (!response.ok) {
        const code =
            payload && typeof payload === "object" ? valueText(payload.code) : "";
        const detail =
            payload && typeof payload === "object"
                ? formatProblemDetail(payload.detail) ||
                  valueText(payload.title) ||
                  valueText(payload.error)
                : formatProblemDetail(payload);
        throw new ApiError(
            response.status,
            code,
            detail || `Request failed with status ${response.status}`,
        );
    }

    if (response.status === 204) return null;
    return payload;
}

function makeElement(tagName, className, text) {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
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

function isAllowed(workspace, capability) {
    return Boolean(workspace && workspace[capability] === true);
}

function humanizeStage(stage) {
    const labels = {
        validate: "validating the connection",
        install: "installing the editor runtime",
        start_remote: "starting the editor",
        start_tunnel: "starting the secure tunnel",
        verify: "verifying the editor connection",
        recover: "recovering the editor",
        stop: "closing the session",
    };
    return labels[stage] || valueText(stage, "working").replace(/_/g, " ");
}

function progressText(workspace) {
    const alias = valueText(workspace.alias, "workspace");
    const stage = valueText(workspace.stage);
    if (workspace.state === "starting") {
        const labels = {
            validate: `Connecting to ${alias}…`,
            install: `Installing the editor runtime on ${alias}…`,
            start_remote: `Starting the editor on ${alias}…`,
            start_tunnel: `Starting a secure tunnel to ${alias}…`,
            verify: `Verifying the editor connection to ${alias}…`,
            recover: `Recovering ${alias}…`,
        };
        return labels[stage] || `Connecting to ${alias}…`;
    }
    if (workspace.state === "stopping") {
        return stage === "stop"
            ? `Closing ${alias}…`
            : `Stopping ${alias}…`;
    }
    if (workspace.state === "ready") return "Ready";
    if (workspace.state === "error") {
        return "Needs attention";
    }
    return stage ? humanizeStage(stage) : "";
}

function countdownText(deadline) {
    const timestamp = Date.parse(valueText(deadline));
    if (!Number.isFinite(timestamp)) return null;
    const remaining = Math.max(0, Math.ceil((timestamp - Date.now()) / 1000));
    return `Auto-close in ${remaining}s`;
}

function safeEditorUrl(editorUrl) {
    const rawUrl = valueText(editorUrl);
    if (!rawUrl) return null;
    try {
        const parsed = new URL(rawUrl, window.location.href);
        if (
            parsed.origin !== window.location.origin ||
            (parsed.protocol !== "http:" && parsed.protocol !== "https:")
        ) {
            return null;
        }
        return rawUrl;
    } catch (_err) {
        return null;
    }
}

function dashboardStatusElement() {
    const container = document.getElementById("workspaces-container");
    if (!container || !container.parentNode) return null;
    let status = document.getElementById("dashboard-status");
    if (!status) {
        status = makeElement("p", "dashboard-status");
        status.id = "dashboard-status";
        status.setAttribute("aria-live", "polite");
        status.setAttribute("aria-atomic", "true");
        container.parentNode.insertBefore(status, container);
    }
    return status;
}

function showDashboardStatus(message, isError = false) {
    const status = dashboardStatusElement();
    if (!status) return;
    status.textContent = message;
    status.className = isError ? "dashboard-status error-msg" : "dashboard-status";
    status.setAttribute("role", isError ? "alert" : "status");
}

function clearDashboardStatus() {
    const status = dashboardStatusElement();
    if (!status) return;
    status.textContent = "";
    status.className = "dashboard-status";
    status.setAttribute("role", "status");
}

function actionFailureText(action, err) {
    const detail = err instanceof ApiError ? err.detail : err && err.message;
    const code = err instanceof ApiError ? err.code : "";
    const suffix = code ? ` (${code})` : "";
    return `${action} failed${suffix}: ${detail || "Unknown error"}`;
}

function addActionButton(actions, className, label, alias, handler, disabled) {
    const button = makeElement("button", className, label);
    button.type = "button";
    button.dataset.alias = alias;
    button.disabled = Boolean(disabled);
    button.addEventListener("click", () => {
        void handler(alias);
    });
    actions.append(button);
}

function appendHostDetail(panel, label, value) {
    const detail = makeElement("p");
    detail.textContent = `${label}: ${value}`;
    panel.append(detail);
}

function hostAddress(hostKey) {
    const host = valueText(hostKey && hostKey.host);
    const port = valueText(hostKey && hostKey.port);
    if (!host) return port;
    if (!port) return host;
    if (host.includes(":") && !host.startsWith("[")) {
        return `[${host}]:${port}`;
    }
    return `${host}:${port}`;
}

function algorithmLabel(algorithm) {
    const raw = valueText(algorithm);
    const normalized = raw.toLowerCase();
    if (normalized.includes("ed25519")) return "Ed25519";
    if (normalized.includes("ecdsa")) return "ECDSA";
    if (normalized.includes("rsa")) return "RSA";
    return raw ? `Unknown (${raw})` : "Unknown";
}

function challengeIsComplete(hostKey) {
    return Boolean(
        hostKey &&
            typeof hostKey.host === "string" &&
            hostKey.host.length > 0 &&
            hostKey.port !== null &&
            hostKey.port !== undefined &&
            typeof hostKey.publicKey === "string" &&
            hostKey.publicKey.length > 0,
    );
}

function createHostTrustPanel(workspace, replace) {
    const alias = valueText(workspace.alias);
    const hostKey = workspace.sshHostKey;
    const panel = makeElement(
        "section",
        replace ? "error-msg host-trust-panel blocking-warning" : "host-trust-panel",
    );
    panel.setAttribute("role", "alert");
    panel.setAttribute("aria-live", "assertive");

    const heading = makeElement(
        "h3",
        null,
        replace ? "SSH host key changed" : "Verify SSH host",
    );
    panel.append(heading);

    if (replace) {
        const warning = makeElement("p");
        warning.textContent = `${hostAddress(hostKey)} is presenting a key that differs from the trusted key. Verify the change before replacing the trusted key.`;
        panel.append(warning);
    } else {
        const warning = makeElement("p");
        warning.textContent = `${hostAddress(hostKey)} is not trusted yet.`;
        panel.append(warning);
    }

    if (hostKey && hostKey.role === "jump") {
        const jumpText = makeElement("p");
        jumpText.textContent = `This is a jump host used by ${alias}.`;
        panel.append(jumpText);
    }

    appendHostDetail(panel, "Algorithm", algorithmLabel(hostKey && hostKey.algorithm));
    appendHostDetail(
        panel,
        replace ? "Currently presented" : "Fingerprint",
        valueText(hostKey && hostKey.fingerprint, "Unavailable in this response"),
    );

    if (replace) {
        const previous = makeElement("p");
        previous.textContent =
            "The previously trusted fingerprint is not provided by the gateway.";
        panel.append(previous);
    } else {
        const sourceWarning = makeElement("p");
        sourceWarning.textContent =
            "Compare this fingerprint with a trusted source before continuing.";
        panel.append(sourceWarning);
    }

    const pending = pendingActions.get(alias);
    if (pending) {
        const status = makeElement("p", "host-trust-status", pending.label);
        status.setAttribute("role", "status");
        panel.append(status);
    }

    const actions = makeElement("span", "actions");
    const busy = Boolean(pending);
    addActionButton(
        actions,
        "cancel-host-btn",
        "Cancel",
        alias,
        closeSession,
        busy || !isAllowed(workspace, "canClose"),
    );
    addActionButton(
        actions,
        replace ? "replace-host-btn" : "trust-host-btn",
        replace ? "Replace trusted key" : "Trust host",
        alias,
        () => trustHost(alias, replace),
        busy || !isAllowed(workspace, "canRetry") || !challengeIsComplete(hostKey),
    );
    panel.append(actions);
    return panel;
}

function appendAuthenticationError(alias, code) {
    const panel = makeElement("section", "error-msg authentication-error");
    panel.setAttribute("role", "alert");
    const heading = makeElement("h3", null, "SSH authentication failed");
    panel.append(heading);

    const message = makeElement("p");
    message.textContent =
        code === "ssh_no_uploaded_keys"
            ? `No SSH keys are uploaded for ${alias}.`
            : `None of your uploaded SSH keys was accepted by ${alias}.`;
    panel.append(message);

    const guidance = makeElement("p");
    guidance.textContent =
        code === "ssh_no_uploaded_keys"
            ? "Upload an SSH key before trying this workspace again."
            : "Check the User and HostName values in SSH Config, or replace an uploaded key.";
    panel.append(guidance);

    const links = makeElement("span", "actions");
    if (code !== "ssh_no_uploaded_keys") {
        const configLink = makeElement("a", "ssh-config-link", "Open SSH Config");
        configLink.href = "/settings/ssh";
        links.append(configLink);
    }
    const keysLink = makeElement("a", "ssh-keys-link", "Manage SSH Keys");
    keysLink.href = "/settings/keys";
    links.append(keysLink);
    panel.append(links);
    return panel;
}

function appendWorkspaceError(card, workspace, alias) {
    const code = valueText(workspace.errorCode);
    if (
        code === "ssh_no_uploaded_keys" ||
        code === "ssh_no_uploaded_key_accepted"
    ) {
        card.append(appendAuthenticationError(alias, code));
        return;
    }

    if (code === "ssh_host_unknown" && workspace.sshHostKey) {
        card.append(createHostTrustPanel(workspace, false));
        return;
    }
    if (code === "ssh_host_changed" && workspace.sshHostKey) {
        card.append(createHostTrustPanel(workspace, true));
        return;
    }

    const message = valueText(workspace.errorMessage);
    if (!message && !code) return;
    const error = makeElement("div", "error-msg");
    error.setAttribute("role", "alert");
    error.textContent = code && message ? `${code}: ${message}` : message || code;
    card.append(error);
}

function renderWorkspaces(workspaces) {
    const container = document.getElementById("workspaces-container");
    if (!container) return;

    currentWorkspaces = Array.isArray(workspaces) ? workspaces.slice() : [];
    workspaceByAlias = new Map();
    for (const workspace of currentWorkspaces) {
        if (!workspace || typeof workspace !== "object") continue;
        workspaceByAlias.set(valueText(workspace.alias), workspace);
    }
    hasRenderedWorkspaces = true;
    clearDashboardStatus();

    if (currentWorkspaces.length === 0) {
        container.replaceChildren(
            makeElement(
                "p",
                null,
                "No workspaces found. Add Host aliases to your SSH config.",
            ),
        );
        return;
    }

    const fragment = document.createDocumentFragment();
    for (const workspace of currentWorkspaces) {
        if (!workspace || typeof workspace !== "object") continue;
        const alias = valueText(workspace.alias);
        const state = valueText(workspace.state, "closed");
        const card = makeElement("div", "workspace-card");
        card.dataset.alias = alias;

        const aliasElement = makeElement("span", "alias", alias);
        if (workspace.catalogMissing) {
            aliasElement.append(document.createTextNode(" (removed from config)"));
        }
        card.append(aliasElement);

        card.append(makeElement("span", `status ${statusClass(state)}`, state));
        card.append(makeElement("span", "stage", progressText(workspace)));

        const connectedClients = workspace.connectedClients;
        if (connectedClients !== null && connectedClients !== undefined) {
            card.append(
                makeElement(
                    "span",
                    "clients",
                    `${valueText(connectedClients)} connected`,
                ),
            );
        } else {
            card.append(makeElement("span", "clients"));
        }

        const deadline = countdownText(workspace.disconnectDeadline);
        if (deadline) {
            const countdown = makeElement("span", "countdown", deadline);
            countdown.dataset.deadline = valueText(workspace.disconnectDeadline);
            card.append(countdown);
        }

        const actions = makeElement("span", "actions");
        const pending = pendingActions.get(alias);
        const busy = Boolean(pending);
        if (busy) card.setAttribute("aria-busy", "true");
        if (isAllowed(workspace, "canOpen")) {
            addActionButton(
                actions,
                "open-btn",
                "Open",
                alias,
                openSession,
                busy,
            );
        }
        if (isAllowed(workspace, "canClose")) {
            addActionButton(
                actions,
                "close-btn danger",
                "Close",
                alias,
                closeSession,
                busy,
            );
        }
        if (isAllowed(workspace, "canRetry")) {
            addActionButton(
                actions,
                "retry-btn",
                "Retry",
                alias,
                retrySession,
                busy,
            );
        }

        const editorUrl = safeEditorUrl(workspace.editorUrl);
        if (state === "ready" && editorUrl && !busy) {
            const editorLink = makeElement("a", "editor-link", "Open Editor");
            editorLink.setAttribute("href", editorUrl);
            editorLink.target = "_blank";
            editorLink.rel = "noopener noreferrer";
            actions.append(editorLink);
        }
        card.append(actions);

        if (pending) {
            const actionStatus = makeElement("span", "action-status", pending.label);
            actionStatus.setAttribute("role", "status");
            card.append(actionStatus);
        }

        const feedback = actionFeedback.get(alias);
        if (feedback) {
            const feedbackElement = makeElement("div", "error-msg", feedback);
            feedbackElement.setAttribute("role", "alert");
            card.append(feedbackElement);
        }
        appendWorkspaceError(card, workspace, alias);
        fragment.append(card);
    }
    container.replaceChildren(fragment);
}

function renderCurrentWorkspaces() {
    if (hasRenderedWorkspaces) renderWorkspaces(currentWorkspaces);
}

async function runSessionAction(alias, action) {
    if (pendingActions.has(alias)) return;
    const workspace = workspaceByAlias.get(alias);
    const capability = {
        open: "canOpen",
        close: "canClose",
        retry: "canRetry",
    }[action];
    if (!workspace || !capability || !isAllowed(workspace, capability)) return;

    const labels = {
        open: "Opening…",
        close: "Closing…",
        retry: "Retrying…",
    };
    const paths = {
        open: "open",
        close: "close",
        retry: "retry",
    };
    pendingActions.set(alias, { label: labels[action] });
    actionFeedback.delete(alias);
    renderCurrentWorkspaces();

    try {
        await fetchJSON(
            `/api/sessions/${encodeURIComponent(alias)}/${paths[action]}`,
            { method: "POST" },
        );
    } catch (err) {
        const message = actionFailureText(
            action.charAt(0).toUpperCase() + action.slice(1),
            err,
        );
        actionFeedback.set(alias, message);
        showDashboardStatus(message, true);
    } finally {
        pendingActions.delete(alias);
        renderCurrentWorkspaces();
        await loadWorkspaces();
    }
}

async function openSession(alias) {
    return runSessionAction(alias, "open");
}

async function closeSession(alias) {
    return runSessionAction(alias, "close");
}

async function retrySession(alias) {
    return runSessionAction(alias, "retry");
}

async function trustHost(alias, replace) {
    if (pendingActions.has(alias)) return;
    const workspace = workspaceByAlias.get(alias);
    const hostKey = workspace && workspace.sshHostKey;
    if (!workspace || !isAllowed(workspace, "canRetry") || !challengeIsComplete(hostKey)) {
        return;
    }

    pendingActions.set(alias, {
        label: replace ? "Replacing trusted host key…" : "Trusting host…",
    });
    actionFeedback.delete(alias);
    renderCurrentWorkspaces();

    const payload = {
        alias,
        host: hostKey.host,
        port: hostKey.port,
        publicKey: hostKey.publicKey,
        replace: Boolean(replace),
    };

    try {
        await fetchJSON("/api/ssh/hosts/trust", {
            method: "POST",
            body: JSON.stringify(payload),
        });
    } catch (err) {
        const message = actionFailureText("Trust host", err);
        actionFeedback.set(alias, message);
        showDashboardStatus(message, true);
        pendingActions.delete(alias);
        renderCurrentWorkspaces();
        await loadWorkspaces();
        return;
    }

    pendingActions.set(alias, { label: "Retrying connection…" });
    renderCurrentWorkspaces();
    try {
        await fetchJSON(`/api/sessions/${encodeURIComponent(alias)}/retry`, {
            method: "POST",
        });
    } catch (err) {
        const message = actionFailureText("Retry", err);
        actionFeedback.set(alias, message);
        showDashboardStatus(message, true);
    } finally {
        pendingActions.delete(alias);
        renderCurrentWorkspaces();
        await loadWorkspaces();
    }
}

function loadFailureText(err) {
    if (err instanceof ApiError) {
        const code = err.code ? ` (${err.code})` : "";
        return `Failed to load workspaces${code}: ${err.detail}`;
    }
    return `Failed to load workspaces: ${err && err.message ? err.message : "Unknown error"}`;
}

async function loadWorkspaces() {
    const container = document.getElementById("workspaces-container");
    if (!container) return;
    const sequence = ++loadSequence;
    try {
        const data = await fetchJSON("/api/sessions");
        if (sequence !== loadSequence) return;
        if (!data || !Array.isArray(data.workspaces)) {
            throw new Error("The sessions response was invalid.");
        }
        renderWorkspaces(data.workspaces);
    } catch (err) {
        if (sequence !== loadSequence) return;
        const message = loadFailureText(err);
        showDashboardStatus(message, true);
        if (!hasRenderedWorkspaces) {
            container.replaceChildren(makeElement("p", "error-msg", message));
        }
        console.error("Failed to load workspaces", err);
    }
}

function pageIsVisible() {
    return document.visibilityState !== "hidden";
}

function stopPolling() {
    if (pollTimer !== null) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

function startPolling() {
    stopPolling();
    if (!pageIsVisible()) return;
    pollTimer = setInterval(() => {
        void loadWorkspaces();
    }, 2000);
}

if (document.getElementById("workspaces-container")) {
    void loadWorkspaces();
    startPolling();
}

document.addEventListener("visibilitychange", () => {
    if (pageIsVisible()) {
        startPolling();
        void loadWorkspaces();
    } else {
        stopPolling();
    }
});
