# xmu-rollcall-cli

A command-line tool for monitoring and auto-answering Tronclass rollcalls at Xiamen University.

> This project is intended for personal learning and automation convenience. Use it at your own risk and comply with your school's rules.

## Features

- Login with XMU unified authentication through `xmulogin`
- Continuous rollcall polling (1-second interval)
- Automatic handling for:
  - Number rollcalls (fetch number code and answer directly)
  - Radar rollcalls (location solving)
- QR rollcalls through a one-time HTTPS scanner link
- Notification abstraction with log, Telegram, Feishu, and WeChat support
- Multi-account management in one local config
- Multi-account monitoring in one process
- Session cookie cache and refresh support

## Installation

Install from PyPI:

```bash
pip install xmu-rollcall-cli
```

After installation, these command aliases are available:

- `xmu`
- `xmu-rollcall-cli`
- `XMUrollcall-cli`

## Quick Start

1. Configure at least one account:

```bash
xmu add-account
```

2. (Optional) Switch active account:

```bash
xmu switch
```

3. Start monitoring the current account:

```bash
xmu start
```

4. Start monitoring every complete account:

```bash
xmu start-all
```

5. If session becomes invalid, refresh cookies:

```bash
xmu refresh
```

## Commands

- `xmu config` - Add/delete accounts and set current account
- `xmu add-account` - Add one account quickly
- `xmu switch` - Switch the current account
- `xmu start` - Start rollcall monitoring loop
- `xmu start-all` - Start all configured complete accounts in one process
- `xmu refresh` - Remove cached cookies for current account
- `xmu --help` - Show help

## Configuration

The package stores local data in a `.xmu_rollcall` directory:

1. `XMU_ROLLCALL_CONFIG_DIR` (if set)
2. `~/.xmu_rollcall` (default)
3. `./.xmu_rollcall` (fallback when home is not writable)

Main files:

- `config.json`: account list and selected account
- `<account_id>.json`: cached cookies per account

Example (custom config directory):

```bash
export XMU_ROLLCALL_CONFIG_DIR="$HOME/Documents/.xmu_rollcall"
```

### Multi-Account Monitoring

Add several accounts with:

```bash
xmu add-account
xmu add-account
```

Start all complete accounts:

```bash
xmu start-all
```

Start selected accounts only:

```bash
xmu start-all --account-id 1 --account-id 2
```

The multi-account monitor uses one Python process, one lightweight thread per account, and one `requests.Session` per account. Each account keeps its own cookie cache as `<account_id>.json`. The default polling interval is 1 second per account, with staggered account starts to avoid request bursts. QR scanner/ngrok startup remains lazy and shared across accounts.

### QR Rollcalls

QR support is disabled unless `qr.enabled` is set to `true`.

Example:

```json
{
  "qr": {
    "enabled": true,
    "ngrok_token": "YOUR_NGROK_TOKEN",
    "session_timeout": 180,
    "flask_port": 5001
  }
}
```

When a QR rollcall is detected, the client starts a local Flask scanner server lazily, exposes it through ngrok, sends the one-time scanner link through the configured notifier, waits for the scanned QR payload, then submits:

```text
PUT https://lnt.xmu.edu.cn/api/rollcall/{rollcallId}/answer_qr_rollcall
```

The existing logged-in `requests.Session` is reused. The QR flow does not store another account or perform another login.

### Notifications

Supported providers:

- `log`: print notifications to stdout.
- `telegram`: send notifications through Telegram Bot API.
- `feishu`: send notifications through a Feishu custom bot webhook.
- `wechat`: send notifications through the WeChat notifier.

Telegram example:

```json
{
  "notification": {
    "provider": "telegram",
    "telegram_bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "YOUR_TELEGRAM_CHAT_ID"
  }
}
```

Feishu custom bot example:

```json
{
  "notification": {
    "provider": "feishu",
    "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_WEBHOOK_ID",
    "feishu_sign_secret": "YOUR_FEISHU_SIGN_SECRET"
  }
}
```

`feishu_sign_secret` is optional. Set it only when signature verification is enabled for the Feishu custom bot.

Environment variable overrides are supported:

- `XMU_ROLLCALL_QR_ENABLED`
- `XMU_ROLLCALL_NGROK_TOKEN`
- `XMU_ROLLCALL_QR_SESSION_TIMEOUT`
- `XMU_ROLLCALL_QR_FLASK_PORT`
- `XMU_ROLLCALL_NOTIFICATION_PROVIDER`
- `XMU_ROLLCALL_TELEGRAM_BOT_TOKEN`
- `XMU_ROLLCALL_TELEGRAM_CHAT_ID`
- `XMU_ROLLCALL_FEISHU_WEBHOOK_URL`
- `XMU_ROLLCALL_FEISHU_SIGN_SECRET`
- `XMU_ROLLCALL_FEISHU_TIMEOUT`

Do not commit local credentials. Use restrictive file permissions:

```bash
chmod 600 ~/.xmu_rollcall/config.json
```

### Rollcall Type Handling

The client handles detected rollcalls in this order:

1. `status == on_call_fine`: already answered.
2. `is_expired == true`: expired.
3. `status != absent`: no answer attempt.
4. `status == absent && is_radar == true`: radar rollcall.
5. `status == absent && is_number == true`: number rollcall.
6. `status == absent && !is_radar && !is_number`: QR rollcall.

Successful number, radar, and QR submissions all emit success notifications.

### macOS LaunchAgent

This workspace includes helper scripts for local persistent execution on macOS:

- `scripts/run-xmu-rollcall.sh` (runs `xmu start-all` by default)
- `scripts/com.lifuyue.xmu-rollcall.plist`
- `scripts/install-launch-agent.sh`

Install or re-install the LaunchAgent:

```bash
/Users/lifuyue/Projects/XMU-Rollcall-Bot/scripts/install-launch-agent.sh
```

Check status:

```bash
launchctl print gui/$(id -u)/com.lifuyue.xmu-rollcall
```

View logs:

```bash
tail -f ~/.xmu_rollcall/logs/launchd.out.log
tail -f ~/.xmu_rollcall/logs/launchd.err.log
```

## Limitations

- This tool depends on Tronclass/XMU API behavior and may break if upstream endpoints change.

## Supported Python Versions

- Python 3.7+

## Project Links

- Homepage: https://github.com/KrsMt-0113/XMU-Rollcall-Bot
- Issues: https://github.com/KrsMt-0113/XMU-Rollcall-Bot/issues

## License

MIT License
