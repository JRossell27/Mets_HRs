"""
Format MLB pitch challenge events into Twitter-ready text messages.
Each message is designed to be copy-pasted directly to Twitter/X.
"""

from typing import Optional

# Team abbreviation → Twitter hashtag mapping
TEAM_HASHTAGS = {
    "NYM": "#LGM",       "NYY": "#RepBX",    "BOS": "#RedSox",
    "TOR": "#BlueJays",  "BAL": "#Orioles",  "TB": "#RaysUp",
    "HOU": "#Astros",    "LAA": "#Angels",   "OAK": "#Athletics",
    "SEA": "#Mariners",  "TEX": "#Rangers",  "CWS": "#WhiteSox",
    "CLE": "#Guardians", "DET": "#Tigers",   "KC": "#Royals",
    "MIN": "#Twins",     "ATL": "#Braves",   "MIA": "#Marlins",
    "PHI": "#Phillies",  "WSH": "#Nationals","NYM": "#Mets",
    "CHC": "#Cubs",      "CIN": "#Reds",     "MIL": "#Brewers",
    "PIT": "#Pirates",   "STL": "#Cardinals","ARI": "#Dbacks",
    "COL": "#Rockies",   "LAD": "#Dodgers",  "SD": "#Padres",
    "SF": "#Giants",
}


def _result_line(challenge: dict) -> str:
    """Return a short result status string."""
    if challenge["is_in_progress"]:
        return "🔄 REVIEW IN PROGRESS"
    if challenge["is_overturned"] is True:
        return "✅ OVERTURNED"
    if challenge["is_overturned"] is False:
        return "❌ UPHELD (Call Stands)"
    return "📋 REVIEW COMPLETE"


def _normalize_call(call: str) -> str:
    c = (call or "").lower()
    if "strike" in c:
        return "Strike"
    if "ball" in c:
        return "Ball"
    return call or "Unknown"


def _result_call(challenge: dict) -> str:
    """
    Infer resulting call after challenge based on overturn/uphold + original call.
    """
    original = _normalize_call(challenge.get("pitch_info", {}).get("original_call", ""))
    overturned = challenge.get("is_overturned")
    if overturned is True:
        if original == "Strike":
            return "Ball"
        if original == "Ball":
            return "Strike"
    if overturned is False:
        return original
    return "Pending"


def _pitch_line(pitch_info: dict) -> str:
    """Format pitch details into a single line."""
    parts = []
    if pitch_info.get("type"):
        parts.append(pitch_info["type"])
    if pitch_info.get("speed"):
        parts.append(f"{pitch_info['speed']:.1f} mph")
    if pitch_info.get("zone_desc"):
        parts.append(pitch_info["zone_desc"])
    if pitch_info.get("original_call"):
        parts.append(f"called {pitch_info['original_call']}")
    return " | ".join(parts) if parts else "Pitch details unavailable"


def _hashtags(away_abbr: str, home_abbr: str) -> str:
    """Return space-separated hashtags for both teams."""
    tags = []
    away_tag = TEAM_HASHTAGS.get(away_abbr, f"#{away_abbr}")
    home_tag = TEAM_HASHTAGS.get(home_abbr, f"#{home_abbr}")
    # Deduplicate (e.g., NYM appears on both sides)
    tags = [away_tag]
    if home_tag != away_tag:
        tags.append(home_tag)
    tags.append("#MLB")
    return " ".join(tags)


def _challenger_stat_line(challenge: dict) -> str:
    """
    Build a one-line season success rate string for the challenging player.
    Returns '' if stats are not yet available (e.g. in-progress challenges).
    """
    stats = challenge.get("challenger_season_stats")
    if not stats or stats.get("challenges", 0) == 0:
        return ""
    name = challenge.get("challenger_name", "")
    role = stats.get("role", challenge.get("challenger_role", "")).title()
    challenges = stats["challenges"]
    overturned = stats["overturned"]
    pct = overturned / challenges * 100
    return f"📊 {name} ({role}) 2026: {pct:.1f}% ({overturned}/{challenges} overturned)"


def format_challenge_message(challenge: dict) -> str:
    """
    Build a full Discord message with a Twitter-ready block.
    Returns a string ready to send in Discord.
    """
    away = challenge["away_team"]
    home = challenge["home_team"]
    away_abbr = challenge["away_abbr"]
    home_abbr = challenge["home_abbr"]
    away_score = challenge["away_score"]
    home_score = challenge["home_score"]
    inning = challenge["inning"]
    half = challenge["inning_half"]
    pitcher = challenge["pitcher"]
    batter = challenge["batter"]
    challenging_team = challenge["challenging_team"]
    review_type = challenge["review_type"]
    venue = challenge["venue"]
    balls = challenge["balls"]
    strikes = challenge["strikes"]
    outs = challenge["outs"]
    pitch_info = challenge["pitch_info"]
    result = _result_line(challenge)
    pitch_line = _pitch_line(pitch_info) if pitch_info else "No pitch data available"
    tags = _hashtags(away_abbr, home_abbr)
    score_str = f"{away} {away_score} — {home_score} {home}"
    inning_str = f"{half} {inning}"
    count_str = f"{balls}-{strikes}, {outs} out{'s' if outs != 1 else ''}"
    original_call = _normalize_call(pitch_info.get("original_call", ""))
    result_call = _result_call(challenge)

    twitter_text = (
        f"ABS CHALLENGE INITIATED\n"
        f"{result}\n"
        f"ORIGINAL CALL: \"{original_call}\"\n"
        f"RESULT: \"{result_call}\"\n"
        f"\n"
        f"🏟 {score_str} | {inning_str}\n"
        f"⚡ {pitcher} → {batter} | {count_str}\n"
        f"📍 {pitch_line}\n"
        f"📢 Challenged by: {challenging_team}\n"
        f"\n"
        f"🏟 {venue}\n"
        f"\n{tags}"
    )

    discord_message = f"```\n{twitter_text}\n```"

    return discord_message


def format_update_message(challenge: dict) -> str:
    """
    Short follow-up message when a previously in-progress challenge resolves.
    """
    result = _result_line(challenge)
    away = challenge["away_team"]
    home = challenge["home_team"]
    inning = challenge["inning"]
    half = challenge["inning_half"]
    review_type = challenge["review_type"]
    away_score = challenge["away_score"]
    home_score = challenge["home_score"]
    away_abbr = challenge["away_abbr"]
    home_abbr = challenge["home_abbr"]
    tags = _hashtags(away_abbr, home_abbr)

    twitter_text = (
        f"⚾ {review_type.upper()} — RESULT\n"
        f"{result}\n"
        f"\n"
        f"🏟 {away} {away_score} — {home_score} {home} | {half} {inning}\n"
        f"\n{tags}"
    )

    discord_message = (
        f"## 🔔 Challenge Result Update\n"
        f"**{result}**\n\n"
        f"```\n{twitter_text}\n```\n"
        f"*Copy the text above to post on Twitter/X*"
    )
    return discord_message
