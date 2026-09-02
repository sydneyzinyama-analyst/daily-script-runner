import os
import sys
import argparse
import logging
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


# ---------------- JOB STATUS TELEGRAM ----------------
def send_job_status(message, bot_token, chat_id):
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        requests.post(url, data=payload, timeout=20)
    except Exception as e:
        log.warning(f"Failed to send job status to Telegram: {e}")


# ---------------- SCRAPER CLASS ----------------
class FlashscoreGoalsScraper:
    def __init__(self, headless=True):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=headless,
            # --no-sandbox / --disable-dev-shm-usage are required in most
            # scheduler contexts: cron/systemd jobs often run as root (where
            # Chromium's sandbox refuses to start without --no-sandbox) or
            # in containers with a tiny /dev/shm. Without these flags the
            # browser can fail to launch even though it works fine when you
            # run the script by hand as a regular user.
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
            ),
        )
        self.page = self.context.new_page()
        self.team_url = ""
        self.team_slug = ""
        self.team_label = ""

    # ---------------- TELEGRAM ----------------
    def send_telegram_message(self, message, bot_token, chat_id):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            # Alert messages use Telegram's legacy Markdown for bold
            # section headers (see evaluate_bet_signals) — team names
            # are pre-escaped there so this doesn't choke on stray
            # _ / * / ` / [ characters.
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

    # ---------------- HELPERS ----------------
    def normalize_name(self, text):
        if not text:
            return ""
        text = unicodedata.normalize("NFKD", text)
        text = text.encode("ascii", "ignore").decode("ascii")
        return re.sub(r"[^a-z0-9]+", "", text.lower())

    def slug_to_team_name(self, slug):
        if not slug:
            return ""
        return slug.replace("-", " ").strip().title()

    def extract_team_slug_from_url(self, team_url):
        try:
            path_parts = urlparse(team_url).path.strip("/").split("/")
            if len(path_parts) >= 2 and path_parts[0] == "team":
                return path_parts[1]
        except Exception:
            pass
        return ""

    def _abs_url(self, href):
        if not href:
            return ""
        if href.startswith("http"):
            return href
        return "https://www.flashscore.co.za" + href

    def _safe_text(self, selector):
        try:
            loc = self.page.locator(selector)
            if loc.count() > 0:
                text = loc.first.inner_text().strip()
                return text
        except Exception:
            pass
        return ""

    def _safe_attr(self, selector, attr_name="href"):
        try:
            loc = self.page.locator(selector)
            if loc.count() > 0:
                val = loc.first.get_attribute(attr_name)
                if val:
                    return val
        except Exception:
            pass
        return ""

    def _parse_stat_value(self, text):
        # Stats on the overall/stats page come in a few shapes:
        #   "19"                -> plain count
        #   "1.59"               -> decimal (xG, xGOT, goals prevented)
        #   "-0.43"              -> negative decimal (goals prevented)
        #   "76%\n(262/343)"     -> percentage stats (passes, tackles...)
        # We only need the leading number in every case.
        if not text:
            return None

        text = text.strip()

        pct_match = re.match(r"^(-?\d+(?:\.\d+)?)\s*%", text)
        if pct_match:
            return float(pct_match.group(1))

        num_match = re.match(r"^-?\d+(?:\.\d+)?", text)
        if num_match:
            try:
                return float(num_match.group(0))
            except ValueError:
                return None

        return None

    def accept_cookies(self):
        # Flashscore (and most EU-facing sites) show a cookie consent
        # overlay on first visit. A scheduler always starts a fresh
        # browser context (no saved consent cookie), so this overlay can
        # intercept clicks and silently break discovery/expansion that
        # worked fine in your already-consented local browser session.
        selectors = [
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept')",
            "button:has-text('I Accept')",
        ]
        for selector in selectors:
            try:
                btn = self.page.locator(selector)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=3000)
                    time.sleep(1)
                    return
            except Exception:
                continue

    def get_team_name_from_page(self):
        selectors = [
            "h1",
            ".heading__name",
            ".participant__participantName a",
            ".participant__participantName",
        ]
        for selector in selectors:
            try:
                loc = self.page.locator(selector)
                if loc.count() > 0:
                    text = loc.first.inner_text().strip()
                    if text:
                        text = re.sub(r"^Soccer:\s*", "", text, flags=re.IGNORECASE)
                        text = re.sub(r"\s+results?\s*$", "", text, flags=re.IGNORECASE)
                        return text
            except Exception:
                pass
        return ""

    # ---------------- SCRAPER ----------------
    def open_team_results(self, team_url):
        self.team_url = team_url
        self.team_slug = self.extract_team_slug_from_url(team_url)
        self.team_label = self.slug_to_team_name(self.team_slug)
        url = team_url.rstrip("/") + "/results/"
        log.info(f"Opening results page: {url}")
        try:
            self.page.goto(url, wait_until="load", timeout=90000)
            time.sleep(3)
            self.accept_cookies()
            page_name = self.get_team_name_from_page()
            if page_name:
                self.team_label = page_name
            return True
        except Exception as e:
            log.error(f"Failed to load results: {e}")
            return False

    def expand_hidden_matches(self):
        try:
            while True:
                btns = self.page.locator("text=/display matches/i")
                count = btns.count()

                if count == 0:
                    break

                clicked = 0

                for i in range(count):
                    try:
                        btn = btns.nth(i)
                        if btn.is_visible():
                            btn.click(timeout=5000)
                            clicked += 1
                            time.sleep(0.3)
                    except Exception as e:
                        log.warning(f"Skipping button {i}: {e}")

                if clicked == 0:
                    break

                time.sleep(1)

        except Exception as e:
            log.warning(f"expand_hidden_matches failed: {e}")

    def discover_matches(self, target_count, max_tries=250):
        matches = []
        seen = set()
        tries = 0

        while len(matches) < target_count and tries < max_tries:
            self.expand_hidden_matches()

            links = self.page.locator("a[href*='/match/'][href*='?mid=']").all()
            for link in links:
                href = link.get_attribute("href")
                if not href:
                    continue

                href = href.split("/tv")[0].split("#")[0]
                full_url = self._abs_url(href)

                if full_url not in seen and "?mid=" in full_url:
                    matches.append(full_url)
                    seen.add(full_url)

                if len(matches) >= target_count:
                    break

            if len(matches) >= target_count:
                break

            try:
                self.page.mouse.wheel(0, 6000)
            except Exception:
                pass

            time.sleep(2)
            tries += 1

        return matches

    def get_match_teams_and_links(self, match_url):
        try:
            self.page.goto(match_url, wait_until="networkidle", timeout=90000)
            time.sleep(3)
        except Exception:
            return None

        home_name = self._safe_text(
            ".duelParticipant__home .participant__participantName a"
        )
        away_name = self._safe_text(
            ".duelParticipant__away .participant__participantName a"
        )

        home_href = self._safe_attr(
            ".duelParticipant__home .participant__participantName a", "href"
        )
        away_href = self._safe_attr(
            ".duelParticipant__away .participant__participantName a", "href"
        )

        return {
            "home_name": home_name,
            "away_name": away_name,
            "home_url": self._abs_url(home_href),
            "away_url": self._abs_url(away_href),
            "match_url": match_url,
        }

    def get_match_stats_url(self, match_url):
        try:
            if "?mid=" not in match_url:
                return None

            base = match_url.split("?mid=")[0]
            mid = match_url.split("?mid=")[1]

            return f"{base}summary/stats/overall/?mid={mid}"

        except Exception:
            return None

    # Maps a stat row's category label (lowercased) to the short key we
    # store it under. Add an entry here to pull in another stat with no
    # other code changes needed to the scraping itself.
    STAT_LABEL_MAP = {
        "expected goals (xg)": "xg",
        "xg on target (xgot)": "xgot",
        "total shots": "shots",
        "shots on target": "shots_on_target",
        "corner kicks": "corners",
        "big chances": "big_chances",
        "yellow cards": "yellow_cards",
        "fouls": "fouls",
        "goals prevented": "goals_prevented",
        "ball possession": "possession",
    }

    def get_match_stats(self, match_url):
        """
        Pulls the full set of stats we care about from the match's
        stats/overall page in a single pass: xG, xGOT, corners, big
        chances, yellow cards, fouls and goalkeeper "goals prevented".

        Returns a dict of home_<stat>/away_<stat> pairs. Any stat not
        found on the page (older matches, different competitions, page
        layout differences) is left as None rather than raising.
        """
        stats_url = self.get_match_stats_url(match_url)

        result = {"match_url": match_url}
        for stat_key in self.STAT_LABEL_MAP.values():
            result[f"home_{stat_key}"] = None
            result[f"away_{stat_key}"] = None

        if not stats_url:
            return result

        try:
            self.page.goto(
                stats_url,
                wait_until="networkidle",
                timeout=90000
            )
            time.sleep(3)

        except Exception:
            return result

        try:
            rows = self.page.locator(
                "[data-testid='wcl-statistics']"
            ).all()

            for row in rows:
                try:
                    label = row.locator(
                        "[data-testid='wcl-statistics-category']"
                    ).inner_text().strip()

                    stat_key = self.STAT_LABEL_MAP.get(label.lower())
                    if not stat_key:
                        continue

                    values = row.locator(
                        "[data-testid='wcl-statistics-value'] span"
                    ).all()

                    if len(values) < 2:
                        continue

                    home_val = self._parse_stat_value(
                        values[0].inner_text()
                    )
                    away_val = self._parse_stat_value(
                        values[1].inner_text()
                    )

                    result[f"home_{stat_key}"] = home_val
                    result[f"away_{stat_key}"] = away_val

                except Exception:
                    continue

        except Exception:
            pass

        return result

    def get_match_goals(self, match_url):
        try:
            self.page.goto(
                match_url,
                wait_until="networkidle",
                timeout=90000
            )
            time.sleep(3)

        except Exception:
            return None

        score_home = None
        score_away = None

        try:
            score_spans = self.page.locator(
                ".detailScore__wrapper span"
            ).all()

            if len(score_spans) >= 3:
                h = score_spans[0].inner_text().strip()
                d = score_spans[1].inner_text().strip()
                a = score_spans[2].inner_text().strip()

                if d == "-" and h.isdigit() and a.isdigit():
                    score_home = int(h)
                    score_away = int(a)

        except Exception:
            pass

        home = self._safe_text(
            ".duelParticipant__home .participant__participantName a"
        ) or "?"

        away = self._safe_text(
            ".duelParticipant__away .participant__participantName a"
        ) or "?"

        return {
            "home": home,
            "away": away,
            "goals_home": score_home,
            "goals_away": score_away,
            "match_url": match_url
        }

    def _team_match_score(self, a, b):
        a_n = self.normalize_name(a)
        b_n = self.normalize_name(b)

        if not a_n or not b_n:
            return 0.0

        if a_n == b_n:
            return 1.0

        if a_n in b_n or b_n in a_n:
            return 0.95

        return SequenceMatcher(None, a_n, b_n).ratio()

    def _team_matches(self, candidate, aliases, threshold=0.62):
        for alias in aliases:
            if not alias:
                continue

            if self._team_match_score(candidate, alias) >= threshold:
                return True

        return False

    def calculate_team_goals(self, results):
        total_goals = 0
        matches_counted = 0

        aliases = [
            self.team_slug,
            self.team_label,
            self.slug_to_team_name(self.team_slug)
        ]

        for r in results:
            home_team = r.get("home", "")
            away_team = r.get("away", "")

            if self._team_matches(home_team, aliases):
                total_goals += r.get("goals_home") or 0
                matches_counted += 1

            elif self._team_matches(away_team, aliases):
                total_goals += r.get("goals_away") or 0
                matches_counted += 1

        avg_goals = (
            total_goals / matches_counted
            if matches_counted > 0
            else 0
        )

        return {
            "team": self.team_label or self.team_slug,
            "total_goals": total_goals,
            "avg_goals": round(avg_goals, 2),
            "matches": matches_counted
        }

    def calculate_team_goals_conceded(self, results):
        total_conceded = 0
        counted = 0

        aliases = [
            self.team_slug,
            self.team_label,
            self.slug_to_team_name(self.team_slug)
        ]

        for r in results:
            home_team = r.get("home", "")
            away_team = r.get("away", "")

            if self._team_matches(home_team, aliases):
                total_conceded += r.get("goals_away") or 0
                counted += 1

            elif self._team_matches(away_team, aliases):
                total_conceded += r.get("goals_home") or 0
                counted += 1

        avg_conceded = (
            total_conceded / counted
            if counted > 0
            else 0
        )

        return round(avg_conceded, 2)

    def calculate_team_xg(self, results):
        total_xg = 0
        counted = 0

        aliases = [
            self.team_slug,
            self.team_label,
            self.slug_to_team_name(self.team_slug)
        ]

        for r in results:
            home_team = r.get("home", "")
            away_team = r.get("away", "")

            if self._team_matches(home_team, aliases):
                if r.get("home_xg") is not None:
                    total_xg += r["home_xg"]
                    counted += 1

            elif self._team_matches(away_team, aliases):
                if r.get("away_xg") is not None:
                    total_xg += r["away_xg"]
                    counted += 1

        if counted == 0:
            return None

        return round(total_xg / counted, 2)

    def calculate_team_xga(self, results):
        total_xga = 0
        counted = 0

        aliases = [
            self.team_slug,
            self.team_label,
            self.slug_to_team_name(self.team_slug)
        ]

        for r in results:
            home_team = r.get("home", "")
            away_team = r.get("away", "")

            if self._team_matches(home_team, aliases):
                if r.get("away_xg") is not None:
                    total_xga += r["away_xg"]
                    counted += 1

            elif self._team_matches(away_team, aliases):
                if r.get("home_xg") is not None:
                    total_xga += r["home_xg"]
                    counted += 1

        if counted == 0:
            return None

        return round(total_xga / counted, 2)

    def _team_stat_avg(self, results, stat_name, side="for"):
        """
        Generic averager for the extra stats (corners, big_chances,
        yellow_cards, fouls, goals_prevented, xgot). Each match_data
        dict is expected to carry home_<stat_name>/away_<stat_name>
        keys, as produced by get_match_stats().

        side="for"     -> the analyzed team's own stat
        side="against" -> the opponent's stat in that match (e.g. big
                           chances *faced*, useful for BTTS-style
                           signals where "for" alone isn't enough)
        """
        total = 0
        counted = 0

        aliases = [
            self.team_slug,
            self.team_label,
            self.slug_to_team_name(self.team_slug)
        ]

        for r in results:
            home_team = r.get("home", "")
            away_team = r.get("away", "")

            is_home = self._team_matches(home_team, aliases)
            is_away = (
                not is_home
                and self._team_matches(away_team, aliases)
            )

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

    def analyze_team(self, team_url):
        if not self.open_team_results(team_url):
            return None

        matches = self.discover_matches(6)
        results = []

        for url in matches:
            match_data = self.get_match_goals(url)

            if match_data:
                extra_stats = self.get_match_stats(url)

                for key, value in extra_stats.items():
                    if key != "match_url":
                        match_data[key] = value

                results.append(match_data)

        stats = self.calculate_team_goals(results)
        avg_gc = self.calculate_team_goals_conceded(results)
        avg_xg = self.calculate_team_xg(results)
        avg_xga = self.calculate_team_xga(results)

        avg_gd = round(
            stats["avg_goals"] - avg_gc,
            2
        )

        if avg_xg is not None and avg_xga is not None:
            avg_xgd = round(
                avg_xg - avg_xga,
                2
            )
        else:
            avg_xgd = None

        stats.update({
            "avg_gc": avg_gc,
            "avg_gd": avg_gd,
            "avg_xg": avg_xg,
            "avg_xga": avg_xga,
            "avg_xgd": avg_xgd,

            # New filters: corners, big chances, cards/fouls, xGOT and
            # goalkeeper "goals prevented".
            "avg_corners_for": self._team_stat_avg(results, "corners", "for"),
            "avg_corners_against": self._team_stat_avg(results, "corners", "against"),
            "avg_big_chances_for": self._team_stat_avg(results, "big_chances", "for"),
            "avg_big_chances_against": self._team_stat_avg(results, "big_chances", "against"),
            "avg_yellow_cards": self._team_stat_avg(results, "yellow_cards", "for"),
            "avg_fouls": self._team_stat_avg(results, "fouls", "for"),
            "avg_xgot_for": self._team_stat_avg(results, "xgot", "for"),
            "avg_xgot_against": self._team_stat_avg(results, "xgot", "against"),
            "avg_goals_prevented": self._team_stat_avg(results, "goals_prevented", "for"),
            "avg_shots_for": self._team_stat_avg(results, "shots", "for"),
            "avg_shots_against": self._team_stat_avg(results, "shots", "against"),
            "avg_sot_for": self._team_stat_avg(results, "shots_on_target", "for"),
            "avg_sot_against": self._team_stat_avg(results, "shots_on_target", "against"),
            "avg_possession": self._team_stat_avg(results, "possession", "for"),
        })

        return {
            "team": stats["team"],
            "matches": matches,
            "results": results,
            "stats": stats
        }

    def close(self):
        try:
            self.browser.close()
            self.playwright.stop()
        except Exception as e:
            log.warning(f"Error while closing browser: {e}")


