# 无人值守调度示例

本文件仅用于用户明确要求的定时源码同步。调度命令固定使用 `sync apply --non-interactive`、JSON 输出和绝对配置文件路径；不使用 snapshot 模式。构建、安装与部署核验由独立任务处理。

将下列 `/ABSOLUTE/PATH/TO/...` 占位符替换为本机绝对路径。

## cron

每天 03:15 运行，并把单行 JSON 追加到专用日志：

```cron
15 3 * * * /ABSOLUTE/PATH/TO/data-infra-sync --config /ABSOLUTE/PATH/TO/workspace.conf --format json sync apply --non-interactive >>/ABSOLUTE/PATH/TO/data-infra-sync.jsonl 2>&1
```

## systemd user unit

`~/.config/systemd/user/data-infra-sync.service`：

```ini
[Unit]
Description=Synchronize the DataInfra composite checkout

[Service]
Type=oneshot
ExecStart=/ABSOLUTE/PATH/TO/data-infra-sync --config /ABSOLUTE/PATH/TO/workspace.conf --format json sync apply --non-interactive
```

`~/.config/systemd/user/data-infra-sync.timer`：

```ini
[Unit]
Description=Run DataInfra composite checkout synchronization daily

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
Unit=data-infra-sync.service

[Install]
WantedBy=timers.target
```

加载并启用：

```bash
systemctl --user daemon-reload
systemctl --user enable --now data-infra-sync.timer
```

## 退出处理

- 退出 0：读取 JSON state；`updated` 或 `up_to_date` 表示源码同步完成。
- 退出 2：安全停点，需要记录并通知处理。常见 state 为 `unconfigured`、`waiting_for_pin` 或 `blocked`；该次运行不修改工作树或本地分支。
- 退出 3：写入前失败，记录 JSON reason 并通知处理。
- 退出 4：发生 `partial`，保存完整 Result 和退出码，读取 [部分失败接管](partial-handoff.md) 并报告现场；不自动恢复。

systemd 默认会把退出 2 标记为失败，便于监控发现等待或阻塞状态。cron 需要由外层监控检查进程退出码与 JSON state。
