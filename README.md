# MLB Pitch Challenge Discord Bot

A Discord bot that monitors all MLB games in real-time and sends an alert whenever a pitch challenge (ABS system challenge or manager challenge) occurs. Each alert includes a pre-formatted Twitter/X-ready message you can copy and paste directly.

## Setup

### 1. Create a Discord Bot

1. Go to https://discord.com/developers/applications
2. Click **New Application**, give it a name
3. Go to the **Bot** tab → click **Add Bot**
4. Under **Token**, click **Copy** — this is your `DISCORD_TOKEN`
5. Under **Privileged Gateway Intents**, enable **Message Content Intent**
6. Go to **OAuth2 → URL Generator**
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Read Message History`, `View Channels`
7. Copy the generated URL, paste it in your browser, and invite the bot to your server

### 2. Get Your Channel ID

1. In Discord, go to **User Settings → Advanced** and enable **Developer Mode**
2. Right-click the channel you want alerts in → **Copy Channel ID**

### 3. Configure Environment

```bash
cp .env.example .env
# Edit .env with your DISCORD_TOKEN and CHANNEL_ID
```

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

### 5. Run the Bot

```bash
python bot.py
```

The bot will start polling all live MLB games every 30 seconds.

---

## Commands

| Command | Description |
|---|---|
| `!status` | Show today's MLB games and live monitoring status |
| `!testchallenge` | (owner only) Send a test formatted challenge message |
| `!help_bot` | Show available commands |

---

## What the Alerts Look Like

When a pitch challenge happens, the bot posts a message like:

```
## ⚾ MLB Pitch Challenge (ABS) Detected!
**✅ OVERTURNED**

[Twitter-ready text block]:
⚾ PITCH CHALLENGE (ABS)
✅ OVERTURNED

🏟 Mets 2 — 3 Yankees | Top 7
⚡ Gerrit Cole → Francisco Lindor | 1-2, 1 out
📍 4-Seam Fastball | 97.4 mph | Up & Away | called Called Strike
📢 Challenged by: Mets
📋 Called strike overturned, ball awarded.

🏟 Yankee Stadium

#LGM #RepBX #MLB
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Discord bot token |
| `CHANNEL_ID` | Yes | — | Channel to post alerts in |
| `POLL_INTERVAL` | No | `30` | Seconds between MLB API polls |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity |

---

## How It Works

1. Every `POLL_INTERVAL` seconds, the bot fetches today's MLB schedule
2. For each live game, it pulls the full live game feed from the MLB Stats API
3. It walks through every play event looking for challenge/review events
4. When a new challenge is detected, it formats and posts an alert
5. If a challenge was still in progress, the bot edits the message when the result is available

The bot uses the free, public MLB Stats API (`statsapi.mlb.com`) — no API key required.
