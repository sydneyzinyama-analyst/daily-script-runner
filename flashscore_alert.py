import os
import sys
import argparse
import json
import logging
import multiprocessing
import queue
import signal
import traceback
from playwright.sync_api import sync_playwright
from urllib.parse import urlparse
from difflib import SequenceMatcher
import time
import re
import unicodedata
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


# ---------------- DIAGNOSTIC / FAIL-FAST TUNABLES ----------------
# This scraper originally targeted Flashscore's DOM and chased a "this
# got much slower all of a sudden" regression there for most of a day:
# navigations pinned at a flat 90s timeout with zero timing/visibility,
# a stats "tab" click that sometimes silently hung for 480-1231+
# seconds with no error (reproduced across three Flashscore domains,
# with and without concurrent browsers — never resolved). Migrated to
# Sofascore's internal JSON API instead (see SofascoreGoalsScraper's
# docstring), which sidesteps that whole class of problem — no DOM
# clicks, no tab to hang on. These tunables are what's left of that
# investigation's fail-fast infrastructure, still useful for the one
# real navigation (establishing a session) and the API-call timeouts.
#
# Lower this to fail faster once you've confirmed something is
# genuinely hanging rather than just slow; raise it back if you start
# seeing false-negative timeouts on a healthy-but-slow connection.
NAV_TIMEOUT_MS = int(os.getenv("SCRAPER_NAV_TIMEOUT_MS", "45000"))

# Hard ceiling on how long discover_matches / get_team_recent_matches
# are allowed to spend paginating through Sofascore's API for one
# team/date, on top of their own page-count caps. Without this, an
# API response shape change could spin through many pages indefinitely.
DISCOVER_TIME_BUDGET_SEC = int(os.getenv("SCRAPER_DISCOVER_BUDGET_SEC", "60"))

# How many analyze_team() calls a single browser process handles
# before SofascoreGoalsScraper.maybe_recycle_browser() closes and
# relaunches it. See maybe_recycle_browser's docstring for the
# investigation that motivated this — was previously implicitly "1"
# (a fresh browser process per team, per match), which is what led
# to the resource exhaustion this whole mechanism replaces.
BROWSER_RECYCLE_EVERY = int(os.getenv("SCRAPER_BROWSER_RECYCLE_EVERY", "40"))

# Title substrings seen on common bot-mitigation interstitials
# (Cloudflare, DataDome, generic "checking your browser" pages). Only
# relevant to the one real page load per session (establishing
# cookies/fingerprint before any API fetch) — if that page shows one
# of these instead of the real Sofascore homepage, that's a strong
# signal we're being challenged rather than just experiencing normal
# latency.
BOT_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "verify you are human",
    "access denied",
    "are you a robot",
)


# ---------------- RESOURCE DIAGNOSTICS ----------------
def _log_resource_usage(label):
    # Diagnostic-only, deliberately dependency-free: reads straight
    # from /proc rather than pulling in psutil (not in requirements.txt,
    # and it turned out to be broken in this environment anyway) —
    # /proc is standard on every Linux runner this script actually runs
    # on. Added to confirm/rule out a suspected leak: each match spins
    # up two brand-new Chromium browser processes (home/away threads)
    # and closes them after, and a run that starts fast (~1min/match)
    # then falls off a cliff into multi-minute stalls partway through
    # looks a lot like those processes/memory not being fully reclaimed
    # and the host progressively starving under the accumulated load.
    # Fails soft — never let a diagnostic break the actual scrape.
    try:
        chrome_procs = 0
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/comm") as f:
                    comm = f.read().strip()
                if "chrom" in comm.lower():
                    chrome_procs += 1
            except Exception:
                continue

        mem_available_kb = None
        mem_total_kb = None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    mem_available_kb = int(line.split()[1])
                elif line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split()[1])

        if mem_available_kb is not None and mem_total_kb is not None:
            mem_str = (
                f"{mem_available_kb / 1024:.0f}MB free / "
                f"{mem_total_kb / 1024:.0f}MB total"
            )
        else:
            mem_str = "unknown"

        log.info(
            f"[resources @ {label}] chrome-family processes="
            f"{chrome_procs}, memory={mem_str}"
        )
    except Exception as e:
        log.debug(f"resource logging failed: {e}")


