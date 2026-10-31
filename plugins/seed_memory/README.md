# seed_memory

Hermes Agent 的结构化长期记忆插件：将会话经历保存为带情绪、重要度和因果关系的记忆种子，并提供检索与后台队列处理。

## 能力

- SQLite 只读/读写模式由 profile 配置控制。
- `recent_seeds` 可注入提取提示，帮助模型保持上下文连续性。
- `caused_by` 与 `led_to` 保存种子之间的因果链。
- 重复内容通过内容指纹去重，并将 inserted/duplicate/rejected 写入数据目录的 `stats.json`。
- 队列单文件连续失败超过阈值后移入 `_failed/`，保留 `last_error` 供诊断，不阻塞后续文件。
- 本地 embedding 不可用时回退到重要度与时效排序。

## Tagger 稳定性配置

以下配置默认启用：

```yaml
memory:
  seed_memory:
    tagger_retries: 3          # 2s、6s、15s，并附加不超过 0.5s 的抖动
```

每个 profile 独立配置标注模型，互不共享锁或运行状态。5xx、超时和连接类瞬态错误会保留队列文件供下一轮重试，不会立即移入 `_failed/`。

运行数据、模型、密钥和 profile 配置不属于公开仓库内容。
