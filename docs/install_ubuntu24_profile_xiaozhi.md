# Ubuntu 24：为现有 Hermes profile 安装 autonomy 与 seed_memory

本文给出一键安装、手工 Git 安装、固定版本 ML 资源、systemd 用户单元及回滚说明。示例 profile 为 `xiaozhi`，替换为你的实际 profile 名称即可。

## 安全边界

安装器遵循以下边界：

- profile 必须已经由 Hermes 创建；不存在时直接报错，不代为创建。
- 不使用 `sudo`，不修改系统 Python，也不向 Hermes 上游 venv 安装包。
- 不读取或修改数据库、`SOUL.md`、auth 文件、`.env`、sessions、memories 和现有种子。
- 修改 `config.yaml` 前先保存完整副本；失败时恢复原配置。
- 已有 `<PROFILE>-autonomy.service` 或 `.timer` 默认保持不变。
- 模型和大体积 vendor 不进入 Git。

支持范围：Ubuntu 24、Hermes v0.22.x、Python 3.14。其他发行版或 Hermes 版本应先在临时 profile 中验证。

## 推荐：一键安装

```bash
git clone https://github.com/mmszxc/autonomy.git
cd autonomy
./install.sh --profile xiaozhi
```

默认会：

1. 从已检出的仓库原子复制 `autonomy` 和 `seed_memory`，再用 Hermes 命令启用它们。
2. 写入插件拥有的最小配置及公开默认预算。
3. 在 profile 内安装固定版本 CPU ML 依赖及固定 revision 的 MiniLM 模型。
4. 生成 `~/.config/systemd/user/xiaozhi-autonomy.service` 和 `.timer`。
5. `daemon-reload`、启用 timer，并运行两个 plugin doctor 和 autonomy doctor。

ML 下载会占用数 GiB 空间，默认先请求交互确认。非交互安装使用：

```bash
./install.sh --profile xiaozhi --yes
```

常用选项：

| 选项 | 行为 |
| --- | --- |
| `--skip-ml` | 不下载 ML 依赖和模型；seed_memory 在 ML 安装完成前不可用 |
| `--no-timer` | 仍写 service，但不写、不启用或改变 timer |
| `--yes` | 跳过大体积下载确认 |
| `--force` | 先备份，再替换已有 profile systemd unit |

首次验证或自动化测试可以使用：

```bash
./install.sh --profile xiaozhi --skip-ml --no-timer --yes
```

这不是“假运行”：它会安装插件和增量配置，只是明确跳过 ML 与 timer。请仅在一次性 profile 上测试。

## profile、配置与备份

安装器通过以下只读命令解析 profile 路径：

```bash
hermes profile show xiaozhi
```

解析失败或目录不存在就停止，不猜测 `HERMES_HOME`。每次运行会在 profile 内建立：

```text
<HERMES_HOME>/.autonomy-install-backups/<UTC时间>-<PID>/
```

其中保存安装前的 `config.yaml`；如果插件目录原已存在，也会在 `plugins/` 子目录保存旧副本。安装器只用 `hermes config set` 写入以下插件自有键：

- `memory.provider`
- `autonomy.memory_provider`
- `autonomy.in_process_loop`
- `autonomy.data_dir`
- `autonomy.thought_log`
- `autonomy.limits.daily_tokens`
- `autonomy.limits.daily_calls`
- `autonomy.limits.daily_prompt_tokens`
- ML 成功后写 `memory.seed_memory.embedding_model_path`

现有 provider credentials、主模型、SOUL、auth 和数据路径之外的内容不会由脚本读取或覆盖。

如需恢复配置：

```bash
cp <HERMES_HOME>/.autonomy-install-backups/<备份目录>/config.yaml \
   <HERMES_HOME>/config.yaml
```

请先检查备份目录名称，不要复制示例占位符。

## ML 安装器

也可单独运行：

```bash
tools/install_ml.sh --profile xiaozhi
```

固定资源如下：

```text
torch: 2.14.1+cpu
sentence-transformers: 6.2.0
model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
revision: e8f8c211226b894fcb81acc59f3b34ba3efd5f42
```

目录布局：

```text
<HERMES_HOME>/plugins/_vendor_ml/
<HERMES_HOME>/models/paraphrase-multilingual-MiniLM-L12-v2/
```

