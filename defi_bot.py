From __future__ import annotations
import os
import sys
import time
import json
import logging
import requests
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Dict, Set, Tuple
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------
# Configuration
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Web3Scout")

# Secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GH_PAT = os.getenv("GH_PAT")

# Paths
DATA_DIR = "data"
SENT_REPOS_PATH = f"{DATA_DIR}/sent_repo_ids.json"

# Tuning
CREATED_DAYS_AGO = 90       # Look for projects created recently
MIN_SCORE_THRESHOLD = 1     # The "Bar" -> Increase to 5 for stricter filtering
SEARCH_PAGE_SIZE = 100
PER_KEYWORD_LIMIT = 15      # Max alerts per keyword per run

USER_AGENT = "web3-scout-v4"

# --------------------------
# The "Brain" (Keywords & Scoring)
# --------------------------

# Words that indicate "Fodder" (Tutorials, Homework, Spam)
TRASH_TERMS = [
    "tutorial", "demo", "example", "test", "playground", "sample",
    "starter", "boilerplate", "course", "assignment", "homework",
    "learning", "practice", "101", "hello-world", "my-first",
    "scaffold", "template", "roadmap", "interview", "challenge", 
    "curated list", "collection", "awesome", "personal site"
]

# Words that indicate "Pro" intention
PRO_TERMS = [
    "protocol", "finance", "dex", "swap", "dao", "chain", "network", 
    "labs", "ventures", "foundation", "exchange", "market", "arbitrage",
    "mev", "flashloan", "bot", "solana", "ethereum", "zk", "rollup"
]

# Your Search Keywords (Unchanged)
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
# Infrastructure
# --------------------------
def get_session():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": USER_AGENT,
        "Authorization": f"token {GH_PAT}" if GH_PAT else None
    })
    return s

