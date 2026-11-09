# autonomy —— 让 Hermes 在没人说话时，也会自己醒来想一想

**autonomy** 是 Hermes Agent 的“独处思考”插件：由定时器触发，每轮回顾记忆、进行自主思考，并把过程写入本地日志。配套的 **seed_memory** 插件把经历保存为带情绪和重要度的“种子”，供后续轮次检索。

> **状态**：持续迭代中，最近更新 2026-11-09。已在 Hermes v0.22.5 + Ubuntu 24 上验证，面向 Hermes v0.22.x。

## 快速开始

安装器只接受已经存在的 Hermes profile；不会创建 profile，也不会修改数据库、`SOUL.md`、认证文件或环境文件。

```bash
# 1. 获取代码
git clone https://github.com/mmszxc/autonomy.git && cd autonomy

# 2. 安装插件、CPU 依赖/模型和用户级定时器
./install.sh --profile xiaozhi

# 3. 验证安装
hermes -p xiaozhi autonomy status && systemctl --user status xiaozhi-autonomy.timer
```

安装后的下一步：

```bash
hermes -p xiaozhi autonomy tick
systemctl --user status xiaozhi-autonomy.timer
```

安装选项速查：`--yes` 用于自动化确认，`--skip-ml` 只安装插件，`--no-timer` 不写入或启用定时器，`--force` 仅在确认备份并替换已有 unit 时使用。大体积依赖默认要求交互确认。

安装器不会覆盖 profile 中的数据库、身份文件、认证文件或环境文件。手工安装、固定模型版本、离线验证、systemd 与回滚说明见 [`docs/install_ubuntu24_profile_xiaozhi.md`](docs/install_ubuntu24_profile_xiaozhi.md)。仓库不包含模型权重或大体积 Python vendor。

## Profile 安装部署（交互式向导）

多 profile 生产部署建议使用 [`scripts/install_profile.sh`](scripts/install_profile.sh)。它自行逐项询问 profile、模型、provider、endpoint、密钥环境变量名、viewer 端口以及 tick/challenge 错峰分钟，不调用 Hermes 自带的交互式 `autonomy init`（无人值守时该命令会卡在模型选择）。执行前必须确认：目标主机已授权、Hermes Agent 正在运行，且本仓库已 checkout 到准备部署的提交。非 login shell 可能找不到 `hermes`，向导默认使用 `/home/hermes/.local/bin/hermes`，也可在询问时指定绝对路径。

```bash
# 示例值均为占位符；密钥通过隐藏输入写入 profile-local .env
./scripts/install_profile.sh
```

向导按以下门禁顺序执行，任一步失败立即停止并逐项回滚：

1. 收集 profile、`model.default`、provider/custom provider、`base_url`、`key_env`、viewer 端口和两个错峰分钟。
2. 把现有 config、`.env`、两个插件和相关 user unit 备份到带 UTC 时间戳的目录。备份只用于逐文件回滚，**不用于整 profile 恢复**。
3. 直接从目标 Git commit 读取 blob 原始 bytes，用 Git blob SHA-1 逐文件复核后，原子安装 `autonomy` 与 `seed_memory`。绝不触碰 `state.db`、`state.db-*`、`gateway.lock`、`logs/` 或 `runtime/`。
4. 逐项更新 `config.yaml`：`plugins.enabled` 同时包含 `autonomy` 与 `seed_memory`；model/provider/endpoint/`${ENV_VAR}` 自洽；custom provider 的 `key_env` 与 `.env` 变量名相同；memory provider 指向 profile-local seed DB/queue/source，并显式设置 `write_enabled: true` 与 tagger model/provider；autonomy 使用 profile-local data/paths，首次部署默认为 `observe_only`。
5. 以隐藏输入写 credential 到 `.env` 并设置 `0600`，不回显密钥；随后执行配置与环境变量静态门。
6. 执行 `hermes -p <profile> gateway restart` 刷新该 profile 句柄；必须收到 ACK、共享 gateway PID 不变，且操作后没有新增 `StateDbReplacedError`。
7. 预检 `plugins list`、`autonomy --help`，并确认 `autonomy`、`seed_memory` 均为 enabled/user 且 seed_memory 已 registered/activated。
8. 用 `nohup` 非交互执行一次首 tick；必须 `exit_reason=ok`，并确认实际 model/provider 与配置相同。
9. 验收 `autonomy.db`：`sqlite_master` 至少含 `schema_meta`、`events`、`receipts`、`evidence`、`evidence_challenges`、`budget_ledger`；同时要求 tick 成功、`seeds.db` 存在且 viewer HTTP 200。
10. 只有所有门禁全绿，才按错峰分钟 enable/start autonomy、challenge 和 viewer units，避免多个 profile 同分钟争抢内存。

### 部署踩坑与安全边界

