#!/usr/bin/env bash
# P0 222 deployment v2.
#
# Release safety rule: the deployment window is fixed in this VERSIONED script
# to minute :25--:35 UTC.  If this gate conflicts with ANY oral or written
# approval, STOP and report it.  An operator MUST NOT edit this gate to make a
# deployment proceed; changing the window requires a reviewed new script
# version and a new approval.
#
# This program intentionally has no ERR trap.  Every mutating profile step is
# run as a checked transaction and has an explicit rollback path.
set -euo pipefail

SERVER=${SERVER:-hermes@192.168.10.222}
SSH_KEY=${SSH_KEY:-"$HOME/.ssh/hermes_server_key"}
RELEASE_ID=${RELEASE_ID:?set RELEASE_ID, e.g. 10e07eb}
ARTIFACT=${ARTIFACT:?set ARTIFACT to the local .tar.gz}
MANIFEST=${MANIFEST:?set MANIFEST to the local .tar.gz.manifest.sha256}
ARCHIVE_SHA256=${ARCHIVE_SHA256:?set ARCHIVE_SHA256}
MANIFEST_SHA256=${MANIFEST_SHA256:?set MANIFEST_SHA256}

ssh_cmd=(ssh -i "$SSH_KEY" -o BatchMode=yes -o ServerAliveInterval=30 "$SERVER")
scp_cmd=(scp -i "$SSH_KEY" -o BatchMode=yes)
remote_release="/home/hermes/releases/$RELEASE_ID"

[[ -f "$ARTIFACT" && -f "$MANIFEST" ]] || { echo "local artifact/manifest absent" >&2; exit 2; }
[[ "$(sha256sum "$ARTIFACT" | awk '{print $1}')" == "$ARCHIVE_SHA256" ]] || { echo "local artifact SHA mismatch" >&2; exit 2; }
[[ "$(sha256sum "$MANIFEST" | awk '{print $1}')" == "$MANIFEST_SHA256" ]] || { echo "local manifest SHA mismatch" >&2; exit 2; }

# Staging lives outside every profile plugins/ scanner.  It is harmless before
# the window; profile/config/DB mutation remains entirely in the remote gate.
"${ssh_cmd[@]}" "mkdir -p '$remote_release'"
"${scp_cmd[@]}" "$ARTIFACT" "$MANIFEST" "$SERVER:$remote_release/"

# Send all dynamic release data as environment assignments, not interpolated
# shell code.  The remote body has explicit rollback; never rely on errexit
# suppression, ERR traps, or an `if ! function` context.
"${ssh_cmd[@]}" \
  "RELEASE_ID='$RELEASE_ID' ARCHIVE_SHA256='$ARCHIVE_SHA256' MANIFEST_SHA256='$MANIFEST_SHA256' bash -s" <<'REMOTE'
set -u -o pipefail
REL="/home/hermes/releases/$RELEASE_ID"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

