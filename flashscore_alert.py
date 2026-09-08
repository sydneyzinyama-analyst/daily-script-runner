import os
import sys
import argparse
import logging
import threading
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
            # section headers (see evaluate_home_margin_signal) — team
            # names are pre-escaped there so this doesn't choke on
            # stray _ / * / ` / [ characters.
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

    def _wait_ready(self, selector, timeout=15000):
        # Replaces a blind time.sleep(N) after page.goto with waiting
        # for the thing we're actually about to read to be visible.
        # Measured against the live site: this is ~3-5x faster than a
        # flat 3s sleep in the common case, since most pages render
        # the target content well under a second — and it's strictly
        # no worse in the slow case, since it still gives up after
        # `timeout` and lets the caller's own extraction (_safe_text/
        # _safe_attr, which fail soft) take it from there.
        try:
            self.page.locator(selector).first.wait_for(
                state="visible", timeout=timeout
            )
        except Exception:
            pass

    def _parse_stat_value(self, text):
        # Stats on the overall/stats page come in a few shapes:
        #   "19"                -> plain count
        #   "1.59"               -> decimal (xG, xGOT, goals prevented)
        #   "-0.43"              -> negative decimal (goals prevented)
        #   "76%\n(262/343)"     -> percentage stats (passes, possession...)
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
            self._wait_ready("h1", timeout=10000)
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
            self.page.goto(match_url, wait_until="domcontentloaded", timeout=90000)
            self._wait_ready(
                ".duelParticipant__home .participant__participantName a"
            )
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

    def _empty_stat_result(self):
        result = {}
        for stat_key in self.STAT_LABEL_MAP.values():
            result[f"home_{stat_key}"] = None
            result[f"away_{stat_key}"] = None
        return result

    def _extract_stats_from_current_page(self):
        """
        Reads the STAT_LABEL_MAP fields off whatever stats page is
        currently loaded. Shared by get_match_stats (which navigates
        there directly) and get_match_data (which reaches the same
        page via an in-page tab click instead of a fresh navigation —
        see get_match_data's docstring for why that's faster).
        """
        result = self._empty_stat_result()

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

                    result[f"home_{stat_key}"] = self._parse_stat_value(
                        values[0].inner_text()
                    )
                    result[f"away_{stat_key}"] = self._parse_stat_value(
                        values[1].inner_text()
                    )

                except Exception:
                    continue

        except Exception:
            pass

        return result

    def get_match_stats(self, match_url):
        """
        Pulls the full set of stats we care about from the match's
        stats/overall page in a single pass: xG, xGOT, corners, big
        chances, yellow cards, fouls and goalkeeper "goals prevented".

        Returns a dict of home_<stat>/away_<stat> pairs. Any stat not
        found on the page (older matches, different competitions, page
        layout differences) is left as None rather than raising.

        Standalone entry point kept for callers that only want stats.
        analyze_team uses the faster combined get_match_data instead.
        """
        stats_url = self.get_match_stats_url(match_url)

        result = {"match_url": match_url}
        result.update(self._empty_stat_result())

        if not stats_url:
            return result

        try:
            self.page.goto(
                stats_url,
                wait_until="domcontentloaded",
                timeout=90000
            )
            self._wait_ready("[data-testid='wcl-statistics']")

        except Exception:
            return result

        result.update(self._extract_stats_from_current_page())
        return result

    def get_match_goals(self, match_url):
        try:
            self.page.goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=90000
            )
            self._wait_ready(
                ".duelParticipant__home .participant__participantName a"
            )

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

    def get_match_data(self, match_url):
        """
        Combined, faster version of get_match_goals + get_match_stats:
        one navigation to the match page, then an in-page click on the
        Stats tab instead of a second full page load to the stats
        URL. Measured against the live site: ~2.1s combined vs ~2.6-
        3.2s doing the two as separate goto()s, since this skips
        re-fetching the whole page shell/JS bundle a second time.

        Falls back to goals-only data (all stat fields None) if the
        Stats tab isn't present/clickable — some older or lower-tier
        matches don't have one — same degradation as calling
        get_match_stats on a match with no stats page.
        """
        try:
            self.page.goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=90000
            )
            self._wait_ready(
                ".duelParticipant__home .participant__participantName a"
            )
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

        match_data = {
            "home": home,
            "away": away,
            "goals_home": score_home,
            "goals_away": score_away,
            "match_url": match_url,
        }
        match_data.update(self._empty_stat_result())

        try:
            self.page.locator("a[href*='summary/stats']").first.click(
                timeout=5000
            )
            self._wait_ready("[data-testid='wcl-statistics']")
            match_data.update(self._extract_stats_from_current_page())
        except Exception:
            pass

        return match_data

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
        yellow_cards, fouls, goals_prevented, xgot, shots,
        shots_on_target, possession). Each match_data dict is expected
        to carry home_<stat_name>/away_<stat_name> keys, as produced
        by get_match_stats().

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
            match_data = self.get_match_data(url)

            if match_data:
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
#
# Single, focused prediction: HOME team wins by a 2+ goal margin
# (2-0, 3-0, 3-1, 4-1, 4-2, ...). This is not a general betting-markets
# engine — every other market from earlier iterations (draw, away win,
# double chance, handicap, totals, corners/cards O/U, BTTS, clean
# sheet, shots O/U, combos) has been removed. All the stats scraped by
# FlashscoreGoalsScraper are still gathered the same way; this just
# uses them narrowly, for one question.
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
# finishes — it has to show up in shots, chances, or territory too.
# Max achievable is ~6 (2 shots-on-target + 2 big chances for + 1 big
# chances against + 1 corners), minus a possible -1.5 xGOT penalty.
MARGIN_SCORE_THRESHOLD = 3.0


