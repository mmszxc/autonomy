# seed_memory

Hermes Agent 的 Seed MemoryProvider 适配器，提供结构化长期记忆、语义检索、会话结束提取和后台队列 worker。

## 目录约定

```text
plugins/seed_memory/
├── __init__.py
├── worker.py
├── plugin.yaml
└── seed_src/hermes/     # 最小核心代码；模型需另行准备
```

在 profile 的 `config.yaml` 中设置：

```yaml
memory:
  provider: seed_memory
  seed_memory:
    source_root: <HERMES_HOME>/plugins/seed_memory/seed_src
    data_dir: <HERMES_HOME>/seed_data
    db_path: <HERMES_HOME>/seed_data/seeds/seeds.db
    queue_dir: <HERMES_HOME>/seed_data/seed_queue
    write_enabled: false
```

建议先使用只读灰度模式。此时检索和自动预取可用，写入工具会返回只读错误；审核通过后再将 `write_enabled` 改为 `true`。

嵌入模型采用惰性、离线加载。如果模型缺失或不完整，检索会退化为重要度和时效性排序。本仓库不包含模型、`_vendor` 或 `_vendor_ml`，请按根目录 README 单独准备。

插件本身不会创建 cron、systemd service 或 timer。
