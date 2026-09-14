import os
import sys
import argparse
import json
import logging
import time
import re
import traceback
import requests


# ---------------- LOGGING ----------------
# Scheduler stdout is frequently not captured/visible, so we always
# write to a log file as well as stdout. Override the path with
# SCRAPER_LOG_PATH if you want logs somewhere specific.
LOG_PATH = os.getenv("SCRAPER_LOG_PATH", "flashscore_scraper.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("flashscore")


# ---------------- TUNABLES ----------------
# HISTORY: this scraper went through two prior incarnations before
# this one. Flashscore's DOM: navigations pinned at a flat 90s timeout,
# a stats "tab" click that sometimes silently hung for 480-1231+
# seconds with no error, reproduced across three Flashscore domains,
# with and without concurrent browsers — never resolved. Then
# Sofascore's internal JSON API via a Playwright-established browser
# session (bare `requests` calls were blocked, 403, but fetch() from
# inside a real page succeeded) — fast and reliable in every local
# test, but blocked outright (instant 403, edge/WAF-level, not
# fingerprint-based) from GitHub Actions' datacenter IP range,
# something no amount of in-page trickery can route around.
#
# This version targets 365scores.com instead, whose equivalent API
# (webws.365scores.com/web/...) answers plain, unauthenticated
# `requests.get()` calls directly — confirmed by direct testing, no
# browser, no session, no fingerprinting needed at all. That means no
# Playwright, no subprocess watchdog, no browser-recycling, none of
# the machinery the prior two versions needed — a stuck HTTP request
# with requests' own `timeout=` genuinely aborts at the socket level,
# which was never reliably true of Playwright's `timeout=` against
# whatever was actually happening with Flashscore. Whether 365scores'
# API is *also* reachable from GitHub Actions' IP range specifically
# is the one thing that couldn't be verified in advance (no way to
# execute this from a GH Actions runner directly) — the real answer
# comes from running this for real.
REQUEST_TIMEOUT_SEC = int(os.getenv("SCRAPER_REQUEST_TIMEOUT_SEC", "20"))

# A transient 5xx (seen once, live: a 504 that succeeded on the very
# next attempt) shouldn't sink an otherwise-good match/team — retried
# with a short pause, not the full kill-and-respawn machinery the
# Playwright-based versions needed for a fundamentally different
# failure mode (a hung request, not a bounce-back error response).
MAX_RETRIES = int(os.getenv("SCRAPER_MAX_RETRIES", "3"))
RETRY_BACKOFF_SEC = float(os.getenv("SCRAPER_RETRY_BACKOFF_SEC", "1.5"))

# Hard ceiling on how long discover_matches / get_team_recent_matches
# are allowed to spend paginating the API for one team/date, on top of
# their own page-count caps. Without this, an API response shape
# change could spin through many pages indefinitely.
DISCOVER_TIME_BUDGET_SEC = int(os.getenv("SCRAPER_DISCOVER_BUDGET_SEC", "60"))


# ---------------- JOB STATUS TELEGRAM ----------------
def send_job_status(message, bot_token, chat_id):
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        requests.post(url, data=payload, timeout=20)
    except Exception as e:
        log.warning(f"Failed to send job status to Telegram: {e}")


# ---------------- SCRAPER CLASS ----------------
class SixtyFiveScoresScraper:
    """
    Talks directly to 365scores.com's own internal JSON API
    (webws.365scores.com/web/...) with plain requests calls — no
    browser, no session establishment, no fingerprinting needed. See
    this module's TUNABLES comment for why (and what this replaces).

    Every endpoint used here was found by watching what the real
    365scores.com website itself calls while browsing it (network
    inspection via a real, one-off Playwright session used only for
    that investigation, not part of this class), then confirmed
    directly against plain `requests.get()` calls — not guessed.
    """

    BASE_URL = "https://webws.365scores.com/web"

    # Required on every call — this is what the site's own frontend
    # always sends; a couple of these (timezoneName, userCountryId)
    # look like they affect response localization/ordering, not
    # authorization, but they're included as-is since that's what a
    # real request looks like.
    COMMON_PARAMS = {
        "appTypeId": 5,
        "langId": 10,
        "timezoneName": "Africa/Johannesburg",
        "userCountryId": 134,
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/134.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })
        self.team_id = None
        self.team_name = None

    def _api_get(self, path, params=None, max_retries=None):
        """
        GET https://webws.365scores.com/web/{path} with COMMON_PARAMS
        plus whatever's passed in `params`, retrying transient
        failures (connection errors, timeouts, 5xx) up to max_retries
        times with a short pause between attempts. Returns the parsed
        JSON body, or None (logged) if every attempt fails or the
        response isn't valid JSON.
        """
        if max_retries is None:
            max_retries = MAX_RETRIES

        url = f"{self.BASE_URL}/{path}"
        full_params = dict(self.COMMON_PARAMS)
        if params:
            full_params.update(params)

        last_error = None

        for attempt in range(1, max_retries + 1):
            t0 = time.time()
            try:
                r = self.session.get(
                    url, params=full_params, timeout=REQUEST_TIMEOUT_SEC
                )
            except Exception as e:
                last_error = f"request error: {e}"
                log.warning(
                    f"API GET {url} attempt {attempt}/{max_retries} "
                    f"failed after {time.time()-t0:.1f}s: {last_error}"
                )
                if attempt < max_retries:
                    time.sleep(RETRY_BACKOFF_SEC)
                continue

            if r.status_code >= 500:
                last_error = f"status={r.status_code}"
                log.warning(
                    f"API GET {url} attempt {attempt}/{max_retries} "
                    f"got {r.status_code} after {time.time()-t0:.1f}s "
                    f"(transient server error, retrying)"
                )
                if attempt < max_retries:
                    time.sleep(RETRY_BACKOFF_SEC)
                continue

            if r.status_code != 200:
                # A 4xx (403, 404, ...) won't fix itself on retry —
                # this is where a real IP/access block would show up.
                log.warning(
                    f"API GET {url} failed after {time.time()-t0:.1f}s: "
                    f"status={r.status_code} body={r.text[:200]!r}"
                )
                return None

            try:
                return r.json()
            except Exception as e:
                log.warning(
                    f"API GET {url} returned invalid JSON after "
                    f"{time.time()-t0:.1f}s: {e}"
                )
                return None

        log.warning(
            f"API GET {url} exhausted {max_retries} attempts, "
            f"last error: {last_error}"
        )
        return None

    # ---------------- DISCOVERY ----------------
    def discover_matches(
        self, target_count, date_str=None, only_upcoming=False, only_finished=False
    ):
        """
        Returns up to `target_count` match dicts for `date_str`
        (today, server-local, if not given, as DD/MM/YYYY — what the
        API expects): each
        {id, home_id, home_name, away_id, away_name, tournament,
        home_goals, away_goals} (the last two are None unless the
        match has already finished).

        A single games/allscores call already returns every match
        across every competition for the given date (163 on the day
        this was tested) — no per-tournament pagination needed, unlike
        the Sofascore version. When only_upcoming is True, skips
        anything not in statusGroup 2 (scheduled/not started) — 4 is
        finished, confirmed by direct inspection. only_finished is the
        mirror of that — used for backtesting a past date, where every
        match is expected to already be finished (statusGroup 4);
        only_upcoming and only_finished are mutually exclusive, not
        enforced here since callers only ever pass one.
        """
        if date_str is None:
            date_str = time.strftime("%d/%m/%Y")

        t0 = time.time()
        data = self._api_get(
            "games/allscores/",
            params={
                "sports": 1,
                "startDate": date_str,
                "endDate": date_str,
                "showOdds": "true",
                "onlyMajorGames": "false",
                "withTop": "true",
            },
        )

        matches = []
        skipped_not_upcoming = 0
        skipped_not_finished = 0

        if data and data.get("games"):
            for g in data["games"]:
                if time.time() - t0 > DISCOVER_TIME_BUDGET_SEC:
                    log.warning(
                        f"discover_matches hit its "
                        f"{DISCOVER_TIME_BUDGET_SEC}s time budget with "
                        f"{len(matches)}/{target_count} found — "
                        f"stopping early"
                    )
                    break

                if len(matches) >= target_count:
                    break

                if only_upcoming and g.get("statusGroup") != 2:
                    skipped_not_upcoming += 1
                    continue

                if only_finished and g.get("statusGroup") != 4:
                    skipped_not_finished += 1
                    continue

                home = g.get("homeCompetitor") or {}
                away = g.get("awayCompetitor") or {}
                if home.get("id") is None or away.get("id") is None:
                    continue

                matches.append({
                    "id": g["id"],
                    "home_id": home["id"],
                    "home_name": home.get("name", ""),
                    "away_id": away["id"],
                    "away_name": away.get("name", ""),
                    "tournament": g.get("competitionDisplayName", ""),
                    # Only meaningful once the match has finished —
                    # None for anything still upcoming. Carried
                    # through purely so backtest runs can show the
                    # real final score next to the prediction; live
                    # (only_upcoming) runs never have this populated.
                    "home_goals": home.get("score"),
                    "away_goals": away.get("score"),
                })

        log.info(
            f"discover_matches: found {len(matches)}/{target_count} "
            f"for {date_str}, {time.time()-t0:.1f}s"
            + (
                f", skipped {skipped_not_upcoming} already-started/finished"
                if only_upcoming
                else ""
            )
            + (
                f", skipped {skipped_not_finished} not-yet-finished"
                if only_finished
                else ""
            )
        )
        return matches

    # ---------------- TEAM HISTORY ----------------
    def get_team_recent_matches(self, team_id, count=6, exclude_match_id=None):
        """
        Returns up to `count` of this team's most recent FINISHED
        matches, each a light dict (id, home_id, home_name, away_id,
        away_name, home_goals, away_goals). games/results/ already
        returns most-recent-first and only finished/awarded games —
        confirmed by direct inspection (statusGroup 4 throughout,
        startTime descending) — filtered by statusGroup defensively
        anyway in case that ever includes something else.

        exclude_match_id: skip this one match id if it appears, and
        take the count beyond it instead. Exists for backtesting a
        past date — as of "today", the fixture being backtested is
        itself now finished, so it would otherwise show up as this
        team's own most recent result and leak its outcome into its
        own "recent form" sample. No-op (id is None) for live/upcoming
        runs, since an upcoming fixture can't appear in games/results/
        yet anyway.
        """
        data = self._api_get(
            "games/results/",
            params={"competitors": team_id, "showOdds": "true"},
        )

        results = []
        if not data or not data.get("games"):
            return results

        for g in data["games"]:
            if g.get("statusGroup") != 4:
                continue

            if exclude_match_id is not None and g.get("id") == exclude_match_id:
                continue

            home = g.get("homeCompetitor") or {}
            away = g.get("awayCompetitor") or {}
            home_goals = home.get("score")
            away_goals = away.get("score")

            if home.get("id") is None or away.get("id") is None:
                continue
            if home_goals is None or away_goals is None:
                continue

            results.append({
                "id": g["id"],
                "home_id": home["id"],
                "home_name": home.get("name", ""),
                "away_id": away["id"],
                "away_name": away.get("name", ""),
                "home_goals": home_goals,
                "away_goals": away_goals,
            })

            if len(results) >= count:
                break

        return results

    # ---------------- MATCH STATISTICS ----------------
    # Maps a 365scores statistics-item's `name` to the short name we
    # store it under. Richer than either prior source for major
    # leagues — this is the only one of the three sites that had an
    # "Expected Goals On Target" (xGOT) figure at all. Minor leagues
    # (reserve/lower divisions) come back with a much smaller stat set
    # (no xG-family fields at all) — every signal-evaluation function
    # already treats every one of these as optional (None-safe checks
    # throughout), so that just means fewer corroboration points are
    # available for those matches, not a broken pipeline.
    STAT_NAME_MAP = {
        "Expected Goals": "xg",
        "Expected Goals On Target": "xgot",
        "Total Shots": "shots",
        "Shots On Target": "shots_on_target",
        "Corners": "corners",
        "Big Chances Created": "big_chances",
        "Yellow Cards": "yellow_cards",
        "Fouls": "fouls",
        "Possession": "possession",
    }

    def _empty_stat_result(self):
        result = {}
        for stat_key in set(self.STAT_NAME_MAP.values()) | {"goals_prevented"}:
            result[f"home_{stat_key}"] = None
            result[f"away_{stat_key}"] = None
        return result

    def get_match_statistics(self, match_id, home_id, away_id):
        """
        Unlike Flashscore/Sofascore's stats payloads (already split
        into home/away fields per stat), 365scores returns one flat
        list of {name, competitorId, value} rows — one row per side
        per stat — so home_id/away_id are needed here to know which
        competitorId maps to which side.
        """
        result = self._empty_stat_result()

        data = self._api_get("game/stats/", params={"games": match_id})
        if not data or not data.get("statistics"):
            return result

        try:
            for item in data["statistics"]:
                stat_name = self.STAT_NAME_MAP.get(item.get("name"))
                if not stat_name:
                    continue

                competitor_id = item.get("competitorId")
                raw_value = item.get("value")
                value = self._parse_stat_value(raw_value)
                if value is None:
                    continue

                if competitor_id == home_id:
                    result[f"home_{stat_name}"] = value
                elif competitor_id == away_id:
                    result[f"away_{stat_name}"] = value
        except Exception as e:
            log.warning(
                f"Error parsing statistics for match {match_id}: {e}"
            )

        return result

    def _parse_stat_value(self, raw_value):
        # Values come back as strings, sometimes with a trailing '%'
        # (e.g. "52%" for possession) — strip that and parse the
        # leading number either way.
        if raw_value is None:
            return None
        try:
            return float(str(raw_value).rstrip("%"))
        except (TypeError, ValueError):
            return None

    # ---------------- STAT AVERAGING ----------------
    def _team_stat_avg(self, results, stat_name, team_id, side="for"):
        """
        Generic averager for every per-match stat (goals, xg, corners,
        cards, ...): side="for" -> team_id's own stat_name in each
        match; side="against" -> the opponent's. Matches by exact team
        ID — every source used here has unambiguous numeric IDs, no
        fuzzy name-aliasing needed.
        """
        total = 0
        counted = 0

        for r in results:
            is_home = r.get("home_id") == team_id
            is_away = r.get("away_id") == team_id
            if not is_home and not is_away:
                continue

            if side == "for":
                value = (
                    r.get(f"home_{stat_name}")
                    if is_home
                    else r.get(f"away_{stat_name}")
                )
            else:
                value = (
                    r.get(f"away_{stat_name}")
                    if is_home
                    else r.get(f"home_{stat_name}")
                )

            if value is None:
                continue

            total += value
            counted += 1

        if counted == 0:
            return None

        return round(total / counted, 2)

    def calculate_team_goals(self, results, team_id):
        total_goals = 0
        matches_counted = 0

        for r in results:
            if r.get("home_id") == team_id:
                total_goals += r.get("home_goals") or 0
                matches_counted += 1
            elif r.get("away_id") == team_id:
                total_goals += r.get("away_goals") or 0
                matches_counted += 1

        avg_goals = (
            total_goals / matches_counted if matches_counted > 0 else 0
        )

        return {
            "team": self.team_name or str(team_id),
            "total_goals": total_goals,
            "avg_goals": round(avg_goals, 2),
            "matches": matches_counted,
        }

    # ---------------- SCRAPER ----------------
    def analyze_team(self, team_id, team_name=None, exclude_match_id=None):
        """
        Fetches this team's last 6 finished matches, each one's
        statistics, and returns the same stats dict shape the
        (unchanged) signal-evaluation functions expect — the only
        thing they care about is the abstract dict shape, not where
        the data came from.

        exclude_match_id: passed straight through to
        get_team_recent_matches — see its docstring. Used for
        backtesting so a fixture doesn't end up in its own team's
        "recent form" sample.
        """
        t0 = time.time()
        self.team_id = team_id
        self.team_name = team_name or str(team_id)

        recent = self.get_team_recent_matches(
            team_id, count=6, exclude_match_id=exclude_match_id
        )
        results = []
        for m in recent:
            match_stats = self.get_match_statistics(
                m["id"], m["home_id"], m["away_id"]
            )
            match_data = dict(m)
            match_data.update(match_stats)
            results.append(match_data)

        log.info(
            f"analyze_team({self.team_name!r}): {len(results)}/6 "
            f"matches fetched in {time.time()-t0:.1f}s total"
        )

        stats = self.calculate_team_goals(results, team_id)
        avg_gc = self._team_stat_avg(results, "goals", team_id, "against") or 0
        avg_xg = self._team_stat_avg(results, "xg", team_id, "for")
        avg_xga = self._team_stat_avg(results, "xg", team_id, "against")

        avg_gd = round(stats["avg_goals"] - avg_gc, 2)

        if avg_xg is not None and avg_xga is not None:
            avg_xgd = round(avg_xg - avg_xga, 2)
        else:
            avg_xgd = None

        stats.update({
            "avg_gc": avg_gc,
            "avg_gd": avg_gd,
            "avg_xg": avg_xg,
            "avg_xga": avg_xga,
            "avg_xgd": avg_xgd,
            "avg_corners_for": self._team_stat_avg(results, "corners", team_id, "for"),
            "avg_corners_against": self._team_stat_avg(results, "corners", team_id, "against"),
            "avg_big_chances_for": self._team_stat_avg(results, "big_chances", team_id, "for"),
            "avg_big_chances_against": self._team_stat_avg(results, "big_chances", team_id, "against"),
            "avg_yellow_cards": self._team_stat_avg(results, "yellow_cards", team_id, "for"),
            "avg_fouls": self._team_stat_avg(results, "fouls", team_id, "for"),
            "avg_xgot_for": self._team_stat_avg(results, "xgot", team_id, "for"),
            "avg_xgot_against": self._team_stat_avg(results, "xgot", team_id, "against"),
            "avg_goals_prevented": self._team_stat_avg(results, "goals_prevented", team_id, "for"),
            "avg_shots_for": self._team_stat_avg(results, "shots", team_id, "for"),
            "avg_shots_against": self._team_stat_avg(results, "shots", team_id, "against"),
            "avg_sot_for": self._team_stat_avg(results, "shots_on_target", team_id, "for"),
            "avg_sot_against": self._team_stat_avg(results, "shots_on_target", team_id, "against"),
            "avg_possession": self._team_stat_avg(results, "possession", team_id, "for"),
        })

        return {
            "team": stats["team"],
            "matches": [m["id"] for m in recent],
            "results": results,
            "stats": stats,
        }

    # ---------------- TELEGRAM ----------------
    def send_telegram_message(self, message, bot_token, chat_id):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
            }
            r = requests.post(url, data=payload, timeout=20)
            if r.status_code != 200:
                log.warning(f"Telegram error: {r.text}")
        except Exception as e:
            log.error(f"Failed to send Telegram message: {e}")

    def close(self):
        try:
            self.session.close()
        except Exception as e:
            log.warning(f"Error closing session: {e}")


