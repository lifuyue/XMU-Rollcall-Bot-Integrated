

<div align="center">

  <img src="https://socialify.git.ci/KrsMt-0113/XMU-Rollcall-Bot/image?font=JetBrains+Mono&forks=1&language=1&name=1&owner=1&pattern=Plus&stargazers=1&theme=Light" />

</div>

<div align="center">

  <img src="https://img.shields.io/github/directory-file-count/KrsMt-0113/XMU-Rollcall-Bot" />
  <img src="https://img.shields.io/github/languages/code-size/KrsMt-0113/XMU-Rollcall-Bot" />

</div>

[简体中文](README_CN.md)

> [!WARNING]
> 2026-05-12 Previous login method has been deprecated. Update with `pip install -U xmulogin` to get the latest version.
>
> 2026-05-15 ~New issue with login process. Stay tuned for updates.~
>
> 2026-05-15 Downgrade `xmulogin` with `pip install xmulogin==1.0.0` to fix it.

## Install

```bash
pip install xmu-rollcall-cli
```

## Usage

```bash
xmu add-account  # add one account quickly.
xmu config       # manage accounts.
xmu start        # monitor the current account.
xmu start-all    # monitor all configured accounts.
```

## Features

- XMU unified-auth login with cached session cookies.
- 1-second polling against `https://lnt.xmu.edu.cn/api/radar/rollcalls`.
- Multi-account monitoring in one process.
- Number rollcall answering.
- Radar rollcall answering.
- Integrated QR rollcall flow with a one-time scanner link.
- Telegram notifications for QR links and successful rollcall answers.
- macOS LaunchAgent scripts for local persistent execution.

## Rollcall Handling Order

When a rollcall is detected, the client handles it in this order:

1. `status == on_call_fine`: already answered; no submission.
2. `is_expired == true`: expired; no submission.
3. `status != absent`: not answerable; no submission.
4. `status == absent && is_radar == true`: radar rollcall.
5. `status == absent && is_number == true`: number rollcall.
6. `status == absent && !is_radar && !is_number`: QR rollcall.

Successful number, radar, and QR answers all emit a success notification.

## Multi-Account Monitoring

Accounts are stored in the same local config file. Add several accounts with:

```bash
xmu add-account
xmu add-account
```

Start every complete account:

```bash
xmu start-all
```

Start selected accounts only:

```bash
xmu start-all --account-id 1 --account-id 2
```

Resource model:

- One Python process, not one process per account.
- One lightweight thread and one `requests.Session` per account.
- Independent cookie cache per account: `<account_id>.json`.
- Default polling is 1 second per account, with staggered thread starts to reduce request bursts.
- The QR scanner service remains lazy and shared; it starts only when a QR rollcall is detected.

## QR and Telegram Configuration

Local configuration is stored at `~/.xmu_rollcall/config.json` by default.

Example:

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

Keep tokens out of git. After writing local credentials, restrict permissions:

```bash
chmod 600 ~/.xmu_rollcall/config.json
```

QR flow:

1. The monitor detects a QR rollcall.
2. A local Flask scanner server starts lazily.
3. ngrok exposes a temporary HTTPS scan link.
4. Telegram sends course info, creator info, scan link, and expiry.
5. You open the link on your phone and scan the classroom QR code.
6. The existing logged-in `requests.Session` submits `answer_qr_rollcall`.
7. Telegram sends a success notification.

## macOS Persistent Service

This local workspace includes LaunchAgent helper scripts:

- `scripts/run-xmu-rollcall.sh` (runs `xmu start-all` by default)
- `scripts/com.lifuyue.xmu-rollcall.plist`
- `scripts/install-launch-agent.sh`

Install or re-install the service:

```bash
/Users/lifuyue/Projects/XMU-Rollcall-Bot/scripts/install-launch-agent.sh
```

Check status and logs:

```bash
launchctl print gui/$(id -u)/com.lifuyue.xmu-rollcall
tail -f ~/.xmu_rollcall/logs/launchd.out.log
tail -f ~/.xmu_rollcall/logs/launchd.err.log
```

Restart:

```bash
launchctl kickstart -k gui/$(id -u)/com.lifuyue.xmu-rollcall
```

## Other

[XMU File Downloader](https://chromewebstore.google.com/detail/imannochailfofibofphcpmlddlbbhao?utm_source=item-share-cb)
