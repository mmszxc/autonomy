# xiaozhi 独立嵌入依赖

## 嵌入依赖

安装目标：`/home/hermes/.hermes/profiles/xiaozhi/plugins/_vendor_ml`（仅 hermes 用户；不修改系统包、default profile 或上游 venv）。Python 为 Hermes 自带 3.14.9，安装总量约 1.4G，机器剩余空间约 34G。

已安装版本（目标服务器）：

- torch 2.14.1+cpu（PyTorch CPU wheel）
- sentence-transformers 6.2.0
- transformers 5.21.0
- tokenizers 0.23.3
- safetensors 0.8.0
- huggingface-hub 2.3.0
- scikit-learn 1.9.1
- scipy 1.18.1、numpy 2.5.4
- PyYAML 6.0.3、narwhals 2.28.0 及其运行依赖

离线验证命令设置 `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`，模型使用已复制的本地 `paraphrase-multilingual-MiniLM-L12-v2`，编码三句中文结果：

```text
torch 2.14.1+cpu
dims (3, 384)
sim_related 0.9414143562316895
sim_unrelated -0.14541803300380707
ordering True
```

模型未访问网络，向量维度 384，语义相近句相似度高于不相近句。`embedding` 依赖安装在 `_vendor_ml`，与 xiaozhi 的 `_vendor` 分离；入口需在存在时将两个目录加入 `sys.path`。

## 每小时自动思考定时器

目标 profile 可使用 hermes 用户级 systemd 定时器，每小时在第 00 和 20 分钟附近触发（最多 30 秒随机延迟）：

- 单元位置：`~/.config/systemd/user/xiaozhi-autonomy.service` 与 `xiaozhi-autonomy.timer`
- 服务执行：`hermes -p <PROFILE> autonomy tick --hermes-home <HERMES_HOME>`
- 查看定时器：`systemctl --user list-timers --all`
- 查看状态：`hermes -p xiaozhi autonomy status`
- 查看最近思考：`hermes -p xiaozhi autonomy log -n 1`
- 关闭：`systemctl --user disable --now xiaozhi-autonomy.timer`
- 重新开启：`systemctl --user enable --now xiaozhi-autonomy.timer`

服务应显式传入 `--hermes-home`：systemd 用户服务没有交互式 shell 上下文，显式传入可以避免 profile 解析歧义，并确保插件使用目标 profile。运行目录以配置中的 `data_dir` 为准。

该定时器不创建系统级 service，不写入密钥，也不会修改其他 profile。若用户级 systemd 已启用 linger，重启后定时器会自动恢复；可用 `systemctl --user is-enabled <PROFILE>-autonomy.timer` 和 `systemctl --user is-active <PROFILE>-autonomy.timer` 检查。