# ---------------- SIGNAL ENGINE ----------------

# Win-score weighting:
# dominance gap (8) + goal/conceded corroboration (4) + xGA (2) = 14 max.

# NOTE ON CONFIDENCE: every threshold in this engine is a heuristic
# cutoff, not a measured probability — nothing here has been validated
# against actual historical outcomes. "HIGH-CONFIDENCE" means "clears
# a deliberately severe set of hand-tuned filters", not "X% likely to
# happen". Treat it as the most conservative reading these heuristics
# can produce, not a calibrated number, until a real backtest exists.

MAX_WIN_SCORE = 14
HIGH_WIN_THRESHOLD = 13

# Fallback path for matches/leagues where xG isn't published on
# Flashscore at all (common outside the top few divisions). _win_score
# is None-safe: with xga=None the two xGA bonus categories (2 points)
# are simply unreachable, so the real ceiling without xG is 12, not 14.
# The threshold is raised proportionally (11/12 ≈ 92%, in line with
# 13/14 ≈ 93% for the xG path) and the underlying goal-difference
# filters are tightened further, since GD alone is noisier than xGD
# over a 5-6 match sample and there's no shot-quality data to
# corroborate it.
GD_ONLY_MAX_SCORE = 12
GD_ONLY_HIGH_THRESHOLD = 11

