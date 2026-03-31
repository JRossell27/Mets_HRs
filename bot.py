"""
MLB Pitch Challenge Discord Bot

Monitors MLB games in real-time and sends a message to a Discord channel
whenever a pitch challenge (ABS challenge or manager challenge) is detected.
Each message contains Twitter/X-ready copy-paste text including the
challenging player's season success rate.

After all games each day are final the bot posts a full ABS season tracker
recap comparing batters, catchers, and pitchers by challenge success rate.

Required environment variables:
  DISCORD_TOKEN    — Your Discord bot token
  CHANNEL_ID       — The Discord channel ID to post alerts in

Optional:
  POLL_INTERVAL    — Seconds between polls (default: 30)
  LOG_LEVEL        — Logging level (default: INFO)
  DATA_DIR         — Directory for persistent data file (default: current dir)
"""

import asyncio
import json
import logging
import os
import sys
from aiohttp import web

import discord
import pytz
from datetime import datetime
from discord.ext import commands, tasks
from dotenv import load_dotenv

from abs_tracker import ABSSeasonTracker
from message_formatter import format_challenge_message, format_update_message
from mlb_monitor import MLBMonitor

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID_STR = os.getenv("CHANNEL_ID", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

EASTERN = pytz.timezone("US/Eastern")

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
tracker = ABSSeasonTracker()

# Track in-progress challenges so we can send result updates.
# {uid: discord.Message}
pending_challenges: dict[str, discord.Message] = {}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _all_games_final(games: list[dict]) -> bool:
    """
    Returns True when every game today is no longer live or upcoming
    (i.e. no game will produce any more challenges).
    """
    if not games:
        return False
    ongoing = {"I", "IR", "IO", "MA", "MF", "S", "PW", "PR"}
    return not any(
        g.get("status", {}).get("statusCode", "") in ongoing for g in games
    )


def _enrich_with_season_stats(challenge: dict) -> dict:
    """
    Attach the challenging player's current season stats to the challenge dict
    so the formatter can display the success percentage.
    Only meaningful for resolved (non-in-progress) challenges.
    """
    stats = tracker.get_player_stats(challenge.get("challenger_name", ""))
    if stats:
        challenge["challenger_season_stats"] = stats
    return challenge


async def _send_chunked_message(target, text: str, limit: int = 2000) -> int:
    """
    Send long Discord content in multiple messages to avoid 2000-char limit.
    Splits on line boundaries when possible.
    Returns the number of messages sent.
    """
    if len(text) <= limit:
        await target.send(text)
        return 1

    sent = 0
    chunk = ""
    for line in text.splitlines(keepends=True):
        # If an individual line is longer than limit, hard-split it.
        if len(line) > limit:
            if chunk:
                await target.send(chunk)
                sent += 1
                chunk = ""
            start = 0
            while start < len(line):
                await target.send(line[start:start + limit])
                sent += 1
                start += limit
            continue

        if len(chunk) + len(line) > limit:
            await target.send(chunk.rstrip("\n"))
            sent += 1
            chunk = line
        else:
            chunk += line

    if chunk:
        await target.send(chunk.rstrip("\n"))
        sent += 1

    return sent


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
            # Challenge resolved — record to tracker FIRST so stats are current
            try:
                tracker.record_challenge(challenge)
                _enrich_with_season_stats(challenge)

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

    # ── Daily recap check ────────────────────────────────────────────────────
    today_str = datetime.now(EASTERN).strftime("%Y-%m-%d")
    if not tracker.has_posted_recap(today_str):
        try:
            games = await monitor.get_todays_games()
            if _all_games_final(games):
                recap = tracker.generate_daily_recap()
                parts = await _send_chunked_message(channel, recap)
                tracker.mark_recap_posted(today_str)
                logger.info("Posted ABS daily recap for %s in %d message(s)", today_str, parts)
        except Exception as exc:
            logger.error("Failed to post daily recap: %s", exc)


@poll_mlb.before_loop
async def before_poll():
    await bot.wait_until_ready()
    logger.info("Bot ready — starting MLB polling every %ss", POLL_INTERVAL)


# ─── Bot events ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    logger.info("Posting alerts to channel ID: %s", CHANNEL_ID)

    # Backfill historical ABS data from season start in the background so
    # the bot is ready to poll immediately without waiting for backfill.
    asyncio.create_task(_run_backfill())

    if not poll_mlb.is_running():
        poll_mlb.start()


async def _run_backfill():
    """Run the season backfill in the background at startup."""
    try:
        recorded = await tracker.backfill_season(monitor)
        logger.info("Season backfill finished — %d historical challenges loaded", recorded)
    except Exception as exc:
        logger.error("Season backfill failed: %s", exc)


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


@bot.command(name="absstats")
async def abs_stats(ctx):
    """Post the current ABS season challenge leaderboard."""
    recap = tracker.generate_daily_recap()
    await _send_chunked_message(ctx, recap)


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
        "challenger_name": "Francisco Lindor",
        "challenger_role": "batter",
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
        # Fake season stats for display test
        "challenger_season_stats": {
            "role": "batter",
            "team": "NYM",
            "challenges": 8,
            "overturned": 5,
            "upheld": 3,
        },
    }
    msg = format_challenge_message(fake)
    await ctx.send(msg)