# ---------------- SIGNAL ENGINE ----------------
#
# Single, focused prediction: HOME team wins by a 2+ goal margin
# (2-0, 3-0, 3-1, 4-1, 4-2, ...). This is not a general betting-markets
# engine — every other market from earlier iterations (draw, away win,
# double chance, handicap, totals, corners/cards O/U, BTTS, clean
# sheet, shots O/U, combos) has been removed. All the stats gathered by
# SofascoreGoalsScraper are still collected the same way; this just
# uses them narrowly, for one question. This whole section is
# site-agnostic — it only ever reads the abstract stats dict shape
# analyze_team produces, never anything Sofascore- or Flashscore-
# specific, so none of it needed to change when the scraper migrated.
#
# NOTE ON CONFIDENCE: every threshold below is a heuristic cutoff, not
# a measured probability — nothing here has been validated against
# actual historical outcomes. "HIGH-CONFIDENCE" means "clears a
# deliberately severe set of hand-tuned filters", not "X% likely to
# happen".

# Every signal requires each team's stats to be built from at least
# this many of the up-to-6 fetched recent matches. Below this, the
# sample is too thin to trust any prediction on it.
MIN_SAMPLE_MATCHES = 6

# The literal target: home win margin (goals for - goals against).
MARGIN_TARGET = 2.0

