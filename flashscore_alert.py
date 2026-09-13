import os
import argparse
import time
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

NAV_TIMEOUT_MS = 30000
STATS_CLICK_TIMEOUT_MS = 5000

MIN_SAMPLE_MATCHES = 6
MARGIN_TARGET = 2.0
XG_MARGIN_BUFFER = 0.9
GD_MARGIN_BUFFER = 1.3
MARGIN_SCORE_THRESHOLD = 4.0

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


def _block_heavy_resources(route):
    try:
        if route.request.resource_type in ("image", "media", "font"):
            route.abort()
        else:
            route.continue_()
    except Exception:
        pass


# ---------------- SCRAPER CLASS ----------------
class FlashscoreGoalsScraper:
    def __init__(self, headless=True):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
            ),
        )
        self.context.route("**/*", _block_heavy_resources)
        self.page = self.context.new_page()
        self.team_url = ""
        self.team_slug = ""
        self.team_label = ""

    def send_telegram_message(self, message, bot_token, chat_id):
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
            r = requests.post(url, data=payload, timeout=20)
            if r.status_code != 200:
                print(f"[WARN] Telegram error: {r.text}")
        except Exception as e:
            print(f"[ERROR] Failed to send Telegram message: {e}")

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
                return loc.first.inner_text().strip()
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

    def _wait_ready(self, selector, timeout=10000):
        try:
            self.page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        except Exception:
            pass

    def _parse_stat_value(self, text):
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
        selectors = ["h1", ".heading__name", ".participant__participantName a", ".participant__participantName"]
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
        print(f"[INFO] Opening results page: {url}")
        try:
            self.page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            self._wait_ready("h1")
            self.accept_cookies()
            page_name = self.get_team_name_from_page()
            if page_name:
                self.team_label = page_name
            return True
        except Exception as e:
            print(f"[ERROR] Failed to load results: {e}")
            return False

    def expand_hidden_matches(self, max_iterations=15):
        for _ in range(max_iterations):
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
                except Exception:
                    continue
            if clicked == 0:
                break
            time.sleep(1)

    def _is_match_upcoming(self, link):
        try:
            row = link.locator("xpath=ancestor::div[contains(@class,'event__match')][1]")
            if row.count() == 0:
                return True
            cls = row.first.get_attribute("class") or ""
            return "event__match--scheduled" in cls
        except Exception:
            return True

    def discover_matches(self, target_count, max_tries=100, only_upcoming=False):
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

                if full_url in seen or "?mid=" not in full_url:
                    continue

                if only_upcoming and not self._is_match_upcoming(link):
                    seen.add(full_url)
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

        print(f"[INFO] discover_matches for {self.team_label or self.team_slug!r}: found {len(matches)}/{target_count}")
        return matches

    def get_match_teams_and_links(self, match_url):
        try:
            self.page.goto(match_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            self._wait_ready(".duelParticipant__home .participant__participantName a")
        except Exception:
            return None

        home_name = self._safe_text(".duelParticipant__home .participant__participantName a")
        away_name = self._safe_text(".duelParticipant__away .participant__participantName a")
        home_href = self._safe_attr(".duelParticipant__home .participant__participantName a", "href")
        away_href = self._safe_attr(".duelParticipant__away .participant__participantName a", "href")

        return {
            "home_name": home_name,
            "away_name": away_name,
            "home_url": self._abs_url(home_href),
            "away_url": self._abs_url(away_href),
            "match_url": match_url,
        }

    def _empty_stat_result(self):
        result = {}
        for stat_key in STAT_LABEL_MAP.values():
            result[f"home_{stat_key}"] = None
            result[f"away_{stat_key}"] = None
        return result

    def _extract_stats_from_current_page(self):
        result = self._empty_stat_result()
        try:
            rows = self.page.locator("[data-testid='wcl-statistics']").all()
            for row in rows:
                try:
                    label = row.locator("[data-testid='wcl-statistics-category']").inner_text().strip()
                    stat_key = STAT_LABEL_MAP.get(label.lower())
                    if not stat_key:
                        continue
                    values = row.locator("[data-testid='wcl-statistics-value'] span").all()
                    if len(values) < 2:
                        continue
                    result[f"home_{stat_key}"] = self._parse_stat_value(values[0].inner_text())
                    result[f"away_{stat_key}"] = self._parse_stat_value(values[1].inner_text())
                except Exception:
                    continue
        except Exception:
            pass
        return result

    def get_match_data(self, match_url):
        try:
            self.page.goto(match_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            self._wait_ready(".duelParticipant__home .participant__participantName a")
        except Exception:
            return None

        score_home = None
        score_away = None
        try:
            score_spans = self.page.locator(".detailScore__wrapper span").all()
            if len(score_spans) >= 3:
                h = score_spans[0].inner_text().strip()
                d = score_spans[1].inner_text().strip()
                a = score_spans[2].inner_text().strip()
                if d == "-" and h.isdigit() and a.isdigit():
                    score_home = int(h)
                    score_away = int(a)
        except Exception:
            pass

        home = self._safe_text(".duelParticipant__home .participant__participantName a") or "?"
        away = self._safe_text(".duelParticipant__away .participant__participantName a") or "?"

        match_data = {
            "home": home,
            "away": away,
            "goals_home": score_home,
            "goals_away": score_away,
            "match_url": match_url,
        }
        match_data.update(self._empty_stat_result())

        try:
            self.page.locator("a[href*='summary/stats']").first.click(timeout=STATS_CLICK_TIMEOUT_MS)
            self._wait_ready("[data-testid='wcl-statistics']", timeout=STATS_CLICK_TIMEOUT_MS)
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
            if alias and self._team_match_score(candidate, alias) >= threshold:
                return True
        return False

    def _aliases(self):
        return [self.team_slug, self.team_label, self.slug_to_team_name(self.team_slug)]

    def calculate_team_goals(self, results):
        total_goals = 0
        matches_counted = 0
        aliases = self._aliases()

        for r in results:
            if self._team_matches(r.get("home", ""), aliases):
                total_goals += r.get("goals_home") or 0
                matches_counted += 1
            elif self._team_matches(r.get("away", ""), aliases):
                total_goals += r.get("goals_away") or 0
                matches_counted += 1

        avg_goals = total_goals / matches_counted if matches_counted > 0 else 0
        return {
            "team": self.team_label or self.team_slug,
            "avg_goals": round(avg_goals, 2),
            "matches": matches_counted,
        }

    def calculate_team_goals_conceded(self, results):
        total_conceded = 0
        counted = 0
        aliases = self._aliases()

        for r in results:
            if self._team_matches(r.get("home", ""), aliases):
                total_conceded += r.get("goals_away") or 0
                counted += 1
            elif self._team_matches(r.get("away", ""), aliases):
                total_conceded += r.get("goals_home") or 0
                counted += 1

        return round(total_conceded / counted, 2) if counted > 0 else 0

    def calculate_team_xg(self, results):
        total_xg = 0
        counted = 0
        aliases = self._aliases()

        for r in results:
            if self._team_matches(r.get("home", ""), aliases):
                if r.get("home_xg") is not None:
                    total_xg += r["home_xg"]
                    counted += 1
            elif self._team_matches(r.get("away", ""), aliases):
                if r.get("away_xg") is not None:
                    total_xg += r["away_xg"]
                    counted += 1

        return round(total_xg / counted, 2) if counted > 0 else None

    def calculate_team_xga(self, results):
        total_xga = 0
        counted = 0
        aliases = self._aliases()

        for r in results:
            if self._team_matches(r.get("home", ""), aliases):
                if r.get("away_xg") is not None:
                    total_xga += r["away_xg"]
                    counted += 1
            elif self._team_matches(r.get("away", ""), aliases):
                if r.get("home_xg") is not None:
                    total_xga += r["home_xg"]
                    counted += 1

        return round(total_xga / counted, 2) if counted > 0 else None

    def _team_stat_avg(self, results, stat_name, side="for"):
        total = 0
        counted = 0
        aliases = self._aliases()

        for r in results:
            is_home = self._team_matches(r.get("home", ""), aliases)
            is_away = not is_home and self._team_matches(r.get("away", ""), aliases)
            if not is_home and not is_away:
                continue

            if side == "for":
                value = r.get(f"home_{stat_name}") if is_home else r.get(f"away_{stat_name}")
            else:
                value = r.get(f"away_{stat_name}") if is_home else r.get(f"home_{stat_name}")

            if value is None:
                continue
            total += value
            counted += 1

        return round(total / counted, 2) if counted > 0 else None

    def analyze_team(self, team_url):
        if not self.open_team_results(team_url):
            return None

        matches = self.discover_matches(6)
        results = [self.get_match_data(url) for url in matches]
        results = [r for r in results if r]

        stats = self.calculate_team_goals(results)
        avg_gc = self.calculate_team_goals_conceded(results)
        avg_xg = self.calculate_team_xg(results)
        avg_xga = self.calculate_team_xga(results)

        stats.update({
            "avg_gc": avg_gc,
            "avg_xg": avg_xg,
            "avg_xga": avg_xga,
            "avg_corners_for": self._team_stat_avg(results, "corners", "for"),
            "avg_corners_against": self._team_stat_avg(results, "corners", "against"),
            "avg_big_chances_for": self._team_stat_avg(results, "big_chances", "for"),
            "avg_big_chances_against": self._team_stat_avg(results, "big_chances", "against"),
            "avg_yellow_cards": self._team_stat_avg(results, "yellow_cards", "for"),
            "avg_fouls": self._team_stat_avg(results, "fouls", "for"),
            "avg_xgot_for": self._team_stat_avg(results, "xgot", "for"),
            "avg_goals_prevented": self._team_stat_avg(results, "goals_prevented", "for"),
            "avg_shots_for": self._team_stat_avg(results, "shots", "for"),
            "avg_shots_against": self._team_stat_avg(results, "shots", "against"),
            "avg_sot_for": self._team_stat_avg(results, "shots_on_target", "for"),
            "avg_sot_against": self._team_stat_avg(results, "shots_on_target", "against"),
            "avg_possession": self._team_stat_avg(results, "possession", "for"),
        })

        return {"team": stats["team"], "results": results, "stats": stats}

    def close(self):
        try:
            self.browser.close()
            self.playwright.stop()
        except Exception:
            pass


# ---------------- SIGNAL ENGINE ----------------
# Single, focused prediction: HOME or AWAY team wins by a 2+ goal margin.
def _margin_score(h_sot_for, a_sot_against, h_bc_for, a_bc_against, a_bc_for, h_bc_against,
                   h_corners_for, a_corners_against, a_xgot_for, a_g, h_xg, h_xgot_for, a_gp):
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

    if a_bc_for is not None and h_bc_against is not None:
        if (a_bc_for + h_bc_against) / 2 <= 1.0:
            score += 1

    if h_corners_for is not None and a_corners_against is not None:
        expected_home_corners = (h_corners_for + a_corners_against) / 2
        if expected_home_corners >= 6.5:
            score += 1
        elif expected_home_corners >= 5.0:
            score += 0.5

    if h_xgot_for is not None and h_xg is not None and h_xgot_for >= h_xg + 0.3:
        score += 1

    if a_gp is not None and a_gp <= -0.2:
        score += 1

    if a_xgot_for is not None and a_g >= a_xgot_for + 0.8:
        score -= 1.5

    return score


def _escape_markdown(text):
    if text is None:
        return ""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def _margin_message(home, away, hs, as_, m_url, favourite_is_home):
    h_g, a_g = hs.get("avg_goals", 0), as_.get("avg_goals", 0)
    h_gc, a_gc = hs.get("avg_gc", 0), as_.get("avg_gc", 0)
    h_xg, a_xg = hs.get("avg_xg"), as_.get("avg_xg")
    h_xga, a_xga = hs.get("avg_xga"), as_.get("avg_xga")

    use_xg = None not in (h_xg, a_xg, h_xga, a_xga)
    margin_buffer = XG_MARGIN_BUFFER if use_xg else GD_MARGIN_BUFFER
    basis = "xG-based" if use_xg else "goals-based, no xG data"

    if use_xg:
        expected_home_goals = (h_xg + a_xga) / 2
        expected_away_goals = (a_xg + h_xga) / 2
    else:
        expected_home_goals = (h_g + a_gc) / 2
        expected_away_goals = (a_g + h_gc) / 2

    if favourite_is_home:
        expected_margin = expected_home_goals - expected_away_goals
        hard_filters_pass = (
            expected_home_goals >= 2.0
            and expected_away_goals <= 1.1
            and h_gc <= 1.1
            and a_g < 1.1
            and expected_margin >= (MARGIN_TARGET + margin_buffer)
        )
        favourite, underdog = home, away
    else:
        expected_margin = expected_away_goals - expected_home_goals
        hard_filters_pass = (
            expected_away_goals >= 2.0
            and expected_home_goals <= 1.1
            and a_gc <= 1.1
            and h_g < 1.1
            and expected_margin >= (MARGIN_TARGET + margin_buffer)
        )
        favourite, underdog = away, home

    if not hard_filters_pass:
        return None

    if favourite_is_home:
        margin_score = _margin_score(
            hs.get("avg_sot_for"), as_.get("avg_sot_against"),
            hs.get("avg_big_chances_for"), as_.get("avg_big_chances_against"),
            as_.get("avg_big_chances_for"), hs.get("avg_big_chances_against"),
            hs.get("avg_corners_for"), as_.get("avg_corners_against"),
            as_.get("avg_xgot_for"), a_g,
            h_xg, hs.get("avg_xgot_for"), as_.get("avg_goals_prevented"),
        )
    else:
        margin_score = _margin_score(
            as_.get("avg_sot_for"), hs.get("avg_sot_against"),
            as_.get("avg_big_chances_for"), hs.get("avg_big_chances_against"),
            hs.get("avg_big_chances_for"), as_.get("avg_big_chances_against"),
            as_.get("avg_corners_for"), hs.get("avg_corners_against"),
            hs.get("avg_xgot_for"), h_g,
            a_xg, as_.get("avg_xgot_for"), hs.get("avg_goals_prevented"),
        )

    if margin_score < MARGIN_SCORE_THRESHOLD:
        return None

    home_e, away_e = _escape_markdown(home), _escape_markdown(away)
    favourite_e = _escape_markdown(favourite)

    return (
        f"⚽ *{home_e} vs {away_e}*\n\n"
        f"🎯 *Prediction: {favourite_e} to win by 2+ goals* ({basis})\n"
        f"Expected margin ~{expected_margin:.2f} "
        f"(home ~{expected_home_goals:.2f}, away ~{expected_away_goals:.2f}) "
        f"| corroboration score {margin_score:.1f}\n\n"
        f"📊 *Stats*\n"
        f"{home_e}   G {h_g} | GA {h_gc} | xG {h_xg if h_xg is not None else 'N/A'} | xGA {h_xga if h_xga is not None else 'N/A'}\n"
        f"{away_e}   G {a_g} | GA {a_gc} | xG {a_xg if a_xg is not None else 'N/A'} | xGA {a_xga if a_xga is not None else 'N/A'}\n\n"
        f"🔗 {m_url}"
    )


def evaluate_margin_signal(home, away, home_data, away_data, m_url):
    hs = home_data["stats"]
    as_ = away_data["stats"]

    if hs.get("matches", 0) < MIN_SAMPLE_MATCHES or as_.get("matches", 0) < MIN_SAMPLE_MATCHES:
        return None

    return _margin_message(home, away, hs, as_, m_url, favourite_is_home=True) or \
        _margin_message(home, away, hs, as_, m_url, favourite_is_home=False)


# ---------------- ALERT SCRIPT ----------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    START = max(0, args.start)
    LIMIT = max(1, args.limit)

    BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
    CHAT_ID = os.getenv("CHAT_ID", "").strip()
    FIXTURES_URL = "https://www.flashscore.co.za/"

    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] BOT_TOKEN or CHAT_ID is missing from environment variables.")
        return

    print(f"[INFO] Starting Flashscore alert script (start={START}, limit={LIMIT})...")
    scraper = FlashscoreGoalsScraper(headless=True)

    try:
        print(f"[INFO] Opening fixtures page: {FIXTURES_URL}")
        scraper.page.goto(FIXTURES_URL, wait_until="load", timeout=NAV_TIMEOUT_MS)
        scraper._wait_ready("a[href*='/match/']")
        scraper.accept_cookies()

        matches = scraper.discover_matches(START + LIMIT, only_upcoming=True)
        batch_matches = matches[START:START + LIMIT]
        print(f"[INFO] Processing {len(batch_matches)} matches ({START} to {START + LIMIT - 1})")

        for idx, m_url in enumerate(batch_matches, start=START + 1):
            print(f"[INFO] Processing match {idx}: {m_url}")

            try:
                fixture = scraper.get_match_teams_and_links(m_url)
                if not fixture or not fixture["home_name"] or not fixture["away_name"]:
                    print("[WARN] Could not extract teams, skipping match")
                    continue

                home = fixture["home_name"]
                away = fixture["away_name"]

                home_data = scraper.analyze_team(fixture["home_url"])
                away_data = scraper.analyze_team(fixture["away_url"])

                if not home_data or not away_data:
                    print("[WARN] Could not analyze one or both teams, skipping match")
                    continue

                msg = evaluate_margin_signal(home, away, home_data, away_data, m_url)

                if msg:
                    print(f"[ALERT]\n{msg}")
                    scraper.send_telegram_message(msg, BOT_TOKEN, CHAT_ID)
                else:
                    print("[INFO] No signal.")

            except Exception as match_err:
                print(f"[ERROR] Error processing match {m_url}: {match_err}")
                continue

    except Exception as e:
        print(f"[ERROR] Job failed: {e}")

    finally:
        print("[INFO] Closing browser...")
        scraper.close()
        print("[INFO] Script finished.")


if __name__ == "__main__":
    main()
