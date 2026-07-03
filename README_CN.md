

<div align="center">

  <img src="https://socialify.git.ci/KrsMt-0113/XMU-Rollcall-Bot/image?font=JetBrains+Mono&forks=1&language=1&name=1&owner=1&pattern=Plus&stargazers=1&theme=Light" />

</div>

<div align="center">

  <img src="https://img.shields.io/github/directory-file-count/KrsMt-0113/XMU-Rollcall-Bot" />
  <img src="https://img.shields.io/github/languages/code-size/KrsMt-0113/XMU-Rollcall-Bot" />

</div>

[English](README.md)

[使用手册](https://krsmt.notion.site/cli-doc)

> [!WARNING]
> 2026-05-12 之前的登录方式已被弃用。使用 `pip install -U xmulogin` 更新相关组件到最新版本。
>
> 2026-05-15 ~登录组件出现新的问题，等待进一步排查修复。~
>
> 2026-05-15 降级`xmulogin`组件以恢复正常: `pip install xmulogin==1.0.0`

## 快速开始

```bash
pip install xmu-rollcall-cli
```

## 使用方法

```bash
xmu config  # 配置账号。使用统一身份认证账号密码。
xmu switch  # 切换账号。
xmu start   # 启动监控。
```

## 当前能力

- 使用 XMU 统一身份认证登录，并缓存 session cookie。
- 每 1 秒轮询一次 `https://lnt.xmu.edu.cn/api/radar/rollcalls`。
- 支持多账号配置和当前账号切换。
- 支持数字码签到、雷达签到。
- 集成版支持二维码签到：检测到二维码签到后，会通过 Telegram 推送一次性扫码链接。
- 所有成功签到方式都会发送成功反馈通知。

## 签到类型判断逻辑

监控进程发现新的 rollcall 后，按以下顺序处理：

1. `status == on_call_fine`：认为已经签到，不重复提交。
2. `is_expired == true`：已过期，不提交。
3. `status != absent`：不是待签到状态，不提交。
4. `status == absent && is_radar == true`：执行雷达签到。
5. `status == absent && is_number == true`：执行数字码签到。
6. `status == absent && !is_radar && !is_number`：按二维码签到处理。

数字码、雷达、二维码任一方式签到成功后，都会通过当前通知渠道发送成功反馈。

## 二维码签到与 Telegram 通知

二维码签到默认关闭，需要在本机配置文件中显式开启。配置文件默认位置：

```bash
~/.xmu_rollcall/config.json
```

示例配置片段：

```json
{
  "qr": {
    "enabled": true,
    "ngrok_token": "YOUR_NGROK_TOKEN",
    "session_timeout": 180,
    "flask_port": 5001
  },
  "notification": {
    "provider": "telegram",
    "telegram_bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "YOUR_TELEGRAM_CHAT_ID"
  }
}
```

安全建议：

- 不要把 `telegram_bot_token`、`telegram_chat_id`、`ngrok_token` 提交到 git。
- 配置完成后建议执行：

```bash
chmod 600 ~/.xmu_rollcall/config.json
```

二维码签到流程：

1. 监控到二维码签到。
2. 本机懒启动 Flask 扫码服务。
3. 使用 ngrok 暴露临时 HTTPS 链接。
4. Telegram 推送课程名、发起人、一次性扫码链接和有效期。
5. 手机打开链接并扫描课堂二维码。
6. 本机复用已登录 `requests.Session` 调用 `answer_qr_rollcall` 完成提交。
7. 成功后发送 Telegram 成功反馈。

## macOS 开机自动运行

本仓库包含本机 `launchd` 配置脚本：

- `scripts/run-xmu-rollcall.sh`：实际运行包装脚本。
- `scripts/com.lifuyue.xmu-rollcall.plist`：LaunchAgent 配置。
- `scripts/install-launch-agent.sh`：安装、启用并拉起服务。

安装或重新拉起：

```bash
/Users/lifuyue/Projects/XMU-Rollcall-Bot/scripts/install-launch-agent.sh
```

查看运行状态：

```bash
launchctl print gui/$(id -u)/com.lifuyue.xmu-rollcall
pgrep -af 'xmu_rollcall.cli|run-xmu-rollcall'
```

查看日志：

```bash
tail -f ~/.xmu_rollcall/logs/launchd.out.log
tail -f ~/.xmu_rollcall/logs/launchd.err.log
```

手动重启服务：

```bash
launchctl kickstart -k gui/$(id -u)/com.lifuyue.xmu-rollcall
```

卸载当前用户 LaunchAgent：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.lifuyue.xmu-rollcall.plist
```

## 本机开发/验证命令

安装当前工作区包和依赖：

```bash
uv pip install --python .venv/bin/python -e ./xmu-rollcall-cli
```

运行检查：

```bash
.venv/bin/python -m compileall -q xmu-rollcall-cli/xmu_rollcall
.venv/bin/python -m unittest discover -s xmu-rollcall-cli/tests -v
uv pip check --python .venv/bin/python
```

## 其他

[数字化教学平台附件下载器](https://chromewebstore.google.com/detail/imannochailfofibofphcpmlddlbbhao?utm_source=item-share-cb)
