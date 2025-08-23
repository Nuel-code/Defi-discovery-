import os
import requests
import json
from datetime import datetime, timedelta
import time

# --- Configuration from Environment Variables (GitHub Secrets) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DEFI_GHACK = os.getenv("DEFI_GHACK") # Changed variable name to DEFI_GHACK

# --- Bot-Specific Configuration ---
SENT_REPOS_FILE = "sent_repo_ids.json"
# Dynamic start date: only look back 60 days
GITHUB_SEARCH_START_DATE = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
PER_KEYWORD_LIMIT_PER_RUN = 15


# Keywords to search for in GitHub repos
KEYWORDS = [
    "defi protocol", "decentralized exchange", "automated market maker",
    "yield farming", "lending protocol", "borrowing protocol",
    "liquidity pool", "staking platform", "perpetual futures",
    "dapp", "web3 application", "cross-chain bridge",
    "layer 2 solution", "zk-rollup", "optimistic rollup",
    "crypto wallet", "multisig wallet", "governance system",
    "token standard", "dex aggregator", "GameFi platform",
    "blockchain explorer", "oracle network", "hardhat project",
    "liquid staking", "real world assets", "tokenized assets"
]

# Obvious junk/personal keywords to exclude
NEGATIVE_KEYWORDS = [
    "tutorial", "example", "test", "demo", "practice", "assignment",
    "hello-world", "sandbox", "toy", "learning", "playground", "trial"
]

# --- Helper Functions ---

def send(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error sending message to Telegram: {e}")

def load_sent_repos():
    if os.path.exists(SENT_REPOS_FILE):
        try:
            with open(SENT_REPOS_FILE, 'r') as f:
                return set(json.load(f))
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error with {SENT_REPOS_FILE}: {e}. Starting with empty set.")
            return set()
    return set()

def save_sent_repos(sent_repos_set):
    try:
        with open(SENT_REPOS_FILE, 'w') as f:
            json.dump(list(sent_repos_set), f, indent=2)
        print(f"Successfully saved updated repo IDs to {SENT_REPOS_FILE}.")
    except IOError as e:
        print(f"Error saving {SENT_REPOS_FILE}: {e}")

# --- Main Bot Logic ---

def systematic_search_and_alert():
    sent_repo_ids = load_sent_repos()
    initial_sent_repo_count = len(sent_repo_ids)
    found_any_new_repo_this_run = False
    
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {DEFI_GHACK}", # Changed to DEFI_GHACK
        "User-Agent": "SmarterDiscoveryBot/1.0"
    }

    if not DEFI_GHACK: # Changed to DEFI_GHACK
        send("🚨 Bot Error: GitHub Personal Access Token (DEFI_GHACK) not found.")
        print("Error: DEFI_GHACK environment variable is not set. Cannot proceed.")
        return

    print(f"Starting search. Loaded {initial_sent_repo_count} previously sent repo IDs.")
    print(f"Searching for repos created since: {GITHUB_SEARCH_START_DATE}")

    pushed_date_filter = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    repos_found_this_run = set()
    total_new_repos_sent = 0

    try:
        for kw in KEYWORDS:
            repos_sent_for_keyword_this_run = 0
            page = 1
            
            while repos_sent_for_keyword_this_run < PER_KEYWORD_LIMIT_PER_RUN:
                # Add negative keywords to the query
                neg_str = "".join([f"+NOT+{w}" for w in NEGATIVE_KEYWORDS])
                
                # Use a combined popularity filter to reduce API calls
                # Stars are a better indicator of early interest than forks
                github_api_url = (
                    f"https://api.github.com/search/repositories?"
                    f"q={kw}+created:>{GITHUB_SEARCH_START_DATE}+stars:>=1+pushed:>{pushed_date_filter}{neg_str}&"
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
                        if repo_id not in sent_repo_ids and repo_id not in repos_found_this_run:
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
                    time.sleep(0.1) # Respectful delay between API calls

                except requests.exceptions.RequestException as e:
                    print(f"Error fetching GitHub data for '{kw}' (Page {page}): {e}")
                    if e.response is not None:
                        print(f"GitHub API Status: {e.response.status_code}")
                        print(f"Body: {e.response.text}")
                    break # Break out of the inner while loop
            
            if repos_sent_for_keyword_this_run > 0:
                print(f"Sent {repos_sent_for_keyword_this_run} NEW repos for '{kw}'.")
            else:
                print(f"No new repos sent for '{kw}' this run.")

    finally:
        # This block will always execute, regardless of whether an error occurred.
        if total_new_repos_sent > 0:
            save_sent_repos(sent_repo_ids)
            print(f"Saved {total_new_repos_sent} new repo IDs. Total sent repos tracked: {len(sent_repo_ids)}")
        else:
            print("No new repo IDs added this run.")

        if not found_any_new_repo_this_run:
            send("😴 No fresh repos worth alerting today. Market’s quiet.")

if __name__ == "__main__":
    systematic_search_and_alert()
 
This video provides a great walkthrough on how to set up and use GitHub Actions Secrets with a Python script.
