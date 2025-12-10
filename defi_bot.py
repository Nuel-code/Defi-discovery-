#!/usr/bin/env python3
"""
web3_scout_bot (Ultimate Edition)

Merged Features:
1. Persistent Listener: Runs forever, scanning every 4 hours.
2. Smart Cleanup: Reply with /cleanup to delete all duplicate messages.
3. Quality Filters: 
   - Ignores repos < 150KB (stops "Hello World" spam).
   - Ignores repos with short/missing descriptions.
   - Ignores "clones" and "tutorials".
4. Optimized Search: Splits logic into specific queries to reduce API noise.
"""
import os
import sys
import json
import logging
import asyncio
import hashlib
import requests
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional

# Telegram Bot Library
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    JobQueue,
)

# --------------------------
# Configuration
# --------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GH_PAT = os.getenv("GH_PAT")

# Run every 4 hours (14400 seconds)
SCAN_INTERVAL_SECONDS = 14400 

# Paths
DATA_DIR = "data"
SENT_REPOS_PATH = f"{DATA_DIR}/sent_repo_ids.json"
MESSAGE_DB_PATH = f"{DATA_DIR}/sent_messages_db.json"

# GitHub Constants
GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "web3-scout-bot/3.0"
SEARCH_PAGE_SIZE = 50 # Reduced to 50 to focus on top results

# --------------------------
# QUALITY FILTERING CONSTANTS
# --------------------------
MIN_REPO_SIZE_KB = 150  # Filter out tiny "fodder" repos
MIN_DESC_LENGTH = 15    # Filter out "test" descriptions

NEGATIVE_KEYWORDS = [
    # Educational / Test
    "tutorial", "demo", "example", "test", "playground", "sample", 
    "hackathon", "learning", "course", "homework", "exercise", 
    "bootcamp", "assignment", "university", "syllabus", "lesson",
    
    # Low Value / Boilerplate
    "template", "boilerplate", "starter", "skeleton", "scaffold",
    "minimal", "setup", "quickstart", "vanilla",
    
    # Clones / Copies
    "clone", "copy", "mirror", "fork", "uniswap-v2", "sushiswap-clone",
    "pancakeswap-clone", "safemoon", "floki", "shiba",
    
    # Non-Code / Lists
    "awesome", "list", "curated", "collection", "resources", "roadmap",
    "interview", "questions", "bot", "telegram" 
]

# Lane 1: Highly specific terms (Any Language OK)
SPECIFIC_KEYWORDS = [
    "automated market maker", "yield aggregator", 
    "mev bot", "arbitrage bot", "liquidator", "perpetual protocol",
    "optimistic rollup", "zk-rollup", "zero knowledge proof",
    "cross-chain bridge", "layer zero", "erc-4337", "account abstraction"
]

# Lane 2: Broader terms (Must be in specific languages to count)
BROAD_KEYWORDS = [
    "defi", "dex", "wallet", "staking", "governance", "blockchain",
    "token", "smart contract", "dao"
]

STRICT_LANGUAGES = ["Solidity", "Rust", "Go", "TypeScript", "Huff", "Vyper", "Cairo"]