- **没有 `autonomy migrate` 命令。** DB 在首次打开时自动 migration；验收查 `sqlite_master`，版本表名是 `schema_meta`，不是 `schema_version`。
- **`seed_memory` 必须写进 `plugins.enabled`。** 它虽是 memory provider，但未进插件目录会报 `not in plugin catalog`，provider 也无法加载。
- **`memory.seed_memory.write_enabled` 必须为 `true`。** 缺少该项或设为 false 会进入 read-only gray mode，只能读已有记忆，不能写入新种子。`source_root` 应指向当前 profile 的 `plugins/seed_memory/seed_src`；tagger model/provider 可单独指定，留空时向导默认跟随主 model/provider。
- **严禁整目录替换 profile 或替换 `state.db`。** 常驻共享 gateway 会继续持有旧 inode，触发 `StateDbReplacedError`，还可能被 autonomy 表面化为 `model connection request_failed`。刷新只能用官方 `hermes -p <profile> gateway restart`；它刷新单 profile，不应重启共享进程或影响其他 profile。
- **传输必须保留原始 bytes。** 使用 Git object/blob SHA 校验；不要用 PowerShell 文本管道或 `Out-File` 传文件，否则 systemd unit 可能被压成单行并报 `Invalid section header`。
- 模型返回 HTTP 503/`overloaded` 时，先停止部署并换用可用 provider/model；可先只读请求 `<gateway>/v1/models` 核对清单。
- 密钥只进入权限 `0600` 的 `.env`，不得写入文档、日志或消息；所有示例必须使用占位符。
- 回滚仅逐项恢复 config、`.env`、插件、units 和本轮创建的 autonomy data；随后做 per-profile restart。绝不整 profile 恢复，绝不改变 `state.db` inode。审计记录只保留时间、命令、返回码和输出路径，不得包含密钥。

## 它是怎么工作的

一轮 tick 的生命周期可以概括为：

```text
定时器唤醒
    → 读取近期记忆与目标
    → 自主思考并按策略执行动作
    → 写入 transcript / thought / receipt / evidence
    → 更新预算账本；异常时进入熔断
```

每轮是一次可审计的短任务，完成后退出，不依赖常驻推理进程。行动、失败、超时、资源异常和无行动都有对应的收据；观察模式下仍记录证据，但不会把学习候选直接晋升为生产策略。

## P0 证据与安全护栏

- **证据与收据层**：动作结果留下 SHA-256 收据和证据缓冲，可由独立 challenge job 按证据流复核；证据状态区分 `unverified`、`verified`、`mismatched` 与 `expired`。
- **预算账本**：预算按 80/10/10 分层，分别约束正常行动、记忆/观察开销与学习/演化开销；每次消耗写入 ledger，避免只看进程日志。
- **分层熔断**：正常行动与学习/晋升使用不同护栏。`closed`、`open`、`half-open` 状态控制故障恢复，学习候选不能绕过审核直接进入 active 策略。
- **observe-only**：设置 `autonomy.p0.mode: observe_only` 可保持正常观察、收据和证据写入，同时阻断 learning、candidate generation、experiment allocation 与 promotion，适合首次部署和观察期。

## 功能特性

- **独处思考**：`autonomy tick` 执行单轮任务，完成后退出，不留下常驻推理进程。
- **记忆优先**：模型调用前预取 seed_memory；失败时安全降级。激活目标的标题与描述会注入检索上下文，事件摘要会剥离遥测噪声。
- **查询轮换**：预取查询在 12 种主题间轮换，其中包含 2 条休眠种子诊断查询，避免每轮只触达相同种子。
- **模型模式**：自主模型留空时继承主模型；指定独立模型时可并行运行。
- **预算与熔断**：每日调用、上下文和提示额度，以及 closed/open/half-open 熔断状态。
- **停止与恢复**：支持 `stop`、`resume`、`reset-circuit` 和抢占标记。
- **完整过程日志**：记录 assistant 文本、工具调用摘要和预取种子 ID，并对敏感字段打码；连续相同工具调用自动折叠。
- **保尾摘要**：喂给 seed_memory 的文本保留头部与尾部上下文；打满 `max_turns` 时从最近助手消息和工具调用自动合成摘要。
- **每日日记**：在本地时间 21:00–24:00 窗口内触发当日反思与次日计划，注入次日首轮提示词。
- **结构化记忆**：SQLite 种子库、后台队列、语义检索、重要度/时效排序。
- **因果记忆与队列治理**：可选 `recent_seeds` 与 `caused_by` / `led_to`；毒丸文件在重试超限后隔离到 `_failed/`；去重与处理结果写入数据目录统计文件。
- **受控自我进化**：成员可在自己的插件目录修改代码；符合标准的改动经审核后合并主版本，候选策略与 active 策略隔离。

## 适用环境

| 项目 | 说明 |
| --- | --- |
| Hermes Agent | v0.22.x（已在 0.22.5 验证） |
| 操作系统 | Ubuntu 24，Linux x86_64 |
| Python | 以 Hermes 自带 Python 为准；已验证 CPython 3.14 |
| 权限 | 普通用户；systemd 使用用户级单元 |
| Windows | 不作为生产运行环境；`fcntl`、systemd 和 Linux 二进制依赖不适用 |

## 仓库结构

```text
identity/                         示例身份文件
plugins/autonomy/                 自主思考、P0 证据与收据层
plugins/seed_memory/              后台队列与 Seed 核心代码
  worker.py
  seed_src/hermes/
tests/                            可独立运行的插件测试
tools/                            打包、日志查看、归档和验证工具
docs/                             面向使用者的说明
deploy/systemd/                   通用用户级 systemd 模板
```

