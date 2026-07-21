const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const csrfToken = csrfMeta ? csrfMeta.getAttribute("content") || "" : "";
const prohibitedDirectives = new Set([
    "include",
    "match",
    "proxycommand",
    "localcommand",
    "permitlocalcommand",
    "localforward",
    "remoteforward",
    "dynamicforward",
    "tunnel",
    "canonicalizehostname",
    "knownhostscommand",
    "pkcs11provider",
    "securitykeyprovider",
    "identityfile",
    "certificatefile",
    "identityagent",
    "userknownhostsfile",
    "globalknownhostsfile",
]);

let saveInProgress = false;

function formatValidationItem(item) {
    if (typeof item === "string") return item;
    if (!item || typeof item !== "object") return String(item);

    const message = typeof item.msg === "string"
        ? item.msg
        : typeof item.message === "string"
            ? item.message
            : "Request validation failed";
    const location = Array.isArray(item.loc)
        ? item.loc.filter((part) => part !== null && part !== undefined).join(".")
        : "";
    return location ? `${location}: ${message}` : message;
}

function formatDetail(detail) {
    if (Array.isArray(detail)) {
        return detail.map(formatValidationItem).filter(Boolean).join("; ");
    }
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") return formatValidationItem(detail);
    return detail === null || detail === undefined ? "" : String(detail);
}

function problemMessage(problem, status) {
    if (problem && typeof problem === "object") {
        const detail = formatDetail(problem.detail);
        if (detail) return detail;
        if (typeof problem.title === "string" && problem.title) return problem.title;
        if (typeof problem.error === "string" && problem.error) return problem.error;
    }
    return `Request failed (HTTP ${status})`;
}

function makeHttpError(problem, status) {
    const error = new Error(problemMessage(problem, status));
    error.status = status;
    error.problem = problem;
    error.code = problem && typeof problem.code === "string" ? problem.code : "";
    error.detail = problem ? problem.detail : "";
    error.requestId = problem && typeof problem.requestId === "string"
        ? problem.requestId
        : "";
    return error;
}

async function fetchJSON(url, options = {}) {
    const resp = await fetch(url, {
        ...options,
        headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
            ...(options.headers || {}),
        },
    });
    if (resp.status === 401) {
        window.location.replace("/login");
    }
    if (resp.status === 204) return null;
    const ok = typeof resp.ok === "boolean"
        ? resp.ok
        : resp.status >= 200 && resp.status < 300;

    let data = null;
    try {
        data = await resp.json();
    } catch (err) {
        if (ok) throw err;
    }

    if (!ok) throw makeHttpError(data, resp.status);
    return data;
}

function clearElement(element) {
    while (element && element.firstChild) element.removeChild(element.firstChild);
}

function appendMessage(container, text, className = "") {
    const message = document.createElement("div");
    if (className) message.className = className;
    message.textContent = text;
    container.appendChild(message);
}

function renderAliases(data) {
    const list = document.getElementById("alias-list");
    if (!list) return;

    clearElement(list);
    if (data && data.error) {
        const item = document.createElement("li");
        item.className = "error-msg";
        item.textContent = `Catalog error: ${String(data.error)}`;
        list.appendChild(item);
        return;
    }

    const aliases = data && Array.isArray(data.aliases) ? data.aliases : [];
    if (aliases.length === 0) {
        const item = document.createElement("li");
        item.textContent = "No Host aliases found in the config.";
        list.appendChild(item);
        return;
    }

    for (const alias of aliases) {
        const item = document.createElement("li");
        item.className = "alias-item";
        item.textContent = String(alias);
        list.appendChild(item);
    }
}

