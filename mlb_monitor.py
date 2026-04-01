"""
MLB API monitor for real-time pitch challenge detection.
Polls the MLB Stats API live game feeds to detect pitch challenges and manager challenges.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

import aiohttp
import pytz

logger = logging.getLogger(__name__)

MLB_API_BASE = "https://statsapi.mlb.com/api"
EASTERN = pytz.timezone("US/Eastern")

# Event keywords that indicate a challenge
CHALLENGE_EVENT_KEYWORDS = [
    "pitch challenge",
    "abs challenge",
    "manager challenge",
    "challenge",
    "replay review",
    "video review",
]

# Review types mapped to human-readable labels
REVIEW_TYPE_LABELS = {
    "pitchChallenge": "Pitch Challenge (ABS)",
    "managerChallenge": "Manager Challenge",
    "umpireReview": "Umpire Review",
    "replayReview": "Replay Review",
}

PITCH_TYPE_LABELS = {
    "FF": "4-Seam Fastball",
    "SI": "Sinker",
    "FC": "Cutter",
    "SL": "Slider",
    "CU": "Curveball",
    "CH": "Changeup",
    "FS": "Splitter",
    "KC": "Knuckle Curve",
    "KN": "Knuckleball",
    "EP": "Eephus",
    "FO": "Forkball",
    "SC": "Screwball",
    "ST": "Sweeper",
    "SV": "Slurve",
    "CS": "Slow Curve",
    "FA": "Fastball",
    "PO": "Pitchout",
    "IN": "Intentional Ball",
    "AB": "Automatic Ball",
}

ZONE_DESCRIPTIONS = {
    1: "Up & In", 2: "Up Middle", 3: "Up & Away",
    4: "Middle In", 5: "Heart of Plate", 6: "Middle Away",
    7: "Down & In", 8: "Down Middle", 9: "Down & Away",
    11: "Way Inside", 12: "Way High", 13: "Way Outside", 14: "Way Low",
}


def _extract_review_details(event_details: dict, play_event: dict, play: dict) -> dict:
    """
    Pull review metadata from whichever shape MLB feed is using.
    """
    candidates = [
        event_details.get("reviewDetails"),
        play_event.get("reviewDetails"),
        play.get("reviewDetails"),
        play.get("result", {}).get("reviewDetails"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def _has_challenge_keyword(*parts: str) -> bool:
    text = " ".join((p or "") for p in parts).lower()
    return any(kw in text for kw in CHALLENGE_EVENT_KEYWORDS)


def _is_challenge_event(event_details: dict, play_event: dict, play: dict) -> bool:
    """
    Check if a play event contains a challenge or ABS review.

    Handles two layouts seen in the MLB Stats API:
    1. A dedicated non-pitch event where details.event/eventType contains
       a challenge keyword  (classic manager/ABS challenge)
    2. The challenge is embedded ON the pitch event itself via reviewDetails
       (common for 2026 ABS where the review is attached to the pitch)
    """
    event = event_details.get("event") or ""
    event_type = event_details.get("eventType") or ""
    description = event_details.get("description") or ""

    play_result = play.get("result", {})
    if _has_challenge_keyword(
        event,
        event_type,
        description,
        play_event.get("description", ""),
        play_result.get("event", ""),
        play_result.get("eventType", ""),
        play_result.get("description", ""),
    ):
        return True

    # Also catch challenges stored directly via reviewDetails in any location.
    review = _extract_review_details(event_details, play_event, play)
    if review.get("reviewType") or review.get("inProgress") is not None:
        return True

    # Extra hints observed in some feeds.
    flags = play_event.get("flags", {})
    if isinstance(flags, dict) and (flags.get("isChallenge") or flags.get("isReview")):
        return True

    return False


def _is_abs_pitch_challenge(
    review_type_raw: str, details: dict, play_event: dict, play: dict, pitch_info: dict
) -> bool:
    """
    Strict ABS challenge classifier used for season stat recording.
    """
    rt = (review_type_raw or "").lower()
    if rt == "pitchchallenge":
        return True

    text = " ".join([
        str(details.get("event", "")),
        str(details.get("eventType", "")),
        str(details.get("description", "")),
        str(play_event.get("description", "")),
        str(play.get("result", {}).get("event", "")),
        str(play.get("result", {}).get("eventType", "")),
        str(play.get("result", {}).get("description", "")),
    ]).lower()

    # MLB ABS narration format example:
    # "Francisco Alvarez challenged ... call on the field was overturned..."
    abs_text_markers = (
        "challenged" in text
        and "call on the field was" in text
        and any(k in text for k in ("overturned", "upheld", "stands"))
        and any(k in text for k in ("called strike", "called ball", "strikes", "balls"))
    )
    if abs_text_markers and "manager challenge" not in text and "replay review" not in text:
        return True

    return False


class MLBMonitor:
    """Polls MLB live game feeds and surfaces new pitch challenge events."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        # {game_pk: {event_uid: state}}
        # state values: "in_progress", "resolved_overturned", "resolved_upheld", "resolved_unknown"
        self._seen_challenges: dict[int, dict[str, str]] = {}
        # {game_pk: bool} - tracks which games we are actively watching
        self._active_games: dict[int, bool] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_todays_games(self) -> list[dict]:
        """Fetch today's MLB schedule (Eastern time)."""
        today = datetime.now(EASTERN).strftime("%Y-%m-%d")
        return await self.get_games_for_date(today)

    async def get_games_for_date(self, date_str: str) -> list[dict]:
        """Fetch the MLB schedule for a specific date (YYYY-MM-DD)."""
        url = (
            f"{MLB_API_BASE}/v1/schedule"
            f"?sportId=1&date={date_str}&hydrate=linescore,team"
        )
        try:
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Schedule API returned %s for date %s", resp.status, date_str)
                    return []
                data = await resp.json()
        except Exception as exc:
            logger.error("Failed to fetch schedule for %s: %s", date_str, exc)
            return []

        games = []
        for date_entry in data.get("dates", []):
            for game in date_entry.get("games", []):
                games.append(game)
        return games

    async def get_live_feed(self, game_pk: int) -> Optional[dict]:
        """Fetch the live game feed for a given gamePk."""
        url = f"{MLB_API_BASE}/v1.1/game/{game_pk}/feed/live"
        try:
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception as exc:
            logger.error("Failed to fetch live feed for game %s: %s", game_pk, exc)
            return None

    def _get_active_catcher(self, feed: dict, fielding_team_key: str) -> str:
        """
        Return the name of the currently active catcher for the given team
        ('home' or 'away') from the boxscore.  Returns '' if not determinable.
        """
        try:
            players = (
                feed.get("liveData", {})
                    .get("boxscore", {})
                    .get("teams", {})
                    .get(fielding_team_key, {})
                    .get("players", {})
            )
            # Position code "2" = Catcher; exclude bench players
            active_catchers = [
                p for p in players.values()
                if p.get("position", {}).get("code") == "2"
                and not p.get("gameStatus", {}).get("isOnBench", False)
            ]
            if active_catchers:
                return active_catchers[0]["person"]["fullName"]
        except Exception:
            pass
        return ""

    def extract_all_challenges_from_feed(self, feed: dict, game_pk: int) -> list[dict]:
        """
        Extract all challenge events from a completed game feed without
        modifying the live-tracking seen-challenge state.  Used for backfill.
        """
        original = self._seen_challenges.get(game_pk, {}).copy()
        self._seen_challenges[game_pk] = {}
        challenges = self._extract_challenges_from_feed(feed, game_pk, emit_updates_only=False)
        self._seen_challenges[game_pk] = original
        return challenges

    def _extract_challenges_from_feed(
        self, feed: dict, game_pk: int, emit_updates_only: bool = True
    ) -> list[dict]:
        """
        Walk the live feed and return any new challenge events not yet seen.
        Returns fully enriched challenge dicts ready for formatting.
        """
        if game_pk not in self._seen_challenges:
            self._seen_challenges[game_pk] = {}

        game_data = feed.get("gameData", {})
        live_data = feed.get("liveData", {})
        all_plays = live_data.get("plays", {}).get("allPlays", [])

        teams = game_data.get("teams", {})
        away_team = teams.get("away", {}).get("teamName", "Away")
        home_team = teams.get("home", {}).get("teamName", "Home")
        away_abbr = teams.get("away", {}).get("abbreviation", "AWY")
        home_abbr = teams.get("home", {}).get("abbreviation", "HME")
        venue = game_data.get("venue", {}).get("name", "Unknown Venue")
        game_pk_str = str(game_data.get("game", {}).get("pk", game_pk))

        linescore = live_data.get("linescore", {})
        away_score = linescore.get("teams", {}).get("away", {}).get("runs", 0)
        home_score = linescore.get("teams", {}).get("home", {}).get("runs", 0)
        current_inning = linescore.get("currentInning", "?")
        inning_half = linescore.get("inningHalf", "").capitalize()

        new_challenges = []

        for play in all_plays:
            at_bat_index = play.get("about", {}).get("atBatIndex", 0)
            play_events = play.get("playEvents", [])
            matchup = play.get("matchup", {})
            batter_name = matchup.get("batter", {}).get("fullName", "Unknown Batter")
            pitcher_name = matchup.get("pitcher", {}).get("fullName", "Unknown Pitcher")

            for event_idx, play_event in enumerate(play_events):
                details = play_event.get("details", {})
                if not _is_challenge_event(details, play_event, play):
                    continue

                review = _extract_review_details(details, play_event, play)
                description_text = " ".join([
                    str(details.get("description", "")),
                    str(play_event.get("description", "")),
                    str(play.get("result", {}).get("description", "")),
                ]).lower()
                is_in_progress = review.get("inProgress", False)
                is_overturned = review.get("isOverturned", None)
                if is_overturned is None:
                    if "overturned" in description_text:
                        is_overturned = True
                    elif any(k in description_text for k in ("upheld", "stands")):
                        is_overturned = False
                if not is_in_progress and "in progress" in description_text:
                    is_in_progress = True
                review_type_raw = review.get("reviewType", "")
                review_type = REVIEW_TYPE_LABELS.get(review_type_raw, review_type_raw or "Challenge")
                challenging_team_id = review.get("challengeTeamId")

                # Find the challenging team name
                away_id = teams.get("away", {}).get("id")
                home_id = teams.get("home", {}).get("id")
                if challenging_team_id == away_id:
                    challenging_team = away_team
                elif challenging_team_id == home_id:
                    challenging_team = home_team
                else:
                    challenging_team = "Unknown Team"

                # Find pitch data.  If the challenge IS on the pitch event
                # itself (2026 ABS style), use that event directly; otherwise
                # search backwards for the most recent prior pitch.
                if play_event.get("isPitch", False):
                    last_pitch = play_event
                else:
                    last_pitch = None
                    for pe in reversed(play_events[:event_idx]):
                        if pe.get("isPitch", False):
                            last_pitch = pe
                            break

                pitch_info = {}
                if last_pitch:
                    pitch_details = last_pitch.get("pitchData", {})
                    pitch_type_code = last_pitch.get("details", {}).get("type", {}).get("code", "")
                    pitch_type = PITCH_TYPE_LABELS.get(pitch_type_code, pitch_type_code or "Unknown")
                    speed = pitch_details.get("startSpeed")
                    zone = pitch_details.get("zone")
                    zone_desc = ZONE_DESCRIPTIONS.get(zone, f"Zone {zone}" if zone else "Unknown Zone")
                    original_call = last_pitch.get("details", {}).get("description", "")
                    pitch_info = {
                        "type": pitch_type,
                        "type_code": pitch_type_code,
                        "speed": speed,
                        "zone": zone,
                        "zone_desc": zone_desc,
                        "original_call": original_call,
                    }

                # Use reviewed pitch identity as the primary UID so challenge
                # start/result events for the same pitch collapse into one.
                reviewed_play_id = (last_pitch or {}).get("playId")
                event_play_id = play_event.get("playId")
                if reviewed_play_id:
                    uid = f"{game_pk_str}_{at_bat_index}_{reviewed_play_id}"
                elif event_play_id:
                    uid = f"{game_pk_str}_{at_bat_index}_{event_play_id}"
                else:
                    uid = f"{game_pk_str}_{at_bat_index}_{event_idx}"

                event_state = (
                    "in_progress"
                    if is_in_progress
                    else (
                        "resolved_overturned"
                        if is_overturned is True
                        else "resolved_upheld" if is_overturned is False else "resolved_unknown"
                    )
                )

                previous_state = self._seen_challenges[game_pk].get(uid)
                self._seen_challenges[game_pk][uid] = event_state

                if emit_updates_only and previous_state == event_state:
                    continue

                count = play.get("count", {})
                play_inning = play.get("about", {}).get("inning", current_inning)
                is_top = play.get("about", {}).get("isTopInning", True)
                half = "Top" if is_top else "Bot"

                event_time = play_event.get("startTime", "")

                # Determine who issued the challenge and their role.
                # Batting team challenge → batter; fielding team → catcher (or
                # pitcher as fallback if catcher cannot be resolved from boxscore).
                batting_team = away_team if is_top else home_team
                fielding_team_key = "home" if is_top else "away"
                if challenging_team == batting_team:
                    challenger_name = batter_name
                    challenger_role = "batter"
                else:
                    catcher = self._get_active_catcher(feed, fielding_team_key)
                    if catcher:
                        challenger_name = catcher
                        challenger_role = "catcher"
                    else:
                        challenger_name = pitcher_name
                        challenger_role = "pitcher"

                challenge = {
                    "uid": uid,
                    "game_pk": game_pk,
                    "game_pk_str": game_pk_str,
                    "away_team": away_team,
                    "home_team": home_team,
                    "away_abbr": away_abbr,
                    "home_abbr": home_abbr,
                    "venue": venue,
                    "away_score": away_score,
                    "home_score": home_score,
                    "inning": play_inning,
                    "inning_half": half,
                    "pitcher": pitcher_name,
                    "batter": batter_name,
                    "challenging_team": challenging_team,
                    "review_type": review_type,
                    "is_in_progress": is_in_progress,
                    "is_overturned": is_overturned,
                    "description": (
                        details.get("description")
                        or play_event.get("description")
                        or play.get("result", {}).get("description", "")
                    ),
                    "pitch_info": pitch_info,
                    "balls": count.get("balls", 0),
                    "strikes": count.get("strikes", 0),
                    "outs": count.get("outs", 0),
                    "event_time": event_time,
                    "challenger_name": challenger_name,
                    "challenger_role": challenger_role,
                    "is_abs_pitch_challenge": _is_abs_pitch_challenge(
                        review_type_raw, details, play_event, play, pitch_info
                    ),
                }
                new_challenges.append(challenge)

        return new_challenges

    async def check_for_new_challenges(self) -> list[dict]:
        """
        Main polling method. Fetches today's live games and returns
        any newly detected challenge events.
        """
        games = await self.get_todays_games()
        all_challenges = []

        live_statuses = {"I", "IR", "IO", "MA", "MF"}  # In Progress statuses

        for game in games:
            status_code = game.get("status", {}).get("statusCode", "")
            if status_code not in live_statuses:
                continue

            game_pk = game.get("gamePk")
            if not game_pk:
                continue

            feed = await self.get_live_feed(game_pk)
            if not feed:
                continue

            challenges = self._extract_challenges_from_feed(feed, game_pk)
            all_challenges.extend(challenges)

        return all_challenges
