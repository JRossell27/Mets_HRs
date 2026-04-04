"""
ABS Season-Long Challenge Tracker

Tracks pitch challenge success rates for batters, catchers, and pitchers
across the 2026 MLB season starting March 25, 2026. Data is persisted to
a JSON file so stats survive bot restarts. On cold starts the backfill
method re-fetches all completed games from season start to rebuild stats.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

EASTERN = pytz.timezone("US/Eastern")
SEASON_START = date(2026, 3, 25)

# Where to store persistent data.  Set DATA_DIR env var to a mounted volume
# path (e.g. /data) so stats survive container restarts on Fly.io.
_DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DATA_FILE = _DATA_DIR / "abs_season_data.json"

# Minimum challenges a player needs to appear in the leaderboard.
MIN_CHALLENGES = 3
CLASSIFIER_VERSION = 14


class ABSSeasonTracker:
    """Persists and analyses ABS pitch challenge results for the 2026 season."""

    def __init__(self, data_file: Path = DATA_FILE):
        self.data_file = data_file
        self.data = self._load()
        self._normalize_data_schema()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self.data_file.exists():
            try:
                with open(self.data_file) as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("Could not load ABS tracker data: %s", exc)
        return {
            "season_year": 2026,
            "season_start": "2026-03-25",
            "classifier_version": CLASSIFIER_VERSION,
            "last_updated": None,
            # player_name -> {role, team, challenges, overturned, upheld}
            "players": {},
            # game PKs (as strings) we have already fully processed
            "processed_game_pks": [],
            # unique challenge IDs already recorded in season totals
            "recorded_challenge_uids": [],
            # dates (YYYY-MM-DD) for which the daily recap has been posted
            "daily_recap_posted": [],
        }

    def _save(self):
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.data_file, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as exc:
            logger.error("Failed to save ABS tracker data: %s", exc)

    def _normalize_data_schema(self):
        """
        Ensure persisted data has expected keys/types after upgrades.
        """
        self.data.setdefault("processed_game_pks", [])
        self.data.setdefault("daily_recap_posted", [])
        self.data.setdefault("players", {})
        self.data.setdefault("recorded_challenge_uids", [])
        self.data.setdefault("posted_discord_uids", [])
        self.data.setdefault("posted_discord_fingerprints", [])
        self.data.setdefault("classifier_version", 1)

        # Backward/forward compatibility: support list or dict.
        recorded = self.data.get("recorded_challenge_uids")
        if isinstance(recorded, dict):
            self.data["recorded_challenge_uids"] = list(recorded.keys())
        elif not isinstance(recorded, list):
            self.data["recorded_challenge_uids"] = []

        # If challenge-classification logic changed, rebuild stats from scratch
        # on next backfill so persisted totals stay consistent with new filters.
        if self.data.get("classifier_version", 1) < CLASSIFIER_VERSION:
            logger.warning(
                "Classifier version changed (%s -> %s). Resetting season aggregates for re-backfill.",
                self.data.get("classifier_version", 1), CLASSIFIER_VERSION,
            )
            self.data["classifier_version"] = CLASSIFIER_VERSION
            self.data["players"] = {}
            self.data["recorded_challenge_uids"] = []
            self.data["processed_game_pks"] = []
            self.data["daily_recap_posted"] = []
            # NOTE: posted_discord_uids is intentionally NOT cleared here.
            # Clearing it would cause every previously-posted challenge to be
            # re-posted on the next startup, flooding Discord.
            self.data["last_updated"] = None
            self._save()

    # ── Game processing state ────────────────────────────────────────────────

    def is_game_processed(self, game_pk: int) -> bool:
        return str(game_pk) in self.data["processed_game_pks"]

    def mark_game_processed(self, game_pk: int):
        pk_str = str(game_pk)
        if pk_str not in self.data["processed_game_pks"]:
            self.data["processed_game_pks"].append(pk_str)
            self._save()

    # ── Challenge recording ──────────────────────────────────────────────────

    def record_challenge(self, challenge: dict) -> bool:
        """
        Record a resolved ABS pitch challenge into the season stats.

        Returns True if the challenge was newly recorded.
        Skips in-progress challenges, inconclusive outcomes, and
        explicitly non-pitch review types (manager/replay challenges).
        """
        uid = challenge.get("uid", "?")
        recorded_uids = set(self.data.get("recorded_challenge_uids", []))
        if uid in recorded_uids:
            logger.debug("Skipping duplicate already-recorded challenge uid=%s", uid)
            return False

        if challenge.get("is_in_progress"):
            logger.debug("Skipping in-progress challenge uid=%s", uid)
            return False

        if challenge.get("is_overturned") is None:
            logger.debug(
                "Skipping challenge with no outcome (uid=%s review_type=%s)",
                uid, challenge.get("review_type"),
            )
            return False

        # Block explicitly non-ABS types; accept anything else that triggered
        # challenge detection only if explicitly classified as ABS pitch review.
        review_type = challenge.get("review_type", "")
        non_abs_types = ("Manager Challenge", "Replay Review", "Umpire Review")
        if any(t in review_type for t in non_abs_types):
            logger.debug("Skipping non-ABS challenge type=%s uid=%s", review_type, uid)
            return False
        if not challenge.get("is_abs_pitch_challenge", False):
            logger.debug(
                "Skipping non-ABS/ambiguous challenge uid=%s review_type=%s",
                uid, review_type,
            )
            return False
        if challenge.get("challenging_team") in ("", "Unknown Team"):
            logger.debug("Skipping challenge with unknown challenging team uid=%s", uid)
            return False
        if challenge.get("challenger_role") not in ("batter", "catcher", "pitcher"):
            logger.debug("Skipping challenge with unsupported role uid=%s", uid)
            return False
        challenger_name = challenge.get("challenger_name", "").strip()
        challenger_role = challenge.get("challenger_role", "").strip()
        if not challenger_name or not challenger_role:
            logger.debug(
                "Skipping challenge - missing challenger info (uid=%s name=%r role=%r)",
                uid, challenger_name, challenger_role,
            )
            return False

        team = challenge.get("challenging_team", "")
        is_overturned = challenge["is_overturned"]

        players = self.data["players"]
        if challenger_name not in players:
            players[challenger_name] = {
                "role": challenger_role,
                "team": team,
                "challenges": 0,
                "overturned": 0,
                "upheld": 0,
            }

        p = players[challenger_name]
        p["challenges"] += 1
        if is_overturned:
            p["overturned"] += 1
        else:
            p["upheld"] += 1

        # Keep role at the most specific value (catcher beats pitcher)
        if challenger_role in ("batter", "catcher"):
            p["role"] = challenger_role
        elif p["role"] not in ("batter", "catcher"):
            p["role"] = challenger_role

        p["team"] = team  # update to most recent team (trades, etc.)
        self.data["last_updated"] = datetime.now(EASTERN).isoformat()
        self.data.setdefault("recorded_challenge_uids", []).append(uid)
        self._save()
        return True

    # ── Stats lookups ────────────────────────────────────────────────────────

    def get_player_stats(self, player_name: str) -> Optional[dict]:
        """Return raw stats dict for a player, or None if not found."""
        return self.data["players"].get(player_name)

    def get_player_pct(self, player_name: str) -> Optional[float]:
        """Return the player's season overturn percentage (0-100), or None."""
        stats = self.get_player_stats(player_name)
        if not stats or stats["challenges"] == 0:
            return None
        return stats["overturned"] / stats["challenges"] * 100

    def get_side_totals(self, side: str) -> dict:
        """
        Return aggregated season totals for offense or defense challenges.

        side:
          - "offense" -> batter challenges
          - "defense" -> catcher/pitcher challenges
        """
        if side == "offense":
            include_roles = {"batter"}
        elif side == "defense":
            include_roles = {"catcher", "pitcher"}
        else:
            include_roles = {"batter", "catcher", "pitcher"}

        players = self.data.get("players", {})
        selected = [
            s for s in players.values()
            if s.get("role") in include_roles
        ]
        challenges = sum(s.get("challenges", 0) for s in selected)
        overturned = sum(s.get("overturned", 0) for s in selected)
        upheld = sum(s.get("upheld", 0) for s in selected)
        pct = (overturned / challenges * 100) if challenges else 0.0
        return {
            "side": side,
            "challenges": challenges,
            "overturned": overturned,
            "upheld": upheld,
            "pct": pct,
        }

    def get_challenge_side_totals(self, challenger_role: str) -> dict:
        """Return offense/defense aggregate for a specific challenge role."""
        side = "offense" if challenger_role == "batter" else "defense"
        return self.get_side_totals(side)

    # ── Daily recap ──────────────────────────────────────────────────────────

    def has_posted_recap(self, date_str: str) -> bool:
        return date_str in self.data.get("daily_recap_posted", [])

    def has_posted_discord(self, uid: str) -> bool:
        return uid in self.data.get("posted_discord_uids", [])

    def mark_discord_posted(self, uid: str):
        lst = self.data.setdefault("posted_discord_uids", [])
        if uid not in lst:
            lst.append(uid)
            self._save()

    def has_posted_fingerprint(self, fingerprint: str) -> bool:
        return fingerprint in self.data.get("posted_discord_fingerprints", [])

    def mark_fingerprint_posted(self, fingerprint: str):
        lst = self.data.setdefault("posted_discord_fingerprints", [])
        if fingerprint not in lst:
            lst.append(fingerprint)
            self._save()

    def mark_recap_posted(self, date_str: str):
        lst = self.data.setdefault("daily_recap_posted", [])
        if date_str not in lst:
            lst.append(date_str)
            self._save()

    def generate_daily_recap(self) -> str:
        """
        Build the daily ABS season-tracker Discord message.
        """
        today_str = datetime.now(EASTERN).strftime("%B %d, %Y")
        players = self.data["players"]

        total_challenges = sum(s["challenges"] for s in players.values())
        total_overturned = sum(s["overturned"] for s in players.values())
        overall_pct = (total_overturned / total_challenges * 100) if total_challenges else 0

        def rate(s: dict) -> float:
            return s["overturned"] / s["challenges"] if s["challenges"] else 0.0

        def player_row(rank: int, name: str, s: dict) -> str:
            pct = rate(s) * 100
            return (
                f"`{rank:2}.` **{name}** ({s['team']})  "
                f"{s['overturned']}/{s['challenges']}  **{pct:.1f}%**"
            )

        sort_key = lambda x: (-x[1]["overturned"], -rate(x[1]))

        def ranked(role: str) -> list:
            qual = {
                n: s for n, s in players.items()
                if s["role"] == role and s["challenges"] >= MIN_CHALLENGES
            }
            return sorted(qual.items(), key=sort_key)[:3]

        top_batters = ranked("batter")
        fielders_qual = {
            n: s for n, s in players.items()
            if s["role"] != "batter" and s["challenges"] >= MIN_CHALLENGES
        }
        top_fielders = sorted(fielders_qual.items(), key=sort_key)[:3]

        offense_totals = self.get_side_totals("offense")
        defense_totals = self.get_side_totals("defense")

        lines = [
            f"## 📊 ABS Challenge Tracker — {today_str}",
            "",
            (
                f"**2026 Season Totals** · "
                f"Challenges: **{total_challenges}** · "
                f"Overturned: **{total_overturned}** · "
                f"Overall Success Rate: **{overall_pct:.1f}%**"
            ),
            (
                f"**Offense (Batters)** · {offense_totals['overturned']}/{offense_totals['challenges']} "
                f"(**{offense_totals['pct']:.1f}%**)"
            ),
            (
                f"**Defense (Catchers/Pitchers)** · {defense_totals['overturned']}/{defense_totals['challenges']} "
                f"(**{defense_totals['pct']:.1f}%**)"
            ),
            "",
        ]

        def section(title: str, emoji: str, rows: list, role_label: str):
            lines.append(f"### {emoji} {title}")
            if rows:
                for i, (name, s) in enumerate(rows, 1):
                    lines.append(player_row(i, name, s))
            else:
                lines.append(f"*No {role_label} with {MIN_CHALLENGES}+ challenges yet.*")
            lines.append("")

        section("Top 3 Batters — Overturn Success", "🏏", top_batters, "batters")
        section("Top 3 Fielders — Overturn Success", "🧤", top_fielders, "fielders")

        lines.append(
            f"*Min. {MIN_CHALLENGES} challenges to qualify · "
            f"Fielders include catcher/pitcher defensive challenges*"
        )
        return "\n".join(lines)

    # ── Backfill ─────────────────────────────────────────────────────────────

    async def backfill_season(self, monitor) -> int:
        """
        Fetch and process every completed game from SEASON_START through
        yesterday (Eastern time) that hasn't been processed yet.

        Returns the number of new challenges recorded.
        """
        today = datetime.now(EASTERN).date()
        current = SEASON_START
        recorded = 0
        games_scanned = 0
        challenges_found = 0

        logger.info(
            "ABS backfill: scanning %s → %s",
            SEASON_START.isoformat(),
            (today - timedelta(days=1)).isoformat(),
        )

        while current < today:
            date_str = current.strftime("%Y-%m-%d")
            games = await monitor.get_games_for_date(date_str)
            logger.info("Backfill %s: %d games found", date_str, len(games))

            # Log the first game's raw status so we can see what the API
            # returns - useful if games keep being skipped unexpectedly.
            if games:
                logger.info(
                    "Backfill %s sample game status: %s",
                    date_str, games[0].get("status", {}),
                )

            for game in games:
                game_pk = game.get("gamePk")
                if not game_pk or self.is_game_processed(game_pk):
                    continue

                # Since we only iterate over dates BEFORE today, every game
                # on those dates is finished.  Fetch the feed directly without
                # trusting schedule API status codes (they vary across API
                # versions and caused all games to be skipped previously).
                feed = await monitor.get_live_feed(game_pk)
                if not feed:
                    logger.warning("No feed for game %s - skipping", game_pk)
                    continue

                # If there are no plays the game was postponed/cancelled.
                all_plays = (
                    feed.get("liveData", {})
                        .get("plays", {})
                        .get("allPlays", [])
                )
                if not all_plays:
                    logger.debug(
                        "Game %s has no plays (postponed/cancelled) - marking processed",
                        game_pk,
                    )
                    self.mark_game_processed(game_pk)
                    continue

                games_scanned += 1
                challenges = monitor.extract_all_challenges_from_feed(feed, game_pk)
                logger.info(
                    "Game %s (%s): %d challenge event(s) found",
                    game_pk, date_str, len(challenges),
                )
                challenges_found += len(challenges)

                for ch in challenges:
                    logger.debug(
                        "Challenge uid=%s review_type=%r overturned=%s challenger=%r role=%r",
                        ch.get("uid"), ch.get("review_type"),
                        ch.get("is_overturned"), ch.get("challenger_name"),
                        ch.get("challenger_role"),
                    )
                    if self.record_challenge(ch):
                        recorded += 1

                self.mark_game_processed(game_pk)

            current += timedelta(days=1)

        logger.info(
            "ABS backfill complete - scanned %d games, found %d challenge events, "
            "recorded %d new challenges",
            games_scanned, challenges_found, recorded,
        )
        return recorded
