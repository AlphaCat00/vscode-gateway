const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const csrfToken = csrfMeta ? csrfMeta.getAttribute("content") || "" : "";

const KEY_SLOTS = [
    { type: "ed25519", label: "Ed25519" },
    { type: "rsa", label: "RSA" },
    { type: "ecdsa", label: "ECDSA" },
];

const KEY_ERROR_MESSAGES = {
    ssh_key_invalid: "This file is not a valid SSH private key.",
    ssh_key_exists: "A key of this type is already uploaded. Replace or delete it first.",
    ssh_key_not_found: "That SSH key is no longer uploaded.",
};

let pageBusy = false;

class ApiError extends Error {
    constructor(message, status, code, payload) {
        super(message);
        this.name = "ApiError";
        this.status = status;
        this.code = code;
        this.payload = payload;
    }
}

function isFormDataBody(body) {
    return typeof FormData !== "undefined" && body instanceof FormData;
}

function formatValidationDetail(detail) {
    if (Array.isArray(detail)) {
        const messages = detail
            .map((item) => {
                if (typeof item === "string") return item;
                if (!item || typeof item !== "object") return "";
                const message = typeof item.msg === "string" ? item.msg : "";
                if (!message) return "";
                const location = Array.isArray(item.loc)
                    ? item.loc.filter((part) => part !== "body").join(".")
                    : "";
                return location ? `${location}: ${message}` : message;
            })
            .filter(Boolean);
        return messages.length ? `Validation failed: ${messages.join("; ")}` : "";
    }
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && typeof detail.msg === "string") {
        return detail.msg;
    }
    return "";
}

function problemMessage(payload, status) {
    if (payload && typeof payload === "object" && !Array.isArray(payload)) {
        const code = typeof payload.code === "string" ? payload.code : "";
        if (code && KEY_ERROR_MESSAGES[code]) return KEY_ERROR_MESSAGES[code];

        const detail = formatValidationDetail(payload.detail);
        if (detail) return detail;
        if (typeof payload.error === "string" && payload.error) return payload.error;
        if (typeof payload.title === "string" && payload.title) return payload.title;
    }
    if (typeof payload === "string" && payload.trim()) return payload.trim();
    return `Request failed (HTTP ${status}).`;
}

async function apiRequest(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (csrfToken && !headers.has("X-CSRF-Token")) {
        headers.set("X-CSRF-Token", csrfToken);
    }
    if (
        options.body !== undefined &&
        !isFormDataBody(options.body) &&
        !headers.has("Content-Type")
    ) {
        headers.set("Content-Type", "application/json");
    }

    const response = await fetch(url, { ...options, headers });
    if (response.status === 401) {
        window.location.replace("/login");
    }
    let payload = null;
    if (response.status !== 204) {
        const raw = await response.text();
        if (raw) {
            try {
                payload = JSON.parse(raw);
            } catch (_error) {
                payload = raw;
            }
        }
    }

    if (!response.ok) {
        const code =
            payload && typeof payload === "object" && !Array.isArray(payload)
                ? payload.code || ""
                : "";
        throw new ApiError(problemMessage(payload, response.status), response.status, code, payload);
    }
    return payload;
}

function statusElement() {
    return document.getElementById("keys-status");
}

function setStatus(message, kind = "info") {
    const status = statusElement();
    if (!status) return;
    status.textContent = message;
    status.dataset.state = kind;
    status.className = kind === "error" ? "error-msg" : kind === "success" ? "success-msg" : "";
    status.setAttribute("role", kind === "error" ? "alert" : "status");
    status.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
}

function setBusy(busy) {
    pageBusy = busy;
    document
        .querySelectorAll("#key-upload-form input, #key-upload-form button, #keys-list button")
        .forEach((control) => {
            control.disabled = busy;
        });
    const form = document.getElementById("key-upload-form");
    const list = document.getElementById("keys-list");
    if (form) form.setAttribute("aria-busy", String(busy));
    if (list) list.setAttribute("aria-busy", String(busy));
}

function appendValue(parent, label, value, className) {
    const paragraph = document.createElement("p");
    const labelElement = document.createElement("strong");
    labelElement.textContent = `${label}: `;
    paragraph.append(labelElement);
    const valueElement = document.createElement("span");
    valueElement.className = className;
    valueElement.textContent = value;
    paragraph.append(valueElement);
    parent.append(paragraph);
}