fail() { echo "DEPLOY_FAIL: $*" >&2; return 1; }
within_window() {
  local m
  m=$(date -u +%M)
  # Decimal conversion avoids bash's leading-zero octal interpretation.
  ((10#$m >= 25 && 10#$m <= 35))
}
rollback_profile() {
  local p=$1 root="/home/hermes/.hermes/profiles/$1" b
  b="$root/plugin-backups/$STAMP"
  echo "EXPLICIT_ROLLBACK profile=$p" >&2
  if [[ -d "$b/autonomy.pre-swap" ]]; then
    rm -rf "$root/plugins/autonomy" || return 1
    mv "$b/autonomy.pre-swap" "$root/plugins/autonomy" || return 1
  fi
  local old
  shopt -s nullglob
  for old in "$b"/autonomy.pre-p0-*; do mv "$old" "$root/plugins/" || return 1; done
  shopt -u nullglob
  if [[ -f "$b/config.pre-swap.yaml" ]]; then mv -f "$b/config.pre-swap.yaml" "$root/config.yaml" || return 1; fi
  rm -rf "$root/.autonomy-new-$STAMP" "$root/.config-new-$STAMP.yaml"
}
rollback_all() {
  # Always attempt both.  A rollback failure is surfaced to the operator.
  rollback_profile xiaozhi || true
  rollback_profile xiaoai || true
}
verify_canonical() {
  local root=$1
  python3 - "$root" "$REL/stage" <<'PY'
import hashlib, sys
from pathlib import Path
root = Path(sys.argv[1]) / 'plugins' / 'autonomy'
stage = Path(sys.argv[2])
expected = {}
for line in (stage.parent / f'autonomy-bundle-{stage.parent.name}.tar.gz.manifest.sha256').read_text(encoding='utf-8').splitlines():
    digest, path = line.split(' *', 1)
    prefix = './plugins/autonomy/'
    if path.startswith(prefix): expected[path[len(prefix):]] = digest
actual = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in root.rglob('*') if p.is_file()}
bad = [p for p in set(actual) | set(expected) if actual.get(p) != expected.get(p)]
print(f'canonical_files={len(actual)} expected={len(expected)} vendor={sum(x.startswith("_vendor/") for x in actual)} bad={len(bad)}')
raise SystemExit(bool(bad))
PY
}
verify_rotation() {
  local root=$1
  python3 - "$root" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
source = (root / 'plugins/autonomy/runner.py').read_text(encoding='utf-8')
config = (root / 'config.yaml').read_text(encoding='utf-8')
# Exact production bytes captured from 222 canonical rollback runner.py:
# "CrowdStrike、国际安全事件、技术基础设施、外部世界变化",
required_base = '"CrowdStrike、国际安全事件、技术基础设施、外部世界变化",'
rejected_drift = '"CrowdStrike、国际安全、国际安全事件、技术基础设施、外部世界变化",'
extras = [
    '光伏、半导体、政策、行业、A股、市场日报',
    '龙哥、投资、不亏钱、空仓、选股、政策意志',
    '外部文件、磁盘痕迹、真实动作、探针、反爬、Cloudflare',
]
ok = required_base in source and rejected_drift not in source and all(x in config for x in extras)
print(f'rotation_exact={required_base in source} rejected_variant={rejected_drift in source} extras={[x in config for x in extras]}')
raise SystemExit(not ok)
PY
}
validate_runtime() {
  local p=$1 root="/home/hermes/.hermes/profiles/$1"
  systemctl --user start "$p-autonomy.service" || return 1
  journalctl --user -u "$p-autonomy.service" -n 180 --no-pager -o short-iso | tee "$root/plugin-backups/$STAMP/post-tick.journal" || return 1
  grep -E "proposals.*\[\]" "$root/plugin-backups/$STAMP/post-tick.journal" >/dev/null || return 1
  python3 - "$root/autonomy_data/autonomy.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
events = c.execute("select kind,summary from events where kind in ('memory_prefetch_child','tick') order by rowid desc limit 10").fetchall()
for table in ('evidence', 'receipts', 'budget_ledger'):
    print(table, c.execute(f'select count(*) from {table}').fetchone()[0])
assert c.execute('select count(*) from evidence').fetchone()[0] > 0
assert c.execute('select count(*) from receipts').fetchone()[0] > 0
assert any('/plugins/autonomy' in str(s) and 'pre-p0' not in str(s) for k, s in events if k == 'memory_prefetch_child')
print('events', events)
PY
}
install_profile() {
  local p=$1 root="/home/hermes/.hermes/profiles/$1" backup="/home/hermes/backups/p0/$STAMP-$1"
  mkdir -p "$backup" "$root/plugin-backups/$STAMP" || return 1
  tar -czf "$backup/profile.before.tgz" -C "$root" plugins config.yaml autonomy_data state.db seed_data || return 1
  cp -a "$REL/stage/plugins/autonomy" "$root/.autonomy-new-$STAMP" || return 1
  if [[ "$p" == xiaoai ]]; then
    cp -a "$root/config.yaml" "$root/.config-new-$STAMP.yaml" || return 1
    python3 - "$root/.config-new-$STAMP.yaml" "$REL/stage/deploy/profiles/xiaoai.autonomy.fragment.yaml" <<'PY' || return 1
import sys, yaml
p, fragment = sys.argv[1:]
raw = open(p, encoding='utf-8').read()
assert 'rotation_extra_keywords:' not in raw and raw.count('autonomy:\n') == 1
words = yaml.safe_load(open(fragment, encoding='utf-8'))['autonomy']['rotation_extra_keywords']
open(p, 'w', encoding='utf-8').write(raw.replace('autonomy:\n', 'autonomy:\n  rotation_extra_keywords:\n' + ''.join('  - ' + x + '\n' for x in words), 1))
PY
  fi
  local old=("$root"/plugins/autonomy.pre-p0-*)
  [[ ${#old[@]} == 1 ]] || return 1
  mv "$root/plugins/autonomy" "$root/plugin-backups/$STAMP/autonomy.pre-swap" || return 1
  mv "${old[0]}" "$root/plugin-backups/$STAMP/" || return 1
  mv "$root/.autonomy-new-$STAMP" "$root/plugins/autonomy" || return 1
  if [[ "$p" == xiaoai ]]; then
    mv "$root/config.yaml" "$root/plugin-backups/$STAMP/config.pre-swap.yaml" || return 1
    mv "$root/.config-new-$STAMP.yaml" "$root/config.yaml" || return 1
  fi
  python3 - "$root/plugins" <<'PY' || return 1
import sys
from pathlib import Path
r = Path(sys.argv[1])
found = [d.name for d in r.iterdir() if (d/'plugin.yaml').is_file() and 'name: autonomy' in (d/'plugin.yaml').read_text(encoding='utf-8')]
print('autonomy manifests', found)
raise SystemExit(found != ['autonomy'])
PY
  verify_canonical "$root" || return 1
  [[ "$p" != xiaoai ]] || verify_rotation "$root" || return 1
  validate_runtime "$p" || return 1
}

within_window || { echo 'WINDOW_DENY :25-:35 UTC only' >&2; exit 1; }
[[ "$(systemctl --user is-active xiaoai-autonomy.service)" == inactive ]] || fail 'xiaoai unit active' || exit 1
[[ "$(systemctl --user is-active xiaozhi-autonomy.service)" == inactive ]] || fail 'xiaozhi unit active' || exit 1
cd "$REL" || exit 1
echo "$ARCHIVE_SHA256  autonomy-bundle-$RELEASE_ID.tar.gz" | sha256sum -c - || exit 1
echo "$MANIFEST_SHA256  autonomy-bundle-$RELEASE_ID.tar.gz.manifest.sha256" | sha256sum -c - || exit 1
rm -rf stage && mkdir stage && tar -xzf "autonomy-bundle-$RELEASE_ID.tar.gz" -C stage || exit 1
(cd stage && sha256sum -c "../autonomy-bundle-$RELEASE_ID.tar.gz.manifest.sha256") || exit 1
[[ $(find stage -type f | wc -l) == 1102 && $(find stage/plugins/autonomy/_vendor -type f | wc -l) == 956 ]] || exit 1

if ! install_profile xiaoai; then rollback_all; exit 1; fi
if ! install_profile xiaozhi; then rollback_all; exit 1; fi
echo "DEPLOY_SUCCESS $(date -u --iso-8601=seconds)"
REMOTE