# How far past MARGIN_TARGET the *expected* margin needs to sit before
# this fires. Match-to-match variance means an expected margin of
# exactly 2.0 is a coinflip on actually clearing it, not a safe call —
# xG is a materially better estimator than raw averaged goals, so it
# gets a smaller required buffer; the goals-only fallback needs more
# daylight to trust it the same amount.
XG_MARGIN_BUFFER = 0.9    # need expected_margin >= 2.9
GD_MARGIN_BUFFER = 1.3    # need expected_margin >= 3.3

# Corroboration score (see _margin_score) required on top of the
# expected-margin gate above. This exists so the signal isn't just
# trusting a goals/xG gap that might be riding a couple of clinical
# finishes — it has to show up in shots, chances, territory, shot
# quality, or the opposing keeper's own record too. Max achievable is
# ~8 (2 shots-on-target + 2 big chances for + 1 big chances against +
# 1 corners + 1 favourite's own xGOT + 1 underdog's leaky keeper),
# minus a possible -1.5 xGOT-overperformance penalty.
MARGIN_SCORE_THRESHOLD = 4.0


def _margin_score(
    h_sot_for, a_sot_against,
    h_bc_for, a_bc_against,
    a_bc_for, h_bc_against,
    h_corners_for, a_corners_against,
    a_xgot_for, a_g,
    h_xg, h_xgot_for, a_gp,
):
    """
    Corroboration score for "home wins by 2+" on top of the expected-
    margin gate. Uses the same for+against blended-expectation
    approach as the corners/shots-on-target signals elsewhere in this
    script (each side's own rate averaged with what their opponent
    typically allows) — NOT a raw subtraction between the two, which
    doesn't work here: h_sot_for and a_sot_against measure different
    things on the same rough scale, so a genuinely strong case (e.g.
    8.0 vs 7.5) can produce a near-zero "gap" despite both numbers
    being high for a reason.

    All args are None-safe (missing stats just contribute nothing
    rather than disqualifying the match). Ends with a penalty, not a
    bonus: if the away side has been scoring above its own shot
    quality (xGOT), it's due a regression that would work against a
    wide home margin.
    """
    score = 0.0

    if h_sot_for is not None and a_sot_against is not None:
        expected_home_sot = (h_sot_for + a_sot_against) / 2
        if expected_home_sot >= 6.0:
            score += 2
        elif expected_home_sot >= 4.5:
            score += 1

    if h_bc_for is not None and a_bc_against is not None:
        expected_home_bc = (h_bc_for + a_bc_against) / 2
        if expected_home_bc >= 3.0:
            score += 2
        elif expected_home_bc >= 2.0:
            score += 1

    # Away creating little in return corroborates the margin holding,
    # not just the home side racking up chances.
    if a_bc_for is not None and h_bc_against is not None:
        expected_away_bc = (a_bc_for + h_bc_against) / 2
        if expected_away_bc <= 1.0:
            score += 1

    if h_corners_for is not None and a_corners_against is not None:
        expected_home_corners = (h_corners_for + a_corners_against) / 2
        if expected_home_corners >= 6.5:
            score += 1
        elif expected_home_corners >= 5.0:
            score += 0.5

    # The favourite's own shot quality running above its raw xG is a
    # positive signal in its own right — not just "not
    # overperforming", but genuinely creating better chances than xG
    # alone credits them for.
    if h_xgot_for is not None and h_xg is not None and h_xgot_for >= h_xg + 0.3:
        score += 1

    # A leaky underdog goalkeeper (conceding more than their own shot
    # quality faced suggests) directly corroborates the margin holding.
    if a_gp is not None and a_gp <= -0.2:
        score += 1

    if a_xgot_for is not None and a_g >= a_xgot_for + 0.8:
        score -= 1.5

    return score


