#!/usr/bin/env bash
# 文件名为兼容旧部署保留；脚本本身是通用、只读的 autonomy 验证器。
set -u

PROFILE=${PROFILE:-default}
EXPECT_TIMER=${EXPECT_TIMER:-on}
TIMER_UNIT=${TIMER_UNIT:-xiaozhi-autonomy.timer}
HERMES_BIN=${HERMES_BIN:-hermes}

args=()
[ "$PROFILE" = default ] || args=(-p "$PROFILE")
pass=0
total=0
report() { total=$((total+1)); [ "$2" = PASS ] && pass=$((pass+1)); printf '%s: %s%s\n' "$1" "$2" "${3:+ | $3}"; }

if command -v "$HERMES_BIN" >/dev/null 2>&1; then
  report "1) hermes 可执行文件" PASS "$HERMES_BIN"
else
  report "1) hermes 可执行文件" FAIL "未找到"
fi

status=$($HERMES_BIN "${args[@]}" autonomy status 2>&1)
case "$status" in
  *parallel*|*dialog_first*) report "2) autonomy status" PASS "识别到运行模式" ;;
  *) report "2) autonomy status" FAIL "命令未返回已知模式" ;;
esac

for plugin in autonomy seed_memory; do
  if $HERMES_BIN "${args[@]}" plugins doctor "$plugin" >/dev/null 2>&1; then
    report "doctor $plugin" PASS
  else
    report "doctor $plugin" FAIL
  fi
done

active=$(systemctl --user is-active "$TIMER_UNIT" 2>/dev/null || true)
enabled=$(systemctl --user is-enabled "$TIMER_UNIT" 2>/dev/null || true)
if [ "$EXPECT_TIMER" = off ]; then
  [ "$active" = inactive ] && [ "$enabled" = disabled ] \
    && report "timer disabled" PASS \
    || report "timer disabled" FAIL "active=$active enabled=$enabled"
else
  [ "$active" = active ] && [ "$enabled" = enabled ] \
    && report "timer active" PASS \
    || report "timer active" FAIL "active=$active enabled=$enabled"
fi

printf 'SUMMARY: %d/%d PASS\n' "$pass" "$total"
[ "$pass" -eq "$total" ]
