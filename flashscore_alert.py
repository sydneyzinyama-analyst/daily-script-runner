# flashscore_alert.py
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
            if not href: continue
            href = href.split("/tv")[0].split("#")[0]
            full_url = self._abs_url(href)
            if full_url not in matches and "?mid=" in full_url:
                matches.append(full_url)
            if len(matches) == 6: break
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
        return {"home": home, "away": away, "goals_home": score_home, "goals_away": score_away, "match_url": match_url}

    def _team_match_score(self, a, b):
        a_n = self.normalize_name(a)
        b_n = self.normalize_name(b)
        if not a_n or not b_n: return 0.0
        if a_n == b_n: return 1.0
        if a_n in b_n or b_n in a_n: return 0.95
        return SequenceMatcher(None, a_n, b_n).ratio()

    def _team_matches(self, candidate, aliases, threshold=0.62):
        for alias in aliases:
            if not alias: continue
            if self._team_match_score(candidate, alias) >= threshold: return True
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
        return {"team": self.team_label or self.team_slug, "total_goals": total_goals, "avg_goals": round(avg_goals,2), "matches": matches_counted}

    def analyze_team(self, team_url):
        if not self.open_team_results(team_url): return None
        matches = self.get_last_6_matches()
        results = []
        for url in matches:
            data = self.get_match_goals(url)
            if data: results.append(data)
        stats = self.calculate_team_goals(results)
        return {"team": stats["team"], "matches": matches, "results": results, "stats": stats}

    def close(self):
        try:
            self.browser.close()
            self.playwright.stop()
        except: pass

# ---------------- ALERT SCRIPT ----------------
def main():
    import os
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    CHAT_ID = os.getenv("CHAT_ID")
    FIXTURES_URL = "https://www.flashscore.co.za/team/slavia-sofia/UopPSUlp/fixtures/"
    NUM_FIXTURES = 1
    HEADLESS = True

    print("[INFO] Starting Flashscore alert script...")
    scraper = FlashscoreGoalsScraper(headless=HEADLESS)

    try:
        print(f"[INFO] Opening fixtures page: {FIXTURES_URL}")
        scraper.page.goto(FIXTURES_URL, wait_until="load", timeout=90000)
        time.sleep(3)

        links = scraper.page.locator("a[href*='/match/'][href*='?mid=']").all()
        matches = []
        seen = set()
        for l in links:
            href = l.get_attribute("href")
            if not href: continue
            href = href.split("/tv")[0].split("#")[0]
            full_url = "https://www.flashscore.co.za" + href if href.startswith("/") else href
            if full_url not in seen:
                matches.append(full_url)
                seen.add(full_url)
            if len(matches) >= NUM_FIXTURES: break

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

            avg_home = home_data["stats"]["avg_goals"] if home_data else 0
            avg_away = away_data["stats"]["avg_goals"] if away_data else 0

            print(f"[INFO] {home} avg goals: {avg_home}")
            print(f"[INFO] {away} avg goals: {avg_away}")

            # ALERT CONDITIONS
            condition1 = (avg_home < 0.7 and avg_away < 0.7)
            condition2 = ((avg_home >= 1.75 and avg_away < 1) or (avg_away >= 1.75 and avg_home < 1))

            if condition1 or condition2:
                msg = f"⚠️ Alert: {home} vs {away}\nAvg Goals: {avg_home} - {avg_away}\nMatch URL: {m_url}"
                print("[INFO] Sending Telegram alert...")
                scraper.send_telegram_message(msg, BOT_TOKEN, CHAT_ID)
            else:
                print("[INFO] No alert conditions met.")

    except Exception as e:
        print("[ERROR]", e)
    finally:
        print("[INFO] Closing browser...")
        scraper.close()
        print("[INFO] Script finished.")

if __name__ == "__main__":
    main()