# Every signal in this engine requires each team's stats to be built
# from at least this many of the up-to-6 fetched recent matches. Below
# this, the sample is too thin to trust any signal on it — pushed to
# require essentially the full window, not just "most of it".
MIN_SAMPLE_MATCHES = 6


def _win_score(
    fav_gd,
    dog_gd,
    fav_g,
    fav_gc,
    dog_g,
    dog_gc,
    fav_xga,
    dog_xga
):
    """
    Points toward fav beating dog.

    gd/xga args are None-safe.
    dog/fav args use the same metric family
    (both GD or both xGD) so a partial xG match
    doesn't mix scales.
    """

    score = 0.0

    # Favourite goal difference
    if fav_gd >= 1.5:
        score += 3

    elif fav_gd >= 1.0:
        score += 2

    elif fav_gd >= 0.5:
        score += 1

    # Underdog goal difference
    if dog_gd <= -1.2:
        score += 3

    elif dog_gd <= -0.8:
        score += 2

    elif dog_gd <= -0.4:
        score += 1

    # Goal difference gap
    gap = fav_gd - dog_gd

    if gap >= 2.5:
        score += 2

    elif gap >= 1.8:
        score += 1

    # Favourite scoring
    if fav_g >= 2.0:
        score += 1

    elif fav_g >= 1.8:
        score += 0.5

    # Favourite defence
    if fav_gc <= 0.9:
        score += 1

    elif fav_gc <= 1.1:
        score += 0.5

    # Underdog scoring
    if dog_g <= 1.0:
        score += 1

    elif dog_g <= 1.2:
        score += 0.5

    # Underdog defence
    if dog_gc >= 1.8:
        score += 1

    elif dog_gc >= 1.6:
        score += 0.5

    # Favourite xGA
    if fav_xga is not None and fav_xga <= 1.0:
        score += 1

    elif fav_xga is not None and fav_xga <= 1.25:
        score += 0.5

    # Underdog xGA
    if dog_xga is not None and dog_xga >= 1.8:
        score += 1

    elif dog_xga is not None and dog_xga >= 1.55:
        score += 0.5

    return score


