# autonomy —— 让 Hermes 在没人说话时，也会自己醒来想一想

**autonomy** 是 Hermes Agent 的“独处思考”插件：由定时器触发，每轮先回顾记忆，再自主思考，并把过程写入日志。**seed_memory** 是配套的长期记忆插件，把经历保存为带情绪和重要度的“种子”，供后续轮次检索。

> **状态**：持续迭代中，最近更新 2026-11-04。已在 Hermes v0.22.5 + Ubuntu 24 上验证，面向 Hermes v0.22.x。

## 功能

- **独处思考**：`autonomy tick` 执行单轮任务，完成后退出，不留下常驻推理进程。
- **记忆优先**：模型调用前从 seed_memory 预取相关记忆，失败时安全降级。
- **目标导向预取**：当前激活目标（goals）自动注入检索上下文，让记忆召回更有方向感；事件摘要自动剥离遥测噪声。
- **查询轮换**：预取查询在 12 种主题间轮换（含 2 条专门面向休眠种子的诊断查询），避免每轮只触达相同种子。
- **对话优先 / 并行模式**：自主模型留空时继承主模型；指定独立模型时并行运行。
- **预算与熔断**：每日调用、token、prompt token 限额，以及 closed/open/half-open 熔断状态。
- **停止与恢复**：支持 stop、resume、reset-circuit 和抢占标记。
- **完整过程日志**：记录 assistant 文本、工具调用摘要、预取种子 ID，并对敏感字段打码；连续相同工具调用自动折叠，减少日志冗余。
- **保尾摘要**：喂给 seed_memory 的文本取"头 3000 字 + 尾 11000 字"，保留最新思考上下文；打满 max_turns 时从最后 3 条助手消息 + 5 条工具调用自动合成摘要。
- **每日日记**：在本地时间 21:00–24:00 窗口内触发当日反思与计划，注入次日首轮提示词。
- **结构化记忆**：SQLite 种子库、后台队列、语义检索、重要度/时效排序。
- **因果记忆与队列治理**：可选 `recent_seeds` 注入帮助模型延续上下文；`caused_by`/`led_to` 保存种子因果链；单文件毒丸在重试超限后隔离到 `_failed/`；去重与处理结果通过数据目录中的 `stats.json` 统计，数据库和队列内容不会进入公开仓库。
- **自我进化**：成员可在自己的插件目录修改代码；符合七条标准的改动经审核后合并主版本（已有实例：xiaoai 自修检索路径与查询轮换 bug）。

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
identity/
  SOUL.md                         作者的示例身份文件，可替换
  LICENSE.md                      身份文件的 CC BY 4.0 说明
plugins/
  autonomy/                       自主思考插件
  seed_memory/
    worker.py                     后台队列 worker
    seed_src/hermes/              最小 Seed 核心代码（不含模型）
tests/                            可独立运行的插件测试
tools/                            打包、日志查看、归档和验证工具
docs/                             面向使用者的说明
deploy/systemd/                   通用用户级 systemd 模板
```

## 运行数据目录

推荐的新目录布局：

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

插件仍会兼容检测旧的 `seeds/`、`seed_queue/` 和 `autonomy/` 位置；新部署应使用上面的统一布局。数据库、日志和队列均不应提交到 Git。

## 安装：3 步完成

安装器只接受已经存在的 Hermes profile；不会创建 profile，也不会修改数据库、`SOUL.md`、auth 文件或 `.env`。

```bash
# 1. 获取代码
git clone https://github.com/mmszxc/autonomy.git && cd autonomy

# 2. 安装两个插件、固定版本的 CPU ML 依赖/模型，以及用户级定时器
./install.sh --profile xiaozhi

# 3. 验证
hermes -p xiaozhi autonomy status && systemctl --user status xiaozhi-autonomy.timer
```

第 2 步的大体积下载默认要求交互确认；自动化环境可加 `--yes`。仅安装插件可加 `--skip-ml`，不写或启用 timer 可加 `--no-timer`。已有同名 systemd unit 默认保持不变；确认备份并替换时才使用 `--force`。

手工 Git URL 安装、固定模型 revision、目录布局、离线验证、systemd 与回滚说明见 [`docs/install_ubuntu24_profile_xiaozhi.md`](docs/install_ubuntu24_profile_xiaozhi.md)。本仓库不包含模型权重或大体积 Python vendor。


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

`deploy/systemd/` 提供通用模板。复制到 `~/.config/systemd/user/` 后，替换：

```text
<PROFILE>
<HERMES_HOME>
<HERMES_BIN_DIR>
```

然后执行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now xiaozhi-autonomy.timer
systemctl --user list-timers --all
journalctl --user -u xiaozhi-autonomy.service -n 50 --no-pager
```

