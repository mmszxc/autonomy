# autonomy 插件使用说明

> 本文对应 `hermes_home/plugins/autonomy/` 当前实现。参考部署 的部署曾完成插件同步与配置迁移，但手动 tick 暴露了旧 service/旧熔断状态问题；因此不要把本文当作“已通过生产验收”的声明。启用定时器前应先完成接力任务中的 service 切换、熔断状态恢复和 tick 验证。

## 安装与启用

标准 Hermes 插件目录为：

```text
$HERMES_HOME/plugins/autonomy/
  plugin.yaml
  __init__.py
  cli.py, config.py, runner.py, wizard.py, ...
```

`plugin.yaml` 使用 `kind: standalone`，配置文件中启用：

```yaml
plugins:
  enabled:
    - autonomy
```

参考部署 部署时应保持插件目录属主为 `hermes:hermes`。旧的 `/var/lib/hermes/app/autonomy_pkg` 是历史实现，不应覆盖；若使用新插件运行定时任务，应将 `hermes-autonomy.service` 的 `ExecStart`/工作路径切换到新插件对应的 `hermes autonomy tick`，旧目录仅作回滚参考。

## 配置 schema

推荐新 schema（示例值与项目配置一致）：

```yaml
autonomy:
  model: <AUTONOMY_MODEL>             # null 表示继承主模型；不同于主模型时为 parallel
  memory_provider: null           # null 表示沿用 Hermes 当前 memory.provider（本项目为 seed_memory）
  in_process_loop: false          # 是否在已有 Hermes 进程内循环；常驻应使用 systemd
  data_dir: /var/lib/hermes/autonomy_data
  thought_log: /var/lib/hermes/autonomy_data/thoughts.log
  quiet_s: 300                    # 对话优先模式静默秒数
  limits:
    daily_tokens: 5000000         # 0 或负数表示不限；项目宽松上限可按部署调整
    circuit_threshold: 3           # 连续失败次数阈值
    circuit_cooldown_s: 1800       # 熔断冷却秒数
    memory_max: 1G                # 资源保护上限
```

代码仍兼容旧 schema（仅用于读取既有部署）：`enabled`、`model.inherit`、`memory.provider`、`budget.daily_tokens`、`circuit.failure_threshold`/`cooldown_seconds`、`resources`、`paths`。新配置写入不应再生成这些旧嵌套键。

## CLI 命令

在 参考部署 上使用 Hermes 虚拟环境的绝对路径，并以 `hermes` 运行：

```bash
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/hermes_home \
  /var/lib/hermes/app/venv/bin/hermes autonomy status
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/hermes_home \
  /var/lib/hermes/app/venv/bin/hermes autonomy init --json
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/hermes_home \
  /var/lib/hermes/app/venv/bin/hermes autonomy stop
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/hermes_home \
  /var/lib/hermes/app/venv/bin/hermes autonomy resume
sudo -u hermes -H env HERMES_HOME=/var/lib/hermes/hermes_home \
  /var/lib/hermes/app/venv/bin/hermes autonomy tick
```

`init` 向导会测试主/自主模型；测试失败应重新选择，不能跳过。它还会选择已配置的外置记忆并执行写入/读回自检。不要在命令、日志或文档中打印 API key/token。

`stop` 创建 `$data_dir/STOP_AUTONOMY`；`resume` 删除它；`status` 报告模式、模型、预算和熔断；`tick` 只执行一轮。

## 模式

- `model: <AUTONOMY_MODEL>` 与主模型 `<MAIN_MODEL>` 不同：`parallel`，用户对话不打断思考。
- `model: null`、空值或与主模型（大小写/首尾空白归一后）相同：`dialog_first`。用户活动更新 `LAST_USER_ACTIVITY` 并写入 `PREEMPT_NOW`，思考让出；静默超过 `quiet_s` 后恢复。
- 模式判定唯一实现为 `mode.decide_mode`；不要在部署脚本里另写比较规则。

## 预算、熔断与错误

预算和连续失败熔断是资源保护，不是内容限制。JSON 解析失败应保留原始模型文本，不应伪装成成功 JSON，也不应因此计入失败熔断。模型连接/认证失败要呈现明确的失败状态和事件；若旧数据库已有永久 `blocked` 状态，先备份数据库并按运维流程清理/冷却，不能仅改配置假称恢复。

## 思考日志

默认日志为：

```text
$HERMES_HOME/autonomy_data/thoughts.log
```

查看最近内容：

```bash
tail -n 80 /var/lib/hermes/autonomy_data/thoughts.log
```

日志使用 UTF-8 写入，当前实现写入后设置只读权限（0444）。若中文显示为 `????`，检查终端/SSH 的 UTF-8 locale、文件实际编码和写入端，不要用 ASCII/系统默认编码重写历史日志。

## systemd 常驻运行

常驻前必须先通过单轮手动验证。推荐服务执行新插件 CLI：

```ini
ExecStart=/var/lib/hermes/app/venv/bin/hermes autonomy tick
```

服务应使用 Hermes 所需的环境文件（例如 `web.env`/`secrets.env`），但不要把其内容复制进文档或日志。确认 `WorkingDirectory`、`HERMES_HOME` 和插件路径，不要继续使用指向旧 `app/autonomy_pkg` 的服务作为新插件验收依据。

每小时 timer 示例：

```ini
[Timer]
OnCalendar=*-*-* *:20:00
RandomizedDelaySec=120
Persistent=false
```

启用前确认停止标记已按运维决定处理：

```bash
systemctl daemon-reload
systemctl enable --now hermes-autonomy.timer
systemctl enable --now hermes-autonomy-resume.timer
systemctl enable hermes-gateway-web
systemctl list-timers 'hermes-autonomy*'
```

本文撰写时 参考部署 的 timer 尚未作为插件验收结果确认，故不要将上述命令视为已经执行。

## 常见问题

### GatewayError / status=blocked

首先确认实际网关监听地址（本项目 参考部署 实测为 `http://127.0.0.1:18787/health`，不是已下线的 8787），以及 tick 进程是否加载了 Hermes 服务所需环境变量。服务 unit 通常包含环境文件；直接 shell 执行若缺少认证环境会收到 401/403。只报告状态码和错误类型，不打印 key。检查旧 `autonomy.db` 的熔断状态，修改前备份。

### status 显示旧 budget/circuit

这通常表示仍读取旧 schema 或 init 向导写回旧嵌套键。确认 config 使用新 `limits.*`，并确认实际运行的插件路径不是旧 `/var/lib/hermes/app/autonomy_pkg`。

### thoughts.log 中文乱码

使用 `LANG=C.UTF-8`/UTF-8 SSH 终端，读写 API 显式指定 UTF-8；不要把密钥、完整环境文件或原始认证响应写入日志。历史乱码不能由插件安全地猜测还原。
