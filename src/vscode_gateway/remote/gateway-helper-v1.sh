#!/bin/sh
# gateway-helper-v1.sh - Remote helper for OpenVSCode SSH Gateway
# Operations: capabilities, runtime-inspect, runtime-install, session-start,
#             session-inspect, session-stop, session-remove, session-list

set -euf

# --- Configuration ---
GATEWAY_STATE_DIR="${GATEWAY_STATE_DIR:-$HOME/.vscode-gateway}"
RUNTIME_DIR="$GATEWAY_STATE_DIR/runtime"
SESSIONS_DIR="$GATEWAY_STATE_DIR/sessions"
HELPER_VERSION="1"

# --- Argument validation ---
whitelist_operation() {
    case "$1" in
        capabilities|runtime-inspect|runtime-install|session-start|\
        session-inspect|session-stop|session-remove|session-list) ;;
        *) printf '{"error":"unknown_operation","op":"%s"}\n' "$1"; exit 1 ;;
    esac
}

require_arg() {
    if [ $# -lt 2 ]; then
        printf '{"error":"missing_argument","op":"%s","arg":"%s"}\n' "$1" "$2"
        exit 1
    fi
}

die() {
    printf '{"error":"%s","detail":"%s"}\n' "$1" "$2"
    exit 1
}

# --- Capabilities ---
cmd_capabilities() {
    arch=$(uname -m)
    os_name=$(uname -s)
    if [ "$os_name" != "Linux" ]; then
        printf '{"platform":"%s","arch":"%s","helper_version":"%s","available":false,"reason":"not_linux"}\n' \
            "$os_name" "$arch" "$HELPER_VERSION"
        exit 0
    fi
    printf '{"platform":"linux","arch":"%s","helper_version":"%s","available":true}\n' \
        "$arch" "$HELPER_VERSION"
}

# --- Runtime inspect ---
cmd_runtime_inspect() {
    local sha256="$1"
    local version="$2"
    local tag="${version}-${sha256}"
    local stamp="$RUNTIME_DIR/$tag/installed"

    if [ -f "$stamp" ]; then
        printf '{"installed":true,"path":"%s","checksum":"%s","version":"%s"}\n' \
            "$RUNTIME_DIR/$tag" "$sha256" "$version"
    else
        printf '{"installed":false}\n'
    fi
}

# --- Runtime install ---
cmd_runtime_install() {
    local archive="$1"
    local sha256="$2"
    local version="$3"

    [ -f "$archive" ] || die "archive_not_found" "Archive $archive not found"

    local computed
    computed=$(sha256sum "$archive" | awk '{print $1}')
    if [ "$computed" != "$sha256" ]; then
        die "digest_mismatch" "Expected $sha256, got $computed"
    fi

    local tag="${version}-${sha256}"
    local dest="$RUNTIME_DIR/$tag"
    local tmp_dest="$RUNTIME_DIR/.tmp.${tag}.$$"

    mkdir -p "$RUNTIME_DIR"

    rm -rf "$tmp_dest"
    mkdir -p "$tmp_dest"

    tar -xzf "$archive" -C "$tmp_dest" --strip-components=1 2>/dev/null || \
        die "extract_failed" "Failed to extract $archive"

    if [ ! -x "$tmp_dest/bin/openvscode-server" ]; then
        rm -rf "$tmp_dest"
        die "invalid_runtime" "openvscode-server not found in archive"
    fi

    rm -rf "$dest"
    mv "$tmp_dest" "$dest"

    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$dest/installed"

    rm -f "$archive"

    printf '{"installed":true,"path":"%s","version":"%s"}\n' "$dest" "$version"
}

# --- Session start ---
cmd_session_start() {
    local session_id="$1"
    [ -n "$session_id" ] || die "missing_argument" "session_id is required"

    local session_dir="$SESSIONS_DIR/$session_id"
    mkdir -p "$session_dir/user-data" "$session_dir/server-data" "$session_dir/logs"
    chmod 700 "$session_dir"

    local openvscode_bin=""
    # set -f (noglob) is in effect; enable globbing temporarily to
    # discover the installed runtime binary.
    set +f
    for cand in "$RUNTIME_DIR"/*/bin/openvscode-server; do
        [ -x "$cand" ] && openvscode_bin="$cand" && break
    done
    set -f
    [ -n "$openvscode_bin" ] || die "runtime_not_installed" "openvscode-server not found"

    "$openvscode_bin" \
        --host 127.0.0.1 \
        --port 0 \
        --server-base-path "/editor/$session_id" \
        --without-connection-token \
        --user-data-dir "$session_dir/user-data" \
        --server-data-dir "$session_dir/server-data" \
        --logsPath "$session_dir/logs" \
        --disable-telemetry \
        > "$session_dir/server.log" 2>&1 &

    local pid=$!

    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        die "start_failed" "openvscode-server exited immediately"
    fi

    local port=""
    # The openvscode-server launch script spawns a `node` child whose PID
    # differs from `$pid`, so `ss -tlnp | grep $pid` will not see it. Try
    # several discovery strategies in order:
    # 1. ss showing any listener whose parent PID is `$pid`;
    # 2. parse the bound-port line that openvscode-server writes to
    #    server.log ("Server bound to 127.0.0.1:<port>");
    # 3. ss again searching for node descendants.
    for i in $(seq 1 20); do
        # Match $pid itself
        port=$(ss -tlnp 2>/dev/null | grep "pid=$pid" | awk '{print $4}' | grep -oP '\d+$' | head -1 || true)
        [ -n "$port" ] && break
        # Match the whole process group
        port=$(ss -tlnp 2>/dev/null | grep -E "pid=($pid|$(pgrep -P $pid 2>/dev/null | paste -sd '|' - || echo 0))" | awk '{print $4}' | grep -oP '\d+$' | head -1 || true)
        [ -n "$port" ] && break
        # Parse the server.log
        port=$(grep -aoP 'bound to \d+\.\d+\.\d+\.\d+:\K\d+' "$session_dir/server.log" 2>/dev/null | head -1 || true)
        [ -n "$port" ] && break
        sleep 0.5
    done

    if [ -z "$port" ]; then
        kill "$pid" 2>/dev/null || true
        die "port_discovery_failed" "Could not determine bound port"
    fi

    local boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo "unknown")
    local proc_start_id=$(awk '{print $22}' /proc/$pid/stat 2>/dev/null || echo "0")
    local exe_path=$(readlink -f /proc/$pid/exe 2>/dev/null || echo "$openvscode_bin")

    echo "$pid" > "$session_dir/pid"
    echo "$port" > "$session_dir/port"
    echo "$boot_id" > "$session_dir/boot_id"
    echo "$proc_start_id" > "$session_dir/proc_start_id"
    echo "$exe_path" > "$session_dir/executable"

    printf '{"pid":%d,"port":%d,"boot_id":"%s","process_start_id":"%s","executable":"%s","session_dir":"%s"}\n' \
        "$pid" "$port" "$boot_id" "$proc_start_id" "$exe_path" "$session_dir"
}

# --- Session inspect ---
cmd_session_inspect() {
    local session_id="$1"
    [ -n "$session_id" ] || die "missing_argument" "session_id is required"

    local session_dir="$SESSIONS_DIR/$session_id"
    [ -d "$session_dir" ] || die "session_not_found" "Session directory not found"

    local pid=$(cat "$session_dir/pid" 2>/dev/null || echo "")
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        printf '{"running":false,"session_dir":"%s"}\n' "$session_dir"
        return
    fi

    local port=$(cat "$session_dir/port" 2>/dev/null || echo "0")
    local boot_id=$(cat "$session_dir/boot_id" 2>/dev/null || echo "unknown")
    local proc_start_id=$(cat "$session_dir/proc_start_id" 2>/dev/null || echo "0")
    local exe_path=$(cat "$session_dir/executable" 2>/dev/null || echo "")
    local current_boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo "unknown")
    local current_proc_start_id=$(awk '{print $22}' /proc/$pid/stat 2>/dev/null || echo "0")

    local identity_ok="true"
    if [ "$boot_id" != "$current_boot_id" ] || [ "$proc_start_id" != "$current_proc_start_id" ]; then
        identity_ok="false"
    fi

    printf '{"running":true,"pid":%s,"port":%s,"boot_id":"%s","process_start_id":"%s","executable":"%s","session_dir":"%s","identity_ok":%s}\n' \
        "$pid" "$port" "$boot_id" "$proc_start_id" "$exe_path" "$session_dir" "$identity_ok"
}

# --- Session stop ---
cmd_session_stop() {
    local session_id="$1"
    [ -n "$session_id" ] || die "missing_argument" "session_id is required"

    local session_dir="$SESSIONS_DIR/$session_id"
    [ -d "$session_dir" ] || {
        printf '{"stopped":true,"reason":"absent"}\n'
        return
    }

    local pid=$(cat "$session_dir/pid" 2>/dev/null || echo "")
    if [ -z "$pid" ]; then
        printf '{"stopped":true,"reason":"no_pid"}\n'
        return
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        printf '{"stopped":true,"reason":"already_dead"}\n'
        return
    fi

    local boot_id=$(cat "$session_dir/boot_id" 2>/dev/null || echo "unknown")
    local proc_start_id=$(cat "$session_dir/proc_start_id" 2>/dev/null || echo "0")
    local current_boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo "unknown")
    local current_proc_start_id=$(awk '{print $22}' /proc/$pid/stat 2>/dev/null || echo "0")

    if [ "$boot_id" != "$current_boot_id" ] || [ "$proc_start_id" != "$current_proc_start_id" ]; then
        die "identity_conflict" "Process identity mismatch; refusing to signal"
    fi

    kill "$pid" 2>/dev/null || true
    sleep 2

    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
    fi

    if kill -0 "$pid" 2>/dev/null; then
        die "stop_failed" "Process still running after SIGKILL"
    fi

    printf '{"stopped":true,"pid":%s}\n' "$pid"
}

# --- Session remove ---
cmd_session_remove() {
    local session_id="$1"
    [ -n "$session_id" ] || die "missing_argument" "session_id is required"
    local session_dir="$SESSIONS_DIR/$session_id"
    rm -rf "$session_dir"
    printf '{"removed":true,"session_id":"%s"}\n' "$session_id"
}

# --- Session list ---
cmd_session_list() {
    mkdir -p "$SESSIONS_DIR"
    local sessions="["
    local first=true
    set +f
    for d in "$SESSIONS_DIR"/*/; do
        [ -d "$d" ] || continue
        local sid=$(basename "$d")
        [ "$sid" = "*" ] && continue
        if $first; then first=false; else sessions="$sessions,"; fi
        sessions="$sessions\"$sid\""
    done
    set -f
    sessions="$sessions]"
    printf '{"sessions":%s}\n' "$sessions"
}

# --- Main ---
main() {
    local op="${1:-}"
    whitelist_operation "$op"
    shift || true

    case "$op" in
        capabilities)      cmd_capabilities ;;
        runtime-inspect)   require_arg "$op" "sha256" "$1"; require_arg "$op" "version" "$2"; cmd_runtime_inspect "$1" "$2" ;;
        runtime-install)   require_arg "$op" "archive" "$1"; require_arg "$op" "sha256" "$2"; require_arg "$op" "version" "$3"; cmd_runtime_install "$1" "$2" "$3" ;;
        session-start)     require_arg "$op" "session_id" "$1"; cmd_session_start "$1" ;;
        session-inspect)   require_arg "$op" "session_id" "$1"; cmd_session_inspect "$1" ;;
        session-stop)      require_arg "$op" "session_id" "$1"; cmd_session_stop "$1" ;;
        session-remove)    require_arg "$op" "session_id" "$1"; cmd_session_remove "$1" ;;
        session-list)      cmd_session_list ;;
    esac
}

main "$@"
