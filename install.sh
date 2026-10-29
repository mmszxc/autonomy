#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROFILE=""
SKIP_ML=0
NO_TIMER=0
ASSUME_YES=0
FORCE_UNITS=0
SUCCESS=0
CONFIG_TOUCHED=0
CONFIG_EXISTED=0
BACKUP_DIR=""
HERMES_HOME=""
ACTIVE_STAGE=""
INSTALLED_PLUGINS=()

usage() {
    cat <<'EOF'
Usage: ./install.sh --profile NAME [options]

Install the autonomy and seed_memory plugins into an existing Hermes profile.

Options:
  --profile NAME  Existing Hermes profile to update (required)
  --skip-ml       Do not install CPU ML dependencies or the embedding model
  --no-timer      Do not write or enable the user timer (service is still written)
  --yes           Accept the large ML download without prompting
  --force         Back up and replace existing profile-specific systemd units
  -h, --help      Show this help

Environment overrides used by tests and advanced deployments:
  HERMES_BIN      Hermes executable (default: hermes from PATH)
  ML_INSTALLER    ML installer path (default: tools/install_ml.sh)
  SYSTEMCTL_BIN   systemctl executable (default: systemctl from PATH)
EOF
}

log() { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARNING: %s\n' "$*" >&2; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

while (($#)); do
    case "$1" in
        --profile)
            (($# >= 2)) || die "--profile requires a value"
            PROFILE=$2
            shift 2
            ;;
        --skip-ml) SKIP_ML=1; shift ;;
        --no-timer) NO_TIMER=1; shift ;;
        --yes) ASSUME_YES=1; shift ;;
        --force) FORCE_UNITS=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ -n "$PROFILE" ]] || { usage >&2; die "--profile is required"; }