def _escape_markdown(text):
    """
    Minimal escaping for Telegram's legacy "Markdown" parse mode: only
    _, *, ` and [ need escaping there (unlike MarkdownV2, which would
    require escaping most punctuation — unworkable given how many
    parens/dots/dashes show up throughout this data).
    """
    if text is None:
        return ""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def evaluate_home_margin_signal(home, away, home_data, away_data, m_url):
    """
    Returns a Telegram-ready message if this match clears the bar for
    "HOME team wins by 2+ goals", or None if it doesn't. Mirrored by
    evaluate_away_margin_signal below for the away side.
    """
    home = _escape_markdown(home)
    away = _escape_markdown(away)

    hs = home_data["stats"]
    as_ = away_data["stats"]

    # Sample-size gate: refuse to predict on either team unless the
    # full recent-match window was actually scraped.
    if (
        hs.get("matches", 0) < MIN_SAMPLE_MATCHES
        or as_.get("matches", 0) < MIN_SAMPLE_MATCHES
    ):
        return None

    h_g = hs.get("avg_goals", 0)
    a_g = as_.get("avg_goals", 0)

    h_gc = hs.get("avg_gc", 0)
    a_gc = as_.get("avg_gc", 0)

    h_xg = hs.get("avg_xg")
    a_xg = as_.get("avg_xg")

    h_xga = hs.get("avg_xga")
    a_xga = as_.get("avg_xga")

    h_corners_for = hs.get("avg_corners_for")
    h_corners_against = hs.get("avg_corners_against")
    a_corners_for = as_.get("avg_corners_for")
    a_corners_against = as_.get("avg_corners_against")

    h_bc_for = hs.get("avg_big_chances_for")
    h_bc_against = hs.get("avg_big_chances_against")
    a_bc_for = as_.get("avg_big_chances_for")
    a_bc_against = as_.get("avg_big_chances_against")

    h_cards = hs.get("avg_yellow_cards")
    a_cards = as_.get("avg_yellow_cards")
    h_fouls = hs.get("avg_fouls")
    a_fouls = as_.get("avg_fouls")

    h_gp = hs.get("avg_goals_prevented")
    a_gp = as_.get("avg_goals_prevented")

    h_shots_for = hs.get("avg_shots_for")
    h_shots_against = hs.get("avg_shots_against")
    a_shots_for = as_.get("avg_shots_for")
    a_shots_against = as_.get("avg_shots_against")

    h_sot_for = hs.get("avg_sot_for")
    h_sot_against = hs.get("avg_sot_against")
    a_sot_for = as_.get("avg_sot_for")
    a_sot_against = as_.get("avg_sot_against")

    h_xgot_for = hs.get("avg_xgot_for")
    a_xgot_for = as_.get("avg_xgot_for")

    h_poss = hs.get("avg_possession")
    a_poss = as_.get("avg_possession")

    use_xg = h_xg is not None and a_xg is not None and h_xga is not None and a_xga is not None

    # -------------------------------------------------
    # EXPECTED MARGIN
    # -------------------------------------------------
    # Each side's expected goals blends their own attacking rate with
    # the opponent's own defensive leakiness — same "for + opponent's
    # against" blend used throughout this script's other stats.

    if use_xg:
        expected_home_goals = (h_xg + a_xga) / 2
        expected_away_goals = (a_xg + h_xga) / 2
        margin_buffer = XG_MARGIN_BUFFER
        basis = "xG-based"
    else:
        expected_home_goals = (h_g + a_gc) / 2
        expected_away_goals = (a_g + h_gc) / 2
        margin_buffer = GD_MARGIN_BUFFER
        basis = "goals-based, no xG data"

    expected_margin = expected_home_goals - expected_away_goals

    # -------------------------------------------------
    # HARD FILTERS
    # -------------------------------------------------
    # Necessary conditions — if any fail, this match doesn't qualify
    # no matter how good the corroboration score looks.

    hard_filters_pass = (
        expected_home_goals >= 2.0   # home actually scores enough
        and expected_away_goals <= 1.1  # away isn't a real threat
        and h_gc <= 1.1               # home defense isn't leaky
        and a_g < 1.1                 # away's raw scoring record agrees
        and expected_margin >= (MARGIN_TARGET + margin_buffer)
    )

    if not hard_filters_pass:
        return None

    # -------------------------------------------------
    # CORROBORATION
    # -------------------------------------------------

    margin_score = _margin_score(
        h_sot_for, a_sot_against,
        h_bc_for, a_bc_against,
        a_bc_for, h_bc_against,
        h_corners_for, a_corners_against,
        a_xgot_for, a_g,
        h_xg, h_xgot_for, a_gp,
    )

    if margin_score < MARGIN_SCORE_THRESHOLD:
        return None

    # -------------------------------------------------
    # RISK FACTORS (shown, don't block the prediction)
    # -------------------------------------------------

    risks = []

    if h_xg is not None and h_g >= h_xg + 1.0:
        risks.append(
            f"{home} may be overperforming its xG — some regression "
            f"toward a smaller margin is possible"
        )

    if h_gc >= 1.0:
        risks.append(
            f"{home} has been conceding at a rate that could keep "
            f"the margin tighter than expected"
        )

    if (
        h_poss is not None and h_poss >= 58
        and h_bc_for is not None and h_bc_for <= 1.0
    ):
        risks.append(
            f"{home} tends to dominate the ball without creating "
            f"many big chances from it — territorial control alone "
            f"won't guarantee the margin"
        )

    # -------------------------------------------------
    # MESSAGE
    # -------------------------------------------------

    def fmt(v):
        return "N/A" if v is None else str(v)

    lines = [
        f"⚽ *{home} vs {away}*",
        "",
        f"🎯 *Prediction: {home} to win by 2+ goals* ({basis})",
        f"Expected margin ~{expected_margin:.2f} "
        f"(home ~{expected_home_goals:.2f}, away ~{expected_away_goals:.2f}) "
        f"| corroboration score {margin_score:.1f}",
        "",
        "📊 *Stats*",
        f"{home}   G {h_g} | GA {h_gc} | xG {fmt(h_xg)} | xGA {fmt(h_xga)}",
        f"{away}   G {a_g} | GA {a_gc} | xG {fmt(a_xg)} | xGA {fmt(a_xga)}",
        f"Possession {fmt(h_poss)}% vs {fmt(a_poss)}%",
        f"Shots {fmt(h_shots_for)}/{fmt(h_shots_against)} vs "
        f"{fmt(a_shots_for)}/{fmt(a_shots_against)} | "
        f"SoT {fmt(h_sot_for)}/{fmt(h_sot_against)} vs "
        f"{fmt(a_sot_for)}/{fmt(a_sot_against)}",
        f"Corners {fmt(h_corners_for)}/{fmt(h_corners_against)} vs "
        f"{fmt(a_corners_for)}/{fmt(a_corners_against)} | "
        f"BigCh {fmt(h_bc_for)}/{fmt(h_bc_against)} vs "
        f"{fmt(a_bc_for)}/{fmt(a_bc_against)}",
        f"Cards {fmt(h_cards)} vs {fmt(a_cards)} | "
        f"Fouls {fmt(h_fouls)} vs {fmt(a_fouls)} | "
        f"GP {fmt(h_gp)} vs {fmt(a_gp)}",
        "",
    ]

    if risks:
        lines.append(f"⚠️ *Risk factors ({len(risks)})*")
        lines.extend(f"• {r}" for r in risks)
        lines.append("")

    lines.append(f"🔗 {m_url}")

    return "\n".join(lines)


def evaluate_away_margin_signal(home, away, home_data, away_data, m_url):
    """
    Mirror image of evaluate_home_margin_signal: returns a message if
    this match clears the bar for "AWAY team wins by 2+ goals", or
    None. Every home/away role in the margin logic is swapped, but the
    thresholds, buffers, and _margin_score corroboration function are
    identical — reused directly with home/away arguments swapped
    pairwise, since _margin_score's parameters are really "the side
    we're backing" and "the side we're backing against", just named
    h_/a_ from how the home version happens to call it.
    """
    home = _escape_markdown(home)
    away = _escape_markdown(away)

    hs = home_data["stats"]
    as_ = away_data["stats"]

    if (
        hs.get("matches", 0) < MIN_SAMPLE_MATCHES
        or as_.get("matches", 0) < MIN_SAMPLE_MATCHES
    ):
        return None

    h_g = hs.get("avg_goals", 0)
    a_g = as_.get("avg_goals", 0)

    h_gc = hs.get("avg_gc", 0)
    a_gc = as_.get("avg_gc", 0)

    h_xg = hs.get("avg_xg")
    a_xg = as_.get("avg_xg")

    h_xga = hs.get("avg_xga")
    a_xga = as_.get("avg_xga")

    h_corners_for = hs.get("avg_corners_for")
    h_corners_against = hs.get("avg_corners_against")
    a_corners_for = as_.get("avg_corners_for")
    a_corners_against = as_.get("avg_corners_against")

    h_bc_for = hs.get("avg_big_chances_for")
    h_bc_against = hs.get("avg_big_chances_against")
    a_bc_for = as_.get("avg_big_chances_for")
    a_bc_against = as_.get("avg_big_chances_against")

    h_cards = hs.get("avg_yellow_cards")
    a_cards = as_.get("avg_yellow_cards")
    h_fouls = hs.get("avg_fouls")
    a_fouls = as_.get("avg_fouls")

    h_gp = hs.get("avg_goals_prevented")
    a_gp = as_.get("avg_goals_prevented")

    h_shots_for = hs.get("avg_shots_for")
    h_shots_against = hs.get("avg_shots_against")
    a_shots_for = as_.get("avg_shots_for")
    a_shots_against = as_.get("avg_shots_against")

    h_sot_for = hs.get("avg_sot_for")
    h_sot_against = hs.get("avg_sot_against")
    a_sot_for = as_.get("avg_sot_for")
    a_sot_against = as_.get("avg_sot_against")

    h_xgot_for = hs.get("avg_xgot_for")
    a_xgot_for = as_.get("avg_xgot_for")

    h_poss = hs.get("avg_possession")
    a_poss = as_.get("avg_possession")

    use_xg = h_xg is not None and a_xg is not None and h_xga is not None and a_xga is not None

    # -------------------------------------------------
    # EXPECTED MARGIN (away's perspective)
    # -------------------------------------------------

    if use_xg:
        expected_home_goals = (h_xg + a_xga) / 2
        expected_away_goals = (a_xg + h_xga) / 2
        margin_buffer = XG_MARGIN_BUFFER
        basis = "xG-based"
    else:
        expected_home_goals = (h_g + a_gc) / 2
        expected_away_goals = (a_g + h_gc) / 2
        margin_buffer = GD_MARGIN_BUFFER
        basis = "goals-based, no xG data"

    expected_margin = expected_away_goals - expected_home_goals

    # -------------------------------------------------
    # HARD FILTERS (away as the favourite, home as the underdog)
    # -------------------------------------------------

    hard_filters_pass = (
        expected_away_goals >= 2.0   # away actually scores enough
        and expected_home_goals <= 1.1  # home isn't a real threat
        and a_gc <= 1.1               # away defense isn't leaky
        and h_g < 1.1                 # home's raw scoring record agrees
        and expected_margin >= (MARGIN_TARGET + margin_buffer)
    )

    if not hard_filters_pass:
        return None

    # -------------------------------------------------
    # CORROBORATION — _margin_score with every home/away pair swapped
    # -------------------------------------------------

    margin_score = _margin_score(
        a_sot_for, h_sot_against,
        a_bc_for, h_bc_against,
        h_bc_for, a_bc_against,
        a_corners_for, h_corners_against,
        h_xgot_for, h_g,
        a_xg, a_xgot_for, h_gp,
    )

    if margin_score < MARGIN_SCORE_THRESHOLD:
        return None

    # -------------------------------------------------
    # RISK FACTORS
    # -------------------------------------------------

    risks = []

    if a_xg is not None and a_g >= a_xg + 1.0:
        risks.append(
            f"{away} may be overperforming its xG — some regression "
            f"toward a smaller margin is possible"
        )

    if a_gc >= 1.0:
        risks.append(
            f"{away} has been conceding at a rate that could keep "
            f"the margin tighter than expected"
        )

    if (
        a_poss is not None and a_poss >= 58
        and a_bc_for is not None and a_bc_for <= 1.0
    ):
        risks.append(
            f"{away} tends to dominate the ball without creating "
            f"many big chances from it — territorial control alone "
            f"won't guarantee the margin"
        )

    # -------------------------------------------------
    # MESSAGE
    # -------------------------------------------------

    def fmt(v):
        return "N/A" if v is None else str(v)

    lines = [
        f"⚽ *{home} vs {away}*",
        "",
        f"🎯 *Prediction: {away} to win by 2+ goals* ({basis})",
        f"Expected margin ~{expected_margin:.2f} "
        f"(away ~{expected_away_goals:.2f}, home ~{expected_home_goals:.2f}) "
        f"| corroboration score {margin_score:.1f}",
        "",
        "📊 *Stats*",
        f"{home}   G {h_g} | GA {h_gc} | xG {fmt(h_xg)} | xGA {fmt(h_xga)}",
        f"{away}   G {a_g} | GA {a_gc} | xG {fmt(a_xg)} | xGA {fmt(a_xga)}",
        f"Possession {fmt(h_poss)}% vs {fmt(a_poss)}%",
        f"Shots {fmt(h_shots_for)}/{fmt(h_shots_against)} vs "
        f"{fmt(a_shots_for)}/{fmt(a_shots_against)} | "
        f"SoT {fmt(h_sot_for)}/{fmt(h_sot_against)} vs "
        f"{fmt(a_sot_for)}/{fmt(a_sot_against)}",
        f"Corners {fmt(h_corners_for)}/{fmt(h_corners_against)} vs "
        f"{fmt(a_corners_for)}/{fmt(a_corners_against)} | "
        f"BigCh {fmt(h_bc_for)}/{fmt(h_bc_against)} vs "
        f"{fmt(a_bc_for)}/{fmt(a_bc_against)}",
        f"Cards {fmt(h_cards)} vs {fmt(a_cards)} | "
        f"Fouls {fmt(h_fouls)} vs {fmt(a_fouls)} | "
        f"GP {fmt(h_gp)} vs {fmt(a_gp)}",
        "",
    ]

    if risks:
        lines.append(f"⚠️ *Risk factors ({len(risks)})*")
        lines.extend(f"• {r}" for r in risks)
        lines.append("")

    lines.append(f"🔗 {m_url}")

    return "\n".join(lines)


