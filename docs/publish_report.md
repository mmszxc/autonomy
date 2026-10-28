# 发布预检报告

日期：2026-10-28
目标：公开 GitHub 仓库 `mmszxc/autonomy`
基线：远端 `main` 现有提交 `e35679d`

## 发布范围

本次严格按白名单组装：

```text
README.md
LICENSE
CONTRIBUTING.md
.gitignore
identity/
plugins/autonomy/
plugins/seed_memory/
tests/
tools/
docs/
deploy/systemd/
```

未纳入虚拟环境、二进制依赖、模型、环境配置、密钥、数据库、运行日志、会话数据、种子归档、历史部署载荷、临时包和本机工作目录。

## 文件树与大小

```text
autonomy-repo/                       # 65 files, 337507 bytes
├── (root files)                     # 4 files, 8995 bytes
├── identity/                        # 2 files, 8971 bytes
│   ├── SOUL.md
│   └── LICENSE.md
├── plugins/                         # 34 files, 222122 bytes
│   ├── autonomy/                    # 25 个源码/配置/说明文件
│   └── seed_memory/                 # 9 个源码/配置/最小核心文件
├── tests/                           # 14 files, 46965 bytes
├── tools/                           # 5 files, 20915 bytes
├── docs/                            # 4 files, 29032 bytes（含本报告）
└── deploy/systemd/                  # 2 files, 509 bytes
```

最大文件为 `plugins/seed_memory/__init__.py`（37468 bytes）；单文件最大值低于 5 MiB。仓库不含模型权重或 vendor 依赖目录。

## 身份文件校验

`identity/SOUL.md` 从作者源文件按字节复制，没有改写内容或换行。

```text
bytes: 8826
sha256: E1644F7871C672FBE1B1C41C543F5C5016B6A5BDB8CEE12BC4F585AA551F0FB2
source == identity copy: true
CRLF count: 0
```

代码和普通文档使用 MIT License；`identity/SOUL.md` 使用 CC BY 4.0，署名 mmszxc，许可证说明只链接 Creative Commons 官方 deed 和 legalcode。

## 脱敏和安全扫描

结果：

- 私有网络地址：0 个未处理命中；
- 本机绝对路径和本机用户名：0 个未处理命中；
- 旧生产部署根目录、受保护服务路径、内部主机名和服务账号：0 个未处理命中；
- 私钥头、SSH 公钥、GitHub/Slack 凭据前缀、长格式模型 API 凭据：0 个疑似真实值；
- 实际环境文件、数据库、日志、模型、vendor 目录和私钥文件：0 个；
- 大于 5 MiB 的文件：0 个。

代码字符串中与凭据相关的允许命中仅包括：

```text
LAN_QWEN_API_KEY          # 环境变量名
API_SERVER_KEY            # 环境变量名
Authorization             # HTTP 请求头名称
secrets.env               # 通用候选配置文件名
api_key / token / secret  # 配置字段、预算字段或脱敏规则
```

没有对应的真实值。测试里的 `fakekey`、`fakepass`、`faketoken`、`fakesecret` 和短示例 `sk-` 字符串是验证 transcript 打码的固定假数据，均不符合长凭据扫描模式。

## 测试文件与 Linux 结果

以下每个入库测试文件均包含在 Linux 全套测试中并通过：

```text
PASS tests/autonomy_plugin/test_activity.py
PASS tests/autonomy_plugin/test_controls.py
PASS tests/autonomy_plugin/test_guard.py
PASS tests/autonomy_plugin/test_memory_first_order.py
PASS tests/autonomy_plugin/test_mode.py
PASS tests/autonomy_plugin/test_plugin_files.py
PASS tests/autonomy_plugin/test_prompt_rules.py
PASS tests/autonomy_plugin/test_resource_governance.py
PASS tests/autonomy_plugin/test_thought_log_cli.py
PASS tests/autonomy_plugin/test_transcript.py
PASS tests/test_autonomy_core.py
PASS tests/test_autonomy_quiet_start.py
PASS tests/test_gateway_runs.py
PASS tests/test_tick_exit_status.py
```