# ---------------- REQUEST BLOCKING ----------------
# Resource types this scraper never needs — it only ever reads text/DOM
# state (team names, scores, stat table values), never anything visual.
# Blocking these cuts page weight and JS/rendering cost substantially,
# which matters a lot with 3 browser instances (fixtures walker + home
# worker + away worker) potentially rendering concurrently — a run
# showed individual 8s-budgeted operations occasionally taking 150-1200+
# seconds, well past what a slow-but-working page load explains, more
# consistent with host CPU contention from heavy pages (ads, trackers,
# video widgets) than with anything else.
#
# Deliberately NOT blocking stylesheets or scripts: _wait_ready's
# state="visible" checks depend on CSS layout, and Flashscore's own
# content (team names, live stats) is client-side rendered via JS, so
# either would break the actual scraping, not just slim it down.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}


def _block_heavy_resources(route):
    try:
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()
    except Exception:
        # A route that's already been handled/the page navigated away
        # mid-request raises here — never let that break the load.
        pass


# ---------------- JOB STATUS TELEGRAM ----------------
def send_job_status(message, bot_token, chat_id):
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        requests.post(url, data=payload, timeout=20)
    except Exception as e:
        log.warning(f"Failed to send job status to Telegram: {e}")


# ---------------- SCRAPER CLASS ----------------
class SofascoreGoalsScraper:
    """
    Talks to Sofascore's own internal JSON API (the same one their React
    app calls) rather than DOM-scraping. Playwright is only used to
    establish one legitimate browser session (a real page load gives us
    real cookies/fingerprint) — every actual data fetch after that goes
    through page.evaluate()-executed fetch() calls against
    www.sofascore.com/api/v1/..., which returns clean structured JSON.

    This matters because a bare `requests.get()` to these same endpoints
    is blocked outright (403), even with a spoofed browser User-Agent —
    confirmed by direct testing — but the identical request made from
    inside an actual loaded page succeeds cleanly. Real bot protection
    exists at the API layer, keyed to session/fingerprint, not headers
    alone.

    This also sidesteps the entire class of problem a prior Flashscore-
    based version of this scraper fought for most of a day: DOM click
    races, a stats "tab" that sometimes silently hung for 480-1231+
    seconds with no error (reproduced across three Flashscore domains,
    with and without concurrent browsers — never resolved), and
    fragile CSS selectors. There's no tab to click here — every value
    is a plain HTTP GET with a real AbortController timeout, so a stuck
    request fails cleanly and fast instead of hanging indefinitely.
    """

    def __init__(self, headless=True):
        self.headless = headless
        self.playwright = sync_playwright().start()
        self.browser = None
        self.context = None
        self.page = None
        self._session_count = 0
        self._launch_browser()
        self.team_id = None
        self.team_name = None

    def _new_context(self):
        # Factored out so both _launch_browser and new_session (below)
        # create contexts with identical settings, and both establish a
        # fresh legitimate session via one real page load before any
        # API fetch is attempted through it.
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
            ),
        )
        self.context.route("**/*", _block_heavy_resources)
        self.page = self.context.new_page()
        self._establish_session()

    def _establish_session(self):
        try:
            self.page.goto(
                "https://www.sofascore.com/",
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT_MS,
            )
            self._check_bot_challenge("session establishment")
        except Exception as e:
            log.warning(f"Failed to establish Sofascore session: {e}")

    def _launch_browser(self):
        # --no-sandbox / --disable-dev-shm-usage are required in most
        # scheduler contexts: cron/systemd jobs often run as root (where
        # Chromium's sandbox refuses to start without --no-sandbox) or
        # in containers with a tiny /dev/shm.
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        self._new_context()
        self._session_count = 0

    def new_session(self):
        """
        Closes the current context and opens a fresh one (new cookies,
        new session) on the SAME browser process. Call this between
        logical units of work (e.g. once per team, before analyze_team)
        that need isolation from each other.
        """
        try:
            if self.context is not None:
                self.context.close()
        except Exception as e:
            log.warning(f"Error closing context for new session: {e}")

        self._new_context()

    def maybe_recycle_browser(self):
        # Periodic full relaunch as a safety net against any slow
        # leak inside a single very-long-lived Chromium process — see
        # BROWSER_RECYCLE_EVERY's comment.
        self._session_count += 1

        if self._session_count < BROWSER_RECYCLE_EVERY:
            return

        log.info(
            f"Recycling browser after {self._session_count} team "
            f"analyses (SCRAPER_BROWSER_RECYCLE_EVERY="
            f"{BROWSER_RECYCLE_EVERY})"
        )

        try:
            self.context.close()
        except Exception as e:
            log.warning(f"Error closing context during recycle: {e}")

        try:
            self.browser.close()
        except Exception as e:
            log.warning(f"Error closing browser during recycle: {e}")

        self._launch_browser()

    def _check_bot_challenge(self, context_label=""):
        try:
            title = (self.page.title() or "").strip()
        except Exception:
            return False

        if any(marker in title.lower() for marker in BOT_CHALLENGE_MARKERS):
            log.warning(
                f"POSSIBLE BOT CHALLENGE"
                f"{' (' + context_label + ')' if context_label else ''}: "
                f"page title={title!r} url={self.page.url!r}"
            )
            return True

        return False

    def _api_get(self, path, timeout_ms=None):
        """
        Fetches a Sofascore API path via the browser's own fetch(),
        using its already-established session/cookies — see the class
        docstring for why a bare requests.get() to these same paths is
        blocked (403) while this isn't. Uses a real AbortController on
        the JS side so a stuck request is genuinely cancelled at the
        network layer after timeout_ms, not just abandoned by our own
        code while the underlying fetch keeps running.

        Returns the parsed JSON body, or None (logged) on any failure:
        network error, timeout, non-2xx status, or invalid JSON.
        """
        if timeout_ms is None:
            timeout_ms = NAV_TIMEOUT_MS

        t0 = time.time()
        try:
            result = self.page.evaluate(
                """async ({ path, timeoutMs }) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), timeoutMs);
                    try {
                        const r = await fetch(path, {
                            headers: { 'Accept': 'application/json' },
                            signal: controller.signal,
                        });
                        const text = await r.text();
                        return { ok: r.ok, status: r.status, text: text };
                    } catch (e) {
                        return { ok: false, status: 0, text: String(e) };
                    } finally {
                        clearTimeout(timer);
                    }
                }""",
                {"path": path, "timeoutMs": timeout_ms},
            )
        except Exception as e:
            log.warning(
                f"API GET {path} evaluate() failed after "
                f"{time.time()-t0:.1f}s: {e}"
            )
            return None

        elapsed = time.time() - t0

        if not result or not result.get("ok"):
            log.warning(
                f"API GET {path} failed after {elapsed:.1f}s: "
                f"status={result.get('status') if result else '?'} "
                f"body={str(result.get('text') if result else '')[:200]!r}"
            )
            return None

        try:
            return json.loads(result["text"])
        except Exception as e:
            log.warning(
                f"API GET {path} returned invalid JSON after "
                f"{elapsed:.1f}s: {e}"
            )
            return None

    # ---------------- DISCOVERY ----------------
    def discover_matches(
        self, target_count, date_str=None, only_upcoming=False,
        max_tournaments=300
    ):
        """
        Returns up to `target_count` match dicts for `date_str`
        (today, server-local, if not given): each
        {id, home_id, home_name, away_id, away_name, tournament}.

        Scans every tournament playing that date (paginating
        scheduled-tournaments -> scheduled-events per tournament) until
        target_count is reached or tournaments run out. When
        only_upcoming is True, skips anything whose status isn't
        "notstarted" — no point predicting on a match that's already
        live or finished.
        """
        if date_str is None:
            date_str = time.strftime("%Y-%m-%d")

        matches = []
        seen_ids = set()
        skipped_not_upcoming = 0
        tournament_page = 1
        tournaments_scanned = 0
        t0 = time.time()

        while len(matches) < target_count and tournaments_scanned < max_tournaments:
            if time.time() - t0 > DISCOVER_TIME_BUDGET_SEC:
                log.warning(
                    f"discover_matches hit its {DISCOVER_TIME_BUDGET_SEC}s "
                    f"time budget after scanning {tournaments_scanned} "
                    f"tournaments, {len(matches)}/{target_count} found "
                    f"— stopping early"
                )
                break

            page_data = self._api_get(
                f"/api/v1/sport/football/scheduled-tournaments/"
                f"{date_str}/page/{tournament_page}"
            )
            if not page_data or not page_data.get("scheduled"):
                break

            tournament_ids = []
            for entry in page_data["scheduled"]:
                # uniqueTournament is nested inside tournament here, not
                # a sibling of it — confirmed by direct inspection, not
                # assumption (an earlier raw single-line JSON slice was
                # misleading about the actual nesting depth).
                tournament = entry.get("tournament") or {}
                ut = tournament.get("uniqueTournament") or {}
                ut_id = ut.get("id")
                if ut_id is not None:
                    tournament_ids.append(ut_id)

            for ut_id in tournament_ids:
                if len(matches) >= target_count:
                    break
                if time.time() - t0 > DISCOVER_TIME_BUDGET_SEC:
                    break

                tournaments_scanned += 1
                events_data = self._api_get(
                    f"/api/v1/unique-tournament/{ut_id}/scheduled-events/{date_str}"
                )
                if not events_data or not events_data.get("events"):
                    continue

                for e in events_data["events"]:
                    eid = e.get("id")
                    if eid is None or eid in seen_ids:
                        continue

                    status_type = (e.get("status") or {}).get("type")
                    if only_upcoming and status_type != "notstarted":
                        seen_ids.add(eid)
                        skipped_not_upcoming += 1
                        continue

                    home = e.get("homeTeam") or {}
                    away = e.get("awayTeam") or {}
                    if home.get("id") is None or away.get("id") is None:
                        continue

                    matches.append({
                        "id": eid,
                        "home_id": home["id"],
                        "home_name": home.get("name", ""),
                        "away_id": away["id"],
                        "away_name": away.get("name", ""),
                        "tournament": (e.get("tournament") or {}).get("name", ""),
                    })
                    seen_ids.add(eid)

                    if len(matches) >= target_count:
                        break

            if not page_data.get("hasNextPage"):
                break
            tournament_page += 1

        log.info(
            f"discover_matches: found {len(matches)}/{target_count} "
            f"across {tournaments_scanned} tournaments, "
            f"{time.time()-t0:.1f}s"
            + (
                f", skipped {skipped_not_upcoming} already-started/finished"
                if only_upcoming
                else ""
            )
        )
        return matches

    # ---------------- TEAM HISTORY ----------------
    def get_team_recent_matches(self, team_id, count=6, max_pages=6):
        """
        Returns up to `count` of this team's most recent FINISHED
        matches, each a light dict (id, home_id, home_name, away_id,
        away_name, home_goals, away_goals). Paginates
        team/{id}/events/last/{page} since that endpoint can include
        postponed/cancelled/awarded entries mixed in with genuinely
        finished ones — filtered defensively by status.type.
        """
        results = []
        seen_ids = set()
        page = 0
        t0 = time.time()

        while len(results) < count and page < max_pages:
            if time.time() - t0 > DISCOVER_TIME_BUDGET_SEC:
                log.warning(
                    f"get_team_recent_matches for team {team_id} hit its "
                    f"{DISCOVER_TIME_BUDGET_SEC}s time budget with "
                    f"{len(results)}/{count} found — stopping early"
                )
                break

            data = self._api_get(f"/api/v1/team/{team_id}/events/last/{page}")
            if not data or not data.get("events"):
                break

            for e in data["events"]:
                eid = e.get("id")
                if eid is None or eid in seen_ids:
                    continue
                seen_ids.add(eid)

                status_type = (e.get("status") or {}).get("type")
                if status_type != "finished":
                    continue

                home = e.get("homeTeam") or {}
                away = e.get("awayTeam") or {}
                home_goals = (e.get("homeScore") or {}).get("current")
                away_goals = (e.get("awayScore") or {}).get("current")

                if home.get("id") is None or away.get("id") is None:
                    continue
                if home_goals is None or away_goals is None:
                    continue

                results.append({
                    "id": eid,
                    "home_id": home["id"],
                    "home_name": home.get("name", ""),
                    "away_id": away["id"],
                    "away_name": away.get("name", ""),
                    "home_goals": home_goals,
                    "away_goals": away_goals,
                })

                if len(results) >= count:
                    break

            if not data.get("hasNextPage"):
                break
            page += 1

        return results

    # ---------------- MATCH STATISTICS ----------------
    # Maps a Sofascore statistics-item's JSON `key` to the short name we
    # store it under. Sofascore has no direct xGOT ("expected goals on
    # target") or goalkeeper "goals prevented" (an xG-based derived
    # stat) equivalent — goalkeeperSaves is a raw save count, a
    # different thing — so avg_xgot_for/against and
    # avg_goals_prevented stay None for Sofascore-sourced data. Every
    # signal-evaluation function already treats those as optional
    # (None-safe checks throughout), so this doesn't break anything —
    # it just means those specific corroboration bonuses never fire.
    STAT_KEY_MAP = {
        "expectedGoals": "xg",
        "totalShotsOnGoal": "shots",
        "shotsOnGoal": "shots_on_target",
        "cornerKicks": "corners",
        "bigChanceCreated": "big_chances",
        "yellowCards": "yellow_cards",
        "fouls": "fouls",
        "ballPossession": "possession",
    }

    def _empty_stat_result(self):
        result = {}
        for stat_key in set(self.STAT_KEY_MAP.values()) | {"xgot", "goals_prevented"}:
            result[f"home_{stat_key}"] = None
            result[f"away_{stat_key}"] = None
        return result

    def get_match_statistics(self, match_id):
        result = self._empty_stat_result()

        data = self._api_get(f"/api/v1/event/{match_id}/statistics")
        if not data or not data.get("statistics"):
            return result

        try:
            overall = data["statistics"][0]  # period == "ALL"
            for group in overall.get("groups", []):
                for item in group.get("statisticsItems", []):
                    stat_name = self.STAT_KEY_MAP.get(item.get("key"))
                    if not stat_name:
                        continue

                    # Several keys (e.g. totalShotsOnGoal, totalTackle)
                    # appear in more than one group with identical
                    # values — keep the first occurrence only.
                    if result.get(f"home_{stat_name}") is None:
                        result[f"home_{stat_name}"] = item.get("homeValue")
                    if result.get(f"away_{stat_name}") is None:
                        result[f"away_{stat_name}"] = item.get("awayValue")
        except Exception as e:
            log.warning(
                f"Error parsing statistics for match {match_id}: {e}"
            )

        return result

    # ---------------- STAT AVERAGING ----------------
    def _team_stat_avg(self, results, stat_name, team_id, side="for"):
        """
        Generic averager for every per-match stat (goals, xg, corners,
        cards, ...): side="for" -> team_id's own stat_name in each
        match; side="against" -> the opponent's. Matches by exact team
        ID rather than the fuzzy name-aliasing a DOM-scraped version
        would need (team_slug/team_label/normalize_name) — Sofascore's
        IDs are unambiguous, so that whole apparatus is unnecessary
        here.
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
    def analyze_team(self, team_id, team_name=None):
        """
        Fetches this team's last 6 finished matches, each one's
        statistics, and returns the same stats dict shape the
        (unchanged) signal-evaluation functions expect — the only
        thing they care about is the abstract dict shape, not where
        the data came from.
        """
        t0 = time.time()
        self.team_id = team_id
        self.team_name = team_name or str(team_id)

        recent = self.get_team_recent_matches(team_id, count=6)
        results = []
        for m in recent:
            match_stats = self.get_match_statistics(m["id"])
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
            self.browser.close()
            self.playwright.stop()
        except Exception as e:
            log.warning(f"Error while closing browser: {e}")


# ---------------- SUBPROCESS WATCHDOG ----------------
# Real hard-kill capability for the ~1200s stalls Playwright's own
# `timeout=` parameter can't reach. Investigation found several
# operations budgeted at 8s instead running ~1200-1231s, landing on
# nearly the same duration across totally unrelated matches/leagues —
# reproduced even with a single, uncontended browser — which points to
# something beneath the layer Playwright's timeout kwarg controls
# (most likely a TCP connection silently dropped in transit, sitting
# until the OS's own retransmission timeout eventually gives up).
#
# A thread-based hard timeout was tried and reverted: Playwright's sync
# API is thread-affine — calling a page/browser from any thread other
# than the one that created it fails immediately ("Cannot switch to a
# different thread"), regardless of whether anything is actually slow.
# That would break every call, not just the slow ones. A genuinely
# separate OS PROCESS has no such restriction and can be killed
# unconditionally from outside, so the scraper now runs in a
# persistent worker subprocess (see RemoteScraperHandle) instead of
# directly in this process.
#
# Granularity is per RPC call (one whole analyze_team() call, one
# discover_matches() call, ...), not per individual navigation inside
# them. Finer-grained (per-navigation) kill/resume was considered and
# rejected: every signal-evaluation function requires exactly
# MIN_SAMPLE_MATCHES (6) fetched matches before firing anything, so
# losing even one of a team's 6 match-fetches already makes that
# team's data unusable for this fixture — there's no partial credit to
# preserve by being more surgical, so the simpler per-call boundary is
# equally effective.
HARD_TIMEOUT_DEFAULT_SEC = int(os.getenv("SCRAPER_HARD_TIMEOUT_DEFAULT_SEC", "90"))

# analyze_team involves one get_team_recent_matches call (bounded by
# DISCOVER_TIME_BUDGET_SEC) plus up to 6 get_match_statistics calls
# (each an _api_get bounded by NAV_TIMEOUT_MS if its AbortController
# fires cleanly, i.e. nothing is actually hung) — legitimate worst case
# is roughly 60 + 6*45 =~ 330s. Left at the same 480s this was
# originally set to against Flashscore's DOM-hang investigation, which
# still gives real margin above that estimate; can likely come down
# once this has run at scale against the API and shown typical timings.
HARD_TIMEOUT_ANALYZE_TEAM_SEC = int(
    os.getenv("SCRAPER_HARD_TIMEOUT_ANALYZE_TEAM_SEC", "480")
)

HARD_TIMEOUTS_BY_METHOD = {
    "analyze_team": HARD_TIMEOUT_ANALYZE_TEAM_SEC,
}


def _scraper_worker_main(cmd_queue, result_queue, headless):
    """
    Entry point for the persistent scraper worker subprocess (see
    RemoteScraperHandle). Owns the ONE Chromium browser for as long as
    this process is alive. A plain module-level function, not a
    closure or bound method — required so multiprocessing's "spawn"
    start method (used instead of Linux's default "fork" because
    Playwright's asyncio/greenlet internals don't survive a fork
    cleanly) can pickle/import it by reference in the fresh child
    interpreter.

    Runs in its own process group (os.setpgid) so the parent can kill
    this process AND whatever it spawned (Chromium) together with
    os.killpg — SIGKILL-ing just this one PID would leave an orphaned
    Chromium process behind, quietly reintroducing the exact resource
    leak the browser-reuse fix exists to avoid.
    """
    try:
        os.setpgid(0, 0)
    except Exception as e:
        log.warning(
            f"Worker could not start its own process group ({e}) — a "
            f"hard-kill of this worker may leave its browser process "
            f"orphaned instead of also being killed"
        )

    scraper = None
    try:
        scraper = SofascoreGoalsScraper(headless=headless)
        while True:
            item = cmd_queue.get()
            if item is None or item[0] == "__stop__":
                break

            call_id, method_name, args, kwargs = item
            try:
                method = getattr(scraper, method_name)
                value = method(*args, **kwargs)
                result_queue.put((call_id, True, value))
            except Exception as e:
                # Exceptions from Playwright internals often aren't
                # picklable (they can hold connection/transport
                # references) — send the message across as plain text.
                result_queue.put((call_id, False, str(e)))
    finally:
        if scraper is not None:
            try:
                scraper.close()
            except Exception:
                pass


class RemoteScraperHandle:
    """
    Parent-side stand-in for SofascoreGoalsScraper: every method call
    is transparently forwarded (via __getattr__) to a persistent
    worker subprocess that actually owns the browser, with a hard,
    OS-enforced timeout on each call. If the worker doesn't respond in
    time, it — and its whole process group, including its Chromium —
    is unconditionally SIGKILLed and immediately replaced with a fresh
    worker so the batch can continue with the next match. See this
    module's "SUBPROCESS WATCHDOG" comment for why this needs a real
    process rather than a thread, and why per-call (not
    per-navigation) granularity is enough.

    Concurrency stays at exactly one: only one worker is ever alive and
    doing real work at a time. A kill+respawn is a brief transient
    moment, not a second worker running alongside the first.
    """

    def __init__(self, headless):
        self.headless = headless
        self._ctx = multiprocessing.get_context("spawn")
        self._call_counter = 0
        self.process = None
        self.cmd_queue = None
        self.result_queue = None
        self._spawn_worker()

    def _spawn_worker(self):
        self.cmd_queue = self._ctx.Queue()
        self.result_queue = self._ctx.Queue()
        self.process = self._ctx.Process(
            target=_scraper_worker_main,
            args=(self.cmd_queue, self.result_queue, self.headless),
            daemon=True,
        )
        self.process.start()
        log.info(f"Scraper worker started (pid={self.process.pid})")

    def _kill_worker(self, reason):
        pid = self.process.pid
        log.warning(
            f"Killing scraper worker (pid={pid}) and its process "
            f"group — {reason}"
        )
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception as e:
            log.warning(
                f"os.killpg failed ({e}), falling back to killing just "
                f"the worker process itself — its browser may be left "
                f"orphaned"
            )
            try:
                self.process.kill()
            except Exception as e2:
                log.warning(f"Error killing worker process: {e2}")

        self.process.join(timeout=10)

    def call(self, method_name, *args, **kwargs):
        self._call_counter += 1
        call_id = self._call_counter
        timeout_sec = HARD_TIMEOUTS_BY_METHOD.get(
            method_name, HARD_TIMEOUT_DEFAULT_SEC
        )

        self.cmd_queue.put((call_id, method_name, args, kwargs))

        try:
            _, ok, value = self.result_queue.get(timeout=timeout_sec)
        except queue.Empty:
            self._kill_worker(
                f"{method_name}() exceeded its hard {timeout_sec}s ceiling"
            )
            self._spawn_worker()
            raise TimeoutError(
                f"{method_name} hard-timed-out after {timeout_sec}s "
                f"and its worker was killed"
            )

        if not ok:
            raise RuntimeError(f"{method_name} failed in worker: {value}")

        return value

    def __getattr__(self, name):
        # Anything not found as a real attribute on this handle is
        # treated as a proxied call on the worker's
        # SofascoreGoalsScraper — so every existing call site that
        # used to call a SofascoreGoalsScraper method directly
        # (scraper.analyze_team(...), scraper.discover_matches(...),
        # etc.) keeps working completely unchanged.
        def _proxy(*args, **kwargs):
            return self.call(name, *args, **kwargs)
        return _proxy

    def close(self):
        try:
            self.cmd_queue.put(("__stop__",))
            self.process.join(timeout=15)
        except Exception:
            pass

        if self.process.is_alive():
            self._kill_worker("did not exit cleanly on close()")


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

    args = parser.parse_args()

    START = max(0, args.start)
    LIMIT = max(1, args.limit)

    TARGET_COUNT = START + LIMIT

    BOT_TOKEN = os.getenv(
        "BOT_TOKEN",
        ""
    ).strip()

    CHAT_ID = os.getenv(
        "CHAT_ID",
        ""
    ).strip()

    HEADLESS = True

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

    send_job_status(
        f"🚀 Job STARTED\n"
        f"Batch START={START} LIMIT={LIMIT}",
        BOT_TOKEN,
        CHAT_ID
    )

    log.info("Starting Sofascore alert script...")
    log.info(f"Batch start={START}, limit={LIMIT}")

    # Declared before the try block so `finally` can safely check it even
    # if construction itself fails (see below).
    #
    # HISTORY: this used to scrape Flashscore's DOM. That fought a
    # stats "tab" that sometimes silently hung for 480-1231+ seconds
    # with no error — reproduced across three Flashscore domains, and
    # with or without concurrent browsers, ruling out both a resource
    # leak and CPU contention as the cause. Migrated to Sofascore's own
    # internal JSON API instead (see SofascoreGoalsScraper's docstring)
    # — no DOM clicks, no "tab" to hang on, just plain HTTP GETs with a
    # real client-side timeout that actually cancels a stuck request.
    #
    # `scraper` is a RemoteScraperHandle, not a SofascoreGoalsScraper
    # directly — every method call below is transparently forwarded to
    # a persistent worker SUBPROCESS that actually owns the one browser
    # session (see RemoteScraperHandle / _scraper_worker_main).
    # Concurrency is still exactly one: nothing here runs two workers
    # at once. A call that exceeds its hard ceiling gets its worker's
    # entire process group SIGKILLed and replaced with a fresh one — a
    # real OS-level kill a thread-based approach can't provide, since
    # Playwright's sync API can't be called across threads at all (see
    # the SUBPROCESS WATCHDOG comment above RemoteScraperHandle for why
    # that was tried and reverted).
    scraper = None

    try:
        # Spawning the worker (which itself launches Playwright +
        # Chromium and establishes a Sofascore session) is now INSIDE
        # the try block. Previously this happened before the try, so
        # any launch failure (missing browser binaries, missing OS
        # deps, sandbox restrictions when run as root under cron, etc.)
        # crashed the whole process with no "❌ Job FAILED" alert and no
        # cleanup — you'd only ever see the "🚀 Job STARTED" message.
        scraper = RemoteScraperHandle(headless=HEADLESS)

        _log_resource_usage("job start, after worker spawn")

        matches = scraper.discover_matches(
            TARGET_COUNT,
            # Only fixtures that haven't kicked off yet — no point
            # spending a full analysis on a match that's already live
            # or finished. Left False (default) everywhere else
            # discover_matches is called — e.g. inside analyze_team,
            # pulling a team's past 6 results, which are *supposed* to
            # already be finished.
            only_upcoming=True
        )

        log.info(f"Found {len(matches)} upcoming matches total")

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
                f"Batch START={START} LIMIT={LIMIT}",
                BOT_TOKEN,
                CHAT_ID
            )

            return

        for idx, match in enumerate(
            batch_matches,
            start=START + 1
        ):

            # discover_matches already returns rich dicts (home/away
            # names AND exact IDs, tournament) straight from Sofascore's
            # API — unlike the Flashscore version, there's no separate
            # "visit the match page to read team names/links" step
            # needed at all.
            m_url = f"https://www.sofascore.com/event/{match['id']}"
            home = match["home_name"]
            away = match["away_name"]

            log.info(
                f"Processing match {idx}: {home} vs {away} "
                f"({match.get('tournament', '')}) {m_url}"
            )

            try:
                if not home or not away:
                    log.warning(
                        "Could not extract teams, skipping match"
                    )
                    continue

                # Sequential, both through the single `scraper` — see
                # the note above `scraper = RemoteScraperHandle(...)`
                # for why. maybe_recycle_browser is the periodic
                # full-relaunch safety net; new_session gives each
                # team's analysis a clean session (no cookie carryover)
                # without paying for a full relaunch to get it.
                home_error = None
                away_error = None

                try:
                    scraper.maybe_recycle_browser()
                    scraper.new_session()
                    home_data = scraper.analyze_team(
                        match["home_id"], match["home_name"]
                    )
                except Exception as e:
                    home_data = None
                    home_error = e

                try:
                    scraper.maybe_recycle_browser()
                    scraper.new_session()
                    away_data = scraper.analyze_team(
                        match["away_id"], match["away_name"]
                    )
                except Exception as e:
                    away_data = None
                    away_error = e

                # Logged after every match — this is the line that shows
                # whether chrome-family process count / free memory is
                # holding steady across the batch instead of climbing
                # (see _log_resource_usage docstring).
                _log_resource_usage(f"after match {idx}")

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
                        log.info(f"ALERT ({label}):\n" + sig_msg)
                        scraper.send_telegram_message(
                            sig_msg,
                            BOT_TOKEN,
                            CHAT_ID
                        )

                if not any_fired:
                    log.info("No signals found.")

            except Exception as match_err:
                # A single bad match (odd page layout, timeout, etc.)
                # should not take down the whole batch — log it and
                # move on to the next match instead.
                log.error(
                    f"Error processing match {m_url}: {match_err}"
                )
                log.debug(traceback.format_exc())
                continue

        send_job_status(
            f"✅ Job FINISHED\n"
            f"Batch START={START} LIMIT={LIMIT}",
            BOT_TOKEN,
            CHAT_ID
        )

    except Exception as e:

        log.error(f"Job failed: {e}")
        log.error(traceback.format_exc())

        send_job_status(
            f"❌ Job FAILED\n"
            f"Batch START={START} LIMIT={LIMIT}\n"
            f"Error: {str(e)}",
            BOT_TOKEN,
            CHAT_ID
        )

    finally:

        log.info("Closing browser...")

        if scraper is not None:
            scraper.close()

        log.info("Script finished.")


if __name__ == "__main__":
    main()