## 运行数据目录

推荐布局：

```text
<HERMES_HOME>/seed_data/
  seeds/seeds.db
  seed_queue/
<HERMES_HOME>/autonomy_data/
  autonomy.db
  thoughts.log
  STOP_AUTONOMY
  LAST_USER_ACTIVITY
  PREEMPT_NOW
```

插件兼容检测旧的 `seeds/`、`seed_queue/` 和 `autonomy/` 位置；新部署应使用统一布局。数据库、日志和队列均不应提交到 Git。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `hermes autonomy init` | 初始化配置并自检 |
| `hermes autonomy status` | 查看模式、预算、熔断和数据路径 |
| `hermes autonomy log -n 3` | 查看最近思考；支持 `--full`、`--seeds`、`--tools`、`--json` |
| `hermes autonomy tick` | 手工执行一轮 |
| `hermes autonomy stop` | 创建停止标记，暂停新轮次 |
| `hermes autonomy resume` | 删除停止标记并恢复 |
| `hermes autonomy reset-circuit` | 清理熔断状态 |

带 profile 时使用：

```bash
hermes -p <PROFILE> autonomy <子命令>
```

## 每小时运行

`deploy/systemd/` 提供通用模板。复制到用户级 systemd 配置目录后，替换 `<PROFILE>`、`<HERMES_HOME>` 和 `<HERMES_BIN_DIR>`，再执行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now <profile>-autonomy.timer
systemctl --user list-timers --all
journalctl --user -u <profile>-autonomy.service -n 50 --no-pager
```

模板名称只是示例，可以按 profile 重命名。需要注销后继续运行时，请按发行版策略启用用户服务驻留。

## 只读网页查看器

`tools/viewer/viewer.py` 是独立的 Python 标准库小程序，不导入插件、不连接模型，也不写 profile 数据。它只读展示最近自主思考、工具调用、种子、预算、熔断、timer 和队列状态。

```bash
python3 tools/viewer/viewer.py --profile xiaozhi
```

默认监听 `0.0.0.0:8765`。可用 `--host`、`--port` 或环境变量 `HERMES_VIEWER_HOST`、`HERMES_VIEWER_PORT` 修改。默认无鉴权，**只应在可信网络中开放**；如需简单口令保护，可使用 `--token '长随机口令'`，客户端通过 Bearer header 或 URL 参数提交；URL 参数可能进入浏览器历史，生产环境更建议使用 Bearer header、反向代理或其他安全认证层。部署前确认端口未被占用，并结合主机防火墙限制来源。

## 示例身份文件

`identity/SOUL.md` 是仓库提供的示例身份文件，不是插件运行所必需，也不会由安装步骤自动覆盖到 profile。你可以阅读、采用或替换为自己的身份文件。

## 测试

```bash
python3 -m pytest tests -q
```

测试不会要求真实 API 凭据，不应连接生产模型。Linux 专有锁和 systemd 行为应在临时 profile 或测试机器验证。

## 安全建议

- 不提交环境文件、密钥、数据库、种子归档、思考日志、模型或二进制依赖。
- 首次部署使用临时 profile。
- systemd 模板包含占位符，替换后再启用。
- 部署现场的额外安全边界可逐行写入 `<autonomy_data>/extra_rules.txt`，不应硬编码进公开源码。

## 贡献

欢迎 issue 和 pull request。贡献说明见 `CONTRIBUTING.md`。

涉及底层契约、成员改装或主版本吸收时，请同时参考 [`docs/DESIGN_PRINCIPLES.md`](docs/DESIGN_PRINCIPLES.md)。

## 近期变更

### 2026-11-06～09（P0 证据层上线）

- 增加结构化 evidence、receipt、budget ledger 与事件审计，六类动作终态均可追踪。
- 增加 80/10/10 预算护栏、分层熔断和 `observe_only` 安全姿态。
- 增加独立 evidence challenge 调度：按证据流进行严格 framed 解析、SHA-256 校验，并区分 `verified`、`mismatched`、`expired` 与不可用证据。
- 隔离 candidate 与 active 策略；观察模式下不会执行 learning、experiment allocation 或 promotion。
- 修复 evidence challenge 的 stream 哈希口径与 framing 解析，避免将合并缓冲误判为单流证据不一致；对应发布提交为 `e731889`。

### 2026-11-04（P0 止血修复）

- 修复转录提取顺序、空摘要、摘要截头丢尾、事件噪声和 goals 未传入预取等问题。
- 增强转录折叠与异常处理，减少日志冗余并提高长轮次稳定性。

### 2026-11-03（Phase 1B）

- 新增每日日记、查询轮换、休眠种子诊断、结构化因果记忆和队列治理。
- 修复备份插件目录覆盖问题：备份目录中的 manifest 应停用，避免静默覆盖 canonical 插件。

## 许可证

本仓库所有内容（包括 `identity/SOUL.md`）均以 **MIT License** 发布，见根目录 `LICENSE`。
