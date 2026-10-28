#!/usr/bin/env bash
# 在临时目录验证 seed_memory 队列 worker；不会修改真实 profile。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
VENDOR_SOURCE=${VENDOR_SOURCE:-}
TMP=$(mktemp -d "${TMPDIR:-/tmp}/seed-worker-e2e.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

PROFILE="$TMP/profile"
PLUGIN="$PROFILE/plugins/seed_memory"
mkdir -p "$PROFILE/plugins" "$PROFILE/seed_data/seed_queue"
cp -a "$ROOT/plugins/seed_memory" "$PLUGIN"

if [ -n "$VENDOR_SOURCE" ]; then
  [ -d "$VENDOR_SOURCE/_vendor" ] && ln -s "$VENDOR_SOURCE/_vendor" "$PLUGIN/_vendor"
  [ -d "$VENDOR_SOURCE/_vendor_ml" ] && ln -s "$VENDOR_SOURCE/_vendor_ml" "$PLUGIN/_vendor_ml"
fi

cat > "$PROFILE/config.yaml" <<EOF
memory:
  provider: seed_memory
  seed_memory:
    source_root: $PLUGIN/seed_src
    data_dir: $PROFILE/seed_data
    db_path: $PROFILE/seed_data/seeds/seeds.db
    queue_dir: $PROFILE/seed_data/seed_queue
    write_enabled: true
EOF

export PYTHONPATH="$PROFILE/plugins:$PLUGIN/seed_src${PYTHONPATH:+:$PYTHONPATH}"
export HERMES_HOME="$PROFILE"

"$PYTHON_BIN" - <<'PY'
from seed_memory import SeedMemoryProvider

provider = SeedMemoryProvider()
provider.initialize(session_id="e2e", hermes_home=None, start_worker=False)
provider.on_session_end([
    {"role": "user", "content": "worker e2e memory"},
    {"role": "assistant", "content": "queue worker validation"},
])
assert list(provider._queue_dir.glob("*.json")), "queue item was not created"
print("queue_created", provider._queue_dir)
PY

"$PYTHON_BIN" -m seed_memory.worker --once

if find "$PROFILE/seed_data/seed_queue" -maxdepth 1 -name '*.json' -print -quit | grep -q .; then
  echo "queue item remains after worker" >&2
  exit 1
fi

echo "worker_e2e_ok"