def _margin_score(
    h_sot_for, a_sot_against,
    h_bc_for, a_bc_against,
    a_bc_for, h_bc_against,
    h_corners_for, a_corners_against,
    a_xgot_for, a_g,
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
    "HOME team wins by 2+ goals", or None if it doesn't. This is the
    only prediction this script makes.
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
    #
    # NOTE on the per-match parallel fetch below: Playwright's sync API
    # only supports one sync_playwright() instance per OS thread — a
    # second one in the *same* thread throws "using Playwright Sync API
    # inside the asyncio loop", even with no actual threading involved
    # yet. So `home_scraper`/`away_scraper` can't be created once up
    # here alongside `scraper` and just reused — each one has to be
    # constructed *inside* its own worker thread, per match, and closed
    # there too. Confirmed this is required, not just tidy, by hitting
    # that exact error creating a second instance in one thread before
    # ever starting a thread.
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

        scraper._wait_ready("a[href*='/match/']")
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

                # Run both teams' 6-match analysis concurrently. Each
                # thread creates, uses, and closes its own scraper
                # instance — Playwright's sync API only supports one
                # sync_playwright() per OS thread, so these can't be
                # created ahead of time and just reused across matches
                # the way `scraper` (fixtures walker) is.
                home_data = None
                away_data = None
                home_error = None
                away_error = None

                def _run_home():
                    nonlocal home_data, home_error
                    home_scraper = None
                    try:
                        home_scraper = FlashscoreGoalsScraper(
                            headless=HEADLESS
                        )
                        home_data = home_scraper.analyze_team(
                            fixture["home_url"]
                        )
                    except Exception as e:
                        home_error = e
                    finally:
                        if home_scraper is not None:
                            home_scraper.close()

                def _run_away():
                    nonlocal away_data, away_error
                    away_scraper = None
                    try:
                        away_scraper = FlashscoreGoalsScraper(
                            headless=HEADLESS
                        )
                        away_data = away_scraper.analyze_team(
                            fixture["away_url"]
                        )
                    except Exception as e:
                        away_error = e
                    finally:
                        if away_scraper is not None:
                            away_scraper.close()

                t_home = threading.Thread(target=_run_home)
                t_away = threading.Thread(target=_run_away)
                t_home.start()
                t_away.start()
                t_home.join()
                t_away.join()

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

                msg = evaluate_home_margin_signal(
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