# -------------------------------------------------
# HOME CLEAN SHEET PREDICTION
# -------------------------------------------------
# Second, independent prediction: HOME team keeps a clean sheet (away
# team fails to score). Same strictness philosophy and for+against
# blended-expectation approach as the margin prediction above — full
# sample, a buffered expected-goals gate, corroboration from the extra
# stats. Independent of evaluate_home_margin_signal: a match can fire
# either, both, or neither (e.g. a predicted 1-0 fires this but not
# the margin signal; a predicted 3-1 fires the margin signal but not
# this one).

CS_XG_AWAY_TARGET = 0.35   # xG-based: expected away goals must sit under this
CS_GD_AWAY_TARGET = 0.3    # goals-only fallback: tighter, no shot-quality backup
CS_SCORE_THRESHOLD = 3.0   # corroboration bar (see _clean_sheet_score)


def _clean_sheet_score(
    a_sot_for, h_sot_against,
    a_bc_for, h_bc_against,
    a_corners_for, h_corners_against,
    h_gp,
    a_xgot_for, a_g,
):
    """
    Corroboration score for "home keeps a clean sheet" — the mirror
    image of _margin_score: here we want AWAY's creation numbers
    (blended with what HOME concedes) to be LOW, not high. Same
    for+against blended-expectation approach as everywhere else in
    this script. All args are None-safe.
    """
    score = 0.0

    if a_sot_for is not None and h_sot_against is not None:
        expected_away_sot = (a_sot_for + h_sot_against) / 2
        if expected_away_sot <= 2.5:
            score += 2
        elif expected_away_sot <= 3.5:
            score += 1

    if a_bc_for is not None and h_bc_against is not None:
        expected_away_bc = (a_bc_for + h_bc_against) / 2
        if expected_away_bc <= 0.7:
            score += 2
        elif expected_away_bc <= 1.2:
            score += 1

    if a_corners_for is not None and h_corners_against is not None:
        expected_away_corners = (a_corners_for + h_corners_against) / 2
        if expected_away_corners <= 3.5:
            score += 1
        elif expected_away_corners <= 4.5:
            score += 0.5

    # A home keeper who's been outperforming their shot quality is
    # extra corroboration for a clean sheet holding up.
    if h_gp is not None and h_gp >= 0.2:
        score += 1

    # Same regression flag as the margin signal: away scoring above
    # its own shot quality is a risk to a clean sheet even against
    # otherwise weak-looking averages.
    if a_xgot_for is not None and a_g >= a_xgot_for + 0.8:
        score -= 1.5

    return score


def evaluate_home_clean_sheet_signal(home, away, home_data, away_data, m_url):
    """
    Returns a Telegram-ready message if this match clears the bar for
    "HOME team keeps a clean sheet" (away fails to score), or None.
    """
    home = _escape_markdown(home)
    away = _escape_markdown(away)

    hs = home_data["stats"]
    as_ = away_data["stats"]

    if (
        hs.get("matches", 0) < MIN_SAMPLE_MATCHES
        or as_.get("matches", 0) < MIN_SAMPLE_MATCHES
    ):
        return None

    h_g = hs.get("avg_goals", 0)
    a_g = as_.get("avg_goals", 0)

    h_gc = hs.get("avg_gc", 0)
    a_gc = as_.get("avg_gc", 0)

    h_xg = hs.get("avg_xg")
    a_xg = as_.get("avg_xg")

    h_xga = hs.get("avg_xga")
    a_xga = as_.get("avg_xga")

    h_corners_for = hs.get("avg_corners_for")
    h_corners_against = hs.get("avg_corners_against")
    a_corners_for = as_.get("avg_corners_for")
    a_corners_against = as_.get("avg_corners_against")

    h_bc_for = hs.get("avg_big_chances_for")
    h_bc_against = hs.get("avg_big_chances_against")
    a_bc_for = as_.get("avg_big_chances_for")
    a_bc_against = as_.get("avg_big_chances_against")

    h_cards = hs.get("avg_yellow_cards")
    a_cards = as_.get("avg_yellow_cards")
    h_fouls = hs.get("avg_fouls")
    a_fouls = as_.get("avg_fouls")

    h_gp = hs.get("avg_goals_prevented")
    a_gp = as_.get("avg_goals_prevented")

    h_shots_for = hs.get("avg_shots_for")
    h_shots_against = hs.get("avg_shots_against")
    a_shots_for = as_.get("avg_shots_for")
    a_shots_against = as_.get("avg_shots_against")

    h_sot_for = hs.get("avg_sot_for")
    h_sot_against = hs.get("avg_sot_against")
    a_sot_for = as_.get("avg_sot_for")
    a_sot_against = as_.get("avg_sot_against")

    a_xgot_for = as_.get("avg_xgot_for")

    h_poss = hs.get("avg_possession")
    a_poss = as_.get("avg_possession")

    use_xg = (
        h_xg is not None and a_xg is not None
        and h_xga is not None and a_xga is not None
    )

    if use_xg:
        expected_away_goals = (a_xg + h_xga) / 2
        away_target = CS_XG_AWAY_TARGET
        basis = "xG-based"
    else:
        expected_away_goals = (a_g + h_gc) / 2
        away_target = CS_GD_AWAY_TARGET
        basis = "goals-based, no xG data"

    # Hard filters — necessary conditions, checked with plain goals as
    # well as the xG-blended estimate so this isn't trusting xG alone.
    hard_filters_pass = (
        expected_away_goals <= away_target
        and a_g <= 0.5    # away's raw scoring record agrees
        and h_gc <= 0.6   # home's own defensive record is solid
    )

    if not hard_filters_pass:
        return None

    cs_score = _clean_sheet_score(
        a_sot_for, h_sot_against,
        a_bc_for, h_bc_against,
        a_corners_for, h_corners_against,
        h_gp,
        a_xgot_for, a_g,
    )

    if cs_score < CS_SCORE_THRESHOLD:
        return None

    risks = []

    if a_xg is not None and a_g >= a_xg + 0.6:
        risks.append(
            f"{away} may be underperforming its xG recently — some "
            f"regression toward actually scoring is possible"
        )

    if h_gp is not None and h_gp <= -0.2:
        risks.append(
            f"{home}'s goalkeeper has been conceding more than shot "
            f"quality suggests — the clean sheet record may be "
            f"shakier than the raw averages imply"
        )

    if (
        a_poss is not None and a_poss >= 55
        and a_bc_for is not None and a_bc_for >= 2.0
    ):
        risks.append(
            f"{away} still creates meaningful chances despite a low "
            f"scoring record — a clean sheet isn't guaranteed just "
            f"because they haven't been converting"
        )

    def fmt(v):
        return "N/A" if v is None else str(v)

    lines = [
        f"⚽ *{home} vs {away}*",
        "",
        f"🧤 *Prediction: {home} to keep a clean sheet* ({basis})",
        f"Expected {away} goals ~{expected_away_goals:.2f} | "
        f"corroboration score {cs_score:.1f}",
        "",
        "📊 *Stats*",
        f"{home}   G {h_g} | GA {h_gc} | xG {fmt(h_xg)} | xGA {fmt(h_xga)}",
        f"{away}   G {a_g} | GA {a_gc} | xG {fmt(a_xg)} | xGA {fmt(a_xga)}",
        f"Possession {fmt(h_poss)}% vs {fmt(a_poss)}%",
        f"Shots {fmt(h_shots_for)}/{fmt(h_shots_against)} vs "
        f"{fmt(a_shots_for)}/{fmt(a_shots_against)} | "
        f"SoT {fmt(h_sot_for)}/{fmt(h_sot_against)} vs "
        f"{fmt(a_sot_for)}/{fmt(a_sot_against)}",
        f"Corners {fmt(h_corners_for)}/{fmt(h_corners_against)} vs "
        f"{fmt(a_corners_for)}/{fmt(a_corners_against)} | "
        f"BigCh {fmt(h_bc_for)}/{fmt(h_bc_against)} vs "
        f"{fmt(a_bc_for)}/{fmt(a_bc_against)}",
        f"Cards {fmt(h_cards)} vs {fmt(a_cards)} | "
        f"Fouls {fmt(h_fouls)} vs {fmt(a_fouls)} | "
        f"GP {fmt(h_gp)} vs {fmt(a_gp)}",
        "",
    ]

    if risks:
        lines.append(f"⚠️ *Risk factors ({len(risks)})*")
        lines.extend(f"• {r}" for r in risks)
        lines.append("")

    lines.append(f"🔗 {m_url}")

    return "\n".join(lines)