下载和验证先在同一 profile 文件系统的临时目录完成。只有离线编码得到 `(2, 384)` 后才原子替换正式目录；旧目录先移动到 `.autonomy-install-backups/`。脚本用 `HF_HUB_OFFLINE=1` 和 `TRANSFORMERS_OFFLINE=1` 做最终验证。

如 Hermes 命令旁没有可识别的 Python，可明确指定：

```bash
HERMES_PYTHON=/path/to/hermes/python tools/install_ml.sh --profile xiaozhi
```

代理或内部 PyPI 环境可设置 `PIP_INDEX_URL` 和 `TORCH_INDEX_URL`。不要去掉模型 revision 固定后直接做生产安装。

## Hermes 原生 Git URL 安装

只安装插件代码时，可以不克隆完整工作树：

```bash
hermes -p xiaozhi plugins install \
  'https://github.com/mmszxc/autonomy.git#plugins/autonomy' --enable --yes
hermes -p xiaozhi plugins install \
  'https://github.com/mmszxc/autonomy.git#plugins/seed_memory' --enable --yes
```

也支持短格式：

```bash
hermes -p xiaozhi plugins install mmszxc/autonomy/plugins/autonomy --enable --yes
hermes -p xiaozhi plugins install mmszxc/autonomy/plugins/seed_memory --enable --yes
```

SSH 形式为：

```text
git@github.com:mmszxc/autonomy.git#plugins/autonomy
```

SSH 形式依赖本机 GitHub key；公开仓库优先使用 HTTPS。仓库根包含两个插件，必须分别安装，不能把仓库根当成单个 plugin。

原生 Git 安装不会为本项目下载 ML、写插件配置或生成 systemd 单元；完成后仍需运行 ML 安装器并手工配置，或直接使用 `install.sh`。

## systemd 用户单元

安装器按 profile 生成：

```text
~/.config/systemd/user/<PROFILE>-autonomy.service
~/.config/systemd/user/<PROFILE>-autonomy.timer
```

service 使用 Hermes 绝对路径、`-p <PROFILE>`、显式 `--hermes-home` 和：

```ini
TimeoutStartSec=3000
```

timer 使用：

```ini
OnCalendar=*-*-* *:00/20:00
RandomizedDelaySec=30
Persistent=false
```

如果任一同名单元已存在，默认两个都跳过，避免出现新旧混合。`--force` 会先把已有文件复制到本轮备份目录，再原子替换。现有 `xiaozhi-autonomy.service` 和 `.timer` 因而与安装器天然兼容，不会在默认运行中被覆盖。

检查：

```bash
systemctl --user daemon-reload
systemctl --user status xiaozhi-autonomy.timer
systemctl --user list-timers --all
journalctl --user -u xiaozhi-autonomy.service -n 50 --no-pager
```

无用户 systemd bus 时，脚本保留已写文件并打印警告，不伪装成已经启用。是否启用 linger 应由系统管理员按机器策略决定。

## 安装后检查

```bash
hermes -p xiaozhi plugins doctor autonomy --ci
hermes -p xiaozhi plugins doctor seed_memory --ci
hermes -p xiaozhi autonomy doctor
hermes -p xiaozhi autonomy status
hermes -p xiaozhi autonomy log -n 1 --tools --seeds
```

建议先手工执行一次 tick，再依赖 timer：

```bash
hermes -p xiaozhi autonomy tick --hermes-home \
  "$(hermes profile show xiaozhi | sed -n 's/^Path:[[:space:]]*//p')"
```

## 幂等与故障处理

- 重复执行会更新两个插件、重新应用同一组配置，并保留每轮 config 备份。
- ML marker、模型关键文件和固定 revision 全部匹配时只做离线验证，不重复下载。
- 配置阶段失败会自动恢复本轮开始前的 `config.yaml`。
- ML 下载或验证失败时清理 staging；进入替换阶段后失败会恢复旧 ML 目录。
- 数据库、种子、日志、SOUL、auth、`.env` 和 sessions 不属于安装事务，脚本不会删除或移动它们。
- 已有 systemd 单元必须显式 `--force` 才能替换，替换前一定备份。

若 doctor 失败，先保留终端输出并检查：Hermes Python 是否匹配、可用磁盘空间、模型 revision 是否完整、`systemctl --user` 是否有 session bus，以及 gateway/provider 配置是否由用户另行完成。
