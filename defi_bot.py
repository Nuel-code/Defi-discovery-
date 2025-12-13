from __future__ import annotations
import os
import sys
import time
import json
import logging
import requests
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Dict, Set
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------
# Configuration
# --------------------------
# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GH_PAT = os.getenv("GH_PAT")

# State & Paths
DATA_DIR = "data"
SENT_REPOS_PATH = f"{DATA_DIR}/sent_repo_ids.json"

# Tuning
# We search for recent creations to find new startups
CREATED_DAYS = 90 
SEARCH_PAGE_SIZE = 100
GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "web3-scout-bot/3.0"

# Limits
PER_KEYWORD_LIMIT_PER_RUN = 10  # Kept low to prioritize quality over quantity

# --------------------------
# Smart Filters (The "Bouncer")
# --------------------------

# 1. Trash Keywords (Immediate Rejection)
# If these appear in description or topics, we assume it's fodder.
TRASH_KEYWORDS = [
    "tutorial", "demo", "example", "test", "playground", "sample",
    "starter", "boilerplate", "course", "assignment", "homework",
    "learning", "practice", "101", "hello-world", "my-first",
    "scaffold", "template", "curated list", "awesome-", "roadmap",
    "interview", "challenge", "bot"
]

# 2. Pro Identity Keywords (Bonus Points)
# If a username contains these, we trust it more.
PRO_IDENTITY_TERMS = [
    "fi", "finance", "dex", "swap", "protocol", "labs", "dao", 
    "chain", "network", "foundation", "capital", "ventures", "tech",
    "system", "solutions", "market", "exchange", "defi", "web3"
]

# 3. Search Keywords (Your Inputs)
KEYWORDS = [
    "defi", "decentralized exchange", "automated market maker", "btc",
    "yield farming", "yield aggregator", "lending protocol",
    "borrowing protocol", "liquidity pool", "staking", "perpetual futures",
    "stablecoin", "rollup", "optimistic rollup",
    "bridge cross-chain", "cross-chain bridge",
    "token", "dex", "dex aggregator", "wallet",
    "rust blockchain", "layer 2"
]

PRIORITY_LANGUAGES = ["Solidity", "Rust", "TypeScript", "JavaScript", "Go", "Python"]

# --------------------------
# Utilities
# --------------------------
def get_github_session():
    """Creates a session with retry logic."""
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update({
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": USER_AGENT,
        "Authorization": f"token {GH_PAT}" if GH_PAT else None
    })
    return session

def load_sent_repo_ids() -> Set[int]:
    if not os.path.exists(SENT_REPOS_PATH):
        return set()
    try:
        with open(SENT_REPOS_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_sent_repo_ids_local(sent: Set[int]) -> None:
    os.makedirs(os.path.dirname(SENT_REPOS_PATH), exist_ok=True)
    with open(SENT_REPOS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(sent)), f, indent=2)