# -------------------------------------------------
# TEAM TOTAL GOALS UNDER 1.5 PREDICTION
# -------------------------------------------------
# Third, independent prediction: does HOME's own total, or AWAY's own
# total, come in under 1.5 goals — checked separately per side, using
# the same expected-own-goals blend as the margin/clean-sheet signals
# (own attacking rate blended with the opponent's own defensive
# leakiness). A match can fire for home only, away only, both, or
# neither. Unlike an under-2.5 line, most teams don't reliably sit
# under 1.5 on their own — this is a real, fairly strict claim, not a
# near-freebie — so the buffer targets are correspondingly tight (0.7
# xG-based, 0.5 goals-only).

U15_XG_BUFFER = 0.8    # xG-based: need expected own goals <= 1.5 - 0.8 = 0.7
U15_GD_BUFFER = 1.0    # goals-only fallback: <= 1.5 - 1.0 = 0.5
# Max achievable is ~4 (1 shots-on-target + 2 big chances + 1 opposing
# keeper outperforming shot quality), minus a possible -1.5 xGOT
# overperformance penalty.
U15_SCORE_THRESHOLD = 2.5   # corroboration bar (see _under_goals_score)


def _under_goals_score(
    team_sot_for, opp_sot_against,
    team_bc_for, opp_bc_against,
    team_xgot_for, team_g,
    opp_gp,
):
    """
    Corroboration score for "this team scores under 1.5" — wants this
    team's own shot/chance creation (blended with what the opponent
    typically concedes) to be modest, not high. All args are
    None-safe. Ends with a penalty, not a bonus: a team scoring well
    above its own shot quality (xGOT) is a live risk of a breakout
    high-scoring game regardless of what the averages otherwise
    suggest.
    """
    score = 0.0

    if team_sot_for is not None and opp_sot_against is not None:
        expected_sot = (team_sot_for + opp_sot_against) / 2
        if expected_sot <= 3.5:
            score += 1

    if team_bc_for is not None and opp_bc_against is not None:
        expected_bc = (team_bc_for + opp_bc_against) / 2
        if expected_bc <= 1.2:
            score += 2
        elif expected_bc <= 2.0:
            score += 1

    # An opposing goalkeeper who's been outperforming their own shot
    # quality faced is direct corroboration of this team scoring less
    # than the raw averages alone suggest.
    if opp_gp is not None and opp_gp >= 0.2:
        score += 1

    if team_xgot_for is not None and team_g >= team_xgot_for + 0.8:
        score -= 1.5

    return score


def evaluate_team_under_1_5_signal(home, away, home_data, away_data, m_url):
    """
    Returns a Telegram-ready message if HOME's own total goals, AWAY's
    own total goals, or both, clear the bar for "under 1.5" — or None
    if neither does.
    """
    home = _escape_markdown(home)
    away = _escape_markdown(away)

    hs = home_data["stats"]
    as_ = away_data["stats"]

    if (
        hs.get("matches", 0) < MIN_SAMPLE_MATCHES
        or as_.get("matches", 0) < MIN_SAMPLE_MATCHES
    ):
        return None

    h_g = hs.get("avg_goals", 0)
    a_g = as_.get("avg_goals", 0)

    h_gc = hs.get("avg_gc", 0)
    a_gc = as_.get("avg_gc", 0)

    h_xg = hs.get("avg_xg")
    a_xg = as_.get("avg_xg")

    h_xga = hs.get("avg_xga")
    a_xga = as_.get("avg_xga")

    h_corners_for = hs.get("avg_corners_for")
    h_corners_against = hs.get("avg_corners_against")
    a_corners_for = as_.get("avg_corners_for")
    a_corners_against = as_.get("avg_corners_against")

    h_bc_for = hs.get("avg_big_chances_for")
    h_bc_against = hs.get("avg_big_chances_against")
    a_bc_for = as_.get("avg_big_chances_for")
    a_bc_against = as_.get("avg_big_chances_against")

    h_cards = hs.get("avg_yellow_cards")
    a_cards = as_.get("avg_yellow_cards")
    h_fouls = hs.get("avg_fouls")
    a_fouls = as_.get("avg_fouls")

    h_gp = hs.get("avg_goals_prevented")
    a_gp = as_.get("avg_goals_prevented")

    h_shots_for = hs.get("avg_shots_for")
    h_shots_against = hs.get("avg_shots_against")
    a_shots_for = as_.get("avg_shots_for")
    a_shots_against = as_.get("avg_shots_against")

    h_sot_for = hs.get("avg_sot_for")
    h_sot_against = hs.get("avg_sot_against")
    a_sot_for = as_.get("avg_sot_for")
    a_sot_against = as_.get("avg_sot_against")

    h_xgot_for = hs.get("avg_xgot_for")
    a_xgot_for = as_.get("avg_xgot_for")

    h_poss = hs.get("avg_possession")
    a_poss = as_.get("avg_possession")

    use_xg = (
        h_xg is not None and a_xg is not None
        and h_xga is not None and a_xga is not None
    )

    if use_xg:
        expected_home_goals = (h_xg + a_xga) / 2
        expected_away_goals = (a_xg + h_xga) / 2
        u15_buffer = U15_XG_BUFFER
        basis = "xG-based"
    else:
        expected_home_goals = (h_g + a_gc) / 2
        expected_away_goals = (a_g + h_gc) / 2
        u15_buffer = U15_GD_BUFFER
        basis = "goals-based, no xG data"

    u15_target = 1.5 - u15_buffer

    home_score = _under_goals_score(
        h_sot_for, a_sot_against,
        h_bc_for, a_bc_against,
        h_xgot_for, h_g,
        a_gp,
    )
    away_score = _under_goals_score(
        a_sot_for, h_sot_against,
        a_bc_for, h_bc_against,
        a_xgot_for, a_g,
        h_gp,
    )

    home_qualifies = (
        expected_home_goals <= u15_target
        and home_score >= U15_SCORE_THRESHOLD
    )
    away_qualifies = (
        expected_away_goals <= u15_target
        and away_score >= U15_SCORE_THRESHOLD
    )

    if not home_qualifies and not away_qualifies:
        return None

    risks = []
    qualifying_lines = []

    if home_qualifies:
        qualifying_lines.append(
            f"• {home} under 2.5 (verified to a stricter under-1.5 "
            f"threshold, {basis}) — expected ~{expected_home_goals:.2f}, "
            f"corroboration {home_score:.1f}"
        )
        if h_xg is not None and h_g >= h_xg + 0.8:
            risks.append(
                f"{home} has been scoring above its xG recently — a "
                f"breakout high-scoring game is possible"
            )

    if away_qualifies:
        qualifying_lines.append(
            f"• {away} under 2.5 (verified to a stricter under-1.5 "
            f"threshold, {basis}) — expected ~{expected_away_goals:.2f}, "
            f"corroboration {away_score:.1f}"
        )
        if a_xg is not None and a_g >= a_xg + 0.8:
            risks.append(
                f"{away} has been scoring above its xG recently — a "
                f"breakout high-scoring game is possible"
            )

    def fmt(v):
        return "N/A" if v is None else str(v)

    lines = [
        f"⚽ *{home} vs {away}*",
        "",
        "🥅 *Prediction: Team Total Goals Under 2.5*",
        "(the actual bet — analysis is verified to a stricter Under "
        "1.5 bar first, for extra safety margin)",
    ]
    lines.extend(qualifying_lines)
    lines.append("")
    lines.append("📊 *Stats*")
    lines.append(
        f"{home}   G {h_g} | GA {h_gc} | xG {fmt(h_xg)} | xGA {fmt(h_xga)}"
    )
    lines.append(
        f"{away}   G {a_g} | GA {a_gc} | xG {fmt(a_xg)} | xGA {fmt(a_xga)}"
    )
    lines.append(f"Possession {fmt(h_poss)}% vs {fmt(a_poss)}%")
    lines.append(
        f"Shots {fmt(h_shots_for)}/{fmt(h_shots_against)} vs "
        f"{fmt(a_shots_for)}/{fmt(a_shots_against)} | "
        f"SoT {fmt(h_sot_for)}/{fmt(h_sot_against)} vs "
        f"{fmt(a_sot_for)}/{fmt(a_sot_against)}"
    )
    lines.append(
        f"Corners {fmt(h_corners_for)}/{fmt(h_corners_against)} vs "
        f"{fmt(a_corners_for)}/{fmt(a_corners_against)} | "
        f"BigCh {fmt(h_bc_for)}/{fmt(h_bc_against)} vs "
        f"{fmt(a_bc_for)}/{fmt(a_bc_against)}"
    )
    lines.append(
        f"Cards {fmt(h_cards)} vs {fmt(a_cards)} | "
        f"Fouls {fmt(h_fouls)} vs {fmt(a_fouls)} | "
        f"GP {fmt(h_gp)} vs {fmt(a_gp)}"
    )
    lines.append("")

    if risks:
        lines.append(f"⚠️ *Risk factors ({len(risks)})*")
        lines.extend(f"• {r}" for r in risks)
        lines.append("")

    lines.append(f"🔗 {m_url}")

    return "\n".join(lines)


