import os, requests
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

KEYWORDS = ["defi", "amm", "vault", "staking", "perps"]
GITHUB_SINCE = "2024-12-01"

def send(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})

def search_and_alert():
    found_something = False

    for kw in KEYWORDS:
        url = f"https://github.com/search?q={kw}+created:%3E{GITHUB_SINCE}&type=repositories&s=updated&o=desc"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(res.text, 'html.parser')
        repos = soup.select('a.v-align-middle')[:3]

        if repos:
            found_something = True

        for repo in repos:
            name = repo.text.strip()
            link = "https://github.com" + repo['href']
            send(f"🔥 New {kw} repo:\n{name}\n{link}")

    if not found_something:
        send("😴 No new repos found for today. Stay sharp.")

if __name__ == "__main__":
    search_and_alert()