function createButton(text, className, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = text;
    button.addEventListener("click", onClick);
    return button;
}

function renderEmptySlot(content, slot) {
    const message = document.createElement("p");
    message.textContent = "No key uploaded";
    content.append(message);

    const button = createButton(`Upload ${slot.label} key`, "upload-slot", () => {
        focusUploadForm(slot.type);
    });
    content.append(button);
}

function renderPresentSlot(content, slot, key) {
    appendValue(content, "Name", String(key.name || ""), "key-name");
    appendValue(content, "Algorithm", String(key.algorithm || "Unknown"), "key-algorithm");
    appendValue(
        content,
        "Fingerprint",
        String(key.fingerprint || "Unavailable"),
        "key-fingerprint",
    );

    const actions = document.createElement("div");
    actions.className = "actions";
    actions.append(
        createButton("Copy public key", "copy-key", () => copyPublicKey(slot.type)),
        createButton("Replace", "replace-key", () =>
            replaceKey(slot.type, String(key.name || "")),
        ),
        createButton("Delete", "delete-key danger", () => deleteKey(slot.type)),
    );
    content.append(actions);
}

function renderSlot(slot, key) {
    const container = document.getElementById(`key-slot-${slot.type}`);
    if (!container) return;
    const content = container.querySelector(".key-slot-content");
    if (!content) return;
    content.replaceChildren();
    if (key && key.present === true) {
        renderPresentSlot(content, slot, key);
    } else {
        renderEmptySlot(content, slot);
    }
}

function renderKeys(keys) {
    KEY_SLOTS.forEach((slot) => renderSlot(slot, keys && keys[slot.type]));
    setBusy(pageBusy);
}

function renderUnavailableSlots() {
    KEY_SLOTS.forEach((slot) => {
        const container = document.getElementById(`key-slot-${slot.type}`);
        const content = container && container.querySelector(".key-slot-content");
        if (!content) return;
        content.replaceChildren();
        const message = document.createElement("p");
        message.textContent = "Key inventory unavailable.";
        content.append(message);
    });
    setBusy(pageBusy);
}

function errorMessage(error) {
    if (error instanceof Error && error.message) return error.message;
    const message = String(error || "Unknown error");
    return message || "Unknown error";
}

async function loadKeys({ announce = true, allowBusy = false } = {}) {
    if (pageBusy && !allowBusy) return false;
    const wasBusy = pageBusy;
    if (!wasBusy) setBusy(true);
    if (announce) setStatus("Loading SSH keys...");
    try {
        const data = await apiRequest("/api/ssh/keys");
        if (
            !data ||
            typeof data !== "object" ||
            !data.keys ||
            typeof data.keys !== "object" ||
            Array.isArray(data.keys)
        ) {
            throw new Error("The server returned an invalid key inventory.");
        }
        renderKeys(data.keys);
        if (announce) setStatus("");
        return true;
    } catch (error) {
        renderUnavailableSlots();
        setStatus(`Unable to load SSH keys: ${errorMessage(error)}`, "error");
        return false;
    } finally {
        if (!wasBusy) setBusy(false);
    }
}

function uploadFormElements() {
    return {
        form: document.getElementById("key-upload-form"),
        name: document.getElementById("key-name"),
        file: document.getElementById("private-key-file"),
    };
}

