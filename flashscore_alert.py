from playwright.sync_api import sync_playwright
from urllib.parse import urlparse
from difflib import SequenceMatcher
import time
import re
import unicodedata
import requests


# ---------------- SCRAPER CLASS ----------------
class FlashscoreGoalsScraper:
    def __init__(self, headless=True):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=headless)
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
            payload = {
                "chat_id": chat_id,
                "text": message
            }
            r = requests.post(url, data=payload, timeout=20)
            if r.status_code != 200:
                print("[WARN] Telegram error:", r.text)
        except Exception as e:
            print("[ERROR] Failed to send Telegram message:", e)

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
        except:
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
            loc = self.page.locator(selector).first
            if loc.count() > 0:
                return loc.inner_text().strip()
        except:
            pass
        return ""

    def _safe_attr(self, selector, attr_name="href"):
        try:
            loc = self.page.locator(selector).first
            if loc.count() > 0:
                val = loc.get_attribute(attr_name)
                if val:
                    return val
        except:
            pass
        return ""

    def get_team_name_from_page(self):
        selectors = [
            "h1",
            ".heading__name",
            ".participant__participantName a",
            ".participant__participantName",
        ]
        for selector in selectors:
            try:
                loc = self.page.locator(selector).first
                if loc.count() > 0:
                    text = loc.inner_text().strip()
                    if text:
                        text = re.sub(r"^Soccer:\s*", "", text, flags=re.IGNORECASE)
                        text = re.sub(r"\s+results?\s*$", "", text, flags=re.IGNORECASE)
                        return text
            except:
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
            self.page.goto(url, wait_until="load", timeout=90000)
            time.sleep(3)
            page_name = self.get_team_name_from_page()
            if page_name:
                self.team_label = page_name
            return True
        except Exception as e:
            print(f"[ERROR] Failed to load results: {e}")
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
                        print(f"[WARN] Skipping button {i}:", e)

                if clicked == 0:
                    break

                time.sleep(1)

        except Exception as e:
            print("[WARN] expand_hidden_matches failed:", e)

    def get_last_6_matches(self):
        try:
            self.page.mouse.wheel(0, 4000)
            time.sleep(2)
        except:
            pass

        matches = []
        links = self.page.locator("a[href*='/match/'][href*='?mid=']").all()
        for link in links:
            href = link.get_attribute("href")
            if not href:
                continue
            href = href.split("/tv")[0].split("#")[0]
            full_url = self._abs_url(href)
            if full_url not in matches and "?mid=" in full_url:
                matches.append(full_url)
            if len(matches) == 6:
                break
        return matches

    def get_match_teams_and_links(self, match_url):
        try:
            self.page.goto(match_url, wait_until="networkidle", timeout=90000)
            time.sleep(3)
        except:
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

    def get_match_stats_url(self, match_url):
        try:
            if "?mid=" not in match_url:
                return None
            base = match_url.split("?mid=")[0]
            mid = match_url.split("?mid=")[1]
            return f"{base}summary/stats/overall/?mid={mid}"
        except:
            return None

    def get_match_xg(self, match_url):
        stats_url = self.get_match_stats_url(match_url)
        if not stats_url:
            return {"home_xg": None, "away_xg": None, "match_url": match_url}

        try:
            self.page.goto(stats_url, wait_until="networkidle", timeout=90000)
            time.sleep(3)
        except:
            return {"home_xg": None, "away_xg": None, "match_url": match_url}

        try:
            rows = self.page.locator("[data-testid='wcl-statistics']").all()

            for row in rows:
                try:
                    label = row.locator("[data-testid='wcl-statistics-category']").inner_text().strip()

                    if "Expected goals" in label:
                        values = row.locator("[data-testid='wcl-statistics-value'] span").all()
                        if len(values) >= 2:
                            home_xg = float(values[0].inner_text().strip())
                            away_xg = float(values[1].inner_text().strip())
                            return {
                                "home_xg": home_xg,
                                "away_xg": away_xg,
                                "match_url": match_url
                            }
                except:
                    continue

            return {"home_xg": None, "away_xg": None, "match_url": match_url}

        except:
            return {"home_xg": None, "away_xg": None, "match_url": match_url}

    def get_match_goals(self, match_url):
        try:
            self.page.goto(match_url, wait_until="networkidle", timeout=90000)
            time.sleep(3)
        except:
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
        except:
            pass

        home = self._safe_text(".duelParticipant__home .participant__participantName a") or "?"
        away = self._safe_text(".duelParticipant__away .participant__participantName a") or "?"

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
        aliases = [self.team_slug, self.team_label, self.slug_to_team_name(self.team_slug)]

        for r in results:
            home_team = r.get("home", "")
            away_team = r.get("away", "")

            if self._team_matches(home_team, aliases):
                total_goals += r.get("goals_home") or 0
                matches_counted += 1
            elif self._team_matches(away_team, aliases):
                total_goals += r.get("goals_away") or 0
                matches_counted += 1

        avg_goals = total_goals / matches_counted if matches_counted > 0 else 0
        return {
            "team": self.team_label or self.team_slug,
            "total_goals": total_goals,
            "avg_goals": round(avg_goals, 2),
            "matches": matches_counted
        }

    def calculate_team_goals_conceded(self, results):
        total_conceded = 0
        counted = 0
        aliases = [self.team_slug, self.team_label, self.slug_to_team_name(self.team_slug)]

        for r in results:
            home_team = r.get("home", "")
            away_team = r.get("away", "")

            if self._team_matches(home_team, aliases):
                total_conceded += r.get("goals_away") or 0
                counted += 1
            elif self._team_matches(away_team, aliases):
                total_conceded += r.get("goals_home") or 0
                counted += 1

        avg_conceded = total_conceded / counted if counted > 0 else 0
        return round(avg_conceded, 2)

    def calculate_team_xg(self, results):
        total_xg = 0
        counted = 0
        aliases = [self.team_slug, self.team_label, self.slug_to_team_name(self.team_slug)]

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

        avg_xg = total_xg / counted if counted else 0
        return round(avg_xg, 2)

    def calculate_team_xga(self, results):
        total_xga = 0
        counted = 0
        aliases = [self.team_slug, self.team_label, self.slug_to_team_name(self.team_slug)]

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

        avg_xga = total_xga / counted if counted else 0
        return round(avg_xga, 2)

    def analyze_team(self, team_url):
        if not self.open_team_results(team_url):
            return None

        matches = self.get_last_6_matches()
        results = []

        for url in matches:
            match_data = self.get_match_goals(url)
            if match_data:
                xg_data = self.get_match_xg(url)
                match_data["home_xg"] = xg_data.get("home_xg") if xg_data else None
                match_data["away_xg"] = xg_data.get("away_xg") if xg_data else None
                results.append(match_data)

        stats = self.calculate_team_goals(results)
        avg_gc = self.calculate_team_goals_conceded(results)
        avg_xg = self.calculate_team_xg(results)
        avg_xga = self.calculate_team_xga(results)

        avg_gd = round(stats["avg_goals"] - avg_gc, 2)
        avg_xgd = round(avg_xg - avg_xga, 2)

        stats.update({
            "avg_gc": avg_gc,
            "avg_gd": avg_gd,
            "avg_xg": avg_xg,
            "avg_xga": avg_xga,
            "avg_xgd": avg_xgd
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
        except:
            pass


# ---------------- SIGNAL ENGINE ----------------
def evaluate_bet_signals(home, away, home_data, away_data, m_url):
    hs = home_data["stats"]
    as_ = away_data["stats"]

    h_g = hs.get("avg_goals", 0)
    a_g = as_.get("avg_goals", 0)

    h_gc = hs.get("avg_gc", 0)
    a_gc = as_.get("avg_gc", 0)

    h_xg = hs.get("avg_xg", 0)
    a_xg = as_.get("avg_xg", 0)

    h_xga = hs.get("avg_xga", 0)
    a_xga = as_.get("avg_xga", 0)

    h_xgd = hs.get("avg_xgd", 0)
    a_xgd = as_.get("avg_xgd", 0)

    h_gd = hs.get("avg_gd", 0)
    a_gd = as_.get("avg_gd", 0)

    positive = []
    warnings = []

    def add_positive(priority, text):
        positive.append((priority, text))

    def add_warning(text):
        if text not in warnings:
            warnings.append(text)

    # 1. High scoring team vs weak defence
    over_trigger = (
        (h_g >= 2.0 and a_gc >= 1.5) or
        (a_g >= 2.0 and h_gc >= 1.5) or
        (h_xg >= 1.8 and a_xga >= 1.6) or
        (a_xg >= 1.8 and h_xga >= 1.6)
    )
    if over_trigger:
        add_positive(30, "Over Goals market: Over 2.5 (and possibly 3.5 if the edge is strong)")

    # 2. xG dominance mismatch
    if h_xgd >= 0.7 and a_xgd <= -0.3:
        add_positive(15, f"{home} to win / home handicap angle")
    elif a_xgd >= 0.7 and h_xgd <= -0.3:
        add_positive(15, f"{away} to win / away handicap angle")

    # 3. False strong attack filter
    if h_g >= 2.0 and h_xg <= 1.5:
        add_warning(f"{home} may be overperforming its finishing (caution on backing them blindly)")
    if a_g >= 2.0 and a_xg <= 1.5:
        add_warning(f"{away} may be overperforming its finishing (caution on backing them blindly)")

    # 4. Both teams aggressive
    if (
        h_xg >= 1.6 and
        a_xg >= 1.6 and
        h_xga >= 1.2 and
        a_xga >= 1.2
    ):
        add_positive(10, "BTTS + Over 2.5 goals")

    # 5. Low tempo / Under goals
    if (
        h_xg <= 1.2 and
        a_xg <= 1.2 and
        h_xga <= 1.3 and
        a_xga <= 1.3
    ):
        add_positive(5, "Under 2.5 goals / Under 3.5 goals")

    # 6. Defensive collapse detection
    if h_gc >= 1.8 or h_xga >= 1.8:
        add_warning(f"{home} defensive weakness: opponent scoring chances look high")
    if a_gc >= 1.8 or a_xga >= 1.8:
        add_warning(f"{away} defensive weakness: opponent scoring chances look high")

    # 7. Pure dominance filter
    if h_xgd >= 0.8 and a_xgd <= -0.5 and h_g >= 1.8:
        add_positive(1, f"Strong home win signal for {home}")
    elif a_xgd >= 0.8 and h_xgd <= -0.5 and a_g >= 1.8:
        add_positive(1, f"Strong away win signal for {away}")

    if not positive:
        return None

    positive.sort(key=lambda x: x[0])
    best_signal = positive[0][1]
    all_signals = [text for _, text in positive]

    message = (
        f"⚽ {home} vs {away}\n\n"
        f"Best signal: {best_signal}\n\n"
        f"📊 Team stats\n"
        f"{home}  Goals: {h_g} | Conceded: {h_gc} | GD: {h_gd} | xG: {h_xg} | xGA: {h_xga} | xGD: {h_xgd}\n"
        f"{away}  Goals: {a_g} | Conceded: {a_gc} | GD: {a_gd} | xG: {a_xg} | xGA: {a_xga} | xGD: {a_xgd}\n\n"
        f"Signals:\n"
        + "\n".join(f"- {s}" for s in all_signals)
    )

    if warnings:
        message += "\n\nCautions:\n" + "\n".join(f"- {w}" for w in warnings)

    message += f"\n\nMatch URL: {m_url}"
    return message


# ---------------- ALERT SCRIPT ----------------
def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    CHAT_ID = os.getenv("CHAT_ID")
    FIXTURES_URL = "https://www.flashscore.co.za/"
    NUM_FIXTURES = 300
    HEADLESS = True

    print("[INFO] Starting Flashscore alert script...")
    scraper = FlashscoreGoalsScraper(headless=HEADLESS)

    try:
        print(f"[INFO] Opening fixtures page: {FIXTURES_URL}")
        scraper.page.goto(FIXTURES_URL, wait_until="load", timeout=90000)
        time.sleep(3)

        matches = []
        seen = set()
        tries = 0

        while len(matches) < NUM_FIXTURES and tries < 40:
            scraper.expand_hidden_matches()

            links = scraper.page.locator("a[href*='/match/'][href*='?mid=']").all()

            for l in links:
                href = l.get_attribute("href")
                if not href:
                    continue

                href = href.split("/tv")[0].split("#")[0]
                full_url = "https://www.flashscore.co.za" + href if href.startswith("/") else href

                if full_url not in seen:
                    matches.append(full_url)
                    seen.add(full_url)

                if len(matches) >= NUM_FIXTURES:
                    break

            scraper.page.mouse.wheel(0, 6000)
            time.sleep(2)
            tries += 1

        print(f"[INFO] Found {len(matches)} upcoming matches")

        for idx, m_url in enumerate(matches, start=1):
            print(f"[INFO] Processing match {idx}: {m_url}")
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

            msg = evaluate_bet_signals(home, away, home_data, away_data, m_url)

            if msg:
                print("[ALERT]")
                print(msg)
                scraper.send_telegram_message(msg, BOT_TOKEN, CHAT_ID)
            else:
                print("[INFO] No signals found.")

    except Exception as e:
        print("[ERROR]", e)
    finally:
        print("[INFO] Closing browser...")
        scraper.close()
        print("[INFO] Script finished.")


if __name__ == "__main__":
    main()
