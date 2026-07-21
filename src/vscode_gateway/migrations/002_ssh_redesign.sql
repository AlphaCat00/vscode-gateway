-- SSH key metadata and persisted host-key challenges.

PRAGMA foreign_keys = ON;

CREATE TABLE ssh_keys (
    type        TEXT PRIMARY KEY
        CHECK (type IN ('ed25519', 'rsa', 'ecdsa')),
    name        TEXT NOT NULL,
    algorithm   TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE pending_host_keys (
    session_id  TEXT PRIMARY KEY
        REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL
        CHECK (role IN ('target', 'jump')),
    alias       TEXT NOT NULL,
    host        TEXT NOT NULL,
    port        INTEGER NOT NULL,
    algorithm   TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    public_key  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

PRAGMA user_version = 2;
