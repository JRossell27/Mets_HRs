"""
MLB Pitch Challenge Discord Bot

Monitors MLB games in real-time and sends a message to a Discord channel
whenever a pitch challenge (ABS challenge or manager challenge) is detected.
Each message contains Twitter/X-ready copy-paste text.

Required environment variables:
  DISCORD_TOKEN    — Your Discord bot token
  CHANNEL_ID       — The Discord channel ID to post alerts in

Optional:
  POLL_INTERVAL    — Seconds between polls (default: 30)
  LOG_LEVEL        — Logging level (default: INFO)
"""

import asyncio
import logging
import os
import sys
from collections import defaultdict
from aiohttp import web

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from message_formatter import format_challenge_message, format_update_message
from mlb_monitor import MLBMonitor

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID_STR = os.getenv("CHANNEL_ID", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pitch_challenge_bot")

# ─── Validation ───────────────────────────────────────────────────────────────
if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN is not set. Add it to your .env file.")
    sys.exit(1)

try:
    CHANNEL_ID = int(CHANNEL_ID_STR)
except ValueError:
    logger.error("CHANNEL_ID is missing or not a valid integer.")
    sys.exit(1)

# ─── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
monitor = MLBMonitor()

# Track in-progress challenges so we can send result updates.
# {uid: discord.Message}
pending_challenges: dict[str, discord.Message] = {}


# ─── Background polling task ─────────────────────────────────────────────────
@tasks.loop(seconds=POLL_INTERVAL)
async def poll_mlb():
    """Periodically poll MLB live feeds and post challenge alerts."""
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        logger.warning("Could not find channel %s — make sure the bot has access.", CHANNEL_ID)
        return

    try:
        challenges = await monitor.check_for_new_challenges()
    except Exception as exc:
        logger.error("Error checking for challenges: %s", exc)
        return

    for challenge in challenges:
        uid = challenge["uid"]

        if challenge["is_in_progress"]:
            # Send an "in progress" message and save it for later editing
            try:
                msg_text = format_challenge_message(challenge)
                msg = await channel.send(msg_text)
                pending_challenges[uid] = msg
                logger.info("Challenge detected (in progress): %s", uid)
            except Exception as exc:
                logger.error("Failed to send challenge message: %s", exc)
        else:
            # Challenge already resolved — send final message
            try:
                # If we had an in-progress message, edit it; otherwise post new
                if uid in pending_challenges:
                    old_msg = pending_challenges.pop(uid)
                    update_text = format_challenge_message(challenge)
                    await old_msg.edit(content=update_text)
                    logger.info("Challenge resolved (edited message): %s", uid)
                else:
                    msg_text = format_challenge_message(challenge)
                    await channel.send(msg_text)
                    logger.info("Challenge resolved (new message): %s", uid)
            except Exception as exc:
                logger.error("Failed to send/edit challenge message: %s", exc)


@poll_mlb.before_loop
async def before_poll():
    await bot.wait_until_ready()
    logger.info("Bot ready — starting MLB polling every %ss", POLL_INTERVAL)


# ─── Bot events ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    logger.info("Posting alerts to channel ID: %s", CHANNEL_ID)
    if not poll_mlb.is_running():
        poll_mlb.start()


@bot.event
async def on_disconnect():
    logger.warning("Bot disconnected from Discord.")


# ─── Commands ────────────────────────────────────────────────────────────────
@bot.command(name="status")
async def status(ctx):
    """Show current monitoring status."""
    games = await monitor.get_todays_games()
    live_statuses = {"I", "IR", "IO", "MA", "MF"}
    live_games = [g for g in games if g.get("status", {}).get("statusCode") in live_statuses]
    total = len(games)
    live = len(live_games)

    lines = [f"**MLB Pitch Challenge Bot — Status**"]
    lines.append(f"Polling interval: every {POLL_INTERVAL}s")
    lines.append(f"Today's games: {total} total, {live} live")

    if live_games:
        lines.append("\n**Live games:**")
        for g in live_games:
            away = g.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation", "?")
            home = g.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation", "?")
            away_score = g.get("teams", {}).get("away", {}).get("score", 0)
            home_score = g.get("teams", {}).get("home", {}).get("score", 0)
            inning = g.get("linescore", {}).get("currentInning", "?")
            half = g.get("linescore", {}).get("inningHalf", "")[:3].title()
            lines.append(f"  • {away} {away_score}–{home_score} {home} | {half} {inning}")
    else:
        lines.append("No live games right now.")

    await ctx.send("\n".join(lines))


@bot.command(name="testchallenge")
@commands.is_owner()
async def test_challenge(ctx):
    """Send a fake challenge message to verify formatting (owner only)."""
    fake = {
        "uid": "test_001",
        "game_pk": 999999,
        "game_pk_str": "999999",
        "away_team": "Mets",
        "home_team": "Yankees",
        "away_abbr": "NYM",
        "home_abbr": "NYY",
        "away_score": 2,
        "home_score": 3,
        "venue": "Yankee Stadium",
        "inning": 7,
        "inning_half": "Top",
        "pitcher": "Gerrit Cole",
        "batter": "Francisco Lindor",
        "challenging_team": "Mets",
        "review_type": "Pitch Challenge (ABS)",
        "is_in_progress": False,
        "is_overturned": True,
        "description": "Called strike overturned, ball awarded.",
        "pitch_info": {
            "type": "4-Seam Fastball",
            "type_code": "FF",
            "speed": 97.4,
            "zone": 3,
            "zone_desc": "Up & Away",
            "original_call": "Called Strike",
        },
        "balls": 1,
        "strikes": 2,
        "outs": 1,
        "event_time": "",
    }
    msg = format_challenge_message(fake)
    await ctx.send(msg)


@bot.command(name="help_bot")
async def help_bot(ctx):
    """Show available commands."""
    help_text = (
        "**MLB Pitch Challenge Bot Commands**\n\n"
        "`!status` — Show today's games and live monitoring status\n"
        "`!testchallenge` — (owner only) Send a test challenge message\n"
        "`!help_bot` — Show this help message\n\n"
        "The bot automatically monitors all MLB games and posts an alert "
        "with Twitter-ready text whenever a pitch challenge occurs."
    )
    await ctx.send(help_text)


# ─── Health check server (required by Fly.io) ────────────────────────────────
async def health_check(request):
    return web.Response(text="OK")

async def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health check server running on port %s", port)

# ─── Entry point ─────────────────────────────────────────────────────────────
async def main():
    asyncio.create_task(start_health_server())
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