# --------------------------
# Logging & State Management
# --------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def load_json_set(path: str) -> Set[int]:
    if not os.path.exists(path): return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_json_set(data: Set[int], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(list(data), f)

def load_message_db() -> Dict[str, List[int]]:
    if not os.path.exists(MESSAGE_DB_PATH): return {}
    try:
        with open(MESSAGE_DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_message_db(db: Dict[str, List[int]]):
    os.makedirs(os.path.dirname(MESSAGE_DB_PATH), exist_ok=True)
    with open(MESSAGE_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f)

def hash_text(text: str) -> str:
    return hashlib.md5(text.strip().encode('utf-8')).hexdigest()

def track_message(text: str, message_id: int):
    db = load_message_db()
    h = hash_text(text)
    if h not in db: db[h] = []
    if message_id not in db[h]:
        db[h].append(message_id)
        save_message_db(db)

# --------------------------
# Core Logic: Quality Filtering
# --------------------------
def is_quality_repo(repo: Dict) -> bool:
    """
    Returns TRUE if the repo passes the 'Real Project' test.
    Returns FALSE if it looks like fodder.
    """
    # 1. Fetch Stats
    stars = int(repo.get("stargazers_count") or 0)
    forks = int(repo.get("forks_count") or 0)
    size_kb = int(repo.get("size") or 0)
    desc = (repo.get("description") or "").strip()
    name = (repo.get("name") or "").lower()
    owner = repo.get("owner", {}) or {}
    owner_type = (owner.get("type") or "").lower()
    topics = repo.get("topics", [])

    # 2. Immediate Rejections
    if repo.get("fork", False): return False
    
    # 3. Negative Keyword Check (Name & Description)
    full_text = (name + " " + desc).lower()
    for bad in NEGATIVE_KEYWORDS:
        if bad in full_text:
            return False

    # 4. The "Weight" Check
    # Most real DApps/Protocols are > 150KB.
    # Exception: It has > 10 stars (might be a tiny brilliant tool).
    if size_kb < MIN_REPO_SIZE_KB and stars < 10:
        return False

    # 5. The "Effort" Check (Description)
    if len(desc) < MIN_DESC_LENGTH:
        # Allow if it has significant stars (community validation)
        if stars < 5:
            return False

    # 6. The "Generic" Check
    # If a user (not org) has 0 stars/forks and no topics, it's likely noise.
    if owner_type == "user" and stars == 0 and forks == 0:
        if not topics:
            return False

    return True

# --------------------------
# Search Execution
# --------------------------
def github_search(query: str) -> List[Dict]:
    """Sync helper to perform the request."""
    # We sort by 'updated' to catch active projects
    url = f"{GITHUB_API_BASE}/search/repositories?q={query}&sort=updated&order=desc&per_page={SEARCH_PAGE_SIZE}"
    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": USER_AGENT}
    if GH_PAT: headers["Authorization"] = f"token {GH_PAT}"
    
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 403 or r.status_code == 429:
            logger.warning("GitHub Rate Limit hit.")
            return []
        r.raise_for_status()
        return r.json().get("items", [])
    except Exception as e:
        logger.error(f"GitHub Search Error: {e}")
        return []

async def run_scan_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Starting optimized scan...")
    sent_repo_ids = load_json_set(SENT_REPOS_PATH)
    
    # Time Filters
    created_after = (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d") # Fresh projects (6 months)
    pushed_after = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%d")   # Active recently (2 months)

    search_queries = []

    # Strategy 1: Specific Terms (Any Language)
    # Good for finding niche things like "mev bot" regardless of language
    for kw in SPECIFIC_KEYWORDS:
        q = f'"{kw}" created:>{created_after} pushed:>{pushed_after} fork:false'
        search_queries.append((kw, urllib.parse.quote_plus(q)))

    # Strategy 2: Broad Terms (Strict Languages Only)
    # Good for "defi" but only if it's in Rust/Solidity (ignores Python homework)
    for kw in BROAD_KEYWORDS:
        for lang in STRICT_LANGUAGES:
            q = f'{kw} language:{lang} created:>{created_after} pushed:>{pushed_after} fork:false'
            search_queries.append((f"{kw} ({lang})", urllib.parse.quote_plus(q)))

    new_count = 0
    
    for label, query_url in search_queries:
        # Pause briefly to be nice to API
        await asyncio.sleep(2) 
        
        items = await asyncio.get_running_loop().run_in_executor(None, github_search, query_url)
        
        for repo in items:
            repo_id = repo.get("id")
            
            # Dupe Check
            if not repo_id or repo_id in sent_repo_ids: continue
            
            # Quality Check
            if not is_quality_repo(repo): continue

            # If we get here, it's a winner.
            full_name = repo.get('full_name')
            url = repo.get('html_url')
            stars = repo.get('stargazers_count', 0)
            lang = repo.get('language') or 'unknown'
            desc = (repo.get('description') or '').strip()
            size = repo.get('size', 0)
            
            msg_text = (
                f"💎 *{full_name}*\n"
                f"🔗 [View on GitHub]({url})\n"
                f"🏷 {label}\n"
                f"🛠 {lang} | ⭐ {stars} | 📦 {size}KB\n"
                f"_{desc}_"
            )

            try:
                sent_msg = await context.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=msg_text,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
                
                # Update State
                sent_repo_ids.add(repo_id)
                track_message(msg_text, sent_msg.message_id) # Save for cleanup
                new_count += 1
                
                # Soft Cap per run to avoid flood
                if new_count >= 20: 
                    save_json_set(sent_repo_ids, SENT_REPOS_PATH)
                    logger.info("Hit soft limit (20 items). Stopping scan for now.")
                    return 

            except Exception as e:
                logger.error(f"Send failed: {e}")

    save_json_set(sent_repo_ids, SENT_REPOS_PATH)
    logger.info(f"Scan complete. Found {new_count} high-quality repos.")

# --------------------------
# Cleanup Command
# --------------------------
async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replies to a message with /cleanup to delete all its duplicates."""
    if not update.message or not update.message.reply_to_message:
        await update.message.reply_text("⚠ Reply to a message to clean up its duplicates.")
        return

    target_msg = update.message.reply_to_message
    
    # Try to grab text from caption (if image) or body
    text = target_msg.text_markdown or target_msg.caption_markdown or target_msg.text or target_msg.caption
    
    if not text:
        await update.message.reply_text("Could not read message text.")
        return

    h = hash_text(text)
    db = load_message_db()
    
    if h not in db or not db[h]:
        await update.message.reply_text("No duplicates found in database.")
        return

    ids = db[h]
    deleted = 0
    target_id = target_msg.message_id
    
    for mid in ids:
        if mid == target_id: continue
        try:
            await context.bot.delete_message(chat_id=TELEGRAM_CHAT_ID, message_id=mid)
            deleted += 1
        except Exception:
            pass # Message might already be deleted

    # Reset DB to only contain the one we kept
    db[h] = [target_id]
    save_message_db(db)

    # Cleanup the command itself
    try: await update.message.delete()
    except: pass

    # Temp confirmation
    conf = await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🧹 Deleted {deleted} duplicates.")
    await asyncio.sleep(3)
    try: await conf.delete() 
    except: pass

# --------------------------
# Entry Point
# --------------------------
if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Error: Missing secrets (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        sys.exit(1)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.job_queue.run_once(run_scan_job, 5) # First run after 5s
    app.job_queue.run_repeating(run_scan_job, interval=SCAN_INTERVAL_SECONDS, first=SCAN_INTERVAL_SECONDS)
    
    app.add_handler(CommandHandler("cleanup", cleanup_command))

    print(f"Bot 3.0 (Smart Filter Edition) is running...")
    app.run_polling()
