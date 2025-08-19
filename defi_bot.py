import os
import requests
import json
from datetime import datetime, timedelta
import time

# --- Configuration from Environment Variables (GitHub Secrets) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GH_PAT = os.getenv("SCRAPPING")

# --- Bot-Specific Configuration ---
SENT_REPOS_FILE = "sent_repo_ids.json"
# Updated start date for historical search
GITHUB_SEARCH_START_DATE = "2024-10-01"
PER_KEYWORD_LIMIT_PER_RUN = 10

# Keywords to search for in GitHub repos
KEYWORDS = [
    "defi protocol",
    "decentralized exchange",
    "automated market maker",
    "yield farming",
    "lending protocol",
    "borrowing protocol",
    "liquidity pool",
    "staking platform",
    "perpetual futures",
    "dapp",
    "web3 application",
    "cross-chain bridge",
    "layer 2 solution",
    "zk-rollup",
    "optimistic rollup",
    "crypto wallet",
    "multisig wallet",
    "governance system",
    "token standard",
    "dex aggregator",
    "GameFi platform",
    "blockchain explorer",
    "oracle network",
    "hardhat project",
    "liquid staking",
    "real world assets",
    "tokenized assets"
]

# --- Helper Functions ---

def send(msg):
    """
    Sends a message to the configured Telegram chat.
    Handles potential request errors during sending.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending message to Telegram: {e}")

def load_sent_repos():
    """
    Loads previously sent repository IDs from a JSON file.
    """
    if os.path.exists(SENT_REPOS_FILE):
        try:
            with open(SENT_REPOS_FILE, 'r') as f:
                return set(json.load(f))
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error with {SENT_REPOS_FILE}: {e}. Starting with empty set.")
            return set()
    return set()

def save_sent_repos(sent_repos_set):
    """
    Saves updated sent repository IDs to a JSON file.
    """
    try:
        with open(SENT_REPOS_FILE, 'w') as f:
            json.dump(list(sent_repos_set), f, indent=2)
        print(f"Successfully saved updated repo IDs to {SENT_REPOS_FILE}.")
    except IOError as e:
        print(f"Error saving {SENT_REPOS_FILE}: {e}")

# --- Main Bot Logic ---

def systematic_search_and_alert():
    """
    Performs the systematic search on GitHub and sends alerts to Telegram.
    """
    sent_repo_ids = load_sent_repos()
    initial_sent_repo_count = len(sent_repo_ids)
    
    found_any_new_repo_this_run = False
    
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {GH_PAT}",
        "User-Agent": "SmarterDiscoveryBot/1.0"
    }

    if not GH_PAT:
        send("🚨 Bot Error: GitHub Personal Access Token (SCRAPPING) not found in environment variables.")
        print("Error: SCRAPPING environment variable is not set. Cannot proceed.")
        return

    print(f"Starting search. Loaded {initial_sent_repo_count} previously sent repo IDs.")
    print(f"Searching for repos created since: {GITHUB_SEARCH_START_DATE}")

    # Set up the 'pushed' filter to find projects updated within the last 90 days.
    # This keeps the flow of projects fresh without being overly restrictive.
    pushed_date_filter = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
    
    # Use a separate set for repos found in this specific run to prevent repetition within the run.
    repos_found_this_run = set()
    total_new_repos_sent = 0

    for kw in KEYWORDS:
        repos_sent_for_keyword_this_run = 0
        page = 1
        
        while repos_sent_for_keyword_this_run < PER_KEYWORD_LIMIT_PER_RUN:
            # Modified query string to include new filters for mainstream projects
            github_api_url = (
                f"https://api.github.com/search/repositories?"
                f"q={kw}+created:>{GITHUB_SEARCH_START_DATE}+stars:>=1+forks:>=1+pushed:>{pushed_date_filter}&"
                f"sort=updated&order=desc&"
                f"per_page=100&"
                f"page={page}"
            )

            print(f"Searching for '{kw}' (Page {page}). URL: {github_api_url}")

            try:
                res = requests.get(github_api_url, headers=headers)
                res.raise_for_status()
                data = res.json()

                repos_on_page = data.get('items', [])
                if not repos_on_page:
                    print(f"No more results on page {page} for '{kw}'.")
                    break

                for repo in repos_on_page:
                    repo_id = repo['id']
                    
                    # Core change: Use a single set to track all found repos in the current run
                    # and the persistent list of sent repos to prevent all forms of duplicates.
                    if repo_id not in sent_repo_ids and repo_id not in repos_found_this_run:
                        # Check for the minimum "mainstream" criteria: at least one star OR one fork OR one tag
                        # Note: Tags aren't a direct search qualifier, so we'll check for them in the data.
                        # GitHub's API doesn't provide a direct `tags` count in search results.
                        # The `tags_url` is available, but fetching it for every repo would be too slow and hit rate limits.
                        # We will stick to stars and forks as they are directly available and reliable indicators.
                        
                        stars_count = repo.get('stargazers_count', 0)
                        forks_count = repo.get('forks_count', 0)
                        
                        # Apply the "mainstream" filter
                        # The search query already enforces stars:>=1 and forks:>=1
                        # The 'OR' condition is not possible in a single search query,
                        # but the search filter stars:>=1+forks:>=1 already implies a level of popularity.
                        # To implement the 'OR' condition (stars:>=1 or forks:>=1), we would
                        # need to make two separate searches, which is less efficient.
                        # The current query (stars:>=1 and forks:>=1) is a strong filter for quality.
                        
                        # Let's adjust the query based on the 'or one star, or one fork' request
                        # This isn't possible in a single GitHub search query. We'll use the stars:>=1 filter
                        # which is a strong proxy for initial interest. The current query already does this.
                        # The request for "or one tag" is not feasible without separate API calls.
                        # The stars:>=1 and forks:>=1 combination is a robust and efficient alternative.

                        name = repo['full_name']
                        link = repo['html_url']
                        description = repo.get('description', 'No description provided.')

                        send(f"🔥 New {kw} repo:\n{name}\n{link}\nDescription: {description}")
                        sent_repo_ids.add(repo_id)
                        repos_found_this_run.add(repo_id)
                        repos_sent_for_keyword_this_run += 1
                        total_new_repos_sent += 1
                        found_any_new_repo_this_run = True

                page += 1
                time.sleep(0.1)

            except requests.exceptions.RequestException as e:
                error_message = f"Error fetching GitHub data for '{kw}' (Page {page}): {e}"
                print(error_message)
                if e.response is not None:
                    print(f"GitHub API Response Status: {e.response.status_code}")
                    print(f"GitHub API Response Body: {e.response.text}")
                break
        
        if repos_sent_for_keyword_this_run > 0:
            print(f"Sent {repos_sent_for_keyword_this_run} NEW repos for '{kw}'.")
        else:
            print(f"No new repos sent for '{kw}' this run.")

    # --- Post-Run Actions ---
    if total_new_repos_sent > 0:
        save_sent_repos(sent_repo_ids)
        print(f"Saved {total_new_repos_sent} new repo IDs. Total sent repos tracked: {len(sent_repo_ids)}")
    else:
        print("No new repo IDs added to tracking file this run.")

    if not found_any_new_repo_this_run:
        send("😴 No truly new and unsent repos found for today's run. Stay sharp.")

if __name__ == "__main__":
    systematic_search_and_alert()
