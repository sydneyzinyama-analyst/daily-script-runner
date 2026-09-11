import os
import sys
import argparse
import logging
import threading
import queue
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
# Added while chasing a "this got much slower all of a sudden in Actions"
# regression: navigations were pinned at a flat 90s timeout with zero
# timing/visibility into where time was actually going, so a single
# slow/challenged page load was indistinguishable from a healthy one
# until it either finished or ate a full 90s. These make that visible
# and cap the worst case instead of silently absorbing it.
#
# Lower this to fail faster once you've confirmed pages are genuinely
# hanging rather than just slow; raise it back if you start seeing
# false-negative timeouts on a healthy-but-slow connection.
NAV_TIMEOUT_MS = int(os.getenv("SCRAPER_NAV_TIMEOUT_MS", "45000"))

# Hard ceiling on how long discover_matches (and the expand_hidden_matches
# loop it calls) is allowed to spend scrolling/expanding for one team,
# on top of the existing max_tries cap. Without this, a page whose
# "display matches" markup no longer matches our selector can spin
# through all 250 tries at ~2s of sleep each — worst case ~8+ minutes —
# per team, invisibly.
DISCOVER_TIME_BUDGET_SEC = int(os.getenv("SCRAPER_DISCOVER_BUDGET_SEC", "60"))

# Timeout for get_match_data's in-page Stats-tab click + the wait for
# the stats table to render. Was a hardcoded 5s click + 15s wait (20s
# worst case) with zero logging — invisible in the log as a multi-
# minute gap once it happened on several matches in a row. Now timed
# (see get_match_data) and tunable; small/regional-league matches
# often have no Stats tab at all, so hitting this timeout is expected
# to happen sometimes, not necessarily a bug.
STATS_TAB_TIMEOUT_MS = int(os.getenv("SCRAPER_STATS_TAB_TIMEOUT_MS", "8000"))

# How many analyze_team() calls a single browser process handles
# before FlashscoreGoalsScraper.maybe_recycle_browser() closes and
# relaunches it. See maybe_recycle_browser's docstring for the
# investigation that motivated this — was previously implicitly "1"
# (a fresh browser process per team, per match), which is what led
# to the resource exhaustion this whole mechanism replaces.
BROWSER_RECYCLE_EVERY = int(os.getenv("SCRAPER_BROWSER_RECYCLE_EVERY", "40"))