@bot.command(name="diagdate")
@commands.is_owner()
async def diag_date(ctx, date_str: str = ""):
    """
    Fetch and display raw challenge data for a specific date (YYYY-MM-DD).
    Usage: !diagdate 2026-03-27
    Owner only.
    """
    if not date_str:
        date_str = datetime.now(EASTERN).strftime("%Y-%m-%d")

    await ctx.send(f"Fetching games for `{date_str}`…")
    games = await monitor.get_games_for_date(date_str)
    if not games:
        await ctx.send("No games found for that date.")
        return

    final_codes = {"F", "FT", "FO", "O", "C", "CR"}
    lines = [f"**{len(games)} game(s) on {date_str}:**"]
    for g in games:
        status = g.get("status", {})
        sc = status.get("statusCode", "?")
        ab = status.get("abstractGameState", "?")
        away = g.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation", "?")
        home = g.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation", "?")
        lines.append(f"  `{g.get('gamePk')}` {away}@{home} — statusCode=`{sc}` abstractState=`{ab}`")
    await ctx.send("\n".join(lines))

    final_games = [
        g for g in games
        if g.get("status", {}).get("abstractGameState") == "Final"
        or g.get("status", {}).get("statusCode") in final_codes
    ]
    await ctx.send(f"{len(final_games)} game(s) considered final — fetching feeds…")

    total_challenges = []
    total_candidates = 0
    for g in final_games:
        game_pk = g.get("gamePk")
        feed = await monitor.get_live_feed(game_pk)
        if not feed:
            await ctx.send(f"  `{game_pk}`: no feed returned")
            continue

        all_plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
        candidate_hits = 0
        for p in all_plays:
            result = p.get("result", {})
            result_txt = " ".join(
                str(result.get(k, "")) for k in ("event", "eventType", "description")
            ).lower()
            if any(k in result_txt for k in ("challenge", "review", "overturned", "upheld")):
                candidate_hits += 1

            for e in p.get("playEvents", []):
                d = e.get("details", {})
                det_txt = " ".join(
                    str(d.get(k, "")) for k in ("event", "eventType", "description")
                ).lower()
                if (
                    any(k in det_txt for k in ("challenge", "review", "overturned", "upheld"))
                    or d.get("reviewDetails")
                    or e.get("reviewDetails")
                ):
                    candidate_hits += 1
        total_candidates += candidate_hits

        challenges = monitor.extract_all_challenges_from_feed(feed, game_pk)
        total_challenges.extend(challenges)
        if challenges:
            for ch in challenges:
                await ctx.send(
                    f"  `{game_pk}` challenge uid=`{ch.get('uid')}`\n"
                    f"  review_type=`{ch.get('review_type')}` "
                    f"overturned=`{ch.get('is_overturned')}` "
                    f"in_progress=`{ch.get('is_in_progress')}`\n"
                    f"  challenger=`{ch.get('challenger_name')}` "
                    f"role=`{ch.get('challenger_role')}`"
                )
        else:
            await ctx.send(
                f"  `{game_pk}`: 0 challenge events detected "
                f"(keyword/review candidates: {candidate_hits})"
            )

    await ctx.send(
        f"**Total: {len(total_challenges)} challenge event(s) across "
        f"{len(final_games)} final game(s) on {date_str}. "
        f"Raw keyword/review candidates: {total_candidates}**"
    )


@bot.command(name="resetbackfill")
@commands.is_owner()
async def reset_backfill(ctx):
    """
    Clear the list of processed games so the next backfill re-processes
    all games from season start.  Use this after fixing a detection bug.
    Owner only.
    """
    count = len(tracker.data.get("processed_game_pks", []))
    tracker.data["processed_game_pks"] = []
    tracker._save()
    await ctx.send(
        f"Cleared {count} processed game PKs. "
        f"Run `!absstats` after the next startup backfill to verify."
    )