function focusUploadForm(type, replacementName = null) {
    const { form, name, file } = uploadFormElements();
    if (!form || !name || !file) return;
    const slot = KEY_SLOTS.find((candidate) => candidate.type === type);
    if (replacementName !== null) {
        form.reset();
        name.value = replacementName || (slot ? `${slot.label} key` : "");
    } else if (!name.value.trim()) {
        name.value = slot ? `${slot.label} key` : "";
    }
    if (typeof form.scrollIntoView === "function") {
        form.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    setStatus(
        `Choose and upload a private key for the ${
            KEY_SLOTS.find((slot) => slot.type === type)?.label || "selected"
        } slot. The server will determine the uploaded key type.`,
    );
    file.focus();
}

async function uploadKey(event) {
    event.preventDefault();
    if (pageBusy) return;
    const { name, file } = uploadFormElements();
    if (!name || !file) return;

    const keyName = name.value.trim();
    const keyFile = file.files && file.files[0];
    if (!keyName) {
        setStatus("Enter a name for the key.", "error");
        name.focus();
        return;
    }
    if (!keyFile) {
        setStatus("Choose a private key file to upload.", "error");
        file.focus();
        return;
    }

    const body = new FormData();
    body.append("name", keyName);
    body.append("private_key", keyFile);
    setBusy(true);
    setStatus("Uploading SSH key...");
    try {
        await apiRequest("/api/ssh/keys", { method: "POST", body });
        const form = document.getElementById("key-upload-form");
        if (form) form.reset();
        const refreshed = await loadKeys({ announce: false, allowBusy: true });
        if (!refreshed) return;
        setStatus("SSH key uploaded. The server determined its algorithm and slot.", "success");
    } catch (error) {
        setStatus(`Upload failed: ${errorMessage(error)}`, "error");
    } finally {
        setBusy(false);
    }
}

async function deleteKey(type) {
    const slot = KEY_SLOTS.find((candidate) => candidate.type === type);
    if (!slot || pageBusy) return;
    if (!window.confirm(`Delete the uploaded ${slot.label} key? This cannot be undone.`)) return;

    setBusy(true);
    setStatus(`Deleting the ${slot.label} key...`);
    try {
        await apiRequest(`/api/ssh/keys/${encodeURIComponent(type)}`, { method: "DELETE" });
        const refreshed = await loadKeys({ announce: false, allowBusy: true });
        if (!refreshed) return;
        setStatus(`${slot.label} key deleted.`, "success");
    } catch (error) {
        setStatus(`Delete failed: ${errorMessage(error)}`, "error");
    } finally {
        setBusy(false);
    }
}

async function replaceKey(type, currentName) {
    const slot = KEY_SLOTS.find((candidate) => candidate.type === type);
    if (!slot || pageBusy) return;
    if (
        !window.confirm(
            `Replace the uploaded ${slot.label} key? The current key will be deleted before you upload a replacement.`,
        )
    ) {
        return;
    }

    setBusy(true);
    setStatus(`Deleting the ${slot.label} key before replacement...`);
    let replacementReady = false;
    try {
        await apiRequest(`/api/ssh/keys/${encodeURIComponent(type)}`, { method: "DELETE" });
        const refreshed = await loadKeys({ announce: false, allowBusy: true });
        if (!refreshed) return;
        focusUploadForm(type, currentName);
        replacementReady = true;
        setStatus(
            `${slot.label} key was deleted. Choose and upload its replacement below; this is a separate upload, and the server will determine its type.`,
            "success",
        );
    } catch (error) {
        setStatus(`Replace failed: ${errorMessage(error)}`, "error");
    } finally {
        setBusy(false);
        if (replacementReady) {
            const { file } = uploadFormElements();
            if (file) file.focus();
        }
    }
}

async function copyText(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        try {
            await navigator.clipboard.writeText(text);
            return;
        } catch (_error) {
            // Fall back to the older clipboard API below.
        }
    }

    const textarea = document.createElement("textarea");
    textarea.className = "clipboard-fallback";
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    document.body.append(textarea);
    textarea.select();
    let copied = false;
    try {
        copied = document.execCommand("copy");
    } finally {
        textarea.remove();
    }
    if (!copied) throw new Error("Clipboard access is unavailable.");
}

async function copyPublicKey(type) {
    const slot = KEY_SLOTS.find((candidate) => candidate.type === type);
    if (!slot || pageBusy) return;
    setBusy(true);
    setStatus(`Fetching the ${slot.label} public key...`);
    try {
        const publicKey = await apiRequest(
            `/api/ssh/keys/${encodeURIComponent(type)}/public`,
        );
        if (typeof publicKey !== "string" || !publicKey.trim()) {
            throw new Error("The server returned no public key.");
        }
        await copyText(publicKey);
        setStatus(`${slot.label} public key copied to the clipboard.`, "success");
    } catch (error) {
        setStatus(`Copy failed: ${errorMessage(error)}`, "error");
    } finally {
        setBusy(false);
    }
}

const uploadForm = document.getElementById("key-upload-form");
if (uploadForm) uploadForm.addEventListener("submit", uploadKey);

loadKeys();

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !pageBusy) loadKeys();
});