def send_telegram(session: requests.Session, message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        r = session.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram Error: {e}")
        return False

# --------------------------
# Core Filtering Logic
# --------------------------
def is_likely_project_account(owner_data: Dict) -> bool:
    """
    Determines if the account looks like a Project/Org or a random User.
    """
    account_type = owner_data.get("type", "User")
    username = owner_data.get("login", "").lower()

    # RULE 1: Organizations are preferred
    if account_type == "Organization":
        return True

    # RULE 2: Filter out "Personal/Spam" usernames
    # Reject usernames ending in 3+ digits (e.g., ayush6671, user992)
    if re.search(r'[a-z]+[0-9]{3,}$', username):
        logger.info(f"    [Filter] Rejecting personal pattern: {username}")
        return False

    # RULE 3: Check for "Pro" keywords in username
    # e.g., "rendexfi" contains "fi", "swapxyz" contains "swap"
    if any(term in username for term in PRO_IDENTITY_TERMS):
        return True

    # If it's a User, has no numbers, but no pro keywords (e.g. "johnsmith"), 
    # we treat it neutrally (let other filters decide), or strict fail?
    # Based on your request for "Organization feel", we lean towards False unless content is amazing.
    # For now, we allow it but rely on content filters.
    return True

def is_high_quality_repo(repo: Dict) -> bool:
    full_name = repo.get("full_name", "").lower()
    desc = (repo.get("description") or "").lower()
    topics = [t.lower() for t in repo.get("topics", [])]
    size_kb = repo.get("size", 0)

    # 1. IMMEDIATE TRASH CHECKS
    # Check for homework/tutorial keywords
    text_corpus = f"{full_name} {desc} {' '.join(topics)}"
    if any(bad in text_corpus for bad in TRASH_KEYWORDS):
        logger.info(f"    [Filter] Trash keyword found in {full_name}")
        return False

    # 2. SIZE CHECK (The "Fodder" Filter)
    # A real startup repo usually has some weight. 
    # Empty or README-only repos are usually < 5KB.
    # We set a bar at 30KB to filter out pure "init" commits.
    if size_kb < 30:
        logger.info(f"    [Filter] Repo too small ({size_kb}KB): {full_name}")
        return False

    # 3. IDENTITY CHECK (The "Ayush" vs "Rendex" Filter)
    owner = repo.get("owner", {})
    if not is_likely_project_account(owner):
        return False

    return True

def check_rate_limit(headers: Dict):
    remaining = int(headers.get('X-RateLimit-Remaining', 10))
    reset_time = int(headers.get('X-RateLimit-Reset', 0))
    if remaining < 5:
        sleep_time = max(1, reset_time - time.time()) + 2
        logger.warning(f"Rate limit hit. Sleeping {sleep_time}s...")
        time.sleep(sleep_time)

# --------------------------
# Main Logic
# --------------------------
def scan_and_alert():
    logger.info("--- Starting Web3 Scout ---")
    session = get_github_session()
    sent_ids = load_sent_repo_ids()
    
    # Time window
    created_since = (datetime.utcnow() - timedelta(days=CREATED_DAYS)).strftime("%Y-%m-%d")
    pushed_since = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%d")

    new_alerts = 0

    for kw in KEYWORDS:
        logger.info(f"🔎 Scanning: {kw}")
        count_for_kw = 0
        
        # We loop languages to dig deeper than just the "default" search sort
        for lang in [None] + PRIORITY_LANGUAGES:
            if count_for_kw >= PER_KEYWORD_LIMIT_PER_RUN:
                break

            page = 1
            while True:
                # Query Construction
                # We do NOT combine keywords. We use the single kw provided.
                query_parts = [kw, f"created:>{created_since}", f"pushed:>{pushed_since}", "fork:false"]
                if lang:
                    query_parts.append(f"language:{lang}")
                
                q = urllib.parse.quote_plus(" ".join(query_parts))
                url = f"{GITHUB_API_BASE}/search/repositories?q={q}&sort=updated&order=desc&per_page={SEARCH_PAGE_SIZE}&page={page}"

                try:
                    r = session.get(url, timeout=15)
                    r.raise_for_status()
                    check_rate_limit(r.headers)
                    data = r.json()
                except Exception as e:
                    logger.error(f"Search error: {e}")
                    break

                items = data.get("items", [])
                if not items:
                    break

                for repo in items:
                    rid = repo.get("id")
                    
                    # 1. Check if already sent (Prevents Repeats)
                    if rid in sent_ids:
                        continue
                        
                    # 2. Run the Bouncer (Prevents Fodder)
                    if not is_high_quality_repo(repo):
                        continue

                    # If we passed all filters, it's a match!
                    stars = repo.get('stargazers_count', 0)
                    lang_tag = repo.get('language') or 'Unknown'
                    desc = (repo.get('description') or 'No description').strip()
                    if len(desc) > 200: desc = desc[:197] + "..."

                    msg = (
                        f"🚀 *{repo.get('full_name')}*\n"
                        f"🔗 [GitHub Link]({repo.get('html_url')})\n"
                        f"🏷️ `{kw}` | 🛠 {lang_tag} | 📦 {repo.get('size',0)}KB\n"
                        f"📝 {desc}"
                    )

                    if send_telegram(session, msg):
                        # CRITICAL: Save immediately to disk
                        sent_ids.add(rid)
                        save_sent_repo_ids_local(sent_ids)
                        
                        logger.info(f"✅ Sent: {repo.get('full_name')}")
                        count_for_kw += 1
                        new_alerts += 1
                        time.sleep(0.5) # Polite delay
                    
                    if count_for_kw >= PER_KEYWORD_LIMIT_PER_RUN:
                        break
                
                if count_for_kw >= PER_KEYWORD_LIMIT_PER_RUN or len(items) < SEARCH_PAGE_SIZE:
                    break
                page += 1
                time.sleep(1)

    logger.info(f"🏁 Run complete. Sent {new_alerts} new projects.")

if __name__ == "__main__":
    try:
        scan_and_alert()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.critical(f"Crash: {e}")
        sys.exit(1)