# -------------------------------------------------
# MATCH TOTAL GOALS UNDER 1.5 PREDICTION
# -------------------------------------------------
# Fourth, independent prediction: the classic combined match total
# (home + away goals together) under 1.5 — distinct from
# evaluate_team_under_1_5_signal, which checks each side's *own*
# total separately. Reuses _under_goals_score from that signal for
# each side and sums them, rather than duplicating the corroboration
# logic — the combined case is just "both sides' under-1.5 case,
# applied together".

MATCH_U15_XG_BUFFER = 1.0   # xG-based: need combined expected <= 1.5 - 1.0 = 0.5
MATCH_U15_GD_BUFFER = 1.3   # goals-only fallback: <= 1.5 - 1.3 = 0.2
# Summed across both sides' _under_goals_score (max ~4 each, so ~8
# combined) — kept at the same ~50% proportional bar as the per-team
# signal's threshold.
MATCH_U15_SCORE_THRESHOLD = 4.0


def evaluate_match_under_1_5_signal(home, away, home_data, away_data, m_url):
    """
    Returns a Telegram-ready message if the combined match total
    (home + away goals) clears the bar for "under 1.5", or None.
    """
    home = _escape_markdown(home)
    away = _escape_markdown(away)

    hs = home_data["stats"]
    as_ = away_data["stats"]

    if (
        hs.get("matches", 0) < MIN_SAMPLE_MATCHES
        or as_.get("matches", 0) < MIN_SAMPLE_MATCHES
    ):
        return None

    h_g = hs.get("avg_goals", 0)
    a_g = as_.get("avg_goals", 0)

    h_gc = hs.get("avg_gc", 0)
    a_gc = as_.get("avg_gc", 0)

    h_xg = hs.get("avg_xg")
    a_xg = as_.get("avg_xg")

    h_xga = hs.get("avg_xga")
    a_xga = as_.get("avg_xga")

    h_corners_for = hs.get("avg_corners_for")
    h_corners_against = hs.get("avg_corners_against")
    a_corners_for = as_.get("avg_corners_for")
    a_corners_against = as_.get("avg_corners_against")

    h_bc_for = hs.get("avg_big_chances_for")
    h_bc_against = hs.get("avg_big_chances_against")
    a_bc_for = as_.get("avg_big_chances_for")
    a_bc_against = as_.get("avg_big_chances_against")

    h_cards = hs.get("avg_yellow_cards")
    a_cards = as_.get("avg_yellow_cards")
    h_fouls = hs.get("avg_fouls")
    a_fouls = as_.get("avg_fouls")

    h_gp = hs.get("avg_goals_prevented")
    a_gp = as_.get("avg_goals_prevented")

    h_shots_for = hs.get("avg_shots_for")
    h_shots_against = hs.get("avg_shots_against")
    a_shots_for = as_.get("avg_shots_for")
    a_shots_against = as_.get("avg_shots_against")

    h_sot_for = hs.get("avg_sot_for")
    h_sot_against = hs.get("avg_sot_against")
    a_sot_for = as_.get("avg_sot_for")
    a_sot_against = as_.get("avg_sot_against")

    h_xgot_for = hs.get("avg_xgot_for")
    a_xgot_for = as_.get("avg_xgot_for")

    h_poss = hs.get("avg_possession")
    a_poss = as_.get("avg_possession")

    use_xg = (
        h_xg is not None and a_xg is not None
        and h_xga is not None and a_xga is not None
    )

    if use_xg:
        expected_home_goals = (h_xg + a_xga) / 2
        expected_away_goals = (a_xg + h_xga) / 2
        u15_buffer = MATCH_U15_XG_BUFFER
        basis = "xG-based"
    else:
        expected_home_goals = (h_g + a_gc) / 2
        expected_away_goals = (a_g + h_gc) / 2
        u15_buffer = MATCH_U15_GD_BUFFER
        basis = "goals-based, no xG data"

    combined_expected_goals = expected_home_goals + expected_away_goals
    u15_target = 1.5 - u15_buffer

    if combined_expected_goals > u15_target:
        return None

    combined_score = (
        _under_goals_score(
            h_sot_for, a_sot_against,
            h_bc_for, a_bc_against,
            h_xgot_for, h_g,
            a_gp,
        )
        + _under_goals_score(
            a_sot_for, h_sot_against,
            a_bc_for, h_bc_against,
            a_xgot_for, a_g,
            h_gp,
        )
    )

    if combined_score < MATCH_U15_SCORE_THRESHOLD:
        return None

    risks = []

    if h_xg is not None and h_g >= h_xg + 0.8:
        risks.append(
            f"{home} has been scoring above its xG recently — a "
            f"breakout high-scoring game is possible"
        )

    if a_xg is not None and a_g >= a_xg + 0.8:
        risks.append(
            f"{away} has been scoring above its xG recently — a "
            f"breakout high-scoring game is possible"
        )

    def fmt(v):
        return "N/A" if v is None else str(v)

    lines = [
        f"⚽ *{home} vs {away}*",
        "",
        f"🥅 *Prediction: Match Total Goals Under 2.5* ({basis})",
        "(the actual bet — analysis is verified to a stricter Under "
        "1.5 bar first, for extra safety margin)",
        f"Expected combined ~{combined_expected_goals:.2f} "
        f"(home ~{expected_home_goals:.2f}, away ~{expected_away_goals:.2f}) "
        f"| corroboration score {combined_score:.1f}",
        "",
        "📊 *Stats*",
        f"{home}   G {h_g} | GA {h_gc} | xG {fmt(h_xg)} | xGA {fmt(h_xga)}",
        f"{away}   G {a_g} | GA {a_gc} | xG {fmt(a_xg)} | xGA {fmt(a_xga)}",
        f"Possession {fmt(h_poss)}% vs {fmt(a_poss)}%",
        f"Shots {fmt(h_shots_for)}/{fmt(h_shots_against)} vs "
        f"{fmt(a_shots_for)}/{fmt(a_shots_against)} | "
        f"SoT {fmt(h_sot_for)}/{fmt(h_sot_against)} vs "
        f"{fmt(a_sot_for)}/{fmt(a_sot_against)}",
        f"Corners {fmt(h_corners_for)}/{fmt(h_corners_against)} vs "
        f"{fmt(a_corners_for)}/{fmt(a_corners_against)} | "
        f"BigCh {fmt(h_bc_for)}/{fmt(h_bc_against)} vs "
        f"{fmt(a_bc_for)}/{fmt(a_bc_against)}",
        f"Cards {fmt(h_cards)} vs {fmt(a_cards)} | "
        f"Fouls {fmt(h_fouls)} vs {fmt(a_fouls)} | "
        f"GP {fmt(h_gp)} vs {fmt(a_gp)}",
        "",
    ]

    if risks:
        lines.append(f"⚠️ *Risk factors ({len(risks)})*")
        lines.extend(f"• {r}" for r in risks)
        lines.append("")

    lines.append(f"🔗 {m_url}")

    return "\n".join(lines)


