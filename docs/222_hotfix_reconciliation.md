# 222 线上热修三方对账（P0 release gate）

核查日：2026-11-06；线上读取方式：`hermes@192.168.10.222` 只读 SSH/scp；未修改线上。基线为 P0 前 commit `c6cacf3`，目标为本地当前 HEAD（P0-A/P0-B）。线上目录为各 profile 的 `~/.hermes/profiles/<profile>/plugins/autonomy`。

## 结论与放行边界

**本地树不能声称逐字等于 xiaoai/xiaozhi：两 profile 本来就不同。** 统一 release 的正确含义是：保留经过验证的可移植生产路径热修，保留 P0 合同改动；丢弃 profile 特有/明显回退的 patch，并在部署前对两 profile 再做差异门禁。下表中“合并”项目已通过本 commit 吸收；runner 线上 patch 由于与 P0 L0/receipt 合同冲突，必须按方案人工三方合并，不能以整文件替换伪造完成。

| 文件 | xiaoai vs 基线 | xiaozhi vs 基线 | P0 vs 基线 | 决策 |
|---|---|---|---|---|
| `cli.py` | release 版本；保留 `.bak-p0/.bak-p2-2` 历史 | 同 xiaoai | 无实质 P0 | 直接上 P0；备份仅作取证 |
| `cli_transport.py` | `RLIMIT_AS=2GiB`，stderr 空输出回退 | 同 | P0 evidence/process 元数据 | **合并**：保留 rlimit + stderr 回退；P0 transport/evidence 不覆盖 |
| `config.py` | `/opt/hermes-life/hermes_home` 路径 | 无 `/opt` 路径 | P0 config/evaluation | **已吸收**：本地路径候选加入 `/opt` 再回退 legacy；P0 上线 |
| `memory_bridge.py` | `/opt/hermes-life/{app/hermes-agent,hermes-agent}` 和 plugin scan | 同 | P0 memory contract | **已吸收**：加入 `/opt` 候选/scan，并保留 legacy 路径/P0 |
| `runner.py` | 外部 context rotate、L0 access snapshot、若干 `.bak` 链 | 主要是大量 `return`/移除 lineage/summary，已裁决为回退 | receipt gate、L0 source/no-action/P0 | **已合并 xiaoai 两项 hunk**：external prefetch rotation、L0 `access` 证据源（仍保留 P0 receipt gate/no-action）；**已裁决 xiaozhi 回退不合并，以仓库为准覆盖** |
| `policy.py`,`policy_eval.py`,`policy_reflect.py` | 旧 candidate active-store 语义 | 同 | candidate library、offline reflect | 直接上 P0（线上旧语义违反 P0 隔离） |
| `db.py`,`audit.py`,`budget.py`,`circuit.py` | 在线旧/热修版本 | profile 不同 | P0 schema/evidence/budget/circuit | 直接上 P0；禁止覆盖 DB 文件，migration 独立执行 |
| `gateway_client.py`,`thought_log.py`,`wizard.py` | xiaoai-only 修改 | 不同或基线 | 无/轻微 P0 | 逐 hunk 人工复核；默认不吸收，部署前 diff gate |
| `event_log.py`,`evidence_challenge.py`,`schema.py`,`validation.py` | 不存在 | 不存在 | P0 新文件 | 直接上 P0 |

## `.bak-*` 留档审计

- `runner.py.bak-bug2fix-*`, `-L0-*`, `-prefetch-fileload-*`, `-rlimit-*`, `-syncworker-*`：有效 runner 确有其后的 L0/access 与 external-context 类变更；但 xiaozhi 有独有回退。因此 `.bak` 不能视为可直接覆盖的 release，需按上表抽 hunk。
- `cli.py.bak-p0-*`, `cli.py.bak-p2-2-*`：有效 `cli.py` 已是这两轮之后版本；当前本地/P0 无需回灌旧备份。
- `cli_transport.py.bak-rlimit-*`、`journal.py.bak-reflection-*`、`prompt.py.bak-extra-rules-*`、`tick.py.bak-fixlimits-*`：作为差异审阅证据保留在线备份；其中 rlimit 需要合并，reflection/prompt 旧 candidate 语义不得回灌。

## 已吸收本地 commit

`59c79b3` — `fix: retain 222 Hermes Life discovery hotfix`：将线上确认的 `/opt/hermes-life` Hermes home、agent 和 memory plugin 路径以 fallback 方式纳入 `config.py`/`memory_bridge.py`。不删除现有开发/legacy 路径。

`<本次提交>` — `fix: retain selected xiaoai runner hotfixes`：吸收 xiaoai 的 external-world prefetch rotation 与 access-count L0 snapshot；保留 P0 的 receipt gate、no-action 计数和 `git/recall/none` 行为。

## Runner 三方合并门（未完成前不得发包）

1. 从 xiaoai current 与对应 `.bak` 建 patch，仅选择：external context 的 rotate/clean、access snapshot 两种功能 hunk。
2. 已将 access snapshot 映射到 P0 `_l0_detect_change()` 的新 `access` 来源（保留字符串 API，未恢复 boolean）；receipt 支撑 no-action 不递增和 surprise receipt gate 均保留。
3. **Lead 预裁决**：xiaozhi 的删除 lineage/summary、过早 return、移除 timeout/receipt 的 hunk 均为被改坏的残留，拒绝合并；部署 diff 再见到它们标记“已裁决：以仓库为准”，不重新争议。
4. 对 merge 后 runner 增加 hotfix regression 测试（/opt 路径、L0 access change、external context rotate）并跑全套 pytest。
5. 上线包对每 profile 重新 `diff -u`；剩余差异必须逐项标为 profile config/data 或已拒绝 patch，绝不“静默覆盖”。