async function loadAliases() {
    const list = document.getElementById("alias-list");
    if (!list) return;

    list.setAttribute("aria-busy", "true");
    clearElement(list);
    const loading = document.createElement("li");
    loading.textContent = "Loading aliases...";
    list.appendChild(loading);
    try {
        const data = await fetchJSON("/api/ssh/catalog");
        renderAliases(data);
    } catch (err) {
        clearElement(list);
        const item = document.createElement("li");
        item.className = "error-msg";
        item.textContent = `Failed to load aliases: ${caughtErrorMessage(err)}`;
        list.appendChild(item);
    } finally {
        list.setAttribute("aria-busy", "false");
    }
}

function renderErrors(messages) {
    const errors = document.getElementById("config-errors");
    if (!errors) return;

    clearElement(errors);
    for (const message of messages) {
        appendMessage(errors, message, "error-msg");
    }
}

function setStatus(message, kind = "info") {
    const status = document.getElementById("config-status");
    if (!status) return;
    status.textContent = message;
    status.className =
        kind === "error" ? "error-msg" : kind === "success" ? "success-msg" : "";
    status.setAttribute("role", kind === "error" ? "alert" : "status");
    status.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
}

function caughtErrorMessage(error) {
    if (error && error.problem) {
        return problemMessage(error.problem, error.status);
    }
    if (error && typeof error.message === "string" && error.message) {
        return error.message;
    }
    return problemMessage(null, error && error.status);
}

function prohibitedDirectiveMessages(text) {
    const messages = [];
    const lines = text.split(/\r\n|\n|\r/);
    const directivePattern = /^\s*([A-Za-z][A-Za-z0-9-]*)\s/;
    lines.forEach((line, index) => {
        const match = directivePattern.exec(line);
        if (!match || !prohibitedDirectives.has(match[1].toLowerCase())) return;
        messages.push(
            `Line ${index + 1}: prohibited directive "${match[1]}" is not supported.`,
        );
    });
    return messages;
}

function isProhibitedDirectiveProblem(error) {
    if (!error || error.code !== "ssh_config_invalid") return false;
    const detail = formatDetail(error.detail);
    return /config contains prohibited directives:/i.test(detail);
}

function errorMessagesForSave(error, text) {
    if (error && (error.status === 409 || error.code === "conflict")) {
        const detail = problemMessage(error.problem, error.status || 409);
        return [
            `Conflict: ${detail} Reload this page before saving again.`,
        ];
    }

    const detail = caughtErrorMessage(error);
    if (isProhibitedDirectiveProblem(error)) {
        const lineMessages = prohibitedDirectiveMessages(text);
        if (lineMessages.length > 0) return [detail, ...lineMessages];
    }
    return [`Save failed: ${detail}`];
}

async function saveConfig() {
    const btn = document.getElementById("save-config");
    const textEl = document.getElementById("config-text");
    const revisionEl = document.getElementById("config-revision");
    const editor = document.getElementById("config-editor");
    if (!btn || !textEl || saveInProgress) return;

    const text = textEl.value;
    const expectedRevision = revisionEl ? revisionEl.value : null;
    saveInProgress = true;
    btn.disabled = true;
    btn.setAttribute("aria-disabled", "true");
    if (editor) editor.setAttribute("aria-busy", "true");
    setStatus("Saving...");
    renderErrors([]);

    try {
        const data = await fetchJSON("/api/ssh/config", {
            method: "PUT",
            body: JSON.stringify({
                text,
                expectedRevision,
            }),
        });
        if (
            !data
            || typeof data !== "object"
            || typeof data.text !== "string"
            || typeof data.revision !== "string"
            || data.error
            || data.type
        ) {
            throw new Error("Save response did not include a revision.");
        }
        if (revisionEl) revisionEl.value = data.revision;
        setStatus("Saved.", "success");
        await loadAliases();
    } catch (err) {
        setStatus("");
        renderErrors(errorMessagesForSave(err, text));
    } finally {
        saveInProgress = false;
        btn.disabled = false;
        btn.setAttribute("aria-disabled", "false");
        if (editor) editor.setAttribute("aria-busy", "false");
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