# Total-goals lines a book actually offers. A line only "hits" when the
# expected-goals figure clears it by a comfortable margin — this keeps
# the ladder from firing on every match at the nearest line.
TOTAL_GOAL_LINES = [0.5, 1.5, 2.5, 3.5, 4.5]
TOTAL_GOAL_MARGIN = 1.5

# Wider margin used when falling back to plain averaged goals instead
# of xG — raw goals-per-game swings more from match to match, so it
# needs more daylight from a line before we trust it.
TOTAL_GOAL_MARGIN_GD = 2.0


def _total_goals_lean(
    expected_goals, label, priority=3, margin=TOTAL_GOAL_MARGIN,
    category="goals"
):
    """
    Picks the single most-confident Over/Under total-goals line for a
    given expected-goals figure (combined match total, or one team's
    own total). Returns a (priority, text, category) tuple, or None if
    nothing clears `margin` on any offered line.

    For Over, we want the *highest* line still comfortably cleared
    (Over 3.5 is a stronger claim than Over 0.5 when both are true).
    For Under, we want the *lowest* line still comfortably cleared,
    for the same reason in the other direction.
    """
    if expected_goals is None:
        return None

    over_line = None
    under_line = None

    for line in TOTAL_GOAL_LINES:
        gap = expected_goals - line

        if gap >= margin:
            over_line = line

        if gap <= -margin and under_line is None:
            under_line = line

    if over_line is not None:
        return (
            priority,
            f"{label}: likely Over {over_line} "
            f"(expected ~{expected_goals:.2f})",
            category
        )

    if under_line is not None:
        return (
            priority,
            f"{label}: likely Under {under_line} "
            f"(expected ~{expected_goals:.2f})",
            category
        )

    return None


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


# Market categories the alert message is grouped into, in display
# order. Only categories that actually produced a signal get a
# section header in the final message.
CATEGORY_ORDER = [
    "result", "goals", "clean_sheet", "corners_cards", "shots", "combo"
]

CATEGORY_LABELS = {
    "result": "🏆 *Match Result*",
    "goals": "⚽ *Goals*",
    "clean_sheet": "🧤 *Clean Sheet*",
    "corners_cards": "🚩 *Corners & Cards*",
    "shots": "🎯 *Shots on Target*",
    "combo": "🔀 *Combo Markets*",
}


