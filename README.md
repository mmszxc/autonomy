# Hermes Autonomy + Seed Memory：从零安装教程

面向第一次接触 Hermes Agent 的用户，以 **Ubuntu Linux + 一个名为 `mybot` 的 profile** 为例。复制命令前先看注释；下文的 `mybot`、模型名称、服务地址都是示例，请按你的实际环境替换。**不要把 API key 提交到 Git。**

- `autonomy` 是自主思考的“大脑”：按定时任务唤醒 Hermes，读取环境和记忆，执行一轮思考（称为 **tick**）。
- `seed_memory` 是记忆的“灵魂”：把会话归纳成 **seed（记忆种子）**，写入 SQLite 数据库，并供以后检索。队列中的文件由后台 **worker（处理进程）** 消费。
- **profile** 是一个机器人的独立配置和数据目录；不同 profile 可以用不同模型，不共用记忆。

> 本项目仅保证 Ubuntu 24.04 等 Linux 环境；不保证 Windows 原生运行。插件是 Hermes Agent 的扩展，**不能代替 Hermes Agent 本体**。

[screenshot: 安装完成后的终端、timer 和 viewer 总览]

## 前置准备：先装 Hermes Agent

1. 准备 Ubuntu 24.04、可用的模型 API 或本地 Ollama、`git`、`python3`、`systemd --user`；安装系统工具：

   ```bash
   sudo apt update
   sudo apt install -y git python3 python3-venv sqlite3
   ```

