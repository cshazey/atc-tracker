# Discord Setup & Channel Reference

ATC Tracker forwards every transcription to Discord *alongside* Telegram (dual-send — neither replaces the other). Each station gets its own channel instead of one shared firehose, keyword matches also mirror into `#alerts`, and full command control (`/mute`, `/pause`, etc.) is available from a private `#commands` channel.

---

## One-time setup

### 1. Create the bot
1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** tab → **Reset Token** / **Copy** → paste into `.env` as `DISCORD_BOT_TOKEN`. Never commit this.
3. **Bot** tab → scroll to **Privileged Gateway Intents** → toggle **Message Content Intent** ON → Save Changes. This is required even though the tracker never opens a gateway/websocket connection — Discord blanks the `content` field on REST message fetches too unless this is enabled, which is what the `#commands` polling loop reads to parse `/mute`, `/pause`, etc. Without it, every command comes back as an empty string and nothing happens. No Discord review is needed for this at low user counts (under 10,000).

### 2. Invite it to your server
**OAuth2 → URL Generator** → scope `bot` → permissions **View Channel**, **Send Messages**, **Embed Links**, **Read Message History**. Open the generated URL, pick your server, authorize.

**Embed Links is easy to miss and required** — almost every message this bot sends (transmissions, status broadcasts, alerts) is an embed, not plain text. Without Embed Links the bot can send plain-text replies (like `/help`) but every embed post gets silently rejected with `403 Missing Permissions` — the tracker only recently gained code to actually surface that error in the terminal, so it's worth double-checking this permission is granted everywhere.

### 3. Create the channels
One per station + `#alerts` + `#commands` (see table below). Make `#commands` private: right-click → **Edit Channel → Permissions** → deny `@everyone` View Channel, then explicitly add the bot (and yourself) with View Channel + Send Messages + Embed Links + Read Message History.

If you lock down any other channel (e.g. to stop regular members posting), make sure the bot keeps an explicit **allow** override for View Channel + Send Messages + **Embed Links** + Read Message History on that channel — a category-level "prevent users posting" change can silently strip the bot's permissions too if it doesn't have its own explicit allow.

### 4. Get each channel's ID
Discord client → **User Settings → Advanced → Developer Mode** (on) → right-click a channel → **Copy Channel ID**.

### 5. Fill in `.env`
```
DISCORD_BOT_TOKEN=
DISCORD_ALERTS_CHANNEL_ID=
DISCORD_COMMANDS_CHANNEL_ID=
DISCORD_CHANNEL_YBCG=
DISCORD_CHANNEL_YSPT=
DISCORD_CHANNEL_YBBN=
```
Discord is active once `DISCORD_BOT_TOKEN`, `DISCORD_ALERTS_CHANNEL_ID`, `DISCORD_COMMANDS_CHANNEL_ID`, and at least one `DISCORD_CHANNEL_<ICAO>` are set (`config.DISCORD_ENABLED`). The terminal header shows `Discord: ON` when this is satisfied.

---

## Channel reference

| Category | Channel | Feeds from | `.env` var |
|---|---|---|---|
| ctafs | `#ybcg-brisbane-center` | YBCG Brisbane Centre | `DISCORD_CHANNEL_YBCG` |
| ctafs | `#yspt-southport-ctaf` | YSPT Southport | `DISCORD_CHANNEL_YSPT` |
| ctafs | `#ybbn-brisbane-center` | YBBN Brisbane Tower | `DISCORD_CHANNEL_YBBN` |
| alerts | `#alerts` | Every station (emergency + interest matches) | `DISCORD_ALERTS_CHANNEL_ID` |
| admin | `#commands` (private) | — (control channel) | `DISCORD_COMMANDS_CHANNEL_ID` |

### `#ybcg-brisbane-center` / `#yspt-southport-ctaf` / `#ybbn-brisbane-center`
**Purpose:** live transcript feed for that station only.
**Posts when:** every transmission on that station (not just keyword matches).
**Example:**
> 📻 **YBCG Brisbane Centre**
> Golf Bravo Charlie cleared runway two eight
> *14:32:01 AEST / 04:32:01Z · YBCG_143201.wav*

The footer names the saved audio file for that transmission (`recordings/<date>/<ICAO>_<HHMMSS>.wav`), so any transcript can be traced back to what was actually said. Omitted when recording is off.

