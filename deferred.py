import os
import requests

# Get your Telegram credentials from GitHub Secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Get your GitHub Personal Access Token from GitHub Secrets (using the correct name GH_PAT)
GH_PAT = os.getenv("GH_PAT")

# Keywords to search for in GitHub repos
KEYWORDS = ["defi", "amm", "vault", "staking", "perps", "evm", "bitcoin"]

# Only show repos created after this date
# GitHub API uses "created:" or "pushed:"
# The format for "created:" is YYYY-MM-DD
GITHUB_SINCE = "2024-12-01"

def send(msg):
    """Send a message to Telegram bot"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
        response.raise_for_status() # Raise an exception for HTTP errors
    except requests.exceptions.RequestException as e:
        print(f"Error sending message to Telegram: {e}")

def search_and_alert():
    found_something = False
    headers = {
        "Accept": "application/vnd.github.v3+json", # Recommended header for GitHub API
        "Authorization": f"token {GH_PAT}",  # Authenticate with your PAT (using GH_PAT)
        "User-Agent": "DeFiRepoBot/1.0" # Good practice to provide a User-Agent
    }

    # Basic check for GH_PAT presence
    if not GH_PAT:
        send("🚨 Bot Error: GitHub Personal Access Token (GH_PAT) not found in environment variables.")
        print("Error: GH_PAT environment variable is not set. Cannot proceed with GitHub API calls.")
        return

    for kw in KEYWORDS:
        # GitHub API Search Repositories endpoint
        # Query format: "q={keyword} created:>{date}"
        # We also want to sort by updated for the "latest" feel, or stars, etc.
        # Per_page is important for pagination, max 100
        # We are searching for repositories, not code *within* repositories, which is slightly different
        github_api_url = (
            f"https://api.github.com/search/repositories?"
            f"q={kw}+created:>{GITHUB_SINCE}&" # Correct API date filter
            f"sort=updated&order=desc&" # Sort by updated date, descending
            f"per_page=3" # Limit to 3 results per keyword, similar to your original code
        )

        print(f"Searching GitHub API for '{kw}' at URL: {github_api_url}")

        try:
            res = requests.get(github_api_url, headers=headers)
            res.raise_for_status() # Raise an exception for HTTP errors (e.g., 403 Forbidden for rate limit)
            data = res.json()

            # GitHub API returns results in 'items' key
            repos = data.get('items', [])

            if repos:
                found_something = True

            for repo in repos:
                name = repo['full_name'] # Full name includes owner/repo
                link = repo['html_url']   # Direct link to the repo
                description = repo.get('description', 'No description provided.')

                send(f"🔥 New {kw} repo:\n{name}\n{link}\nDescription: {description}")

            # Check rate limits (optional, but good for debugging)
            rate_limit_remaining = res.headers.get('X-RateLimit-Remaining')
            rate_limit_reset = res.headers.get('X-RateLimit-Reset')
            print(f"GitHub API Rate Limit Remaining: {rate_limit_remaining}")
            if rate_limit_remaining and int(rate_limit_remaining) < 10:
                print(f"Approaching rate limit. Reset at: {rate_limit_reset}")

        except requests.exceptions.RequestException as e:
            error_message = f"Error fetching GitHub data for keyword '{kw}': {e}"
            print(error_message)
            if e.response is not None:
                print(f"GitHub API Response Status: {e.response.status_code}")
                print(f"GitHub API Response Body: {e.response.text}")
                # Send error to Telegram if you want, but be careful not to spam
                # send(f"🚨 Bot Error for '{kw}': {e.response.status_code} - {e.response.text}")
            else:
                # send(f"🚨 Bot Error for '{kw}': {e}")
                pass # Or log to a file, etc.


    if not found_something:
        send("😴 No new repos found for today. Stay sharp.")

if __name__ == "__main__":
    search_and_alert()