模板名称只是示例，可以按 profile 重命名。需要注销后继续运行时，请由管理员按发行版策略启用 linger。

## 只读网页查看器

`tools/viewer/viewer.py` 是独立的 Python 标准库小程序，不导入插件、不连接模型，也不写 profile 数据。它只读展示最近自主思考、工具调用、种子、预算、熔断、timer 和队列状态。

```bash
python3 tools/viewer/viewer.py --profile xiaozhi
```

默认监听 `0.0.0.0:8765`，可用 `--host`、`--port` 或 `HERMES_VIEWER_HOST`、`HERMES_VIEWER_PORT` 修改。默认无鉴权，**只应在可信局域网中开放**；需要简单口令时使用：

```bash
python3 tools/viewer/viewer.py --profile xiaozhi --token '自行生成的长随机口令'
```

客户端可用 `Authorization: Bearer <口令>`，浏览器也可在 URL 查询参数中传入 `token`。查询参数可能被浏览器历史记录保存，优先使用反向代理或 Bearer header。数据接口为 `/api/thoughts?n=20`、`/api/seeds?limit=20&q=` 和 `/api/status`；页面不会自动轮询，仅在首次打开、切换标签或点击“刷新”时读取数据。生产部署前请确认端口未被占用，并结合主机防火墙限制来源地址。

## 示例身份文件

`identity/SOUL.md` 是作者 **mmszxc** 提供的示例身份文件，不是插件运行所必需，也不会由安装步骤自动覆盖到 profile。你可以阅读、采用或替换为自己的身份文件。

## 测试

```bash
python3 -m pytest tests -q
```

测试不会要求真实 API key，不应连接生产模型。Linux 专有锁和 systemd 行为应在临时 profile 或测试机器验证。

## 安全建议

- 不提交环境文件、密钥、数据库、种子归档、思考日志、模型或二进制依赖。
- 首次部署使用临时 profile。
- systemd 模板包含占位符，替换后再启用。
- 部署现场的额外安全边界可逐行写入 `<autonomy_data>/extra_rules.txt`，不应硬编码进公开源码。

## 贡献

欢迎 issue 和 pull request。贡献说明见 `CONTRIBUTING.md`。

涉及底层契约、成员改装或主版本吸收时，请同时参考 [`docs/DESIGN_PRINCIPLES.md`](docs/DESIGN_PRINCIPLES.md)。

## 近期变更

### 2026-11-04（P0 止血修复）

- **修复 `_extract_transcript` 顺序倒置**：将转录提取移至 `_decode_or_raw` 之前；打满 30 轮（`max_turns` 命中）时不再产生空摘要，改从最后 3 条助手消息 + 最后 5 条工具调用自动合成。
- **修复摘要截头丢尾**：`[:16000]` 改为头 3000 + 尾 11000，保留最新 10–15 轮上下文。
- **修复事件噪声**：`list_events(5)` 改调 `recent_runs(5)`，过滤掉 `seed_queue/transcript` 路径条目；event 摘要中的 `elapsed=…/usage=…` 遥测串在用作检索 query 前自动剥离。
- **修复 goals 未传入预取**：激活目标（title + description）现在注入 `_prefetch_memory`，检索更有方向感。
- **修复 `UnboundLocalError`**：`raw`/`usage` 移到外层 `try` 之前初始化，避免 prefetch 阶段异常导致异常处理器本身崩溃。
- **转录折叠**：连续工具调用满足"工具名 + 完整参数预览 + 去时间戳结果预览"三同时，折叠为单行 `(连续重复 N 次)`。

### 2026-11-03（Phase 1B + xiaoai 自进化合并）

- 新增每日日记功能（`journal.py`）：本地时间 21:00–24:00 触发当日反思与次日计划，注入下一轮提示词。
- 合并 xiaoai 自修改动：12 条查询轮换（含 2 条休眠种子诊断查询）+ `_vendor_ml` 路径修复（解决 `transformers` 模块无法加载导致检索降级的问题）。
- 修复备份插件目录覆盖问题：`autonomy.bak-*/plugin.yaml` 因字母序排在原目录之后会静默覆盖新版本；现要求备份后立即将 `plugin.yaml` 改名为 `plugin.yaml.disabled`。

## 许可证

本仓库所有内容（包括 `identity/SOUL.md`）均以 **MIT License** 发布，见根目录 `LICENSE`。