The timestamp (both the footer text and Discord's own clock-formatted embed timestamp) reflects **when the transmission was received** — i.e. when the radio call ended and VAD flushed the buffer — not when the message was posted. Transcription runs after that, so for a long or queued transmission the actual Discord message can land a little later; the timestamp still reflects the original receipt moment, not the post moment.

Also receives a small status embed whenever:
- the station is muted/unmuted (via `/mute`/`/unmute` from either Telegram or Discord, **or** a keyboard number-key toggle in the terminal)
- the tracker is globally paused/resumed (via `/pause`/`/resume` or the `P` key)
- the LiveATC stream connects for the first time, or reconnects after dropping (🟢 Connected / 🔴 Disconnected — only posted on the transition, not on every retry, so a persistently flaky feed doesn't spam the channel)

Connection status uses **one pinned message per channel, created at startup and edited in place** on every subsequent transition. Earlier versions posted and pinned a fresh message each time, which spams the channel and walks into Discord's hard cap of 50 pins per channel over a long event. Requires the bot to have **Read Message History** and **Manage Messages** on that channel in addition to Send Messages/Embed Links (pinning needs to read the target message as well as manage the pin itself).

**Suggested channel topic:** `Live ATC transcript — <ICAO> <Station Name>. Muted/paused/connection status posts here too.`

### `#alerts`
**Purpose:** cross-station visibility for anything matching a monitored keyword. Alerts are **two-tier** — see `KEYWORDS_EMERGENCY` and `KEYWORDS_INTEREST` in `config.py`.

| Tier | Matches | Behaviour |
|---|---|---|
| 🚨 Emergency | MAYDAY, PAN PAN, EMERGENCY, squawk 7700/7600/7500, GUARD | Red embed **and an `@here` ping** naming the matched term |
| 🔴 Interest | Display and military callsigns (HORNET, ROULETTES, F-35, WARBIRD, FORMATION…), RESTRICTED, COASTAL | Orange embed, **no ping** |

**Posts when:** any station's transmission matches — mirrors the same transmission that also posted to its own station channel.

Only the emergency tier pings, deliberately: an airshow weekend generates a constant stream of interest-tier matches, and pinging on all of them would train everyone to ignore the channel. Bare numeric keywords (`"18"`, `"500"`) were removed for the same reason — they fired on every "runway 18" and "500 feet".

Outbound mentions are otherwise suppressed via `allowed_mentions`, so a transcript that happens to contain "@everyone" cannot ping the server.

**Suggested channel topic:** `Alert mirror from every station — @here on emergencies only.`

### `#commands` (private)
**Purpose:** bidirectional control, equivalent to the Telegram bot chat.
**Commands:**
| Command | Effect |
|---|---|
| `/help` | List commands |
| `/status` | Show every station's mute state, keyword toggle, pause state |
| `/mute <N\|ICAO\|all>` | Mute a station (or all) |
| `/unmute <N\|ICAO\|all>` | Unmute a station (or all) |
| `/url <N\|ICAO>` | Show the active stream URL for one station |
| `/urls` | Show all active stream URLs |
| `/seturl <N\|ICAO> https://...` | Update a station stream URL and reconnect it |
| `/reseturl <N\|ICAO>` | Restore a station stream URL to the default in `config.py` |
| `/keywords on\|off` | Toggle terminal keyword highlighting |
| `/pause` / `/resume` | Suspend/resume transcription & forwarding on every station |
| `/health` | Per-station last audio, last TX, reconnect and dropped counts, active URL, transcriber queue depth, gated count, outbox depth |
| `/reconnect <N\|ICAO>` | Force a station to drop and redial its stream |
| `/vad <N\|ICAO> [value]` | Show or set that station's VAD threshold live |
| `/record on\|off` | Toggle saving transmission audio to `recordings/` |

`/health` is the one to reach for mid-event: it tells you whether a quiet station is quiet because there's no traffic (`audio` recent, `last TX` old) or because the feed is dead (`audio` old, `reconnects` climbing).

Stream URL changes are saved in `runtime_config.json`, which is local-only and ignored by git, so they survive restarts without editing `config.py`.

Every command reply appears in `#commands`, and any station mute/unmute or global pause/resume also posts a status embed into the affected station channel(s) — this happens regardless of whether the command was typed in Discord or Telegram, so both platforms always agree on current state.

**Access:** kept private on purpose — only trusted admins (and the bot) should be able to view/post here, since anyone with access can mute stations or pause the whole tracker.

**Suggested channel topic:** `Private control channel — /help for commands. Restricted access.`

---

## Troubleshooting

**`403 Missing Access` (code 50001) when the bot tries to post:** the bot either hasn't been invited to the server (redo the OAuth2 invite in step 2) or the channel has permission overrides that exclude it (add an explicit allow for the bot in that channel's permissions — this is the usual cause for a private channel like `#commands`).

**`403 Missing Permissions` (code 50013), plain-text replies work but transmissions/status never show up:** the bot can see and post to the channel but is missing **Embed Links** specifically — almost everything this bot sends is an embed. Add Embed Links to the bot's permission override on that channel (or its role/category). The terminal now logs `⚠ Discord send failed [channel_id]: HTTP 403 ...` when this happens — earlier versions of this integration swallowed non-2xx responses silently, so if you were on that version you'd have seen nothing in the terminal at all.

**Connection status posts fine but doesn't get pinned:** the bot needs `Read Message History` *and* `Manage Messages` on that channel (pinning has to read the message before it can pin it). The terminal logs `⚠ Discord pin failed [channel_id]: HTTP ...` when this happens. Note `403 Missing Access` (code 50001) here usually means Read Message History is missing, while `403 Missing Permissions` (code 50013) usually means Manage Messages specifically is missing — both need to be explicitly ✅ on the bot's permission entry for that channel, not just inherited/neutral.

**`Discord: OFF` in the terminal header:** one of `DISCORD_BOT_TOKEN`, `DISCORD_ALERTS_CHANNEL_ID`, `DISCORD_COMMANDS_CHANNEL_ID`, or every `DISCORD_CHANNEL_<ICAO>` is empty in `.env`.

**Commands not responding (message shows up but bot ignores it):** almost always **Message Content Intent** is off. Developer Portal → your app → **Bot** tab → **Privileged Gateway Intents** → enable **Message Content Intent** → Save. Without this, Discord returns every message's `content` field as an empty string on REST fetches, so the tracker sees `/mute YBCG` as `""` and silently does nothing — there's no error, it just looks like the bot isn't listening.

**Commands not responding at all (nothing happens, not even the above):** confirm the bot can view *and* read history in `#commands` (Read Message History permission — the tracker polls for new messages every ~2.5s, it does not use a live gateway connection).

**Discord/network outage spams the terminal:** the tracker now logs the first command polling failure, backs off up to 60 seconds between retries, then logs once when polling recovers. Transcription and stream reconnects keep running while Discord is unreachable.
