# PITFALLS

现场部署与运行避坑清单。每项固定为“现象 → 根因 → 对策”。示例不包含密钥或生产地址。

## A. Profile 安装

### 1. 无人值守初始化卡住

**现象** → Hermes 自带交互式 `autonomy init` 卡在模型选择。

**根因** → 该命令设计为人工交互，不是无人值守安装接口。

**对策** → 用安装向导一次收集 profile、模型、provider、endpoint、key_env 和错峰参数，再逐项执行门禁。

### 2. 误找 migration 命令

**现象** → 尝试运行 `autonomy migrate`，但命令不存在。

**根因** → DB 首次打开时自动 migration；版本表名是 `schema_meta`，不是 `schema_version`。

**对策** → 不调用 migrate；验收只读查询 `sqlite_master`，确认 `schema_meta` 及业务表存在。

### 3. seed_memory 不在插件目录

**现象** → memory provider 报 `not in plugin catalog` 或加载不到。

**根因** → 只配置 `memory.provider=seed_memory`，却漏了 `plugins.enabled`。

**对策** → `plugins.enabled` 同时列出 `autonomy` 和 `seed_memory`，并通过 plugins list 预检。

### 4. 空 profile 首 tick 达到 iteration limit

**现象** → 冷启动首 tick 重复返回 iteration-limit，无法生成 summary。

**根因** → 空 goals/events 没有有界启动合约，模型没有可完成的初始工作。

**对策** → 实施有界 bootstrap：明确最小启动状态、目标、步数与成功/停止条件，再允许正常 tick。

### 5. dormant flag 跨 profile 串扰

**现象** → 一个 profile 读到了另一个 profile 的 dormant flag。

**根因** → 代码曾硬编码固定 profile 路径。

**对策** → 按当前 profile 的 data directory 和 Hermes home/seed 目录解析，禁止固定用户名或 profile 路径。

### 6. profile-local 密钥未被模型使用

**现象** → `.env` 中 key 看似正确，但 tick 仍报 model connection/request failure。

**根因** → tick 进程只会使用其实际环境；文件存在不等于 Hermes core 已加载，且 `key_env` 名称必须一致。

**对策** → 确认当前 profile 的 `.env`（权限 `0600`）被启动环境加载，`key_env` 与变量名完全一致；不把值写入日志或文档。

### 7. 模型过载

**现象** → 模型返回 HTTP 503 或 `overloaded`。

**根因** → provider 当前容量不足，不是本地 DB migration 问题。

**对策** → 停止重试风暴，先查可用模型清单，再切换可用 provider/model。

### 8. sub2api endpoint scheme 错误

**现象** → HTTP endpoint 返回 301，tick 的 request 失败；HTTPS endpoint 可用。

**根因** → endpoint 强制 TLS/HTTPS，明文 HTTP 只做重定向，客户端未必跟随或重定向后请求方法/认证不兼容。

**对策** → 配置并探测 HTTPS `/v1` endpoint；先用 `/v1/models` 做不泄密的可用性门禁。

### 9. seed_memory 进入只读 gray mode

**现象** → 能读已有种子，但新种子不写入。

**根因** → `memory.seed_memory.write_enabled` 缺失或为 false。

**对策** → 显式设置 `write_enabled: true`，并验收 seeds DB 的写入增长；同时确认 source_root 与 seed queue 指向当前 profile。

## B. 记忆系统

### 1. 语义提炼从未生效

**现象** → tick 运行，但 `semantic_memories` 表不存在或始终为空。

**根因** → `init_db` 漏建 `CREATE TABLE semantic_memories`。

**对策** → 将表创建纳入初始化与 fixture 门禁，并查询 `sqlite_master` 验证。

### 2. 语义质量字段是占位值

**现象** → 所有语义记忆都是 `world_model`、固定 confidence/evidence_count，或 embedding 写入 TEXT。

**根因** → 字段由占位常量填充；float32 bytes 与 TEXT 列类型错配。

**对策** → category 从来源种子类型确定，confidence/evidence_count 从实际证据计算；embedding 使用 BLOB 写法，或在向量能力落地前不写 embedding。

### 3. 衰减字段未参与排序

**现象** → seeds 有 `decay_rate`，但长期未访问的记忆排名不变。

**根因** → `get_ranked_seeds` 没有应用衰减计算。

**对策** → 在排序公式和 fixture 中显式应用 decay，并验证时间推进后排名变化。

### 4. observe-only 拦截种子写入

**现象** → 自主 profile 的 tick 成功，却记不住新东西。

**根因** → P0 `observe_only` 模式拦截 autonomy tick 的种子写入。

**对策** → 需要持久化记忆的 profile 切到 normal/允许写入模式，并单独验收写回。

### 5. seed worker 不启动

**现象** → seed queue 积压，worker 启动即 `AttributeError`。

**根因** → `runner.py` 某处对字符串 `self.hermes_home` 使用 `.name`；另一处已正确用 `Path(...).name`。

**对策** → 统一先 `Path(self.hermes_home).name`，并用 detached worker fixture 覆盖两种 Hermes home 输入。

## C. 部署与传输

### 1. systemd unit 被破坏

**现象** → systemd 报 `Invalid section header`，unit 内容变成单行。

**根因** → PowerShell 文本管道/`Out-File` 改写了换行或编码。

**对策** → 使用 Git object 原始 bytes 传输；不要用 PowerShell 文本管道或 `Out-File` 写 unit。

### 2. 传输后代码不一致

**现象** → 目标机文件存在，但与目标提交内容不一致。

**根因** → 文本转换、换行转换或复制时未校验对象。

**对策** → 从 Git object 读取原始 bytes，逐文件用 Git blob SHA-1 校验后再原子安装。

### 3. 替换 profile 或 state.db 触发句柄错误

**现象** → 出现 `StateDbReplacedError`，autonomy 还可能误报 `model connection request_failed`。

**根因** → 共享常驻 gateway 仍持有旧 inode；整目录或 state.db 替换破坏了句柄契约。

**对策** → 禁止整 profile/state.db 替换；只做逐文件更新，保留 state.db inode。

### 4. 句柄未刷新

**现象** → 配置已改，但 gateway 继续使用旧配置或旧文件句柄。

**根因** → 未执行 profile 级刷新，或误重启共享 gateway。

**对策** → 使用官方 `hermes -p <profile> gateway restart`，确认 ACK、共享 PID 不变且其他 profile 不受影响。

### 5. 生产热修不可回滚

**现象** → 热修失败后无法判断哪些文件已改变。

**根因** → 修改前未做带时间戳的文件级备份，也未保留命令/返回码证据。

**对策** → 修改前 `cp` 备份目标文件；每步记录时间、命令、rc、输出路径；失败立即逐项回滚并再次 profile restart。

### 6. 共享工作区误伤身份文件

**现象** → 其他成员的身份/行为突然改变。

**根因** → 多成员共享工作区时误修改 `identity/SOUL.md` 等不属于任务范围的文件。

**对策** → 只 `git add` 指定文件，修改前后检查 status/diff；严禁 `commit -am` 或整批暂存。