def evaluate_bet_signals(
    home,
    away,
    home_data,
    away_data,
    m_url
):
    # Escape once up front — every message string built below uses
    # these names directly, so this covers headers, bullets and
    # warnings without touching each call site individually.
    home = _escape_markdown(home)
    away = _escape_markdown(away)

    hs = home_data["stats"]
    as_ = away_data["stats"]

    # Sample-size gate: refuse to signal on either team unless most of
    # the recent matches we tried to fetch actually matched up. A
    # small/noisy sample is the single biggest way a "high-confidence"
    # signal turns out to be wrong.
    if (
        hs.get("matches", 0) < MIN_SAMPLE_MATCHES
        or as_.get("matches", 0) < MIN_SAMPLE_MATCHES
    ):
        return None

    h_g = hs.get("avg_goals", 0)
    a_g = as_.get("avg_goals", 0)

    h_gc = hs.get("avg_gc", 0)
    a_gc = as_.get("avg_gc", 0)

    h_gd = hs.get("avg_gd", 0)
    a_gd = as_.get("avg_gd", 0)

    h_xg = hs.get("avg_xg")
    a_xg = as_.get("avg_xg")

    h_xga = hs.get("avg_xga")
    a_xga = as_.get("avg_xga")

    h_xgd = hs.get("avg_xgd")
    a_xgd = as_.get("avg_xgd")

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

    positive = []
    warnings = []

    def add_positive(priority, text, category):
        positive.append((priority, text, category))

    def add_warning(text):
        if text not in warnings:
            warnings.append(text)

    use_xg = (
        h_xg is not None
        and a_xg is not None
        and h_xga is not None
        and a_xga is not None
        and h_xgd is not None
        and a_xgd is not None
    )

    # -------------------------------------------------
    # OVERPERFORMANCE WARNINGS
    # -------------------------------------------------

    if h_g >= 2.0 and (
        h_xg is not None and h_xg <= 1.5
    ):
        add_warning(
            f"{home} may be overperforming its finishing "
            f"(caution on backing them blindly)"
        )

    if a_g >= 2.0 and (
        a_xg is not None and a_xg <= 1.5
    ):
        add_warning(
            f"{away} may be overperforming its finishing "
            f"(caution on backing them blindly)"
        )

    # xGOT is a sharper version of the same check: it's shots on target
    # weighted by quality, so goals scored well above it means a team
    # is converting chances at a rate the shots themselves don't
    # support — a more precise "this is due to regress" flag than
    # comparing goals to xG alone.
    if h_xgot_for is not None and h_g >= h_xgot_for + 0.8:
        add_warning(
            f"{home} scoring well above its shot quality (xGOT) — "
            f"finishing likely unsustainable"
        )

    if a_xgot_for is not None and a_g >= a_xgot_for + 0.8:
        add_warning(
            f"{away} scoring well above its shot quality (xGOT) — "
            f"finishing likely unsustainable"
        )

    # -------------------------------------------------
    # STERILE POSSESSION WARNING
    # -------------------------------------------------
    # Possession alone is a weak predictor — plenty of teams win
    # comfortably on 35% of the ball. It's only useful here as context:
    # a team hogging the ball without anything to show for it in shots
    # or big chances is "controlling" the game in name only, which is
    # worth flagging on a team we're otherwise backing.

    if (
        h_poss is not None
        and h_poss >= 58
        and h_bc_for is not None
        and h_bc_for <= 1.0
    ):
        add_warning(
            f"{home} dominates possession ({h_poss:.0f}%) but creates "
            f"few big chances from it — territorial control isn't "
            f"converting into danger"
        )

    if (
        a_poss is not None
        and a_poss >= 58
        and a_bc_for is not None
        and a_bc_for <= 1.0
    ):
        add_warning(
            f"{away} dominates possession ({a_poss:.0f}%) but creates "
            f"few big chances from it — territorial control isn't "
            f"converting into danger"
        )

    # -------------------------------------------------
    # DEFENSIVE WEAKNESS WARNINGS
    # -------------------------------------------------

    if h_gc >= 1.8:
        add_warning(
            f"{home} defensive weakness: "
            f"opponent scoring chances look high"
        )

    if a_gc >= 1.8:
        add_warning(
            f"{away} defensive weakness: "
            f"opponent scoring chances look high"
        )

    # -------------------------------------------------
    # GOALKEEPER FORM WARNING (Goals prevented)
    # -------------------------------------------------
    # Goals prevented = xGOT faced minus goals actually conceded.
    # A meaningfully negative average means the keeper is conceding
    # more than the shots they face would suggest — a defensive
    # frailty flag that goals-conceded averages alone won't show.

    if h_gp is not None and h_gp <= -0.3:
        add_warning(
            f"{home}'s goalkeeper has been conceding more than "
            f"expected recently (goals prevented avg {h_gp})"
        )

    if a_gp is not None and a_gp <= -0.3:
        add_warning(
            f"{away}'s goalkeeper has been conceding more than "
            f"expected recently (goals prevented avg {a_gp})"
        )

    # -------------------------------------------------
    # WIN SCORES (both directions)
    # -------------------------------------------------
    # Computed unconditionally so the softer "lean" markets below
    # (Double Chance / DNB / Handicap) have something to work with
    # even when the strict outright-win hard filters don't pass.
    # Use xGD when complete xG data exists, otherwise plain GD.

    # xGA args passed to _win_score are always each team's own xGA —
    # only the GD/xGD "fav vs dog" framing flips between the two calls.
    xga_args = (h_xga, a_xga) if use_xg else (None, None)

    fav_metric_h, dog_metric_h = (
        (h_xgd, a_xgd) if use_xg else (h_gd, a_gd)
    )

    home_win_score = _win_score(
        fav_metric_h, dog_metric_h,
        h_g, h_gc, a_g, a_gc,
        xga_args[0], xga_args[1]
    )

    fav_metric_a, dog_metric_a = (
        (a_xgd, h_xgd) if use_xg else (a_gd, h_gd)
    )

    away_win_score = _win_score(
        fav_metric_a, dog_metric_a,
        a_g, a_gc, h_g, h_gc,
        xga_args[1], xga_args[0]
    )

    # -------------------------------------------------
    # HOME / AWAY WIN SIGNALS (3 Way)
    # -------------------------------------------------
    #
    # HARD FILTERS (mandatory — if any condition fails for a side,
    # that side's win signal cannot be generated):
    #
    # 1. Opponent must average well under a goal per game.
    # 2. This team must concede very little.
    #
    # Two confidence paths, picked automatically per match:
    #
    #   - xG available (use_xg): score out of 14, threshold 12.
    #   - No xG (common outside top divisions): score out of 12
    #     (the xGA bonus categories are simply unreachable), goal
    #     filters tightened further, and threshold raised to 10/12 to
    #     land at roughly the same strictness as the xG path.
    #
    # Everything downstream (message text) is tagged with which basis
    # actually produced the signal, so a goals-only alert never reads
    # as equivalent to an xG-backed one.
    # -------------------------------------------------

    win_basis = "xG-based" if use_xg else "goals-based, no xG data"

    if use_xg:
        win_threshold = HIGH_WIN_THRESHOLD
        win_score_max = MAX_WIN_SCORE
        home_win_eligible = a_g < 0.6 and h_gc <= 0.6
        away_win_eligible = h_g < 0.6 and a_gc <= 0.6
    else:
        win_threshold = GD_ONLY_HIGH_THRESHOLD
        win_score_max = GD_ONLY_MAX_SCORE
        home_win_eligible = a_g < 0.5 and h_gc <= 0.5
        away_win_eligible = h_g < 0.5 and a_gc <= 0.5

    home_high_conf = (
        home_win_eligible and home_win_score >= win_threshold
    )

    away_high_conf = (
        away_win_eligible and away_win_score >= win_threshold
    )

    if home_high_conf:
        add_positive(
            1,
            f"HIGH-CONFIDENCE home win signal for "
            f"{home} ({win_basis}, "
            f"score {home_win_score:.1f}/{win_score_max})",
            "result"
        )

    if away_high_conf:
        add_positive(
            1,
            f"HIGH-CONFIDENCE away win signal for "
            f"{away} ({win_basis}, "
            f"score {away_win_score:.1f}/{win_score_max})",
            "result"
        )

    # -------------------------------------------------
    # SHOT DOMINANCE CROSS-CHECK
    # -------------------------------------------------
    # A win signal built off a good goals/xG record can still be
    # riding a couple of clinical finishes rather than real control of
    # the game. If the side we're backing is actually being outshot on
    # target by the side we're backing against, that's worth knowing
    # even though it doesn't cancel the signal outright.

    if (
        home_high_conf
        and h_sot_for is not None
        and a_sot_against is not None
        and h_sot_for < a_sot_against - 1.0
    ):
        add_warning(
            f"{home} is being backed to win but averages fewer shots "
            f"on target than {away} concedes — the goal record may be "
            f"outrunning actual chance creation"
        )

    if (
        away_high_conf
        and a_sot_for is not None
        and h_sot_against is not None
        and a_sot_for < h_sot_against - 1.0
    ):
        add_warning(
            f"{away} is being backed to win but averages fewer shots "
            f"on target than {home} concedes — the goal record may be "
            f"outrunning actual chance creation"
        )

    # -------------------------------------------------
    # DRAW SIGNAL
    # -------------------------------------------------
    # Fires on a very tight metric gap, near-identical scoring rates,
    # and neither side anywhere close to a win signal — a draw lean is
    # specifically "these two are genuinely hard to separate", not
    # just "no win signal happened to trigger". Same two-path split as
    # the win signals: xGD when available, plain GD (with a slightly
    # wider gap allowance) when not.

    if use_xg:
        draw_metric_gap = abs(h_xgd - a_xgd)
        draw_signal = (
            draw_metric_gap <= 0.1
            and abs(h_g - a_g) <= 0.2
            and home_win_score < win_threshold - 2
            and away_win_score < win_threshold - 2
        )
    else:
        draw_metric_gap = abs(h_gd - a_gd)
        draw_signal = (
            draw_metric_gap <= 0.15
            and abs(h_g - a_g) <= 0.25
            and home_win_score < win_threshold - 2
            and away_win_score < win_threshold - 2
        )

    if draw_signal:
        draw_text = (
            f"Draw signal ({win_basis}): {home} and {away} closely "
            f"matched in current form (metric gap {draw_metric_gap:.2f})"
        )

        # Possession is weak on its own, but even territorial control
        # is a reasonable extra corroboration for a signal that's
        # already claiming "these two are hard to separate".
        if (
            h_poss is not None
            and a_poss is not None
            and abs(h_poss - a_poss) <= 6
        ):
            draw_text += " — territorial control also even"

        add_positive(2, draw_text, "result")

    # -------------------------------------------------
    # MATCH LEAN -> DOUBLE CHANCE / DNB / HANDICAP
    # -------------------------------------------------
    # These ride strictly on the HIGH-CONFIDENCE win/draw signals above
    # — no separate, softer threshold. If we're not confident enough
    # for an outright signal, we're not confident enough for these
    # either.

    if home_high_conf:
        lean = "home"
    elif away_high_conf:
        lean = "away"
    elif draw_signal:
        lean = "draw"
    else:
        lean = None

    if lean == "home":
        add_positive(
            2,
            f"Double Chance lean: {home} or Draw (1X)",
            "result"
        )
        add_positive(
            2,
            f"Draw No Bet lean: {home}",
            "result"
        )

    elif lean == "away":
        add_positive(
            2,
            f"Double Chance lean: Draw or {away} (X2)",
            "result"
        )
        add_positive(
            2,
            f"Draw No Bet lean: {away}",
            "result"
        )

    # Handicap direction: bucket the metric gap on the leaning side's
    # favour into a rough line suggestion. Only meaningful for a
    # home/away lean, not a draw lean.

    if lean == "home":
        handicap_gap = fav_metric_h - dog_metric_h
        handicap_team = home
    elif lean == "away":
        handicap_gap = fav_metric_a - dog_metric_a
        handicap_team = away
    else:
        handicap_gap = None
        handicap_team = None

    if handicap_gap is not None:
        if handicap_gap >= 3.5:
            add_positive(
                2,
                f"Handicap lean: {handicap_team} -1 (or better)",
                "result"
            )
        elif handicap_gap >= 2.5:
            add_positive(
                2,
                f"Handicap lean: {handicap_team} -0.5/-1",
                "result"
            )
        elif handicap_gap >= 1.5:
            add_positive(
                2,
                f"Handicap lean: {handicap_team} -0.5",
                "result"
            )

    # -------------------------------------------------
    # LOW GOAL / UNDER 2.5 SIGNAL (strict) + TOTAL GOALS LADDER
    # -------------------------------------------------

    low_goal_signal_fired = False

    if use_xg:

        conditions_met = 0

        # Both teams create few chances
        if h_xg <= 0.65 and a_xg <= 0.65:
            conditions_met += 1

        # Both teams concede few chances
        if h_xga <= 0.85 and a_xga <= 0.85:
            conditions_met += 1

        # Teams have similar xGD
        if abs(h_xgd - a_xgd) <= 0.2:
            conditions_met += 1

        if conditions_met == 3:
            add_positive(
                3,
                "Strong low-goal signal (xG-based): likely Under 2.5",
                "goals"
            )
            low_goal_signal_fired = True

    else:

        conditions_met = 0

        # Both teams score little themselves
        if h_g <= 0.7 and a_g <= 0.7:
            conditions_met += 1

        # Both teams concede little
        if h_gc <= 0.85 and a_gc <= 0.85:
            conditions_met += 1

        # Similar goal difference (no lopsided form skewing it)
        if abs(h_gd - a_gd) <= 0.2:
            conditions_met += 1

        if conditions_met == 3:
            add_positive(
                3,
                "Strong low-goal signal (goals-based, no xG data): "
                "likely Under 2.5",
                "goals"
            )
            low_goal_signal_fired = True

    # Combined + per-team Over/Under ladder (0.5-4.5). Uses xG when
    # available; falls back to plain averaged goals with a wider margin
    # (TOTAL_GOAL_MARGIN_GD) otherwise, since raw goals-per-game swings
    # more match to match than xG does. Skipped for the match total
    # when the strict signal above already covered it, to avoid two
    # near-duplicate Under 2.5 lines in the same alert.

    if use_xg:
        combined_expected_goals = h_xg + a_xg
        home_expected_goals = h_xg
        away_expected_goals = a_xg
        goals_margin = TOTAL_GOAL_MARGIN
        goals_basis = "xG-based"
    else:
        combined_expected_goals = h_g + a_g
        home_expected_goals = h_g
        away_expected_goals = a_g
        goals_margin = TOTAL_GOAL_MARGIN_GD
        goals_basis = "goals-based"

    if not low_goal_signal_fired:
        total_goals_signal = _total_goals_lean(
            combined_expected_goals,
            f"Total Goals ({goals_basis})",
            margin=goals_margin
        )
        if total_goals_signal:
            add_positive(*total_goals_signal)

    home_goals_signal = _total_goals_lean(
        home_expected_goals,
        f"{home} Total Goals ({goals_basis})",
        margin=goals_margin
    )
    if home_goals_signal:
        add_positive(*home_goals_signal)

    away_goals_signal = _total_goals_lean(
        away_expected_goals,
        f"{away} Total Goals ({goals_basis})",
        margin=goals_margin
    )
    if away_goals_signal:
        add_positive(*away_goals_signal)

    # Coarse Over/Under 2.5 direction, reused by the combo markets
    # below regardless of which specific line the ladder picked. The
    # goals-only fallback needs a wider buffer than the xG version for
    # the same reason as the ladder margin above.
    goals_combo_dir = None

    if use_xg:
        if combined_expected_goals >= 3.4:
            goals_combo_dir = "Over 2.5"
        elif combined_expected_goals <= 1.6:
            goals_combo_dir = "Under 2.5"
    else:
        if combined_expected_goals >= 3.8:
            goals_combo_dir = "Over 2.5"
        elif combined_expected_goals <= 1.2:
            goals_combo_dir = "Under 2.5"

    # -------------------------------------------------
    # CORNERS SIGNAL (Over/Under)
    # -------------------------------------------------
    # Expected total corners blends each team's own corner rate with
    # what their opponent's profile tends to concede, so a team that
    # wins a lot of corners *and* faces an opponent who concedes a lot
    # of corners pushes the total up (and vice versa for Under).

    corners_data_complete = all(
        v is not None
        for v in [
            h_corners_for, h_corners_against,
            a_corners_for, a_corners_against
        ]
    )

    if corners_data_complete:
        expected_corners = (
            h_corners_for + a_corners_against
            + a_corners_for + h_corners_against
        ) / 2

        # Require both teams' own tendencies to individually point the
        # same direction, not just a combined average — one lopsided
        # match shouldn't be enough to swing the whole signal.
        both_high = (
            h_corners_for >= 6.0
            and a_corners_against >= 6.0
            and a_corners_for >= 6.0
            and h_corners_against >= 6.0
        )
        both_low = (
            h_corners_for <= 4.0
            and a_corners_against <= 4.0
            and a_corners_for <= 4.0
            and h_corners_against <= 4.0
        )

        if expected_corners >= 13.0 and both_high:
            add_positive(
                2,
                f"Corners signal: likely Over 9.5 corners "
                f"(expected ~{expected_corners:.1f})",
                "corners_cards"
            )

        elif expected_corners <= 5.5 and both_low:
            add_positive(
                2,
                f"Corners signal: likely Under 8.5 corners "
                f"(expected ~{expected_corners:.1f})",
                "corners_cards"
            )

    # -------------------------------------------------
    # SHOTS ON TARGET SIGNAL (Over/Under)
    # -------------------------------------------------
    # Same shape as the corners signal: blend each team's own rate
    # with what their opponent tends to concede, and require both
    # teams' individual for/against numbers to independently agree
    # with the direction — not just a combined average one lopsided
    # match could produce.

    sot_data_complete = all(
        v is not None
        for v in [h_sot_for, h_sot_against, a_sot_for, a_sot_against]
    )

    if sot_data_complete:
        expected_sot = (
            h_sot_for + a_sot_against
            + a_sot_for + h_sot_against
        ) / 2

        sot_both_high = (
            h_sot_for >= 5.0
            and a_sot_against >= 5.0
            and a_sot_for >= 5.0
            and h_sot_against >= 5.0
        )
        sot_both_low = (
            h_sot_for <= 2.5
            and a_sot_against <= 2.5
            and a_sot_for <= 2.5
            and h_sot_against <= 2.5
        )

        if expected_sot >= 9.5 and sot_both_high:
            add_positive(
                2,
                f"Shots on Target signal: likely Over 7.5 "
                f"(expected ~{expected_sot:.1f})",
                "shots"
            )

        elif expected_sot <= 4.5 and sot_both_low:
            add_positive(
                2,
                f"Shots on Target signal: likely Under 6.5 "
                f"(expected ~{expected_sot:.1f})",
                "shots"
            )

    # -------------------------------------------------
    # CARDS SIGNAL (Over/Under)
    # -------------------------------------------------
    # Cards average alone is noisy (ref-dependent), so this only
    # fires when it's corroborated by a high combined fouls count.

    if (
        h_cards is not None
        and a_cards is not None
        and h_fouls is not None
        and a_fouls is not None
    ):
        expected_cards = h_cards + a_cards
        combined_fouls = h_fouls + a_fouls

        if expected_cards >= 6.0 and combined_fouls >= 26:
            add_positive(
                2,
                f"Cards signal: likely Over 3.5 cards "
                f"(expected ~{expected_cards:.1f}, "
                f"combined fouls ~{combined_fouls:.0f})",
                "corners_cards"
            )

        elif expected_cards <= 1.5 and combined_fouls <= 9:
            add_positive(
                2,
                f"Cards signal: likely Under 3.5 cards "
                f"(expected ~{expected_cards:.1f}, "
                f"combined fouls ~{combined_fouls:.0f})",
                "corners_cards"
            )

    # -------------------------------------------------
    # BTTS SIGNAL (Both Teams To Score)
    # -------------------------------------------------
    # Big chances created AND conceded by both sides is a better BTTS
    # predictor than raw goals, since it isn't skewed by finishing
    # variance the way goals-per-game can be.

    btts_data_complete = all(
        v is not None
        for v in [h_bc_for, a_bc_for, h_bc_against, a_bc_against]
    )

    # Big chances (like corners/cards) are a basic match stat Flashscore
    # publishes independently of whether it has an xG model for this
    # competition, so this doesn't need use_xg — it has its own data
    # quality bar via btts_data_complete + the thresholds below.
    btts_signal_fired = (
        btts_data_complete
        and h_g >= 1.5
        and a_g >= 1.5
        and h_bc_against >= 2.0
        and a_bc_against >= 2.0
    )

    if btts_signal_fired:
        add_positive(
            2,
            "BTTS signal: both teams create and concede "
            "big chances regularly",
            "goals"
        )

    # -------------------------------------------------
    # CLEAN SHEET / WIN TO NIL
    # -------------------------------------------------
    # A team keeps a clean sheet when the opponent rarely scores/
    # creates (goals and xG both low) and this team's own defence
    # holds up. Win to Nil layers that on top of an eligible win lean.

    if use_xg:
        home_clean_sheet = a_g <= 0.4 and h_gc <= 0.5 and a_xg <= 0.6
        away_clean_sheet = h_g <= 0.4 and a_gc <= 0.5 and h_xg <= 0.6
    else:
        # No xG to corroborate with, so lean on goals alone but with a
        # tighter bar to compensate.
        home_clean_sheet = a_g <= 0.25 and h_gc <= 0.35
        away_clean_sheet = h_g <= 0.25 and a_gc <= 0.35

    if home_clean_sheet:
        add_positive(
            2,
            f"{home} Clean Sheet signal: {away} rarely scores "
            f"and {home} defends well",
            "clean_sheet"
        )

    if away_clean_sheet:
        add_positive(
            2,
            f"{away} Clean Sheet signal: {home} rarely scores "
            f"and {away} defends well",
            "clean_sheet"
        )

    if home_clean_sheet and home_high_conf:
        add_positive(1, f"{home} Win to Nil signal", "clean_sheet")

    if away_clean_sheet and away_high_conf:
        add_positive(1, f"{away} Win to Nil signal", "clean_sheet")

    # A clean sheet on either side is a reasonable proxy for "BTTS No"
    # even when the BTTS-Yes conditions above didn't fire.
    btts_no_signal = home_clean_sheet or away_clean_sheet

    # -------------------------------------------------
    # COMBO MARKETS
    # -------------------------------------------------
    # 3 Way & BTTS, 1x2 & Total Goals, Total Goals & BTTS. Each only
    # fires when both halves of the combo have an actual directional
    # read — never guessed just to fill in a combo.

    if lean in ("home", "away"):
        combo_team = home if lean == "home" else away

        if btts_signal_fired:
            add_positive(3, f"Combo: {combo_team} win & BTTS Yes", "combo")
        elif btts_no_signal:
            add_positive(3, f"Combo: {combo_team} win & BTTS No", "combo")

        if goals_combo_dir:
            add_positive(
                3,
                f"Combo: {combo_team} win & {goals_combo_dir}",
                "combo"
            )

    if goals_combo_dir == "Over 2.5" and btts_signal_fired:
        add_positive(3, "Combo: Over 2.5 & BTTS Yes", "combo")

    elif goals_combo_dir == "Under 2.5" and btts_no_signal:
        add_positive(3, "Combo: Under 2.5 & BTTS No", "combo")

    # -------------------------------------------------
    # FINAL OUTPUT
    # -------------------------------------------------

    if not positive:
        return None

    positive.sort(key=lambda x: x[0])

    best_signal = positive[0][1]

    def fmt(v):
        return "N/A" if v is None else str(v)

    # Bucket every fired signal into its market category, preserving
    # the priority ordering already applied above within each bucket.
    grouped = {cat: [] for cat in CATEGORY_ORDER}
    for _, text, category in positive:
        grouped.setdefault(category, []).append(text)

    lines = [
        f"⚽ *{home} vs {away}*",
        "",
        "🎯 *Best signal*",
        best_signal,
        "",
    ]

    for cat in CATEGORY_ORDER:
        entries = grouped.get(cat) or []
        if not entries:
            continue
        lines.append(CATEGORY_LABELS[cat])
        lines.extend(f"• {e}" for e in entries)
        lines.append("")

    lines.append("📊 *Stats*")
    lines.append(
        f"{home}   G {h_g} | GA {h_gc} | GD {h_gd} | "
        f"xG {fmt(h_xg)} | xGA {fmt(h_xga)} | xGD {fmt(h_xgd)}"
    )
    lines.append(
        f"{away}   G {a_g} | GA {a_gc} | GD {a_gd} | "
        f"xG {fmt(a_xg)} | xGA {fmt(a_xga)} | xGD {fmt(a_xgd)}"
    )
    lines.append(
        f"Possession {fmt(h_poss)}% vs {fmt(a_poss)}%"
    )
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

    if warnings:
        lines.append(f"⚠️ *Cautions ({len(warnings)})*")
        lines.extend(f"• {w}" for w in warnings)
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

    FIXTURES_URL = (
        "https://www.flashscore.co.za/"
    )

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

    log.info("Starting Flashscore alert script...")
    log.info(f"Batch start={START}, limit={LIMIT}")

    # Declared before the try block so `finally` can safely check it even
    # if construction itself fails (see below).
    scraper = None

    try:
        # Building the scraper (Playwright start + browser launch) is now
        # INSIDE the try block. Previously this happened before the try,
        # so any launch failure (missing browser binaries, missing OS
        # deps, sandbox restrictions when run as root under cron, etc.)
        # crashed the whole process with no "❌ Job FAILED" alert and no
        # cleanup — you'd only ever see the "🚀 Job STARTED" message.
        scraper = FlashscoreGoalsScraper(headless=HEADLESS)

        log.info(f"Opening fixtures page: {FIXTURES_URL}")

        scraper.page.goto(
            FIXTURES_URL,
            wait_until="load",
            timeout=80000
        )

        time.sleep(3)
        scraper.accept_cookies()

        matches = scraper.discover_matches(
            TARGET_COUNT
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

        for idx, m_url in enumerate(
            batch_matches,
            start=START + 1
        ):

            log.info(f"Processing match {idx}: {m_url}")

            try:
                fixture = (
                    scraper.get_match_teams_and_links(
                        m_url
                    )
                )

                if (
                    not fixture
                    or not fixture["home_name"]
                    or not fixture["away_name"]
                ):

                    log.warning(
                        "Could not extract teams, skipping match"
                    )

                    continue

                home = fixture["home_name"]
                away = fixture["away_name"]

                home_data = scraper.analyze_team(
                    fixture["home_url"]
                )

                away_data = scraper.analyze_team(
                    fixture["away_url"]
                )

                if not home_data or not away_data:

                    log.warning(
                        "Could not analyze one or both teams, "
                        "skipping match"
                    )

                    continue

                msg = evaluate_bet_signals(
                    home,
                    away,
                    home_data,
                    away_data,
                    m_url
                )

                if msg:

                    log.info("ALERT:\n" + msg)

                    scraper.send_telegram_message(
                        msg,
                        BOT_TOKEN,
                        CHAT_ID
                    )

                else:

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