def load_history() -> Set[int]:
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    
    if not os.path.exists(SENT_REPOS_PATH):
        return set()
        
    try:
        with open(SENT_REPOS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except Exception:
        # If corrupt, backup and reset
        try:
            os.rename(SENT_REPOS_PATH, f"{SENT_REPOS_PATH}.bak")
        except: pass
        return set()

def save_history(history: Set[int]):
    try:
        with open(SENT_REPOS_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(list(history)), f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save history: {e}")

# --------------------------
# The "Scorer" (The New Logic)
# --------------------------
def calculate_quality_score(repo: Dict) -> Tuple[int, List[str]]:
    """
    Returns (Score, List of Reasons).
    Positive score = Good. Negative score = Trash.
    """
    score = 0
    reasons = []
    
    # Extract Data
    name = repo.get("name", "").lower()
    full_name = repo.get("full_name", "")
    desc = (repo.get("description") or "").lower()
    topics = [t.lower() for t in repo.get("topics", [])]
    owner_type = repo.get("owner", {}).get("type", "User")
    homepage = repo.get("homepage")
    has_license = repo.get("license") is not None
    size = repo.get("size", 0)
    stars = repo.get("stargazers_count", 0)
    
    text_corpus = f"{name} {desc} {' '.join(topics)}"

    # --- 1. The "Trash" Deductions ---
    if any(bad in text_corpus for bad in TRASH_TERMS):
        score -= 50
        reasons.append("Contains 'trash' keywords")
    
    if size < 50: # Increased from 30KB
        score -= 20
        reasons.append("Too small (<50KB)")
        
    if not desc:
        score -= 10
        reasons.append("No description")

    # --- 2. The "Pro" Bonuses ---
    if owner_type == "Organization":
        score += 10
        reasons.append("🏛️ Organization")
    
    if homepage and len(homepage) > 5:
        score += 5
        reasons.append("🔗 Has Website")
        
    if has_license:
        score += 5
        reasons.append("📜 Licensed")
    
    if stars > 5:
        score += 5
        reasons.append(f"⭐ {stars} Stars")

    # --- 3. Keyword Relevance ---
    # Does the name/desc actually sound like a financial protocol?
    if any(pro in text_corpus for pro in PRO_TERMS):
        score += 3
    
    return score, reasons

# --------------------------
# The "Designer" (New Display)
# --------------------------
def send_telegram_card(session, repo: Dict, score: int, reasons: List[str], keyword: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
        
    # Data Prep
    name = repo.get("name")
    url = repo.get("html_url")
    desc = (repo.get("description") or "No description provided.").strip()
    if len(desc) > 250: desc = desc[:247] + "..."
    
    lang = repo.get("language") or "Code"
    stars = repo.get("stargazers_count", 0)
    forks = repo.get("forks_count", 0)
    
    # Calculate "Freshness"
    pushed_at = repo.get("pushed_at", "")
    try:
        dt = datetime.strptime(pushed_at, "%Y-%m-%dT%H:%M:%SZ")
        freshness = dt.strftime("%d %b")
    except:
        freshness = "Unknown"

    owner_data = repo.get("owner", {})
    avatar_url = owner_data.get("avatar_url", "")
    
    # Filter specific reasons for display (keep it short)
    # We only show the "Good" reasons in the UI
    display_tags = [r for r in reasons if "trash" not in r and "small" not in r and "description" not in r]
    tags_str = " • ".join(display_tags) if display_tags else "New Discovery"

    # --- VISUAL DESIGN ---
    # 1. [\u200b] is the invisible image anchor
    # 2. Bold Title with Link
    # 3. Code Block for metrics (monospaced alignment)
    # 4. Clean description
    
    msg = (
        f"[\u200b]({avatar_url})"
        f"💎 *{name}* `{lang}`\n"
        f"_{desc}_\n\n"
        f"📊 `{stars}⭐`  `{forks}🍴`  `{freshness}📅`\n"
        f"✅ {tags_str}\n"
        f"🔎 Found via `{keyword}`\n"
        f"[View on GitHub]({url})"
    )

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False 
    }
    
    try:
        r = session.post(api_url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram fail: {e}")
        return False

# --------------------------
# Main Execution
# --------------------------
def run_scout():
    logger.info("--- Starting Scout v4 (Score-Based) ---")
    session = get_session()
    sent_ids = load_history()
    
    # Date Math
    created_since = (datetime.utcnow() - timedelta(days=CREATED_DAYS_AGO)).strftime("%Y-%m-%d")
    
    new_count = 0

    for kw in KEYWORDS:
        logger.info(f"Scanning: {kw}...")
        kw_hits = 0
        
        # We iterate pages just a bit to ensure depth
        for page in range(1, 3): 
            if kw_hits >= PER_KEYWORD_LIMIT: break
            
            # Construct Query
            q = f"{kw} created:>{created_since} fork:false"
            # Encode
            encoded_q = urllib.parse.quote_plus(q)
            url = f"https://api.github.com/search/repositories?q={encoded_q}&sort=updated&order=desc&per_page={SEARCH_PAGE_SIZE}&page={page}"
            
            try:
                r = session.get(url, timeout=15)
                # Rate Limit Handling
                if r.status_code == 403 or r.status_code == 429:
                    logger.warning("Rate limit hit. Cooling down 15s...")
                    time.sleep(15)
                    continue
                    
                data = r.json()
                items = data.get("items", [])
                
                if not items: break # End of results
                
                for repo in items:
                    rid = repo.get("id")
                    full_name = repo.get("full_name")
                    
                    # 1. Check History
                    if rid in sent_ids:
                        continue
                        
                    # 2. Check Score (The New Filter)
                    score, reasons = calculate_quality_score(repo)
                    
                    if score < MIN_SCORE_THRESHOLD:
                        # Log rejected items only to console for debugging
                        # logger.info(f"   [REJECT] {full_name} (Score: {score})") 
                        continue
                        
                    # 3. Send Alert
                    logger.info(f"   [MATCH] {full_name} Score: {score} ({reasons})")
                    
                    success = send_telegram_card(session, repo, score, reasons, kw)
                    if success:
                        sent_ids.add(rid)
                        save_history(sent_ids)
                        kw_hits += 1
                        new_count += 1
                        time.sleep(0.5) # Polite delay
                    
                    if kw_hits >= PER_KEYWORD_LIMIT:
                        break
                        
            except Exception as e:
                logger.error(f"Error on {kw}: {e}")
                time.sleep(5)
                
            time.sleep(2) # Delay between pages

    logger.info(f"🏁 Scout finished. Found {new_count} new gems.")

if __name__ == "__main__":
    try:
        run_scout()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.critical(f"Main crash: {e}")
        sys.exit(1)

