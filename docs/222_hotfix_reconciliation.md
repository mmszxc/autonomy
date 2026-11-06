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
| `runner.py` | 外部 context rotate、L0 access snapshot、若干 `.bak` 链 | 主要是大量 `return`/移除 lineage/summary，疑似回退 | receipt gate、L0 source/no-action/P0 | **不整文件吸收**；只从 xiaoai 逐 hunk 取不冲突的 context rotation、access snapshot，并将 L0 结果适配为 P0 `git/recall/none`；xiaozhi 回退不吸收 |
| `policy.py`,`policy_eval.py`,`policy_reflect.py` | 旧 candidate active-store 语义 | 同 | candidate library、offline reflect | 直接上 P0（线上旧语义违反 P0 隔离） |
| `db.py`,`audit.py`,`budget.py`,`circuit.py` | 在线旧/热修版本 | profile 不同 | P0 schema/evidence/budget/circuit | 直接上 P0；禁止覆盖 DB 文件，migration 独立执行 |
| `gateway_client.py`,`thought_log.py`,`wizard.py` | xiaoai-only 修改 | 不同或基线 | 无/轻微 P0 | 逐 hunk 人工复核；默认不吸收，部署前 diff gate |
| `event_log.py`,`evidence_challenge.py`,`schema.py`,`validation.py` | 不存在 | 不存在 | P0 新文件 | 直接上 P0 |

## `.bak-*` 留档审计

- `runner.py.bak-bug2fix-*`, `-L0-*`, `-prefetch-fileload-*`, `-rlimit-*`, `-syncworker-*`：有效 runner 确有其后的 L0/access 与 external-context 类变更；但 xiaozhi 有独有回退。因此 `.bak` 不能视为可直接覆盖的 release，需按上表抽 hunk。
- `cli.py.bak-p0-*`, `cli.py.bak-p2-2-*`：有效 `cli.py` 已是这两轮之后版本；当前本地/P0 无需回灌旧备份。
- `cli_transport.py.bak-rlimit-*`、`journal.py.bak-reflection-*`、`prompt.py.bak-extra-rules-*`、`tick.py.bak-fixlimits-*`：作为差异审阅证据保留在线备份；其中 rlimit 需要合并，reflection/prompt 旧 candidate 语义不得回灌。

## 已吸收本地 commit

`<待提交>` — `fix: retain 222 /opt Hermes Life discovery hotfix`：将线上确认的 `/opt/hermes-life` Hermes home、agent 和 memory plugin 路径以 fallback 方式纳入 `config.py`/`memory_bridge.py`。不删除现有开发/legacy 路径。

## Runner 三方合并门（未完成前不得发包）

1. 从 xiaoai current 与对应 `.bak` 建 patch，仅选择：external context 的 rotate/clean、access snapshot 两种功能 hunk。
2. 将 access snapshot 映射到 P0 `_l0_detect_change()` 返回 `git/recall/none`，不得恢复 boolean API；保留 receipt 支撑 no-action 不递增和 surprise receipt gate。
3. 使用 xiaozhi 只作反例：任何删除 lineage/summary、过早 return、移除 timeout/receipt 的 hunk 均拒绝。
4. 对 merge 后 runner 增加 hotfix regression 测试（/opt 路径、L0 access change、external context rotate）并跑全套 pytest。
5. 上线包对每 profile 重新 `diff -u`；剩余差异必须逐项标为 profile config/data 或已拒绝 patch，绝不“静默覆盖”。
