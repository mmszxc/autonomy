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

客户端可用 `Authorization: Bearer <口令>`，浏览器也可在 URL 查询参数中传入 `token`。查询参数可能被浏览器历史记录保存，优先使用反向代理或 Bearer header。数据接口为 `/api/thoughts?n=20`、`/api/seeds?limit=20&q=` 和 `/api/status`，页面每 15 秒自动刷新。生产部署前请确认端口未被占用，并结合主机防火墙限制来源地址。

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
