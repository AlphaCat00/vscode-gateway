PRAGMA foreign_keys = ON;

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    alias TEXT NOT NULL UNIQUE,

    state TEXT NOT NULL
        CHECK (state IN ('starting', 'ready', 'stopping', 'error')),
    stage TEXT
        CHECK (
            stage IS NULL OR
            stage IN (
                'validate',
                'install',
                'start_remote',
                'start_tunnel',
                'verify',
                'recover',
                'stop'
            )
        ),

    remote_pid INTEGER,
    remote_port INTEGER,
    remote_boot_id TEXT,
    remote_process_start_id TEXT,
    remote_executable TEXT,

    local_port INTEGER,
    tunnel_pid INTEGER,

    connected_clients INTEGER NOT NULL DEFAULT 0
        CHECK (connected_clients >= 0),
    last_connected_at TEXT,
    last_disconnected_at TEXT,
    disconnect_deadline_at TEXT,

    error_code TEXT,
    error_message TEXT,
    close_reason TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX sessions_state_idx ON sessions(state);
CREATE INDEX sessions_disconnect_deadline_idx
    ON sessions(disconnect_deadline_at)
    WHERE disconnect_deadline_at IS NOT NULL;

PRAGMA user_version = 1;