Ubuntu 24 测试机使用 Hermes 自带 CPython 3.14，并把 pytest 与 PyYAML 安装到独立临时 `--target` 目录：

```text
119 passed in 2.22s
```

测试目录随后已删除，没有修改目标 profile 的生产依赖、配置或数据。

Windows 本机结果：

```text
118 passed, 1 skipped in 6.13s
```

跳过项为 Linux 专有行为；没有弱化断言。

明确排除的过时测试：

```text
test_autonomy_plugin.py          # 旧接口契约
旧 stage 加载测试                # 旧仓库布局约定
种子归档仓库 fixture 测试        # 依赖未发布的仓库级 fixture
```

## Git 文件清单

```text
.gitignore
CONTRIBUTING.md
LICENSE
README.md
deploy/systemd/xiaozhi-autonomy.service
deploy/systemd/xiaozhi-autonomy.timer
docs/autonomy_plugin_usage.md
docs/install_ubuntu24_profile_xiaozhi.md
docs/publish_report.md
docs/插件说明.md
identity/LICENSE.md
identity/SOUL.md
plugins/autonomy/__init__.py
plugins/autonomy/activity.py
plugins/autonomy/audit.py
plugins/autonomy/budget.py
plugins/autonomy/circuit.py
plugins/autonomy/cli.py
plugins/autonomy/cli_transport.py
plugins/autonomy/config.example.yaml
plugins/autonomy/config.py
plugins/autonomy/controls.py
plugins/autonomy/db.py
plugins/autonomy/gateway_client.py
plugins/autonomy/guard.py
plugins/autonomy/memory_bridge.py
plugins/autonomy/mode.py
plugins/autonomy/plugin.yaml
plugins/autonomy/prompt.py
plugins/autonomy/proposals.py
plugins/autonomy/runner.py
plugins/autonomy/solitude.py
plugins/autonomy/thought_log.py
plugins/autonomy/tick.py
plugins/autonomy/transcript.py
plugins/autonomy/wizard.py
plugins/autonomy/插件说明.md
plugins/seed_memory/README.md
plugins/seed_memory/__init__.py
plugins/seed_memory/plugin.yaml
plugins/seed_memory/seed_src/hermes/__init__.py
plugins/seed_memory/seed_src/hermes/memory/__init__.py
plugins/seed_memory/seed_src/hermes/memory/retrieval.py
plugins/seed_memory/seed_src/hermes/memory/schema.py
plugins/seed_memory/seed_src/hermes/memory/store.py
plugins/seed_memory/worker.py
tests/autonomy_plugin/test_activity.py
tests/autonomy_plugin/test_controls.py
tests/autonomy_plugin/test_guard.py
tests/autonomy_plugin/test_memory_first_order.py
tests/autonomy_plugin/test_mode.py
tests/autonomy_plugin/test_plugin_files.py
tests/autonomy_plugin/test_prompt_rules.py
tests/autonomy_plugin/test_resource_governance.py
tests/autonomy_plugin/test_thought_log_cli.py
tests/autonomy_plugin/test_transcript.py
tests/test_autonomy_core.py
tests/test_autonomy_quiet_start.py
tests/test_gateway_runs.py
tests/test_tick_exit_status.py
tools/build_bundle.sh
tools/export_seeds_archive.py
tools/soak/run_soak.py
tools/verify_208.sh
tools/view_thoughts.ps1
```

共 65 个文件。

## Git 与推送策略

- 使用本机默认 SSH 配置，认证账号为仓库所有者；
- 基于远端 `main` 的现有历史普通提交；
- 保留并更新远端 MIT LICENSE 的版权行为 `Copyright (c) 2026 mmszxc`；
- README 使用本次公开版，旧版保留在 Git 历史中；
- 不 force push；
- 提交信息使用中文，并附指定 Co-Authored-By 行。