2. **先按 [Hermes Agent 官方仓库](https://github.com/NousResearch/hermes-agent) 的当前安装说明安装并完成首次配置**。官方安装命令和依赖可能随版本改变，请以该仓库 README 为准，不建议复制过期的第三方安装脚本。本教程从 `hermes` 已能运行开始：

   ```bash
   command -v hermes
   hermes --help
   systemctl --user --version
   ```

   **成功标志：**第一条输出 `hermes` 可执行文件路径，第二条打印帮助而不是 `command not found`；最后一条显示 systemd 版本。若失败，先完成官方安装、重新登录以刷新 PATH，再继续。后面的 systemd 服务同样需要能在该用户的 PATH 下找到 `hermes`。

> 下文只在**当前普通用户**的 `~/.hermes` 中操作，不要用 `sudo` 运行 Hermes 或把配置文件放到 `/root` 下。

## 第一步：新建机器人 profile

```bash
# 将 mybot 换成你自己的英文短名称，后文保持一致
export PROFILE=mybot
export PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE"
hermes init --profile "$PROFILE"
ls -ld "$PROFILE_HOME"
```

如果你安装的 Hermes 版本没有 `init --profile`，先执行 `hermes --help` 查看其 profile 初始化命令；也可以手工创建**目录骨架**，然后按第三步建立 `config.yaml`：

```bash
mkdir -p "$PROFILE_HOME/plugins" "$PROFILE_HOME/autonomy_data" "$PROFILE_HOME/seed_data/seeds"
```

**成功标志：**`ls -ld "$PROFILE_HOME"` 能看到目录。该目录下的 `config.yaml` 是此机器人的配置；`plugins/` 放插件；`autonomy_data/` 和 `seed_data/` 存数据。手工建目录不等于已经完成 Hermes 自身的 API/模型初始化。

[screenshot: ~/.hermes/profiles/mybot 目录结构]

## 第二步：下载仓库并安装两个插件

在**运行 Hermes 的同一台机器、同一个 Linux 用户**下：

```bash
cd "$HOME"
git clone https://github.com/mmszxc/autonomy.git
mkdir -p "$PROFILE_HOME/plugins"
cp -a "$HOME/autonomy/plugins/autonomy" "$PROFILE_HOME/plugins/"
cp -a "$HOME/autonomy/plugins/seed_memory" "$PROFILE_HOME/plugins/"
ls -ld "$PROFILE_HOME/plugins/autonomy" "$PROFILE_HOME/plugins/seed_memory"
```

**成功标志：**两个目录都存在，并能找到 `plugins/autonomy/tick.py`、`plugins/seed_memory/worker.py`。更新时**先备份再替换**；备份放在 `~/.hermes/deploy_backups/`，**不要**把旧插件目录留在 `plugins/` 内，以免被重复加载。

如果仓库在笔记本、Hermes 在另一台 Linux 机器，先在**目标机器**创建 profile 的 `plugins/`，再从笔记本运行下面的示例（把占位符换成真实 SSH 用户、主机和 home；示例不含真实服务器地址）：

```bash
scp -r ./autonomy/plugins/autonomy ./autonomy/plugins/seed_memory \
  '<ssh-user>@<your-server>:~/.hermes/profiles/mybot/plugins/'
```

> `scp` 中 `~` 的展开依 SSH 服务端而异；传输后请登录目标机器运行上述 `ls -ld` 核实目录。跨机器的 `PROFILE_HOME` 必须指向**目标机器**的 home。

[screenshot: 两个插件均安装在 profile/plugins 下]

## 第三步：配置自主思考模型和记忆

模型是 Hermes 思考时调用的 AI 服务。先确认你已经通过 Hermes 官方说明配置好模型及访问凭据，再用编辑器打开 profile 配置：

```bash
nano "$PROFILE_HOME/config.yaml"
```

**完整示例 A：本机 Ollama（OpenAI 兼容接口）**。先安装并启动 [Ollama](https://ollama.com/download)，拉取你实际可运行的模型，验证 `curl http://127.0.0.1:11434/api/tags` 能连接；以下模型名 `qwen2.5:7b` 仅为示例。不同 Hermes 版本对 custom provider 命名可能有差异，请以 Hermes 自身的模型配置文档和 `hermes --help` 核对。

```yaml
# ~/.hermes/profiles/mybot/config.yaml
model:
  default: qwen2.5:7b
  provider: custom:local-ollama
providers:
  local-ollama:
    api: http://127.0.0.1:11434/v1
    key_env: OLLAMA_API_KEY

autonomy:
  enabled: true
  p0:
    mode: normal
  max_turns: 8
  max_seconds: 300
  interval_minutes: 20
  memory:
    provider: seed_memory
  memory_provider: seed_memory # 兼容较早读取平铺键的版本
  peer_share:
    enabled: false

memory:
  provider: seed_memory
  seed_memory:
    write_enabled: true
```

如 Hermes/Ollama 客户端要求非空 key，可在 profile 专属的 `.env` 中设置 `OLLAMA_API_KEY=ollama`；**不要在公开配置文件或截图中暴露真实密钥**。

**完整示例 B：远程 OpenAI 兼容 API**。将 `<your-api-host>`、`<your-model-id>` 换成服务商提供的地址与模型标识；这是占位符，不是可用的公共服务。使用 HTTPS，避免把密钥写入 YAML：

```yaml
# ~/.hermes/profiles/mybot/config.yaml
model:
  default: <your-model-id>
  provider: custom:my-openai
providers:
  my-openai:
    api: https://<your-api-host>/v1
    key_env: MY_MODEL_API_KEY

autonomy:
  enabled: true
  p0:
    mode: normal
  max_turns: 8
  max_seconds: 300
  interval_minutes: 20
  memory:
    provider: seed_memory
  memory_provider: seed_memory
  peer_share:
    enabled: false

memory:
  provider: seed_memory
  seed_memory:
    write_enabled: true
```

将 `MY_MODEL_API_KEY=...` 放进 Hermes 支持的 profile `.env` 或 systemd 的受限 `EnvironmentFile=`，并执行 `chmod 600 "$PROFILE_HOME/.env"`；具体加载机制请按所装 Hermes 版本确认。**不要覆盖原 `config.yaml` 中已经存在的其他设置：把示例字段合并进去。**

**最容易填错的路径：**

- 必须是嵌套的 `autonomy.p0.mode: normal`，不是 `autonomy.p0_mode: normal`；`normal` 才启用正常运行模式。
- 顶层 `memory.provider: seed_memory` 给 Hermes 记忆桥使用，`autonomy.memory.provider: seed_memory` 给 autonomy Runner 使用；`autonomy.memory_provider` 是兼容键。它们**不是**同一处配置。
- 必须是 `memory.seed_memory.write_enabled: true`，旧键 `seed_memory.enabled` 不能替代它。
- `model.default` 和 `model.provider` 是两层不同字段；模型标识、provider 名称以及 `/v1` API 地址须与实际服务和 Hermes 版本一致。
- `autonomy.max_turns` 控制每轮允许的对话步数，`autonomy.max_seconds` 限制单轮耗时；timer 的间隔最终以第五步 `.timer` 文件为准，改 `interval_minutes` 后也要同步修改 timer。

**成功标志：**`config.yaml` 可被 YAML 解析，`hermes` 可访问你所选的模型，且 `autonomy.p0.mode` 确实是 `normal`。如需本地验证 YAML，可用安装了 PyYAML 的 Python 执行 `python3 -c 'import yaml; yaml.safe_load(open("config.yaml"))'`（先 `cd "$PROFILE_HOME"`）；没有 PyYAML 不代表 Hermes 插件不可运行。

[screenshot: config.yaml 层级示意，敏感字段打码]

## 第四步：准备数据目录与数据库

```bash
mkdir -p "$PROFILE_HOME/autonomy_data" "$PROFILE_HOME/seed_data/seeds"
ls -ld "$PROFILE_HOME/autonomy_data" "$PROFILE_HOME/seed_data/seeds"
```

**成功标志：**两个目录存在。**不必手工创建空 `.db` 文件**：首次正常 tick 会初始化 `autonomy_data/autonomy.db`，`seed_memory` 首次实际写入时初始化 `seed_data/seeds/seeds.db`。实际的 seed 库在 `seed_data/seeds/`，不要把 `autonomy_data/seeds.db` 当成同一个库。模型处理和后台 worker 可能有延迟，第一次 tick 后 seed 计数仍为 0 时先看队列和日志。

## 第五步：用 systemd user timer 定时思考（Ubuntu）

`service` 是执行一次 tick 的命令，`timer` 是定时唤醒它的闹钟。下面的示例每 20 分钟运行一次；`%h` 代表当前 Linux 用户的 home。**先检查你机器上可用的 Python 解释器**：`python3 -m autonomy.tick` 所在环境必须能导入 Hermes、autonomy 及其依赖；Hermes 官方安装的 Python 环境若不同，请把 `ExecStart` 中的 `/usr/bin/python3` 改为该环境的 Python 绝对路径（不是让你改 worker 的解释器）。

```bash
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/mybot-autonomy.service" <<'UNIT'
[Unit]
Description=Hermes autonomy tick for mybot

[Service]
Type=oneshot
WorkingDirectory=%h/.hermes/profiles/mybot
Environment=HERMES_HOME=%h/.hermes/profiles/mybot
Environment=PYTHONPATH=%h/.hermes/profiles/mybot/plugins:%h/.hermes/hermes-agent
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 -m autonomy.tick --hermes-home %h/.hermes/profiles/mybot
TimeoutStartSec=1800
UNIT

cat > "$HOME/.config/systemd/user/mybot-autonomy.timer" <<'UNIT'
[Unit]
Description=Run mybot autonomy every 20 minutes

[Timer]
OnBootSec=2min
OnUnitInactiveSec=20min
Unit=mybot-autonomy.service
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now mybot-autonomy.timer
systemctl --user list-timers --all | grep mybot-autonomy
```

若 Hermes 不安装在 `%h/.hermes/hermes-agent`，请将 `PYTHONPATH` 的第二段改为 Hermes Agent 源码/安装位置；如它已安装为系统 Python 包，则不需要那一段。若需要读取 `.env` 中的模型 key，在 service 的 `[Service]` 段加入 `EnvironmentFile=%h/.hermes/profiles/mybot/.env`（先确保文件存在并 `chmod 600`）；修改后重新 `daemon-reload`。不要把 key 直接写到 unit 文件或 README。

**成功标志：**`systemctl --user list-timers --all` 显示 `mybot-autonomy.timer`，并有非空的 `NEXT` 时间。也可手动试跑一轮：

```bash
systemctl --user start mybot-autonomy.service
journalctl --user -u mybot-autonomy.service -n 50 --no-pager
```

`systemctl start` 成功只说明 service 运行结束，**不等于** tick 的业务结果是 `exit_reason=ok`；还需按下文查数据库。服务器上用户退出登录后若 timer 不再触发，请向管理员咨询 `loginctl enable-linger "$USER"` 是否合适。

[screenshot: systemctl --user list-timers 显示下一次触发时间]

## 第六步：打开 viewer 查看页面

viewer 是这个仓库另带的**可视化工具**，不会因为安装两个插件就自动出现在 `~/.local/lib/xiaozhi-viewer/`。先从**刚下载的本仓库**拷贝 `tools/viewer/`（其中包括 `viewer.py` 和 `viewer_app/`）：

```bash
mkdir -p "$HOME/.local/lib/xiaozhi-viewer"
cp -a "$HOME/autonomy/tools/viewer/." "$HOME/.local/lib/xiaozhi-viewer/"
/usr/bin/python3 "$HOME/.local/lib/xiaozhi-viewer/viewer.py" --help
```

此 viewer 基于 Python 标准库，不需要为它单独执行 `pip install`。`--help` 应打印 `--profile`、`--profile-home`、`--host` 等选项；若缺模块，先核实 `viewer_app/` 与脚本一起复制。

本机访问时优先绑定 `127.0.0.1`，需要其他电脑访问且已经配置防火墙/认证时才绑定 `0.0.0.0`：

```bash
/usr/bin/python3 "$HOME/.local/lib/xiaozhi-viewer/viewer.py" \
  --profile mybot --profile-home "$PROFILE_HOME" --host 127.0.0.1 --port 8766
# 仅确认已做好访问控制、确需局域网远程访问时：--host 0.0.0.0
```

同机浏览器打开 `http://127.0.0.1:8766`；如你**有意**开放到内网，可从另一台机器打开 `http://<server-ip>:8766`。公网不要裸露开放 viewer；可用 SSH 端口转发：`ssh -L 8766:127.0.0.1:8766 <ssh-user>@<your-server>` 后在本地访问 `http://127.0.0.1:8766`。

**成功标志：**终端显示 viewer 启动/监听端口，浏览器能打开页面，看见该 profile 的思考日志和种子（刚安装时为空是正常的）。具体页面功能随仓库版本变化。

[screenshot: viewer 首页和种子列表]

## 验证是否真正跑通

至少等一次 timer 触发，或者手动 `systemctl --user start mybot-autonomy.service`。随后逐项核实：

```bash
systemctl --user list-timers --all | grep mybot-autonomy
journalctl --user -u mybot-autonomy.service -n 80 --no-pager
sqlite3 "$PROFILE_HOME/autonomy_data/autonomy.db" \
  'SELECT kind, exit_reason, substr(summary,1,120) FROM events ORDER BY id DESC LIMIT 15;'
sqlite3 "$PROFILE_HOME/autonomy_data/autonomy.db" \
  'SELECT status, started_at, finished_at FROM last_tick;'
sqlite3 "$PROFILE_HOME/seed_data/seeds/seeds.db" 'SELECT count(*) FROM seeds;'
ls -lah "$PROFILE_HOME/seed_data/seed_queue" 2>/dev/null
```

**成功标志：**timer 有下一次触发时间；本轮 tick 的 `events` / `last_tick` 显示正常结束（目标 `exit_reason=ok`，不是 `run_timeout`、`RuntimeError` 或 `degraded`）；`memory_prefetch` 不报 degraded；经过有内容的会话和 worker 处理后，`seeds` 数量增加，viewer 能展示数据。没有可提取的内容时，`memory_prefetch=empty` 或 seed 数量暂时为 0 **不一定是故障**。数据库结构随插件版本可能变化，先运行 `sqlite3 "$PROFILE_HOME/autonomy_data/autonomy.db" '.schema events'` 核对列名。

[screenshot: events 正常记录、seeds count 大于零]

## 常见问题 FAQ

**Q：timer 停了、过了 20 分钟也没运行？** 先执行 `systemctl --user status mybot-autonomy.timer`、`systemctl --user list-timers --all`；必要时 `systemctl --user start mybot-autonomy.timer`，再查 `journalctl --user -u mybot-autonomy.service -n 100 --no-pager`。如果已部署更新，务必确认 timer 在结束维护后重新启动并且 `NEXT` 非空。

**Q：tick 总是 `run_timeout`？** 检查模型 API 可达性及调用耗时；减小 `autonomy.max_turns`、在明确了解资源限制后调大 `autonomy.max_seconds`，或换更快的模型。**不要把 systemd service 启动成功当成 tick 成功**；在数据库查最终 `exit_reason`。改配置、unit 后重新执行 `systemctl --user daemon-reload`，按需要重启 timer。

**Q：worker 不消费队列、seeds 一直 0？** 检查 `"$PROFILE_HOME/seed_data/seed_queue"` 中的 `session_*.json` 或时间戳开头的 JSON，再看目录下的 `worker.log`；worker 是后台任务，可能仍在做慢速模型提取。若日志报失败，先看具体异常、模型凭据和数据库权限，不要直接删除队列文件。seed 库在 `seed_data/seeds/seeds.db`，不是 `autonomy_data/seeds.db`。

**Q：worker 提示 `No module named yaml` / `numpy`？** 自动 worker 使用 Hermes 随附/嵌入式 Python 及插件提供的按需 YAML 加载；不要擅自把它改成 `/usr/bin/python3`，系统 Python 可能缺 numpy。确认安装的是本仓库最新 `seed_memory`、检查 worker.log 和对应解释器环境。不要仅凭 `worker` 进程退出码 0 判定已有 seed，实际执行 SQL 查计数。

**Q：`memory_prefetch=degraded`？** 核实 `memory.provider` 与 `autonomy.memory.provider` 均为 `seed_memory`；`autonomy.p0.mode` 为 `normal`，然后查 `events.summary` 中的真实异常。只配置顶层 provider 不会自动使 autonomy Runner 使用 seed_memory。

**Q：viewer 在另一台机器打不开？** 确认进程确实监听 8766；`127.0.0.1` 只能在本机或 SSH 隧道访问，`0.0.0.0` 需要配合网络防火墙与认证。不要把未经保护的 viewer 直接暴露到公网。

---

更多设计背景见 [`docs/`](docs/)；插件代码见 [`plugins/autonomy/`](plugins/autonomy/) 和 [`plugins/seed_memory/`](plugins/seed_memory/)。
