#!/usr/bin/env bash
# 文件名为兼容既有流程保留；脚本可在任意临时 Linux 测试目录运行。
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
TARGET=${PYTEST_TARGET:-"$ROOT/.pytest-target"}

rm -rf "$TARGET"
"$PYTHON_BIN" -m pip install --quiet --target "$TARGET" pytest PyYAML
PYTHONPATH="$TARGET${PYTHONPATH:+:$PYTHONPATH}" \
  "$PYTHON_BIN" -m pytest "$ROOT/tests" -q

if [ "${KEEP_PYTEST_TARGET:-0}" != 1 ]; then
  rm -rf "$TARGET"
fi
