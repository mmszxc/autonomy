# autonomy —— 让 Hermes 在没人说话时，也会自己醒来想一想

**autonomy** 是 Hermes Agent 的“独处思考”插件：由定时器触发，每轮先回顾记忆，再自主思考，并把过程写入日志。**seed_memory** 是配套的长期记忆插件，把经历保存为带情绪和重要度的“种子”，供后续轮次检索。

> **状态**：这是 2026-10-28 的公开快照，后续会继续更新。已在 Hermes v0.22.5 + Ubuntu 24 上验证，面向 Hermes v0.22.x。

## 功能

- **独处思考**：`autonomy tick` 执行单轮任务，完成后退出，不留下常驻推理进程。
- **记忆优先**：模型调用前从 seed_memory 预取相关记忆，失败时安全降级。
- **对话优先 / 并行模式**：自主模型留空时继承主模型；指定独立模型时并行运行。
- **预算与熔断**：每日调用、token、prompt token 限额，以及 closed/open/half-open 熔断状态。
- **停止与恢复**：支持 stop、resume、reset-circuit 和抢占标记。
- **完整过程日志**：记录 assistant 文本、工具调用摘要、预取种子 ID，并对敏感字段打码。
- **结构化记忆**：SQLite 种子库、后台队列、语义检索、重要度/时效排序。

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

## 安装

### 1. 复制插件

```bash
cp -a plugins/autonomy <HERMES_HOME>/plugins/
cp -a plugins/seed_memory <HERMES_HOME>/plugins/
```

### 2. 准备依赖

依赖和模型体积较大且与平台/Python 版本相关，因此不进入仓库。

给 autonomy 准备轻量依赖：

```bash
python3 -m pip install --target <HERMES_HOME>/plugins/autonomy/_vendor \
  PyYAML numpy
```

给 seed_memory 准备通用依赖：

```bash
python3 -m pip install --target <HERMES_HOME>/plugins/seed_memory/_vendor \
  PyYAML numpy
```

安装 CPU 版 PyTorch，再安装 sentence-transformers：

```bash
python3 -m pip install --target <HERMES_HOME>/plugins/seed_memory/_vendor_ml \
  torch --index-url https://download.pytorch.org/whl/cpu
python3 -m pip install --target <HERMES_HOME>/plugins/seed_memory/_vendor_ml \
  sentence-transformers
```

请使用 Hermes 自带 Python，并确认 wheel 与 Python 版本、Linux 和 CPU 架构匹配。

### 3. 准备嵌入模型

模型名称：

```text
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

从 Hugging Face 官方模型页下载完整快照，放到：

```text
<HERMES_HOME>/plugins/seed_memory/seed_src/hermes/model/paraphrase-multilingual-MiniLM-L12-v2/
```

联网环境也可以先由 sentence-transformers 下载，再把完整目录复制到离线机器。仓库不含模型权重。

### 4. 配置 profile

示例：

```yaml
plugins:
  enabled: [autonomy]

memory:
  provider: seed_memory
  seed_memory:
    source_root: <HERMES_HOME>/plugins/seed_memory/seed_src
    data_dir: <HERMES_HOME>/seed_data
    db_path: <HERMES_HOME>/seed_data/seeds/seeds.db
    queue_dir: <HERMES_HOME>/seed_data/seed_queue
    embedding_model_path: <HERMES_HOME>/plugins/seed_memory/seed_src/hermes/model/paraphrase-multilingual-MiniLM-L12-v2
    prefetch_limit: 5
    write_enabled: true

autonomy:
  model: null
  memory_provider: seed_memory
  data_dir: <HERMES_HOME>/autonomy_data
  thought_log: <HERMES_HOME>/autonomy_data/thoughts.log
```

完整字段见 `plugins/autonomy/config.example.yaml` 和 `docs/`。

### 5. 初始化和检查

```bash
hermes -p <PROFILE> plugins doctor autonomy
hermes -p <PROFILE> plugins doctor seed_memory
hermes -p <PROFILE> autonomy init
hermes -p <PROFILE> autonomy status
hermes -p <PROFILE> autonomy tick
hermes -p <PROFILE> autonomy log -n 1 --tools --seeds
```

先手工执行一轮，确认 provider、模型、中文日志和数据路径都正常，再启用 timer。

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

## 示例身份文件

`identity/SOUL.md` 是作者 **mmszxc** 提供的示例身份文件，不是插件运行所必需，也不会由安装步骤自动覆盖到 profile。你可以阅读、采用或替换为自己的身份文件。

身份文件与代码采用不同许可证，详见下一节。

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

## 许可证

- 除 `identity/` 单独标注的内容外，仓库代码和文档以 **MIT License** 发布，见根目录 `LICENSE`。
- `identity/SOUL.md` 以 **Creative Commons Attribution 4.0 International（CC BY 4.0）** 发布，署名 **mmszxc**，见 `identity/LICENSE.md`。