@bot.command(name="help_bot")
async def help_bot(ctx):
    """Show available commands."""
    help_text = (
        "**MLB Pitch Challenge Bot Commands**\n\n"
        "`!status` — Show today's games and live monitoring status\n"
        "`!absstats` — Show the current ABS season challenge leaderboard\n"
        "`!testchallenge` — (owner only) Send a test challenge message\n"
        "`!help_bot` — Show this help message\n\n"
        "The bot automatically monitors all MLB games and posts an alert "
        "with Twitter-ready text whenever a pitch challenge occurs, including "
        "the challenging player's season success rate.  After all games each "
        "day are final, a full ABS season tracker recap is posted."
    )
    await ctx.send(help_text)


# ─── Web test panel ──────────────────────────────────────────────────────────

_HTML_PANEL = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MLB Bot — Test Panel</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0d1117; color: #e6edf3;
      min-height: 100vh; padding: 2rem 1rem;
    }
    .container { max-width: 760px; margin: 0 auto; }
    h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: 1.5rem; }
    h1 span { color: #58a6ff; }
    h2 { font-size: 0.7rem; font-weight: 600; color: #8b949e;
         text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.75rem; }
    .card {
      background: #161b22; border: 1px solid #30363d;
      border-radius: 8px; padding: 1.25rem; margin-bottom: 1rem;
    }
    .status-row { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.6rem; }
    .badge {
      background: #21262d; border: 1px solid #30363d;
      border-radius: 4px; padding: 0.3rem 0.6rem;
      font-size: 0.78rem; white-space: nowrap;
    }
    .dot { display: inline-block; width: 8px; height: 8px;
           border-radius: 50%; margin-right: 6px; background: #3fb950; }
    .actions { display: flex; flex-wrap: wrap; gap: 0.6rem; }
    .btn {
      padding: 0.5rem 1.1rem; border: none; border-radius: 6px;
      font-size: 0.875rem; font-weight: 600; cursor: pointer;
      transition: filter 0.15s;
    }
    .btn:hover:not(:disabled) { filter: brightness(1.15); }
    .btn:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-blue   { background: #1f6feb; color: #fff; }
    .btn-green  { background: #238636; color: #fff; }
    .btn-orange { background: #9e6a03; color: #fff; }
    .btn-purple { background: #6e40c9; color: #fff; }
    .output {
      background: #010409; border: 1px solid #30363d; border-radius: 6px;
      padding: 1rem; font-family: "SF Mono", Consolas, monospace;
      font-size: 0.78rem; white-space: pre-wrap; word-break: break-word;
      max-height: 420px; overflow-y: auto; min-height: 72px;
      color: #7ee787; line-height: 1.55;
    }
    .spinner {
      display: inline-block; width: 12px; height: 12px;
      border: 2px solid #8b949e; border-top-color: #58a6ff;
      border-radius: 50%; animation: spin 0.7s linear infinite;
      margin-right: 6px; vertical-align: middle;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .err { color: #f85149; }
    .ok  { color: #7ee787; }
  </style>
</head>
<body>
<div class="container">
  <h1>⚾ MLB Pitch Challenge Bot — <span>Test Panel</span></h1>

  <div class="card" id="status-card">
    <h2>Bot Status</h2>
    <p><span class="dot"></span>Loading…</p>
  </div>

  <div class="card">
    <h2>Actions</h2>
    <div class="actions">
      <button class="btn btn-blue"   onclick="run('test-challenge')">📨 Send Test Challenge</button>
      <button class="btn btn-green"  onclick="run('post-recap')">📊 Post Season Recap</button>
      <button class="btn btn-orange" onclick="run('run-backfill')">🔄 Run Season Backfill</button>
      <button class="btn btn-purple" onclick="loadStatus()">↺ Refresh Status</button>
    </div>
  </div>

  <div class="card">
    <h2>Output</h2>
    <div class="output" id="output">Click an action above…</div>
  </div>
</div>

<script>
async function loadStatus() {
  const card = document.getElementById('status-card');
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const recapLabel = d.recap_posted_today ? '✅ Yes' : '❌ Not yet';
    card.innerHTML = `
      <h2>Bot Status</h2>
      <p><span class="dot"></span><strong>Online</strong> — polling every ${d.poll_interval}s</p>
      <div class="status-row">
        <span class="badge">📅 ${d.today}</span>
        <span class="badge">🎮 Live games: ${d.live_games}</span>
        <span class="badge">📊 Season challenges: ${d.total_challenges}</span>
        <span class="badge">✅ Overturned: ${d.total_overturned}</span>
        <span class="badge">📈 Success rate: ${d.overall_pct}%</span>
        <span class="badge">🏏 Batters tracked: ${d.batters_tracked}</span>
        <span class="badge">🧤 Catchers tracked: ${d.catchers_tracked}</span>
        <span class="badge">⚾ Pitchers tracked: ${d.pitchers_tracked}</span>
        <span class="badge">📋 Recap posted today: ${recapLabel}</span>
      </div>`;
  } catch (e) {
    card.innerHTML = '<h2>Bot Status</h2><p class="err">Could not reach API.</p>';
  }
}

async function run(action) {
  const out = document.getElementById('output');
  out.className = 'output';
  out.innerHTML = '<span class="spinner"></span>Running ' + action + '…';
  document.querySelectorAll('.btn').forEach(b => b.disabled = true);
  try {
    const r = await fetch('/api/' + action, { method: 'POST' });
    const d = await r.json();
    out.textContent = d.message || JSON.stringify(d, null, 2);
    out.className = d.ok ? 'output ok' : 'output err';
    loadStatus();
  } catch (e) {
    out.textContent = 'Error: ' + e.message;
    out.className = 'output err';
  } finally {
    document.querySelectorAll('.btn').forEach(b => b.disabled = false);
  }
}

loadStatus();
</script>
</body>
</html>"""


async def _panel(request):
    return web.Response(text=_HTML_PANEL, content_type="text/html")


async def _api_status(request):
    try:
        today_str = datetime.now(EASTERN).strftime("%Y-%m-%d")
        games = await monitor.get_todays_games()
        live_statuses = {"I", "IR", "IO", "MA", "MF"}
        live_count = sum(
            1 for g in games
            if g.get("status", {}).get("statusCode") in live_statuses
        )
        players = tracker.data.get("players", {})
        total_ch = sum(s["challenges"] for s in players.values())
        total_ov = sum(s["overturned"] for s in players.values())
        overall_pct = f"{total_ov / total_ch * 100:.1f}" if total_ch else "0.0"
        return web.Response(
            text=json.dumps({
                "today": today_str,
                "poll_interval": POLL_INTERVAL,
                "live_games": live_count,
                "total_challenges": total_ch,
                "total_overturned": total_ov,
                "overall_pct": overall_pct,
                "batters_tracked":  sum(1 for s in players.values() if s["role"] == "batter"),
                "catchers_tracked": sum(1 for s in players.values() if s["role"] == "catcher"),
                "pitchers_tracked": sum(1 for s in players.values() if s["role"] == "pitcher"),
                "recap_posted_today": tracker.has_posted_recap(today_str),
            }),
            content_type="application/json",
        )
    except Exception as exc:
        return web.Response(
            text=json.dumps({"ok": False, "message": str(exc)}),
            content_type="application/json",
            status=500,
        )


async def _api_test_challenge(request):
    try:
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            raise RuntimeError("Bot channel not found — is the bot connected?")
        fake = {
            "uid": "webtest_001",
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
            "challenger_name": "Francisco Lindor",
            "challenger_role": "batter",
            "challenging_team": "Mets",
            "review_type": "Pitch Challenge (ABS)",
            "is_in_progress": False,
            "is_overturned": True,
            "description": "Called strike overturned, ball awarded. [TEST]",
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
            "challenger_season_stats": {
                "role": "batter",
                "team": "NYM",
                "challenges": 8,
                "overturned": 5,
                "upheld": 3,
            },
        }
        msg = format_challenge_message(fake)
        await channel.send(msg)
        return web.Response(
            text=json.dumps({"ok": True, "message": "✅ Test challenge posted to Discord."}),
            content_type="application/json",
        )
    except Exception as exc:
        return web.Response(
            text=json.dumps({"ok": False, "message": f"❌ {exc}"}),
            content_type="application/json",
            status=500,
        )


async def _api_post_recap(request):
    try:
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            raise RuntimeError("Bot channel not found — is the bot connected?")
        recap = tracker.generate_daily_recap()
        parts = await _send_chunked_message(channel, recap)
        return web.Response(
            text=json.dumps({
                "ok": True,
                "message": f"✅ Season recap posted to Discord in {parts} message(s).",
            }),
            content_type="application/json",
        )
    except Exception as exc:
        return web.Response(
            text=json.dumps({"ok": False, "message": f"❌ {exc}"}),
            content_type="application/json",
            status=500,
        )


async def _api_run_backfill(request):
    try:
        recorded = await tracker.backfill_season(monitor)
        return web.Response(
            text=json.dumps({
                "ok": True,
                "message": f"✅ Backfill complete — {recorded} new challenges recorded.",
            }),
            content_type="application/json",
        )
    except Exception as exc:
        return web.Response(
            text=json.dumps({"ok": False, "message": f"❌ {exc}"}),
            content_type="application/json",
            status=500,
        )


async def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/",                  _panel)
    app.router.add_get("/api/status",        _api_status)
    app.router.add_post("/api/test-challenge", _api_test_challenge)
    app.router.add_post("/api/post-recap",   _api_post_recap)
    app.router.add_post("/api/run-backfill", _api_run_backfill)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Web panel + health server running on port %s", port)

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
