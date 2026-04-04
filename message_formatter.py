"""
Format MLB pitch challenge events into Twitter-ready text messages.
Each message is designed to be copy-pasted directly to Twitter/X.
"""

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


def _abs_original_call(challenge: dict) -> str:
    """
    For ABS pitch challenges, derive the original (disputed) call from
    challenger_role.  Batters always challenge Called Strikes; fielders
    (catcher/pitcher) always challenge Balls.  This is authoritative
    because the 2026 API may store the post-overturn outcome in the
    pitch's call field rather than the original disputed call.
    """
    role = challenge.get("challenger_role", "")
    if role == "batter":
        return "Strike"
    if role in ("catcher", "pitcher"):
        return "Ball"
    return ""


def _result_call(challenge: dict) -> str:
    """
    Infer resulting call after challenge based on overturn/uphold + original call.
    """
    if challenge.get("is_abs_pitch_challenge"):
        original = _abs_original_call(challenge)
    else:
        original = _normalize_call(challenge.get("pitch_info", {}).get("original_call", ""))
    overturned = challenge.get("is_overturned")
    if overturned is True:
        if original == "Strike":
            return "Ball"
        if original == "Ball":
            return "Strike"
        return "Overturned"   # resolved but original call unknown
    if overturned is False:
        return original or "Upheld"
    return "Pending"          # only when is_overturned is None


def _pitch_line(pitch_info: dict) -> str:
    """Format pitch details into a single line."""
    parts = []
    if pitch_info.get("type"):
        parts.append(pitch_info["type"])
    if pitch_info.get("speed"):
        parts.append(f"{pitch_info['speed']:.1f} mph")
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


def _side_stat_line(challenge: dict) -> str:
    """Build offense/defense season aggregate line for this challenge role."""
    side_stats = challenge.get("season_side_stats")
    if not side_stats or side_stats.get("challenges", 0) == 0:
        return ""
    role = challenge.get("challenger_role", "")
    side_label = "Offense" if role == "batter" else "Defense"
    return (
        f"📈 {side_label} challenges 2026: "
        f"{side_stats['pct']:.1f}% ({side_stats['overturned']}/{side_stats['challenges']} overturned)"
    )


def _format_count(balls: int, strikes: int) -> str:
    return f"{balls}-{strikes}"


def _pre_pitch_count(challenge: dict, original_call: str) -> str:
    """Approximate count before the challenged pitch."""
    balls = int(challenge.get("balls", 0))
    strikes = int(challenge.get("strikes", 0))

    if original_call == "Strike":
        strikes = max(0, strikes - 1)
    elif original_call == "Ball":
        balls = max(0, balls - 1)

    return _format_count(balls, strikes)


def _new_count(challenge: dict, original_call: str) -> str:
    """Compute count after review result."""
    pre = _pre_pitch_count(challenge, original_call)
    pre_balls, pre_strikes = (int(x) for x in pre.split("-"))
    overturned = challenge.get("is_overturned")

    if overturned is True and original_call == "Strike":
        return _format_count(pre_balls + 1, pre_strikes)
    if overturned is True and original_call == "Ball":
        return _format_count(pre_balls, pre_strikes + 1)
    if overturned is False:
        return _format_count(int(challenge.get("balls", 0)), int(challenge.get("strikes", 0)))
    return "Pending"


def format_challenge_message(challenge: dict) -> str:
    """Build a Discord message with the ABS challenge template text."""
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
    pitch_info = challenge.get("pitch_info", {})
    tags = _hashtags(away_abbr, home_abbr)

    if challenge.get("is_abs_pitch_challenge"):
        original_call = _abs_original_call(challenge)
    else:
        original_call = _normalize_call(pitch_info.get("original_call", ""))

    abs_call = _result_call(challenge)
    decision = "Pending"
    if challenge.get("is_overturned") is True:
        decision = "Overturned"
    elif challenge.get("is_overturned") is False:
        decision = "Confirmed"

    pre_pitch_count = _pre_pitch_count(challenge, original_call)
    new_count = _new_count(challenge, original_call)
    zone_note = pitch_info.get("zone_desc", "Unknown Zone")
    video_url = challenge.get("media_video_url", "")
    image_url = challenge.get("media_image_url", "")
    media_line = ""
    if video_url:
        media_line = f"Video: {video_url}\n"
    elif image_url:
        media_line = f"Photo: {image_url}\n"
    challenger_line = _challenger_stat_line(challenge)
    side_line = _side_stat_line(challenge)
    stats_lines = ""
    if challenger_line:
        stats_lines += f"\n{challenger_line}"
    if side_line:
        stats_lines += f"\n{side_line}"

    twitter_text = (
        f"ABS Challenge 🚨\n"
        f"{away} {away_score} — {home} {home_score}\n"
        f"{half} {inning} — Count: {pre_pitch_count}\n"
        f"\n"
        f"{pitcher} vs {batter}\n"
        f"Called: {original_call}\n"
        f"ABS: {abs_call}\n"
        f"Result: {decision} → Count: {new_count}\n"
        f"\n"
        f"Zone: {zone_note}\n"
        f"{media_line}"
        f"{stats_lines}\n"
        f"\n{tags}"
    )

    return f"```\n{twitter_text}\n```"


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
        f"⚾ {review_type.upper()} - RESULT\n"
        f"{result}\n"
        f"\n"
        f"🏟 {away} {away_score} - {home_score} {home} | {half} {inning}\n"
        f"\n{tags}"
    )

    discord_message = (
        f"## 🔔 Challenge Result Update\n"
        f"**{result}**\n\n"
        f"```\n{twitter_text}\n```\n"
        f"*Copy the text above to post on Twitter/X*"
    )
    return discord_message
