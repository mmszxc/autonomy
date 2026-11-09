#!/usr/bin/env bash
# Interactive, fail-closed profile deployment draft for Ubuntu 24 / Hermes v0.22.x.
# It never replaces a whole profile and never touches state.db, gateway.lock, logs, or runtime.
set -Eeuo pipefail
umask 077

say() { printf '[install-profile] %s\n' "$*"; }
die() {
  say "ERROR: $*" >&2
  if declare -F rollback >/dev/null && [[ -n ${BACKUP_ROOT:-} ]] && (( ${COMMITTED:-0} == 0 )); then rollback 1; fi
  exit 1
}
ask() { local var=$1 prompt=$2 default=${3-} value; read -r -p "$prompt${default:+ [$default]}: " value; printf -v "$var" '%s' "${value:-$default}"; }
valid_name() { [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; }
valid_minute() { [[ $1 =~ ^[0-9]+$ ]] && (( 10#$1 >= 0 && 10#$1 <= 59 )); }
record() { printf '%s\t%s\n' "$(date -u +%FT%TZ)" "$*" >>"$AUDIT"; }
run() { record "RUN $*"; "$@" >>"$RUN_LOG" 2>&1; local rc=$?; record "RC=$rc $*"; return "$rc"; }

[[ $(uname -s) == Linux ]] || die 'This installer supports Linux only.'
command -v git >/dev/null || die 'git is required.'
command -v python3 >/dev/null || die 'python3 is required.'
command -v sqlite3 >/dev/null || die 'sqlite3 is required.'
command -v systemctl >/dev/null || die 'systemd user units are required.'
REPO=$(git rev-parse --show-toplevel 2>/dev/null) || die 'Run inside the autonomy Git checkout.'
cd "$REPO"
TARGET_COMMIT=$(git rev-parse HEAD)
git diff --quiet && git diff --cached --quiet || die 'Checkout must have no tracked modifications.'

ask PROFILE 'Existing Hermes profile name'
valid_name "$PROFILE" || die 'Invalid profile name.'
ask HERMES_HOME 'Hermes home' "$HOME/.hermes"
PROFILE_DIR="$HERMES_HOME/profiles/$PROFILE"
[[ -d $PROFILE_DIR ]] || die "Profile does not exist: $PROFILE_DIR"
ask HERMES_BIN 'Absolute hermes CLI path' '/home/hermes/.local/bin/hermes'
[[ -x $HERMES_BIN ]] || die "Hermes CLI is not executable: $HERMES_BIN"
ask MODEL 'model.default' '<MODEL_NAME>'
ask PROVIDER_KIND 'Provider type (custom/builtin)' 'custom'
ask PROVIDER 'Provider name' '<CUSTOM_PROVIDER>'
ask TAGGER_MODEL 'seed_memory tagger model (Enter follows model.default)' "$MODEL"
ask TAGGER_PROVIDER 'seed_memory tagger provider (Enter follows provider)' "$PROVIDER"
ask BASE_URL 'Provider base_url' 'https://<MODEL_GATEWAY>/v1'
ask KEY_ENV 'API-key environment variable name' '<MODEL_API_KEY_ENV>'
[[ $KEY_ENV =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die 'key_env must be a shell environment-variable name.'
ask VIEWER_PORT 'Viewer port' '8765'
[[ $VIEWER_PORT =~ ^[0-9]+$ ]] && (( VIEWER_PORT > 0 && VIEWER_PORT < 65536 )) || die 'Invalid port.'
ask TICK_MINUTE 'Autonomy timer minute (0-59)' '7'
ask CHALLENGE_MINUTE 'Challenge timer minute (0-59)' '17'
valid_minute "$TICK_MINUTE" && valid_minute "$CHALLENGE_MINUTE" || die 'Invalid stagger minute.'
[[ $TICK_MINUTE != "$CHALLENGE_MINUTE" ]] || die 'Tick and challenge minutes must differ.'
ask P0_MODE 'P0 mode' 'observe_only'
[[ $P0_MODE == observe_only || $P0_MODE == active ]] || die 'P0 mode must be observe_only or active.'

printf 'API key for %s (input hidden; never logged): ' "$KEY_ENV" >&2
IFS= read -r -s API_KEY
printf '\n' >&2
[[ -n $API_KEY ]] || die 'API key must not be empty.'

TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_ROOT="$HERMES_HOME/deploy_backups/${PROFILE}-interactive-install-$TS"
AUDIT="$BACKUP_ROOT/audit.tsv"
RUN_LOG="$BACKUP_ROOT/commands.log"
mkdir -p "$BACKUP_ROOT/files" "$BACKUP_ROOT/was-absent"
: >"$AUDIT"; : >"$RUN_LOG"
record "commit=$TARGET_COMMIT profile=$PROFILE (credential omitted)"

CONFIG="$PROFILE_DIR/config.yaml"
ENV_FILE="$PROFILE_DIR/.env"
USER_UNITS="$HOME/.config/systemd/user"
mkdir -p "$USER_UNITS"
ITEMS=(
  "$CONFIG"
  "$ENV_FILE"
  "$PROFILE_DIR/plugins/autonomy"
  "$PROFILE_DIR/plugins/seed_memory"
  "$PROFILE_DIR/autonomy_data"
  "$USER_UNITS/$PROFILE-autonomy.service"
  "$USER_UNITS/$PROFILE-autonomy.timer"
  "$USER_UNITS/$PROFILE-autonomy-evidence-challenge.service"
  "$USER_UNITS/$PROFILE-autonomy-evidence-challenge.timer"
  "$USER_UNITS/$PROFILE-viewer.service"
)
for item in "${ITEMS[@]}"; do
  tag=$(printf '%s' "$item" | sha256sum | cut -d' ' -f1)
  if [[ -e $item ]]; then cp -a -- "$item" "$BACKUP_ROOT/files/$tag"; printf '%s\n' "$item" >"$BACKUP_ROOT/files/$tag.path"
  else printf '%s\n' "$item" >"$BACKUP_ROOT/was-absent/$tag.path"
  fi
done
[[ -d $PROFILE_DIR/autonomy_data ]] && AUTONOMY_DATA_PREEXISTED=1 || AUTONOMY_DATA_PREEXISTED=0

GATEWAY_PID_BEFORE=$(pgrep -f 'gateway run' | head -n1 || true)
ERRORS="$PROFILE_DIR/logs/errors.log"
ERROR_SIZE_BEFORE=0; [[ -f $ERRORS ]] && ERRORS_SIZE_BEFORE=$(stat -c %s "$ERRORS")
COMMITTED=0
rollback() {
  local rc=${1:-$?}
  (( COMMITTED == 1 )) && return "$rc"
  trap - ERR INT TERM
  say "Failure detected (rc=$rc); rolling back individual artifacts from $BACKUP_ROOT"
  for pf in "$BACKUP_ROOT"/files/*.path; do
    [[ -e $pf ]] || continue; target=$(cat "$pf"); tag=${pf%.path}; rm -rf -- "$target"; cp -a -- "$tag" "$target"
  done
  for pf in "$BACKUP_ROOT"/was-absent/*.path; do [[ -e $pf ]] && rm -rf -- "$(cat "$pf")"; done
  if (( AUTONOMY_DATA_PREEXISTED == 0 )); then rm -rf -- "$PROFILE_DIR/autonomy_data"; fi
  systemctl --user daemon-reload >>"$RUN_LOG" 2>&1 || true
  "$HERMES_BIN" -p "$PROFILE" gateway restart -y >>"$RUN_LOG" 2>&1 || true
  record "ROLLBACK original_rc=$rc"
  exit "$rc"
}
trap rollback ERR INT TERM

install_plugin_from_git() {
  local plugin=$1 prefix="plugins/$1/" stage dest rel blob got
  stage=$(mktemp -d "$PROFILE_DIR/plugins/.${plugin}.stage.XXXXXX")
  while IFS=$'\t' read -r meta path; do
    [[ $path == "$prefix"* ]] || continue
    blob=${meta##* }; rel=${path#"$prefix"}; mkdir -p "$stage/$(dirname "$rel")"
    git cat-file blob "$blob" >"$stage/$rel"
    got=$(git hash-object "$stage/$rel"); [[ $got == "$blob" ]] || die "Blob mismatch: $path"
  done < <(git ls-tree -r "$TARGET_COMMIT" -- "$prefix")
  [[ -f $stage/manifest.json ]] || die "Missing $plugin manifest in commit."
  dest="$PROFILE_DIR/plugins/$plugin"; old="$PROFILE_DIR/plugins/.${plugin}.old.$TS"
  [[ -e $dest ]] && mv -- "$dest" "$old"
  mv -- "$stage" "$dest"
  rm -rf -- "$old"
  record "installed plugin=$plugin via verified Git blobs"
}
install_plugin_from_git autonomy
install_plugin_from_git seed_memory

# PyYAML preserves data, not comments. Refuse to continue if it is unavailable.
CONFIG="$CONFIG" PROFILE="$PROFILE" MODEL="$MODEL" PROVIDER_KIND="$PROVIDER_KIND" PROVIDER="$PROVIDER" TAGGER_MODEL="$TAGGER_MODEL" TAGGER_PROVIDER="$TAGGER_PROVIDER" BASE_URL="$BASE_URL" KEY_ENV="$KEY_ENV" P0_MODE="$P0_MODE" python3 <<'PY'
import os, pathlib, yaml
p=pathlib.Path(os.environ['CONFIG']); d=yaml.safe_load(p.read_text()) or {}
profile=os.environ['PROFILE']; provider=os.environ['PROVIDER']; key=os.environ['KEY_ENV']
plugins=d.setdefault('plugins',{}); enabled=plugins.setdefault('enabled',[])
for name in ('autonomy','seed_memory'):
    if name not in enabled: enabled.append(name)
model=d.setdefault('model',{}); model.update(default=os.environ['MODEL'], provider=provider,
    base_url=os.environ['BASE_URL'], api_key='${'+key+'}')
if os.environ['PROVIDER_KIND']=='custom':
    providers=d.setdefault('custom_providers',[])
    item=next((x for x in providers if x.get('name')==provider),None)
    if item is None: item={'name':provider}; providers.append(item)
    item.update(provider='custom', base_url=os.environ['BASE_URL'], key_env=key, model=os.environ['MODEL'])
root=str(p.parent)
mem=d.setdefault('memory',{}); mem['provider']='seed_memory'
sm=mem.setdefault('seed_memory',{})
sm['db_path']=root+'/seed_data/seeds/seeds.db'
sm['queue_dir']=root+'/seed_data/seed_queue'
sm['source_root']=root+'/plugins/seed_memory/seed_src'
sm['write_enabled']=True
# Blank answers are normalized by the shell prompts to the main model/provider.
sm['tagger_model']=os.environ['TAGGER_MODEL']
sm['tagger_provider']=os.environ['TAGGER_PROVIDER']
a=d.setdefault('autonomy',{}); a['data_dir']=root+'/autonomy_data'
paths=a.setdefault('paths',{}); paths.setdefault('seed_db',sm['db_path']); paths.setdefault('seed_queue',sm['queue_dir'])
a.setdefault('p0',{})['mode']=os.environ['P0_MODE']
tmp=p.with_name('.config.yaml.install.tmp'); tmp.write_text(yaml.safe_dump(d,sort_keys=False)); tmp.replace(p)
PY

# Replace only the selected key; do not echo it.
tmp=$(mktemp "$PROFILE_DIR/.env.install.XXXXXX")
if [[ -f $ENV_FILE ]]; then grep -v -E "^${KEY_ENV}=" "$ENV_FILE" >"$tmp" || true; fi
printf '%s=%s\n' "$KEY_ENV" "$API_KEY" >>"$tmp"; API_KEY=''; chmod 600 "$tmp"; mv -- "$tmp" "$ENV_FILE"

CONFIG="$CONFIG" ENV_FILE="$ENV_FILE" MODEL="$MODEL" PROVIDER="$PROVIDER" BASE_URL="$BASE_URL" KEY_ENV="$KEY_ENV" python3 <<'PY'
import os,re,yaml
c=yaml.safe_load(open(os.environ['CONFIG'])); m=c['model']; key=os.environ['KEY_ENV']
assert m['default']==os.environ['MODEL'] and m['provider']==os.environ['PROVIDER']
assert m['base_url']==os.environ['BASE_URL'] and m['api_key']=='${'+key+'}'
assert {'autonomy','seed_memory'} <= set(c['plugins']['enabled'])
assert c['memory']['provider']=='seed_memory'
assert c['memory']['seed_memory']['write_enabled'] is True
assert re.search(r'^'+re.escape(key)+r'=.+$',open(os.environ['ENV_FILE']).read(),re.M)
print('static gate: OK')
PY

# Write bytes locally on Linux; never transfer units through a text-converting pipe.
cat >"$USER_UNITS/$PROFILE-autonomy.service" <<EOF
[Unit]
Description=Hermes autonomy tick ($PROFILE)
[Service]
Type=oneshot
Environment=HERMES_HOME=$HERMES_HOME
WorkingDirectory=$PROFILE_DIR
ExecStart=$HERMES_BIN -p $PROFILE autonomy tick --hermes-home $HERMES_HOME --json
EOF
cat >"$USER_UNITS/$PROFILE-autonomy.timer" <<EOF
[Unit]
Description=Hermes autonomy timer ($PROFILE)
[Timer]
OnCalendar=*-*-* *:$TICK_MINUTE:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
cat >"$USER_UNITS/$PROFILE-autonomy-evidence-challenge.service" <<EOF
[Unit]
Description=Hermes evidence challenge ($PROFILE)
[Service]
Type=oneshot
Environment=HERMES_HOME=$HERMES_HOME
WorkingDirectory=$PROFILE_DIR
ExecStart=$HERMES_BIN -p $PROFILE autonomy challenge --hermes-home $HERMES_HOME --json
EOF
cat >"$USER_UNITS/$PROFILE-autonomy-evidence-challenge.timer" <<EOF
[Unit]
Description=Hermes evidence challenge timer ($PROFILE)
[Timer]
OnCalendar=*-*-* *:$CHALLENGE_MINUTE:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
cat >"$USER_UNITS/$PROFILE-viewer.service" <<EOF
[Unit]
Description=Hermes autonomy read-only viewer ($PROFILE)
[Service]
ExecStart=/usr/bin/python3 $REPO/tools/viewer/viewer.py --profile $PROFILE --port $VIEWER_PORT
Restart=on-failure
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

RESTART_OUT="$BACKUP_ROOT/gateway-restart.out"
"$HERMES_BIN" -p "$PROFILE" gateway restart -y >"$RESTART_OUT" 2>&1
 grep -Eq 'restarted|restart.*profile|ACK' "$RESTART_OUT" || die 'No per-profile restart ACK.'
GATEWAY_PID_AFTER=$(pgrep -f 'gateway run' | head -n1 || true)
[[ -n $GATEWAY_PID_BEFORE && $GATEWAY_PID_AFTER == "$GATEWAY_PID_BEFORE" ]] || die 'Shared gateway PID changed.'
if [[ -f $ERRORS ]]; then
  tail -c +$((ERROR_SIZE_BEFORE+1)) "$ERRORS" | grep -q StateDbReplacedError && die 'New StateDbReplacedError.' || true
fi

PLUGINS_OUT="$BACKUP_ROOT/plugins-list.out"
"$HERMES_BIN" -p "$PROFILE" plugins list >"$PLUGINS_OUT" 2>&1
for name in autonomy seed_memory; do grep -Ei "$name.*(enabled|user)|(enabled|user).*$name" "$PLUGINS_OUT" >/dev/null || die "$name is not enabled/user"; done
"$HERMES_BIN" -p "$PROFILE" autonomy --help >>"$RUN_LOG" 2>&1
# Registered/activated may be visible in plugin output or profile logs.
grep -Eqi 'seed_memory.*(registered|activated|enabled|user)' "$PLUGINS_OUT" "$PROFILE_DIR"/logs/*.log 2>/dev/null || die 'No seed_memory registered/activated evidence.'

TICK_LOG="$BACKUP_ROOT/first-tick.out"
nohup "$HERMES_BIN" -p "$PROFILE" autonomy tick --json >"$TICK_LOG" 2>&1 & TICK_PID=$!
wait "$TICK_PID"
grep -Eq '"(exit_reason|status)"[[:space:]]*:[[:space:]]*"ok"' "$TICK_LOG" || die 'First tick was not OK.'
grep -F "$MODEL" "$TICK_LOG" >/dev/null || die 'Tick output does not confirm configured model.'
grep -F "$PROVIDER" "$TICK_LOG" >/dev/null || die 'Tick output does not confirm configured provider.'

DB="$PROFILE_DIR/autonomy_data/autonomy.db"
[[ -f $DB ]] || die 'autonomy.db was not created.'
for table in schema_meta events receipts evidence evidence_challenges budget_ledger; do
  sqlite3 -readonly "$DB" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='$table';" | grep -q 1 || die "Missing DB table: $table"
done
[[ -f $PROFILE_DIR/seed_data/seeds/seeds.db ]] || die 'seeds.db was not created.'
systemctl --user start "$PROFILE-viewer.service"
for _ in {1..15}; do code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$VIEWER_PORT/" || true); [[ $code == 200 ]] && break; sleep 1; done
[[ ${code:-000} == 200 ]] || die "Viewer did not return HTTP 200 (got ${code:-000})."

systemctl --user enable --now "$PROFILE-autonomy.timer" "$PROFILE-autonomy-evidence-challenge.timer" "$PROFILE-viewer.service"
COMMITTED=1
trap - ERR INT TERM
record 'INSTALL_COMMITTED all gates green'
say "SUCCESS: profile=$PROFILE commit=$TARGET_COMMIT backup/audit=$BACKUP_ROOT"
