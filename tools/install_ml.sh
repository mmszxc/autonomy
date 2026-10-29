#!/usr/bin/env bash
set -Eeuo pipefail

PROFILE=""
ASSUME_YES=0
COMMIT_STARTED=0
HERMES_HOME=""
TEMP_ROOT=""
BACKUP_DIR=""
OLD_VENDOR=0
OLD_MODEL=0
CONFIG_EXISTED=0
CONFIG_TOUCHED=0

# These versions were verified together on the supported CPU-only deployment.
TORCH_VERSION="2.14.1+cpu"
SENTENCE_TRANSFORMERS_VERSION="6.2.0"
MODEL_ID="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REVISION="e8f8c211226b894fcb81acc59f3b34ba3efd5f42"

usage() {
    cat <<'EOF'
Usage: tools/install_ml.sh --profile NAME [--yes]

Install pinned CPU ML dependencies and a pinned MiniLM snapshot into an
existing Hermes profile. This script never uses sudo or modifies the Hermes
virtual environment.

Options:
  --profile NAME  Existing Hermes profile (required)
  --yes           Skip the large-download confirmation
  -h, --help      Show this help

Environment overrides:
  HERMES_BIN      Hermes executable
  HERMES_PYTHON   Python matching Hermes (normally inferred beside HERMES_BIN)
  PIP_INDEX_URL   Python package index (default: https://pypi.org/simple)
  TORCH_INDEX_URL CPU torch extra index
EOF
}

log() { printf '[install-ml] %s\n' "$*"; }
warn() { printf '[install-ml] WARNING: %s\n' "$*" >&2; }
die() { printf '[install-ml] ERROR: %s\n' "$*" >&2; exit 1; }

while (($#)); do
    case "$1" in
        --profile)
            (($# >= 2)) || die "--profile requires a value"
            PROFILE=$2
            shift 2
            ;;
        --yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
[[ -n "$PROFILE" ]] || { usage >&2; die "--profile is required"; }
[[ "$PROFILE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid profile name: $PROFILE"

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
    die "profile '$PROFILE' does not exist; refusing to create it"
fi
while IFS= read -r line; do
    case "$line" in
        Path:*)
            HERMES_HOME=${line#Path:}
            HERMES_HOME=${HERMES_HOME#"${HERMES_HOME%%[![:space:]]*}"}
            ;;
    esac
done <<< "$profile_output"
[[ -n "$HERMES_HOME" && -d "$HERMES_HOME" ]] || die "could not resolve the existing profile path"
HERMES_HOME=$(CDPATH= cd -- "$HERMES_HOME" && pwd -P)

if [[ -n ${HERMES_PYTHON:-} ]]; then
    PYTHON=$HERMES_PYTHON
else
    hermes_dir=$(CDPATH= cd -- "$(dirname -- "$HERMES_BIN")" && pwd -P)
    if [[ -x "$hermes_dir/python" ]]; then
        PYTHON="$hermes_dir/python"
    elif [[ -x "$hermes_dir/python3" ]]; then
        PYTHON="$hermes_dir/python3"
    else
        die "cannot infer the Hermes Python; set HERMES_PYTHON to its interpreter"
    fi
fi
[[ -x "$PYTHON" ]] || die "Hermes Python is not executable: $PYTHON"
"$PYTHON" -m pip --version >/dev/null || die "pip is unavailable in the selected Hermes Python"

VENDOR_DIR="$HERMES_HOME/plugins/_vendor_ml"
MODEL_DIR="$HERMES_HOME/models/paraphrase-multilingual-MiniLM-L12-v2"
MARKER="$VENDOR_DIR/.autonomy-ml-versions"
EXPECTED_MARKER=$(cat <<EOF
torch=$TORCH_VERSION
sentence-transformers=$SENTENCE_TRANSFORMERS_VERSION
model=$MODEL_ID
revision=$MODEL_REVISION
EOF
)

already_current=0
if [[ -f "$MARKER" && -f "$MODEL_DIR/modules.json" && -f "$MODEL_DIR/model.safetensors" ]]; then
    current_marker=$(cat -- "$MARKER")
    [[ "$current_marker" == "$EXPECTED_MARKER" ]] && already_current=1
fi

if ((already_current == 0 && ASSUME_YES == 0)); then
    printf '%s\n' \
        "This downloads pinned CPU PyTorch, sentence-transformers, their wheels," \
        "and a pinned multilingual MiniLM model. Several GiB of free disk may be needed." \
        "Nothing is installed into the system Python or Hermes virtual environment."
    if [[ ! -t 0 ]]; then
        die "confirmation is required in non-interactive mode; rerun with --yes"
    fi
    read -r -p "Continue with the ML download? [y/N] " reply
    [[ "$reply" == y || "$reply" == Y || "$reply" == yes || "$reply" == YES ]] || die "cancelled"
fi

umask 077
BACKUP_DIR="$HERMES_HOME/.autonomy-install-backups/ml-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p -- "$BACKUP_DIR"
CONFIG_PATH="$HERMES_HOME/config.yaml"
if [[ -f "$CONFIG_PATH" || -L "$CONFIG_PATH" ]]; then
    cp -a -- "$CONFIG_PATH" "$BACKUP_DIR/config.yaml"
    CONFIG_EXISTED=1
else
    : > "$BACKUP_DIR/config.yaml.absent"
fi

rollback() {
    local rc=$?
    if ((rc != 0)); then
        warn "ML installation failed; cleaning the staging directory"
        if ((COMMIT_STARTED)); then
            rm -rf -- "$VENDOR_DIR" "$MODEL_DIR"
            ((OLD_VENDOR)) && mv -- "$BACKUP_DIR/_vendor_ml" "$VENDOR_DIR"
            ((OLD_MODEL)) && mv -- "$BACKUP_DIR/paraphrase-multilingual-MiniLM-L12-v2" "$MODEL_DIR"
        fi
        if ((CONFIG_TOUCHED)); then
            if ((CONFIG_EXISTED)); then
                rm -f -- "$CONFIG_PATH"
                cp -a -- "$BACKUP_DIR/config.yaml" "$CONFIG_PATH"
            else
                rm -f -- "$CONFIG_PATH"
            fi
        fi
    fi
    [[ -n "$TEMP_ROOT" ]] && rm -rf -- "$TEMP_ROOT"
    exit "$rc"
}
trap rollback EXIT

verify_offline() {
    local vendor=$1 model=$2
    PYTHONPATH="$vendor${PYTHONPATH:+:$PYTHONPATH}" \
        HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false \
        "$PYTHON" - "$model" <<'PY'
import sys
from sentence_transformers import SentenceTransformer
model = SentenceTransformer(sys.argv[1], local_files_only=True, device="cpu")
vectors = model.encode(["安装验证", "离线向量"], show_progress_bar=False)
if tuple(vectors.shape) != (2, 384):
    raise SystemExit("unexpected embedding shape: %r" % (tuple(vectors.shape),))
print("offline embedding verified: shape=(2, 384)")
PY
}

if ((already_current)); then
    log "pinned dependencies and model are already present; verifying offline"
    verify_offline "$VENDOR_DIR" "$MODEL_DIR"
    CONFIG_TOUCHED=1
    "$HERMES_BIN" -p "$PROFILE" config set memory.seed_memory.embedding_model_path "$MODEL_DIR" --force >/dev/null
    trap - EXIT
    log "ML installation is already current; config backup: $BACKUP_DIR"
    exit 0
fi

available_kb=$(df -Pk "$HERMES_HOME" | awk 'NR==2 {print $4}')
if [[ "$available_kb" =~ ^[0-9]+$ ]] && ((available_kb < 3145728)); then
    warn "less than 3 GiB is free; installation may fail"
fi

TEMP_ROOT=$(mktemp -d "$HERMES_HOME/.autonomy-ml-stage.XXXXXX")
mkdir -p -- "$TEMP_ROOT/vendor" "$TEMP_ROOT/model"

PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.org/simple}
TORCH_INDEX_URL=${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}
log "downloading pinned CPU dependencies into a staging directory"
"$PYTHON" -m pip install \
    --disable-pip-version-check --no-input --only-binary=:all: \
    --index-url "$PIP_INDEX_URL" --extra-index-url "$TORCH_INDEX_URL" \
    --target "$TEMP_ROOT/vendor" \
    "torch==$TORCH_VERSION" \
    "sentence-transformers==$SENTENCE_TRANSFORMERS_VERSION" \
    "PyYAML==6.0.3" \
    "httpx2==2.16.0" \
    "narwhals==2.28.0"

log "downloading pinned model revision $MODEL_REVISION"
PYTHONPATH="$TEMP_ROOT/vendor${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" - "$TEMP_ROOT/model" "$MODEL_REVISION" <<'PY'
import sys
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    revision=sys.argv[2],
    local_dir=sys.argv[1],
    allow_patterns=["*.json", "*.model", "model.safetensors", "1_Pooling/*"],
)
PY
printf '%s\n' "$EXPECTED_MARKER" > "$TEMP_ROOT/vendor/.autonomy-ml-versions"
verify_offline "$TEMP_ROOT/vendor" "$TEMP_ROOT/model"

# Commit only after both staged artifacts pass the offline test. Existing copies
# are moved, never deleted, until the replacement and config update both succeed.
mkdir -p -- "$(dirname -- "$VENDOR_DIR")" "$(dirname -- "$MODEL_DIR")"
COMMIT_STARTED=1
if [[ -e "$VENDOR_DIR" ]]; then
    mv -- "$VENDOR_DIR" "$BACKUP_DIR/_vendor_ml"
    OLD_VENDOR=1
fi
if [[ -e "$MODEL_DIR" ]]; then
    mv -- "$MODEL_DIR" "$BACKUP_DIR/paraphrase-multilingual-MiniLM-L12-v2"
    OLD_MODEL=1
fi
mv -- "$TEMP_ROOT/vendor" "$VENDOR_DIR"
mv -- "$TEMP_ROOT/model" "$MODEL_DIR"
CONFIG_TOUCHED=1
"$HERMES_BIN" -p "$PROFILE" config set memory.seed_memory.embedding_model_path "$MODEL_DIR" --force >/dev/null
verify_offline "$VENDOR_DIR" "$MODEL_DIR"

COMMIT_STARTED=0
trap - EXIT
rm -rf -- "$TEMP_ROOT"
log "ML installation complete"
log "model revision: $MODEL_REVISION"
log "config backup: $BACKUP_DIR"
if ((OLD_VENDOR || OLD_MODEL)); then
    log "previous ML artifacts were preserved at $BACKUP_DIR"
fi
