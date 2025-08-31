#!/usr/bin/env python3
"""
web3_scout_bot (updated)

Changes in this version:
- Removed the repo "size" check entirely from is_personal_junk().
- Fixed the typo/bug (Tru1 -> True).
- Ensure searches only return repos created in the last 6 months (180 days).
- Keep sorting by "updated" (so recently updated repos appear first).
- Maintains negative keyword filtering, personal-junk heuristics (without size),
  Telegram sending, and persistent sent_repo_ids state.
"""
from __future__ import annotations
import os
import sys
import time
import json
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional
from pathlib import Path
import urllib.parse

# --------------------------
# Configuration (env secrets)
# --------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GH_PAT = os.getenv("GH_PAT")  # used for authenticated GitHub queries

# Data / runtime config
DATA_DIR = "data"
SENT_REPOS_PATH = f"{DATA_DIR}/sent_repo_ids.json"
PER_KEYWORD_MIN_RESULTS = 20
PER_KEYWORD_LIMIT_PER_RUN = 30    # cap per keyword to avoid spamming
SEARCH_PAGE_SIZE = 100
GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "web3-scout-bot/1.0"

# Force created:> to last 6 months
CREATED_DAYS = 180

# Negative/trash keywords (case-insensitive)
NEGATIVE_KEYWORDS = [
    "tutorial", "demo", "example", "test", "playground", "sample",
    "hackathon", "learning", "course", "homework", "exercise", "template", "bot"
]

# Keyword list - web3 / defi oriented
KEYWORDS = [
    "defi", "decentralized exchange", "automated market maker", "btc",
    "yield farming", "yield aggregator","lending protocol",
    "borrowing protocol", "liquidity pool", "staking", "perpetual futures",
    "stablecoin", "rollup", "optimistic rollup",
    "bridge cross-chain", "cross-chain bridge",
    "token", "dex", "dex aggregator", "wallet",
    "rust blockchain", "layer 2",
]

PRIORITY_LANGUAGES = ["Solidity", "Rust", "TypeScript", "JavaScript", "Go", "Python"]

