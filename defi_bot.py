import os
import requests
from datetime import datetime, timedelta

# --- Configuration from Environment Variables (GitHub Secrets) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GH_PAT = os.getenv("GH_PAT")

# --- Query Config ---
# Look back 6 months
created_date_filter = (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d")

# Focused DeFi topics
KEYWORDS = [
    "defi+protocol",
    "yield+aggregator",
    "lending+borrowing",
    "liquidity+pool",
    "staking+rewards",
    "perpetual+futures",
    "amm+exchange",
    "stablecoin+vault",
    "bridge+crosschain",
    "options+derivatives",
    "rollup+evm"
]

# Trash filter
NEGATIVE_KEYWORDS = [
    "tutorial", "demo", "example", "test", "playground", "sample",
    "hackathon", "learning", "course"
]

def fetch_github_repos():
    headers = {"Authorization": f"token {GH_PAT}"}
    results = []

    for kw in KEYWORDS:
        page = 1
        while True:
            query = (
                f"https://api.github.com/search/repositories?"
                f"q={kw}+created:>{created_date_filter}&"
                f"sort=updated&order=desc&per_page=100&page={page}"
            )
            r = requests.get(query, headers=headers)
            if r.status_code != 200:
                print(f"GitHub API error {r.status_code}: {r.text}")
                break

            data = r.json()
            repos = data.get("items", [])
            if not repos:
                break

            for repo in repos:
                name = repo["name"].lower()
                desc = (repo["description"] or "").lower()
                if not any(bad in name or bad in desc for bad in NEGATIVE_KEYWORDS):
                    results.append(repo)

            if "next" not in r.links:  # No more pages
                break
            page += 1

    return results

def send_to_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    r = requests.post(url, json=payload)
    if r.status_code != 200:
        print(f"Telegram error {r.status_code}: {r.text}")

def main():
    repos = fetch_github_repos()
    if not repos:
        send_to_telegram("No DeFi repos worth alerting today.")
        return

    message = "*🚨 New/Updated DeFi Repositories Found 🚨*\n\n"
    for repo in repos[:15]:  # cap to avoid spam
        message += f"[{repo['full_name']}]({repo['html_url']}) ⭐ {repo['stargazers_count']}\n"
        if repo["description"]:
            message += f"_{repo['description']}_\n\n"

    send_to_telegram(message)

if __name__ == "__main__":
    main()