# Title substrings seen on common bot-mitigation interstitials
# (Cloudflare, DataDome, generic "checking your browser" pages). If a
# page we expect to be a normal Flashscore page shows one of these
# instead, that's a strong signal we're being challenged/slow-walked
# rather than just experiencing normal latency.
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
        self.headless = headless
        self.playwright = sync_playwright().start()
        self.browser = None
        self.context = None
        self.page = None
        self._session_count = 0
        self._launch_browser()
        self.team_url = ""
        self.team_slug = ""
        self.team_label = ""

    def _launch_browser(self):
        # Factored out of __init__ so maybe_recycle_browser (below) can
        # relaunch with identical settings instead of duplicating them.
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
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
        self._session_count = 0

    def maybe_recycle_browser(self):
        # Was: a brand-new FlashscoreGoalsScraper (full Playwright +
        # Chromium launch) built and torn down for every single team,
        # every single match — up to ~200 browser launches in a
        # 100-match batch. A logged run showed this snowballing partway
        # through a batch into multi-hundred/multi-thousand-second
        # stalls on operations budgeted at 8s, with unrelated matches
        # stalling for near-identical durations simultaneously — the
        # signature of host-level resource starvation (process/memory
        # accumulation), not per-page network issues. The fix: reuse
        # one browser across many teams' analyze_team() calls (call
        # this between them) instead of relaunching per team, with a
        # periodic full recycle as a safety net against any slower
        # leak inside a single very-long-lived Chromium process.
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

    def _timed_goto(self, url, **kwargs):
        # Every navigation in this class routes through here now so a
        # slow run's log shows exactly which page loads are the ones
        # ballooning, instead of the whole discover/analyze pipeline
        # being one opaque gap between log lines. kwargs are passed
        # straight through to page.goto (wait_until, timeout, ...).
        t0 = time.time()
        try:
            self.page.goto(url, **kwargs)
            elapsed = time.time() - t0
            log.info(f"goto {url} took {elapsed:.1f}s")
        except Exception as e:
            elapsed = time.time() - t0
            log.warning(f"goto {url} failed after {elapsed:.1f}s: {e}")
            raise

    def _check_bot_challenge(self, context_label=""):
        # Cheap check for a bot-mitigation interstitial: if we're
        # sitting on a "Just a moment..."-style page instead of the
        # real Flashscore page, every downstream selector wait will
        # silently time out and *look* like ordinary slowness. This
        # makes that distinguishable in the log instead of guessing.
        try:
            title = (self.page.title() or "").strip()
        except Exception:
            return False

        if any(marker in title.lower() for marker in BOT_CHALLENGE_MARKERS):
            log.warning(
                f"POSSIBLE BOT CHALLENGE{' (' + context_label + ')' if context_label else ''}: "
                f"page title={title!r} url={self.page.url!r}"
            )
            return True

        return False

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
            self._timed_goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            self._wait_ready("h1", timeout=10000)
            self._check_bot_challenge(f"results page for {self.team_slug}")
            self.accept_cookies()
            page_name = self.get_team_name_from_page()
            if page_name:
                self.team_label = page_name
            return True
        except Exception as e:
            log.error(f"Failed to load results: {e}")
            return False

    def expand_hidden_matches(self):
        # Bounded by both an iteration count and a wall-clock budget
        # (whichever hits first) so a "display matches" button that
        # keeps reappearing — e.g. after a markup change — can't turn
        # this into an unbounded loop hiding inside discover_matches'
        # own retry budget.
        t0 = time.time()
        iterations = 0
        max_iterations = 40

        try:
            while True:
                if time.time() - t0 > DISCOVER_TIME_BUDGET_SEC:
                    log.warning(
                        f"expand_hidden_matches hit its "
                        f"{DISCOVER_TIME_BUDGET_SEC}s time budget after "
                        f"{iterations} iterations, giving up for this pass"
                    )
                    break

                if iterations >= max_iterations:
                    log.warning(
                        f"expand_hidden_matches hit max_iterations="
                        f"{max_iterations}, giving up for this pass"
                    )
                    break

                iterations += 1

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

    def _is_match_upcoming(self, link):
        """
        True if `link` (an <a href*='/match/'> Locator on the fixtures
        page) belongs to a match that hasn't started yet. Flashscore
        tags each match row's class with 'event__match--scheduled' for
        not-yet-started, 'event__match--live' for in progress, and
        neither for already-finished (row text starts "Finished", full
        score already filled in) — confirmed against the live site.

        Only meaningful on the fixtures/upcoming page. discover_matches
        is also used against a team's *results* page to pull their past
        6 matches for stats (see analyze_team) — those rows are
        supposed to be finished, so this check is opt-in via
        only_upcoming rather than applied unconditionally.

        Fails open (returns True, i.e. don't skip) on any lookup
        failure or unrecognized row structure — the existing behavior
        of analyzing a match we shouldn't have is far less bad than
        silently dropping matches because a markup detail shifted.
        """
        try:
            row = link.locator(
                "xpath=ancestor::div[contains(@class,'event__match')][1]"
            )
            if row.count() == 0:
                return True

            cls = row.first.get_attribute("class") or ""
            return "event__match--scheduled" in cls
        except Exception:
            return True

    def discover_matches(self, target_count, max_tries=250, only_upcoming=False):
        matches = []
        seen = set()
        tries = 0
        skipped_not_upcoming = 0
        t0 = time.time()

        while len(matches) < target_count and tries < max_tries:
            if time.time() - t0 > DISCOVER_TIME_BUDGET_SEC:
                log.warning(
                    f"discover_matches for {self.team_label or self.team_slug!r} "
                    f"hit its {DISCOVER_TIME_BUDGET_SEC}s time budget after "
                    f"{tries} tries with {len(matches)}/{target_count} found "
                    f"— stopping early instead of grinding to max_tries"
                )
                break

            self.expand_hidden_matches()

            links = self.page.locator("a[href*='/match/'][href*='?mid=']").all()
            for link in links:
                href = link.get_attribute("href")
                if not href:
                    continue

                href = href.split("/tv")[0].split("#")[0]
                full_url = self._abs_url(href)

                if full_url in seen or "?mid=" not in full_url:
                    continue

                if only_upcoming and not self._is_match_upcoming(link):
                    # Already live or finished — no point spending a
                    # full analyze_team() pass predicting a match that
                    # has already happened. Still mark as seen so we
                    # don't keep re-checking the same row every retry
                    # pass, but don't count it toward target_count.
                    seen.add(full_url)
                    skipped_not_upcoming += 1
                    continue

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

        log.info(
            f"discover_matches for {self.team_label or self.team_slug!r}: "
            f"found {len(matches)}/{target_count} in {tries} tries, "
            f"{time.time()-t0:.1f}s"
            + (
                f", skipped {skipped_not_upcoming} already-started/finished"
                if only_upcoming
                else ""
            )
        )
        return matches

    def get_match_teams_and_links(self, match_url):
        try:
            self._timed_goto(
                match_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS
            )
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
            self._timed_goto(
                stats_url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT_MS
            )
            self._wait_ready("[data-testid='wcl-statistics']")

        except Exception:
            return result

        result.update(self._extract_stats_from_current_page())
        return result

    def get_match_goals(self, match_url):
        try:
            self._timed_goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT_MS
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
            self._timed_goto(
                match_url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT_MS
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

        # This click+wait pair was the last unlogged step in the whole
        # pipeline — every navigation goes through _timed_goto now, but
        # this in-page tab switch doesn't navigate, so a slow/failed
        # click here was invisible in the log: a run could show a
        # multi-minute gap between two goto lines with nothing to
        # explain it. Timed explicitly so that gap has a cause now.
        t0 = time.time()
        try:
            self.page.locator("a[href*='summary/stats']").first.click(
                timeout=STATS_TAB_TIMEOUT_MS
            )
            self._wait_ready(
                "[data-testid='wcl-statistics']", timeout=STATS_TAB_TIMEOUT_MS
            )
            match_data.update(self._extract_stats_from_current_page())
            log.info(
                f"stats tab for {match_url} took {time.time()-t0:.1f}s"
            )
        except Exception as e:
            log.warning(
                f"stats tab for {match_url} failed after "
                f"{time.time()-t0:.1f}s: {e}"
            )

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
        # Logged so total per-team time is visible even though the two
        # teams' analyze_team calls run concurrently in separate threads
        # and their goto/discover_matches lines interleave in the log —
        # this line bounds where each team's slice actually started and
        # ended.
        t0 = time.time()

        if not self.open_team_results(team_url):
            return None

        matches = self.discover_matches(6)
        results = []

        for url in matches:
            match_data = self.get_match_data(url)

            if match_data:
                results.append(match_data)

        log.info(
            f"analyze_team({self.team_label or self.team_slug!r}): "
            f"{len(results)}/{len(matches)} matches fetched in "
            f"{time.time()-t0:.1f}s total"
        )

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

    # Declared before the try block so `finally` can safely check them
    # even if construction itself fails (see below).
    #
    # NOTE on the per-match parallel fetch below: Playwright's sync API
    # only supports one sync_playwright() instance per OS thread — a
    # second one in the *same* thread throws "using Playwright Sync API
    # inside the asyncio loop", even with no actual threading involved
    # yet. So `home`/`away` team analysis each need their own dedicated
    # OS thread. Previously each of those threads built and tore down a
    # brand-new FlashscoreGoalsScraper (a full Playwright + Chromium
    # launch) *per match* — up to ~200 browser launches in a 100-match
    # batch. A logged run showed that snowballing partway through a
    # batch into multi-hundred/multi-thousand-second stalls on
    # operations budgeted at 8s, with unrelated matches stalling for
    # near-identical durations simultaneously — host-level resource
    # starvation from the accumulated launches, not per-page network
    # issues. Fix: one persistent worker thread per side, each owning
    # ONE scraper (see maybe_recycle_browser) for the whole batch,
    # pulling team URLs off a queue instead of being spawned fresh
    # every match.
    scraper = None
    home_thread = None
    away_thread = None
    home_queue_in = queue.Queue()
    home_queue_out = queue.Queue()
    away_queue_in = queue.Queue()
    away_queue_out = queue.Queue()

    def _team_worker(queue_in, queue_out, label):
        worker_scraper = None
        try:
            worker_scraper = FlashscoreGoalsScraper(headless=HEADLESS)
            while True:
                team_url = queue_in.get()
                if team_url is None:
                    break
                try:
                    worker_scraper.maybe_recycle_browser()
                    data = worker_scraper.analyze_team(team_url)
                    queue_out.put((data, None))
                except Exception as e:
                    queue_out.put((None, e))
        finally:
            if worker_scraper is not None:
                worker_scraper.close()
            _log_resource_usage(f"{label} worker exiting")

    try:
        # Building the scraper (Playwright start + browser launch) is now
        # INSIDE the try block. Previously this happened before the try,
        # so any launch failure (missing browser binaries, missing OS
        # deps, sandbox restrictions when run as root under cron, etc.)
        # crashed the whole process with no "❌ Job FAILED" alert and no
        # cleanup — you'd only ever see the "🚀 Job STARTED" message.
        scraper = FlashscoreGoalsScraper(headless=HEADLESS)

        # Started here (before fixtures discovery, which itself can take
        # up to a minute — see DISCOVER_TIME_BUDGET_SEC) so their own
        # browser launches overlap with that instead of adding to the
        # critical path.
        home_thread = threading.Thread(
            target=_team_worker,
            args=(home_queue_in, home_queue_out, "home"),
            daemon=True,
        )
        away_thread = threading.Thread(
            target=_team_worker,
            args=(away_queue_in, away_queue_out, "away"),
            daemon=True,
        )
        home_thread.start()
        away_thread.start()

        _log_resource_usage("job start, after fixtures browser launch")

        log.info(f"Opening fixtures page: {FIXTURES_URL}")

        scraper._timed_goto(
            FIXTURES_URL,
            wait_until="load",
            timeout=NAV_TIMEOUT_MS
        )

        scraper._wait_ready("a[href*='/match/']")
        scraper._check_bot_challenge("fixtures page")
        scraper.accept_cookies()

        matches = scraper.discover_matches(
            TARGET_COUNT,
            # Only fixtures that haven't kicked off yet — no point
            # spending a full analysis on a match that's already live
            # or finished (see _is_match_upcoming). Left False (default)
            # everywhere else discover_matches is called — e.g. inside
            # analyze_team, pulling a team's past 6 results, which are
            # *supposed* to already be finished.
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

                # Hand both teams off to their persistent worker threads
                # (started once, before the match loop — see the note
                # above `scraper = FlashscoreGoalsScraper(...)`) and
                # block until both results are back. Still concurrent
                # per match, just without relaunching a browser to do it.
                home_queue_in.put(fixture["home_url"])
                away_queue_in.put(fixture["away_url"])

                home_data, home_error = home_queue_out.get()
                away_data, away_error = away_queue_out.get()

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

        # Signal both persistent team-analysis workers to stop (they
        # each close their own scraper/browser in _team_worker's own
        # `finally` block on seeing this sentinel) and wait for that to
        # happen before the process exits.
        try:
            if home_thread is not None and home_thread.is_alive():
                home_queue_in.put(None)
            if away_thread is not None and away_thread.is_alive():
                away_queue_in.put(None)
            if home_thread is not None:
                home_thread.join(timeout=30)
            if away_thread is not None:
                away_thread.join(timeout=30)
        except Exception as e:
            log.warning(f"Error shutting down team worker threads: {e}")

        log.info("Script finished.")


if __name__ == "__main__":
    main()