[[ "$PROFILE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid profile name: $PROFILE"
[[ -f "$ROOT_DIR/plugins/autonomy/plugin.yaml" ]] || die "autonomy plugin is missing from this checkout"
[[ -f "$ROOT_DIR/plugins/seed_memory/plugin.yaml" ]] || die "seed_memory plugin is missing from this checkout"

HERMES_BIN=${HERMES_BIN:-$(command -v hermes || true)}
[[ -n "$HERMES_BIN" ]] || die "Hermes executable not found; set HERMES_BIN"
if [[ "$HERMES_BIN" != */* ]]; then
    HERMES_BIN=$(command -v "$HERMES_BIN" || true)
fi
[[ -n "$HERMES_BIN" && -x "$HERMES_BIN" ]] || die "Hermes executable is not executable: $HERMES_BIN"
HERMES_BIN=$(CDPATH= cd -- "$(dirname -- "$HERMES_BIN")" && pwd -P)/$(basename -- "$HERMES_BIN")

profile_output=""
if ! profile_output=$("$HERMES_BIN" profile show "$PROFILE" 2>&1); then
    printf '%s\n' "$profile_output" >&2
    die "profile '$PROFILE' does not exist; create it with Hermes before installing"
fi
while IFS= read -r line; do
    case "$line" in
        Path:*)
            HERMES_HOME=${line#Path:}
            HERMES_HOME=${HERMES_HOME#"${HERMES_HOME%%[![:space:]]*}"}
            ;;
    esac
done <<< "$profile_output"
[[ -n "$HERMES_HOME" && -d "$HERMES_HOME" ]] || die "could not resolve profile path from 'hermes profile show $PROFILE'"
HERMES_HOME=$(CDPATH= cd -- "$HERMES_HOME" && pwd -P)
[[ "$HERMES_HOME" != *$'\n'* ]] || die "profile path contains a newline"

if ((SKIP_ML == 0 && ASSUME_YES == 0)); then
    printf '%s\n' \
        "The full installation downloads pinned CPU PyTorch, sentence-transformers," \
        "and a pinned multilingual MiniLM model. Several GiB may be required."
    [[ -t 0 ]] || die "ML download confirmation is required in non-interactive mode; rerun with --yes or --skip-ml"
    read -r -p "Continue with the full installation? [y/N] " reply
    [[ "$reply" == y || "$reply" == Y || "$reply" == yes || "$reply" == YES ]] || die "cancelled before making changes"
fi

CONFIG_PATH="$HERMES_HOME/config.yaml"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="$HERMES_HOME/.autonomy-install-backups/${TIMESTAMP}-$$"
umask 077
mkdir -p -- "$BACKUP_DIR"
if [[ -f "$CONFIG_PATH" || -L "$CONFIG_PATH" ]]; then
    cp -a -- "$CONFIG_PATH" "$BACKUP_DIR/config.yaml"
    CONFIG_EXISTED=1
else
    : > "$BACKUP_DIR/config.yaml.absent"
fi

restore_on_failure() {
    local rc=$? plugin target index
    if ((rc != 0 && SUCCESS == 0)); then
        [[ -n "$ACTIVE_STAGE" ]] && rm -rf -- "$ACTIVE_STAGE"
        if ((CONFIG_TOUCHED)); then
            warn "installation failed; restoring the original config.yaml"
            if ((CONFIG_EXISTED)); then
                rm -f -- "$CONFIG_PATH"
                cp -a -- "$BACKUP_DIR/config.yaml" "$CONFIG_PATH"
            else
                rm -f -- "$CONFIG_PATH"
            fi
        fi
        for ((index=${#INSTALLED_PLUGINS[@]} - 1; index >= 0; index--)); do
            plugin=${INSTALLED_PLUGINS[index]}
            target="$HERMES_HOME/plugins/$plugin"
            rm -rf -- "$target"
            if [[ -e "$BACKUP_DIR/plugins/$plugin" || -L "$BACKUP_DIR/plugins/$plugin" ]]; then
                mv -- "$BACKUP_DIR/plugins/$plugin" "$target"
            fi
        done
    fi
    exit "$rc"
}
trap restore_on_failure EXIT

log "profile: $PROFILE"
log "Hermes home: $HERMES_HOME"
log "config backup: $BACKUP_DIR"

# Copy from the checked-out, reviewed tree. Hermes' Git installer accepts remote
# identifiers but not local source directories, and this repository contains two
# independent manifests. Each replacement is staged and can be restored on error.
mkdir -p -- "$HERMES_HOME/plugins" "$BACKUP_DIR/plugins"
for plugin in autonomy seed_memory; do
    log "installing plugin: $plugin"
    target="$HERMES_HOME/plugins/$plugin"
    ACTIVE_STAGE=$(mktemp -d "$HERMES_HOME/plugins/.${plugin}.install.XXXXXX")
    cp -a -- "$ROOT_DIR/plugins/$plugin/." "$ACTIVE_STAGE/"
    [[ -f "$ACTIVE_STAGE/plugin.yaml" ]] || die "staged $plugin is missing plugin.yaml"
    if [[ -e "$target" || -L "$target" ]]; then
        mv -- "$target" "$BACKUP_DIR/plugins/$plugin"
    fi
    INSTALLED_PLUGINS+=("$plugin")
    mv -- "$ACTIVE_STAGE" "$target"
    ACTIVE_STAGE=""
done

# Enabling through Hermes preserves other enabled plugins. It can modify config,
# which is why the complete config backup was created before plugin replacement.
CONFIG_TOUCHED=1
"$HERMES_BIN" -p "$PROFILE" plugins enable autonomy --no-allow-tool-override >/dev/null
"$HERMES_BIN" -p "$PROFILE" plugins enable seed_memory --no-allow-tool-override >/dev/null

config_set() {
    local key=$1 value=$2
    "$HERMES_BIN" -p "$PROFILE" config set "$key" "$value" --force >/dev/null
}

# Only plugin-owned keys are set. Provider credentials, model selection, SOUL.md,
# auth files, .env files, databases, sessions, and memories are never read or changed.
config_set memory.provider seed_memory
config_set autonomy.memory_provider seed_memory
config_set autonomy.in_process_loop false
config_set autonomy.data_dir "$HERMES_HOME/autonomy_data"
config_set autonomy.thought_log "$HERMES_HOME/autonomy_data/thoughts.log"
config_set autonomy.limits.daily_tokens 100000000
config_set autonomy.limits.daily_calls 500
config_set autonomy.limits.daily_prompt_tokens 100000000

if ((SKIP_ML)); then
    warn "ML installation skipped; seed_memory remains unavailable until tools/install_ml.sh succeeds"
else
    ML_INSTALLER=${ML_INSTALLER:-$ROOT_DIR/tools/install_ml.sh}
    [[ -x "$ML_INSTALLER" ]] || die "ML installer is not executable: $ML_INSTALLER"
    ml_args=(--profile "$PROFILE" --yes)
    HERMES_BIN="$HERMES_BIN" "$ML_INSTALLER" "${ml_args[@]}"
fi

systemd_escape_value() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//%/%%}
    printf '%s' "$value"
}

UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
UNIT_BASE="${PROFILE}-autonomy"
SERVICE_PATH="$UNIT_DIR/$UNIT_BASE.service"
TIMER_PATH="$UNIT_DIR/$UNIT_BASE.timer"
install_units=1
if [[ -e "$SERVICE_PATH" || -L "$SERVICE_PATH" || -e "$TIMER_PATH" || -L "$TIMER_PATH" ]]; then
    if ((FORCE_UNITS)); then
        mkdir -p -- "$BACKUP_DIR/systemd"
        [[ -e "$SERVICE_PATH" || -L "$SERVICE_PATH" ]] && cp -a -- "$SERVICE_PATH" "$BACKUP_DIR/systemd/"
        [[ -e "$TIMER_PATH" || -L "$TIMER_PATH" ]] && cp -a -- "$TIMER_PATH" "$BACKUP_DIR/systemd/"
        log "existing units backed up under $BACKUP_DIR/systemd"
    else
        warn "$UNIT_BASE units already exist; leaving both untouched (use --force to back up and replace)"
        install_units=0
    fi
fi

if ((install_units)); then
    mkdir -p -- "$UNIT_DIR"
    hermes_q=$(systemd_escape_value "$HERMES_BIN")
    profile_q=$(systemd_escape_value "$PROFILE")
    home_q=$(systemd_escape_value "$HERMES_HOME")
    service_tmp=$(mktemp "$UNIT_DIR/.${UNIT_BASE}.service.XXXXXX")
    cat > "$service_tmp" <<EOF
[Unit]
Description=Hermes autonomy tick for profile $profile_q
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment="HERMES_HOME=$home_q"
ExecStart="$hermes_q" -p "$profile_q" autonomy tick --hermes-home "$home_q"
TimeoutStartSec=3000

[Install]
WantedBy=default.target
EOF
    chmod 0644 "$service_tmp"
    mv -f -- "$service_tmp" "$SERVICE_PATH"
    log "wrote $SERVICE_PATH"

    if ((NO_TIMER == 0)); then
        timer_tmp=$(mktemp "$UNIT_DIR/.${UNIT_BASE}.timer.XXXXXX")
        cat > "$timer_tmp" <<EOF
[Unit]
Description=Run Hermes autonomy for profile $profile_q at minute 00 and 20

[Timer]
OnCalendar=*-*-* *:00/20:00
RandomizedDelaySec=30
Persistent=false
Unit=$UNIT_BASE.service

[Install]
WantedBy=timers.target
EOF
        chmod 0644 "$timer_tmp"
        mv -f -- "$timer_tmp" "$TIMER_PATH"
        log "wrote $TIMER_PATH"
    else
        log "--no-timer: timer file and timer state were left untouched"
    fi
fi

if ((install_units)); then
    SYSTEMCTL_BIN=${SYSTEMCTL_BIN:-$(command -v systemctl || true)}
    if [[ -z "$SYSTEMCTL_BIN" ]]; then
        warn "systemctl not found; units were written but not loaded"
    elif ! "$SYSTEMCTL_BIN" --user daemon-reload; then
        warn "systemctl --user is unavailable; run 'systemctl --user daemon-reload' in a user session"
    elif ((NO_TIMER == 0)); then
        if ! "$SYSTEMCTL_BIN" --user enable --now "$UNIT_BASE.timer"; then
            warn "timer was written but could not be enabled; enable it manually after fixing the user systemd session"
        fi
    fi
fi

log "running plugin doctors"
"$HERMES_BIN" -p "$PROFILE" plugins doctor autonomy --ci
"$HERMES_BIN" -p "$PROFILE" plugins doctor seed_memory --ci
if ! "$HERMES_BIN" -p "$PROFILE" autonomy doctor; then
    warn "autonomy doctor reported a runtime warning; plugin installation itself completed"
fi

SUCCESS=1
trap - EXIT
log "installation complete"
log "rollback copy of the original config is at $BACKUP_DIR"
if ((NO_TIMER == 0)); then
    log "timer: systemctl --user status $UNIT_BASE.timer"
fi
