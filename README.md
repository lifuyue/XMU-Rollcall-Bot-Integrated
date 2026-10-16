

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

## Attendance Threshold

Number and radar submissions require **at least 30% and at least 10 other students**
to have attended. All complete configured accounts are excluded from both counts,
including accounts omitted by `--account-id`. Student identities are verified with
`/api/profile`; an unresolved managed account blocks automatic submission until its
cached session is valid. Only `on_call` and `on_call_fine` count as attended.

For number rollcalls, `/api/timetable_rollcalls?course_ids=<course_id>&rollcall_date=<yyyy-mm-dd>`
supplies the attendance roster and code, matched by course ID, UTC date, and rollcall ID.
Radar rollcalls read their roster from `/api/rollcall/<rollcall_id>` and verify the
rollcall ID, course ID, radar type, and timestamps, since timetable results may omit
radar rollcalls. Both sources use the same attendance threshold below.
Pending rollcalls are checked again each poll, even when discovery data is unchanged.
Submission proceeds immediately once both thresholds are met, without an extra delay.
Finished/expired rollcalls are skipped; API failures, missing data, duplicate student
IDs, and unavailable codes block submission. Counts and allow decisions are never cached.
Classes with fewer than 10 other students cannot pass the threshold. QR scanning
and success notifications retain their existing behavior; waiting reasons are logged.

Each account gets one radar campaign per rollcall, after the attendance threshold
passes. Its **60-second total budget includes the original distance method**, status
queries, fixed-location submissions, and the one-second pause between search rounds;
waiting for the attendance threshold does not count. The original probes and circle
intersections run once, with a **10-second hard limit from the start of the distance
phase**, including its status GETs and answer PUTs. Failures such as missing distance
or non-intersecting circles switch to fallback immediately. If the 10-second limit
expires, terminate the distance worker first, then use the remaining 60-second total
budget for at most two fixed-location search rounds. Neither transition resets the
total budget or the persisted campaign claim. Each round first
tries the three centers in order: **latitude 24.6082, longitude 118.2953**;
**latitude 24.6087, longitude 118.3090**; **latitude 24.6075, longitude 118.2950**
(24°36′27″ N, 118°17′42″ E). Then try each center's east, south, west, and north
points 100 metres away, in center order. Each of the 15 points is tried once per
round. Preserve the centers' four decimals and full computed precision for offsets.
The maximum is 30 fallback PUTs plus four original-method PUTs, subject to the budget.

Before every PUT, query the rollcall detail and verify the activity and own attendance.
Stop on HTTP 200, an already-attended status, a finished/expired activity, unverifiable
status, HTTP 401/429, total-budget expiry, or exhausted rounds. Fixed responses need no
distance or JSON body. The payload fields and attendance thresholds are unchanged;
request timeouts shrink to the remaining phase and total budgets. Distance and fallback
work run in spawned child processes that are terminated at their deadlines, including
when DNS or a slow response blocks requests. A request already received by the server
cannot be recalled.

Claims are atomically persisted in `~/.xmu_rollcall/radar_attempts.json` before starting
the first child. Later polls and service restarts never restart a claimed campaign, including
one interrupted mid-flight. Failure to save the claim prevents submission. The persistent
lock requires POSIX `fcntl` (Linux/macOS); unsupported platforms stop without submitting.

Radar submission emits `[Radar]` JSON diagnostics with the rollcall ID, account
worker (for example `xmu-account-1`), probe/candidate/fixed attempt, round, processing phase, and
HTTP status (`null` when no response arrived). Exceptions include their concrete
type and a fixed reason; JSON errors include line/column, and distance errors
include field types. Request errors, malformed JSON, missing/invalid distance,
non-intersecting circles, and rejected candidates can be distinguished without
logging raw exception text, response bodies, cookies, device IDs, or coordinates.
Non-200 probe responses may carry valid distances; check subsequent `exception`
or `failed` events before treating them as failures. A `fallback_start` event marks
the switch to fixed locations; `fixed_1/2/3` identify centers, and direction suffixes
such as `fixed_1_east` identify offsets. `status_response`, `round_start/round_end`,
`success`, and `stopped` identify status checks, rounds, and termination. Campaign
logs separately identify the persisted claim and process timeout without logging secrets.

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