# ---------------- ALERT SCRIPT ----------------

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--start",
        type=int,
        default=0
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=100
    )

    # ---- BACKTEST MODE ----
    # Temporary, for validating predictions against a date that's
    # already been played (so real scorelines exist to compare
    # against) — see this module's TUNABLES comment history for
    # context; not meant to stay wired into the live scheduled run.
    parser.add_argument(
        "--backtest",
        action="store_true",
        help=(
            "Backtest mode: analyze an already-finished date's "
            "fixtures instead of today's upcoming ones, using each "
            "team's 6 finished matches BEFORE that fixture (its own "
            "now-finished result is excluded from its own 'recent "
            "form' sample, since including it would leak the outcome "
            "being predicted into the prediction)."
        ),
    )

    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help=(
            "DD/MM/YYYY date to analyze. Only meaningful with "
            "--backtest; defaults to yesterday (server-local) if "
            "--backtest is set and this is omitted."
        ),
    )

    args = parser.parse_args()

    START = max(0, args.start)
    LIMIT = max(1, args.limit)

    TARGET_COUNT = START + LIMIT

    BACKTEST = args.backtest

    if BACKTEST:
        DATE_STR = args.date or time.strftime(
            "%d/%m/%Y", time.localtime(time.time() - 86400)
        )
    else:
        DATE_STR = args.date  # None -> discover_matches defaults to today

    BOT_TOKEN = os.getenv(
        "BOT_TOKEN",
        ""
    ).strip()

    CHAT_ID = os.getenv(
        "CHAT_ID",
        ""
    ).strip()

    if not BOT_TOKEN or not CHAT_ID:
        # NOTE: cron/systemd/most schedulers do NOT source your shell
        # profile (.bashrc/.profile/.env), so env vars that are visible
        # in an interactive shell can be empty here even though "they're
        # set". Make sure BOT_TOKEN/CHAT_ID are exported explicitly in
        # whatever mechanism launches this script (crontab line,
        # systemd unit's Environment=, scheduler's env config, etc.).
        log.error(
            "BOT_TOKEN or CHAT_ID is missing from environment variables."
        )
        return

    backtest_tag = f"\n🔬 BACKTEST date={DATE_STR}" if BACKTEST else ""

    send_job_status(
        f"🚀 Job STARTED\n"
        f"Batch START={START} LIMIT={LIMIT}{backtest_tag}",
        BOT_TOKEN,
        CHAT_ID
    )

    log.info("Starting 365scores alert script...")
    log.info(f"Batch start={START}, limit={LIMIT}")
    if BACKTEST:
        log.info(
            f"BACKTEST MODE: analyzing already-finished date "
            f"{DATE_STR} — each team's own fixture is excluded from "
            f"its own recent-form sample."
        )

    # Declared before the try block so `finally` can safely check it even
    # if construction itself fails (see below).
    #
    # No browser, no subprocess watchdog, no worker process to spawn or
    # recycle — see this module's TUNABLES comment for why. `scraper`
    # is just a plain requests.Session() wrapper; every call is a
    # direct HTTP GET with its own retry/timeout handling built in
    # (see SixtyFiveScoresScraper._api_get).
    scraper = None

    try:
        scraper = SixtyFiveScoresScraper()

        matches = scraper.discover_matches(
            TARGET_COUNT,
            date_str=DATE_STR,
            # Only fixtures that haven't kicked off yet — no point
            # spending a full analysis on a match that's already live
            # or finished. Left False (default) everywhere else
            # discover_matches is called — e.g. inside analyze_team,
            # pulling a team's past 6 results, which are *supposed* to
            # already be finished. Backtest mode flips this around:
            # DATE_STR is a past date, so every fixture on it should
            # already be finished (only_finished=True) rather than
            # upcoming.
            only_upcoming=(not BACKTEST),
            only_finished=BACKTEST,
        )

        log.info(
            f"Found {len(matches)} "
            f"{'finished' if BACKTEST else 'upcoming'} matches total"
        )

        batch_matches = matches[
            START:START + LIMIT
        ]

        log.info(
            f"This job will process {len(batch_matches)} matches "
            f"from {START} to {START + LIMIT - 1}"
        )

        if not batch_matches:

            log.info("No matches in this batch. Exiting.")

            send_job_status(
                f"⚠️ Job FINISHED (No matches)\n"
                f"Batch START={START} LIMIT={LIMIT}{backtest_tag}",
                BOT_TOKEN,
                CHAT_ID
            )

            return

        for idx, match in enumerate(
            batch_matches,
            start=START + 1
        ):

            # discover_matches already returns rich dicts (home/away
            # names AND exact IDs, tournament) straight from the API —
            # no separate "visit the match page to read team
            # names/links" step needed.
            m_url = f"https://www.365scores.com/en-uk/football/game/{match['id']}"
            home = match["home_name"]
            away = match["away_name"]

            # Only populated in backtest mode (discover_matches only
            # captures these from a finished game) — lets the log and
            # any fired alert show the real result right next to the
            # prediction, no manual cross-referencing needed.
            actual_result = None
            if (
                BACKTEST
                and match.get("home_goals") is not None
                and match.get("away_goals") is not None
            ):
                actual_result = (
                    f"{home} {int(match['home_goals'])}-"
                    f"{int(match['away_goals'])} {away}"
                )

            log.info(
                f"Processing match {idx}: {home} vs {away} "
                f"({match.get('tournament', '')}) {m_url}"
                + (f" | ACTUAL: {actual_result}" if actual_result else "")
            )

            try:
                if not home or not away:
                    log.warning(
                        "Could not extract teams, skipping match"
                    )
                    continue

                home_error = None
                away_error = None

                try:
                    home_data = scraper.analyze_team(
                        match["home_id"],
                        match["home_name"],
                        # No-op in live mode: an upcoming fixture can't
                        # already be in games/results/. In backtest
                        # mode this keeps the fixture's own now-known
                        # result out of the "recent form" it's judged
                        # against.
                        exclude_match_id=match["id"],
                    )
                except Exception as e:
                    home_data = None
                    home_error = e

                try:
                    away_data = scraper.analyze_team(
                        match["away_id"],
                        match["away_name"],
                        exclude_match_id=match["id"],
                    )
                except Exception as e:
                    away_data = None
                    away_error = e

                if home_error:
                    log.error(f"Home team analysis failed: {home_error}")

                if away_error:
                    log.error(f"Away team analysis failed: {away_error}")

                if not home_data or not away_data:

                    log.warning(
                        "Could not analyze one or both teams, "
                        "skipping match"
                    )

                    continue

                # Independent predictions per match — each can fire or
                # not fire on its own (e.g. a predicted 1-0 fires the
                # home margin signal, the clean sheet signal, AND the
                # away-under-1.5 signal all at once; a predicted 0-2
                # fires the away margin signal and the home-under-1.5
                # signal but not the home clean sheet or margin ones).
                margin_msg = evaluate_home_margin_signal(
                    home,
                    away,
                    home_data,
                    away_data,
                    m_url
                )

                away_margin_msg = evaluate_away_margin_signal(
                    home,
                    away,
                    home_data,
                    away_data,
                    m_url
                )

                clean_sheet_msg = evaluate_home_clean_sheet_signal(
                    home,
                    away,
                    home_data,
                    away_data,
                    m_url
                )

                under_15_msg = evaluate_team_under_1_5_signal(
                    home,
                    away,
                    home_data,
                    away_data,
                    m_url
                )

                match_under_15_msg = evaluate_match_under_1_5_signal(
                    home,
                    away,
                    home_data,
                    away_data,
                    m_url
                )

                # Send whichever of the above fired, each as its own
                # alert. A loop instead of a repeated if/send block per
                # predictor, since that was already getting unwieldy at
                # four and will only grow.
                fired_signals = [
                    ("home margin", margin_msg),
                    ("away margin", away_margin_msg),
                    ("clean sheet", clean_sheet_msg),
                    # Bet framed as Under 2.5 — the underlying check is
                    # still verified to a stricter Under 1.5 bar first
                    # (see evaluate_team_under_1_5_signal), these labels
                    # just match what the alert message itself says.
                    ("team under 2.5", under_15_msg),
                    ("match under 2.5", match_under_15_msg),
                ]

                any_fired = False

                for label, sig_msg in fired_signals:
                    if sig_msg:
                        any_fired = True
                        if actual_result:
                            sig_msg = (
                                f"🔬 *BACKTEST* — Actual result: "
                                f"{_escape_markdown(actual_result)}\n\n"
                                + sig_msg
                            )
                        log.info(f"ALERT ({label}):\n" + sig_msg)
                        scraper.send_telegram_message(
                            sig_msg,
                            BOT_TOKEN,
                            CHAT_ID
                        )

                if not any_fired:
                    log.info(
                        "No signals found."
                        + (f" ACTUAL: {actual_result}" if actual_result else "")
                    )

            except Exception as match_err:
                # A single bad match (missing data, timeout, etc.)
                # should not take down the whole batch — log it and
                # move on to the next match instead.
                log.error(
                    f"Error processing match {m_url}: {match_err}"
                )
                log.debug(traceback.format_exc())
                continue

        send_job_status(
            f"✅ Job FINISHED\n"
            f"Batch START={START} LIMIT={LIMIT}{backtest_tag}",
            BOT_TOKEN,
            CHAT_ID
        )

    except Exception as e:

        log.error(f"Job failed: {e}")
        log.error(traceback.format_exc())

        send_job_status(
            f"❌ Job FAILED\n"
            f"Batch START={START} LIMIT={LIMIT}{backtest_tag}\n"
            f"Error: {str(e)}",
            BOT_TOKEN,
            CHAT_ID
        )

    finally:

        log.info("Closing scraper session...")

        if scraper is not None:
            scraper.close()

        log.info("Script finished.")


if __name__ == "__main__":
    main()