# --------------------------
# Utilities
# --------------------------
def send_telegram(message: str) -> bool:
    """Send a message to Telegram. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID. Skipping send.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[telegram] send error: {e} - resp: {getattr(e, 'response', None)}")
        return False


def load_sent_repo_ids(path: str = SENT_REPOS_PATH) -> Set[int]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(int(x) for x in data)
    except FileNotFoundError:
        print(f"[state] No sent repo file at {path}. Starting fresh.")
        return set()
    except Exception as e:
        print(f"[state] Failed to load sent repo ids ({e}), starting fresh.")
        return set()


def save_sent_repo_ids_local(sent: Set[int], path: str = SENT_REPOS_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(sent)), f, indent=2)
    print(f"[state] Saved {len(sent)} sent repo ids to {path}.")


def is_personal_junk(repo: Dict) -> bool:
    """
    Heuristics for likely personal/junk repos to reduce noise.

    NOTE: size checks removed entirely per request.
    Keeps:
      - skip forks
      - skip very low-signals personal repos (owner.type == 'User' and zero stars & forks & very few issues)
    """
    owner = repo.get("owner", {}) or {}
    owner_type = (owner.get("type") or "").lower()
    stars = int(repo.get("stargazers_count") or 0)
    forks = int(repo.get("forks_count") or 0)
    open_issues = int(repo.get("open_issues_count") or 0)

    # Skip forks
    if repo.get("fork", False):
        print(f"[skip-reason] fork: {repo.get('full_name')}")
        return True

    # Owner is user + zero traction + almost no issues -> likely personal toy repo
    # No size check here by design.
    if owner_type == "user" and stars == 0 and forks == 0 and open_issues <= 1:
        print(f"[skip-reason] personal low-signal repo: {repo.get('full_name')} stars={stars} forks={forks} issues={open_issues}")
        return True

    return False


def negative_keyword_in(repo: Dict) -> bool:
    """Return True if any negative keyword exists in name or description."""
    name = (repo.get("name") or "").lower()
    desc = (repo.get("description") or "").lower()
    for bad in NEGATIVE_KEYWORDS:
        if bad in name or bad in desc:
            return True
    return False


def build_search_q(keyword: str,
                   created_after: Optional[str] = None,
                   pushed_after: Optional[str] = None,
                   language: Optional[str] = None) -> str:
    parts = [keyword]
    if created_after:
        parts.append(f"created:>{created_after}")
    if pushed_after:
        parts.append(f"pushed:>{pushed_after}")
    parts.append("fork:false")
    if language:
        parts.append(f"language:{language}")
    # Join with spaces then URL-encode
    q = " ".join(parts)
    return urllib.parse.quote_plus(q)


def github_search_repos(q_encoded: str, page: int = 1, per_page: int = SEARCH_PAGE_SIZE, token: Optional[str] = None) -> Dict:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": USER_AGENT
    }
    if token:
        headers["Authorization"] = f"token {token}"
    # Keep sorting by updated as you requested
    url = f"{GITHUB_API_BASE}/search/repositories?q={q_encoded}&sort=updated&order=desc&per_page={per_page}&page={page}"
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


# --------------------------
# Main scanning logic
# --------------------------
def scan_and_alert():
    print(f"[start] UTC now: {datetime.utcnow().isoformat()} | GH_PAT set: {bool(GH_PAT)}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[start] Telegram secrets are missing; messages will not be sent (but script will run).")

    sent_repo_ids = load_sent_repo_ids()
    new_repo_items: List[Dict] = []
    per_keyword_sent_count = {}

    # created filter = last 6 months
    created_after = (datetime.utcnow() - timedelta(days=CREATED_DAYS)).strftime("%Y-%m-%d")
    # prefer repos with activity within the last 90 days (keeps them relevant)
    pushed_after = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")

    for kw in KEYWORDS:
        per_keyword_sent_count[kw] = 0
        collected_for_kw: List[Dict] = []

        # try first without language bias, then with priority languages if needed
        language_options = [None] + PRIORITY_LANGUAGES

        for lang in language_options:
            page = 1
            while True:
                q_enc = build_search_q(kw, created_after=created_after, pushed_after=pushed_after, language=lang)
                print(f"[search] kw='{kw}' lang='{lang}' created_after='{created_after}' page={page} q={urllib.parse.unquote_plus(q_enc)}")
                try:
                    data = github_search_repos(q_enc, page=page, per_page=SEARCH_PAGE_SIZE, token=GH_PAT)
                except requests.HTTPError as e:
                    print(f"[search] HTTPError for q={q_enc}: {e} - resp: {getattr(e, 'response', None)}")
                    break
                except Exception as e:
                    print(f"[search] Error for q={q_enc}: {e}")
                    break

                items = data.get("items", [])
                total_count = data.get("total_count", 0)
                print(f"[search] total_count={total_count}, items_on_page={len(items)}")

                if not items:
                    break

                for repo in items:
                    repo_id = repo.get("id")
                    if not repo_id:
                        continue

                    if repo_id in sent_repo_ids:
                        # show which repo was skipped due to prior send
                        print(f"[skip] already sent id={repo_id} full_name={repo.get('full_name')}")
                        continue

                    if negative_keyword_in(repo):
                        print(f"[skip] negative keyword matched id={repo_id} name={repo.get('full_name')}")
                        continue

                    if is_personal_junk(repo):
                        print(f"[skip] personal junk heuristics id={repo_id} name={repo.get('full_name')}")
                        continue

                    # candidate accepted
                    created_at = repo.get("created_at", "unknown")
                    pushed_at = repo.get("pushed_at", "unknown")
                    collected_for_kw.append(repo)

                    # attempt to send; if send succeeds mark as sent
                    message = (f"🔥 [{repo.get('full_name')}]({repo.get('html_url')})\n"
                               f"Keyword: {kw}\n"
                               f"Lang: {repo.get('language') or 'unknown'} ⭐ {repo.get('stargazers_count',0)}\n"
                               f"Created: {created_at} — Last push: {pushed_at}\n"
                               f"{(repo.get('description') or '').strip()}\n")
                    sent_ok = send_telegram(message)
                    if sent_ok:
                        sent_repo_ids.add(repo_id)
                        per_keyword_sent_count[kw] += 1
                        new_repo_items.append((kw, repo))
                        print(f"[new] sent id={repo_id} name={repo.get('full_name')} created={created_at} pushed={pushed_at}")
                    else:
                        print(f"[warn] Failed to send Telegram for id={repo_id}; not marking as sent so it can be retried.")

                    if per_keyword_sent_count[kw] >= PER_KEYWORD_MIN_RESULTS or len(collected_for_kw) >= PER_KEYWORD_LIMIT_PER_RUN:
                        break

                if per_keyword_sent_count[kw] >= PER_KEYWORD_MIN_RESULTS or len(collected_for_kw) >= PER_KEYWORD_LIMIT_PER_RUN:
                    break

                if len(items) < SEARCH_PAGE_SIZE:
                    break
                page += 1
                time.sleep(0.2)

            if per_keyword_sent_count[kw] >= PER_KEYWORD_MIN_RESULTS or len(collected_for_kw) >= PER_KEYWORD_LIMIT_PER_RUN:
                break

        print(f"[summary] keyword='{kw}' found_new={per_keyword_sent_count[kw]}")

    if not new_repo_items:
        msg = "😴 No truly new and unsent repos found for today's run."
        print(msg)
        send_telegram(msg)
    else:
        print(f"[done] Total new repos sent this run: {len(new_repo_items)}")

    # Save state locally
    save_sent_repo_ids_local(set(sent_repo_ids), SENT_REPOS_PATH)


if __name__ == "__main__":
    try:
        scan_and_alert()
    except KeyboardInterrupt:
        print("Interrupted")
    except Exception as e:
        print(f"Fatal error: {e}")
        try:
            send_telegram(f"🚨 web3-scout-bot error: {e}")
        except Exception:
            pass
        sys.exit(1)
