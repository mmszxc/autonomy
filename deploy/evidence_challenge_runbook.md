# Evidence challenge 调度部署/回滚一页纸（本地准备）

适用：Ubuntu 24、systemd `--user`、`xiaoai`/`xiaozhi` 双 profile。本文只供开发2执行；开发1未连接或修改 222。

## 部署前门禁

1. 在安全窗口（避开 tick 的 `:00/:20/:40` 前后 5 分钟）逐 profile 记录现有 autonomy plugin、service/timer 文件 SHA；本变更不得覆盖或修改它们。
2. 核验四个 challenge unit 文件与仓库版本、`PYTHONPATH`、profile DB 路径；确认 `evidence_challenges` migration 已随插件包发布。
3. 备份 unit 文件及对应 `autonomy.db`，确认 SQLite integrity check 通过。

## 安装与首次验收

```bash
systemctl --user daemon-reload
systemctl --user enable xiaoai-autonomy-evidence-challenge.timer
systemctl --user enable xiaozhi-autonomy-evidence-challenge.timer
systemctl --user start xiaoai-autonomy-evidence-challenge.timer
systemctl --user start xiaozhi-autonomy-evidence-challenge.timer
systemctl --user start xiaoai-autonomy-evidence-challenge.service
systemctl --user start xiaozhi-autonomy-evidence-challenge.service
systemctl --user status '*autonomy-evidence-challenge*'
```

`xiaoai` 定时在每小时 `:10`，`xiaozhi` 在每小时 `:50`，均不占用 tick 窗口。首次手动触发必须成功；随后分别核验：

```bash
sqlite3 "$XIAOAI_DB" 'pragma integrity_check; select count(*) from evidence_challenges; select status,count(*) from evidence_challenges group by status;'
sqlite3 "$XIAOZHI_DB" 'pragma integrity_check; select count(*) from evidence_challenges; select status,count(*) from evidence_challenges group by status;'
```

验收门禁：pending evidence 有 challenge 行；已知正确哈希为 `verified`，错误哈希为 `mismatched`，超过 `challenge_deadline` 为 `overdue`；重复手动触发不增加行数；`receipts`、tick 事件、budget ledger 等既有表仅保持原计数/合法新增；现有 autonomy service/timer SHA 不变；下一个自然 tick 为绿色且仍 observe-only。

## 回滚

仅回滚 challenge 调度，不回滚插件或数据库历史证据：

```bash
systemctl --user stop xiaoai-autonomy-evidence-challenge.timer xiaoai-autonomy-evidence-challenge.service
systemctl --user stop xiaozhi-autonomy-evidence-challenge.timer xiaozhi-autonomy-evidence-challenge.service
systemctl --user disable xiaoai-autonomy-evidence-challenge.timer xiaozhi-autonomy-evidence-challenge.timer
rm ~/.config/systemd/user/xiaoai-autonomy-evidence-challenge.{service,timer}
rm ~/.config/systemd/user/xiaozhi-autonomy-evidence-challenge.{service,timer}
systemctl --user daemon-reload
systemctl --user reset-failed
```

恢复备份的 unit（如有），再次核对原 autonomy service/timer SHA，并观察一个自然 tick：tick 必须仍为绿色。`evidence_challenges` 行和 L0/evidence 原始记录不得删除；若需撤销 migration，先由独立变更审批确认，不执行物理删除。
