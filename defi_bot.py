#!/usr/bin/env python3
"""
web3_scout_bot

Scans GitHub daily for early Web3/DeFi projects and sends alerts to Telegram.
- Uses three secrets (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GH_PAT).
- Persistent sent-repo tracking saved to data/sent_repo_ids.json and committed back to the repo when run in GitHub Actions.
- Heuristics to filter out "trash" / personal repos while still catching early projects.
- Progressive widening of created:> filter to try to gather at least N results per keyword.
"""

from __future__ import annotations
import os
import sys
import time
import json
import math
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional

# --------------------------
# Configuration (env secrets)
# --------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GH_PAT = os.getenv("GH_PAT")  # used both for GitHub Search and to commit state back in CI

# Repository information (used for committing the sent IDs file in CI)
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")  # e.g. "owner/repo" provided by Actions
GIT_COMMIT_NAME = os.getenv("GIT_COMMIT_NAME", "web3-scout-bot")
GIT_COMMIT_EMAIL = os.getenv("GIT_COMMIT_EMAIL", "web3-scout-bot@users.noreply.github.com")

# Data / runtime config
SENT_REPOS_PATH = "data/sent_repo_ids.json"
PER_KEYWORD_MIN_RESULTS = 15
PER_KEYWORD_LIMIT_PER_RUN = 30    # cap per keyword to avoid spamming
SEARCH_PAGE_SIZE = 100
GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "web3-scout-bot/1.0"

# Initial created window (how new a project should be). We'll progressively widen if not enough results.
INITIAL_CREATED_DAYS = 90
WIDEN_STEPS = [90, 180, 365, None]  # None means no created: qualifier (full history)

# Negative/trash keywords (case-insensitive)
NEGATIVE_KEYWORDS = [
    "tutorial", "demo", "example", "test", "playground", "sample",
    "hackathon", "learning", "course", "homework", "exercise", "template"
]

# Keyword list - web3 / defi oriented. Keep a mix of concrete and broad terms.
KEYWORDS = [
    "defi", "decentralized exchange", "automated market maker", "amm",
    "yield farming", "yield aggregator", "vault", "lending protocol",
    "borrowing protocol", "liquidity pool", "staking", "perpetual futures",
    "options", "stablecoin", "rollup", "zk-rollup", "optimistic rollup",
    "bridge cross-chain", "cross-chain bridge", "oracle", "governance",
    "token", "dex", "dex aggregator", "wallet", "multisig", "smart contract",
     "rust blockchain", "substrate", "evm rollup",
    "layer 2", "zk", "zk proof", 
]

# Languages to prioritize (optional). If a query returns too few items, we'll add language filters to expand matches
PRIORITY_LANGUAGES = ["Solidity", "Rust", "TypeScript", "JavaScript", "Go", "Python"]


# --------------------------
# Utilities
# --------------------------
def send_telegram(message: str) -> bool:
    """Send a message to Telegram. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing. Skipping send.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
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
        print(f"No sent repo file found at {path}. Starting fresh.")
        return set()
    except Exception as e:
        print(f"Failed to load sent repo ids ({e}), starting fresh.")
        return set()


def save_sent_repo_ids_local(sent: Set[int], path: str = SENT_REPOS_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(sent)), f, indent=2)
    print(f"Saved {len(sent)} sent repo ids to {path} (local).")


# Heuristic for "personal / junk" repos:
def is_personal_junk(repo: Dict) -> bool:
    """
    Heuristics to detect likely personal/training/repos to skip:
    - owner.type == "User" AND
      (stars==0 and forks==0 and size small and open_issues small)
    - repo name contains common words like 'exercise' (already covered by NEGATIVE_KEYWORDS)
    These heuristics balance removing obvious junk while keeping early projects.
    """
    owner = repo.get("owner", {}) or {}
    owner_type = owner.get("type", "").lower()
    stars = repo.get("stargazers_count", 0) or 0
    forks = repo.get("forks_count", 0) or 0
    size = repo.get("size", 0) or 0
    open_issues = repo.get("open_issues_count", 0) or 0

    # If it's a fork, treat as less interesting (we exclude forks in query, but keep this defensive)
    if repo.get("fork", False):
        return True

    # If owner is a user and repo is tiny AND has zero traction, consider it personal junk.
    if owner_type == "user" and stars == 0 and forks == 0 and size < 100 and open_issues < 3:
        return True

    return False


def negative_keyword_in(repo: Dict) -> bool:
    """Return True if any negative keyword exists in name or description (case-insensitive)."""
    name = (repo.get("name") or "").lower()
    desc = (repo.get("description") or "").lower()
    for bad in NEGATIVE_KEYWORDS:
        if bad in name or bad in desc:
            return True
    return False


def build_search_query(keyword: str,
                       created_after: Optional[str] = None,
                       pushed_after: Optional[str] = None,
                       language: Optional[str] = None) -> str:
    """
    Build the 'q' parameter for GitHub repository search.
    Examples:
      'defi created:>2025-01-01 pushed:>2025-01-01 fork:false language:Solidity'
    """
    parts = [keyword]
    if created_after:
        parts.append(f"created:>{created_after}")
    if pushed_after:
        parts.append(f"pushed:>{pushed_after}")
    # Exclude forks by default
    parts.append("fork:false")
    if language:
        parts.append(f"language:{language}")
    return "+".join(parts)


def github_search_repos(q: str, page: int = 1, per_page: int = SEARCH_PAGE_SIZE, token: Optional[str] = None) -> Dict:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": USER_AGENT
    }
    if token:
        headers["Authorization"] = f"token {token}"
    url = f"{GITHUB_API_BASE}/search/repositories?q={q}&sort=created&order=desc&per_page={per_page}&page={page}"
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


# --------------------------
# Main scanning logic
# --------------------------
def scan_and_alert():
    if not GH_PAT:
        print("GH_PAT not set. The script will still attempt unauthenticated searches (low rate limit), but commit-back won't work.")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets are missing. Bot will run but cannot send messages.")

    sent_repo_ids = load_sent_repo_ids()
    new_repo_items: List[Dict] = []
    per_keyword_sent_count = {}

    pushed_after = (datetime.utcnow() - timedelta(days=180)).strftime("%Y-%m-%d")  # ensure recent activity

    for kw in KEYWORDS:
        collected_for_kw: List[Dict] = []
        per_keyword_sent_count[kw] = 0
        # Progressive widening of created_after filter
        for days in WIDEN_STEPS:
            if per_keyword_sent_count[kw] >= PER_KEYWORD_MIN_RESULTS or len(collected_for_kw) >= PER_KEYWORD_LIMIT_PER_RUN:
                break

            if days is None:
                created_after = None
            else:
                created_after = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

            # Try without language filter first, then try with priority languages if we still need more
            language_options = [None] + PRIORITY_LANGUAGES if days is not None else [None]

            for lang in language_options:
                page = 1
                # Keep paging until we reach enough results for this keyword or no more results
                while True:
                    q = build_search_query(kw, created_after=created_after, pushed_after=pushed_after, language=lang)
                    try:
                        print(f"[search] kw='{kw}' lang='{lang}' created_after='{created_after}' page={page} q={q}")
                        data = github_search_repos(q, page=page, per_page=SEARCH_PAGE_SIZE, token=GH_PAT)
                    except requests.HTTPError as e:
                        print(f"[search] HTTPError for q={q}: {e} - response: {getattr(e, 'response', None)}")
                        break
                    except Exception as e:
                        print(f"[search] Error for q={q}: {e}")
                        break

                    items = data.get("items", [])
                    if not items:
                        print(f"[search] No items returned for q={q} page={page}")
                        break

                    for repo in items:
                        repo_id = repo.get("id")
                        if not repo_id:
                            continue

                        # Skip if we've already sent it
                        if repo_id in sent_repo_ids:
                            continue

                        # Basic negative filters
                        if negative_keyword_in(repo):
                            continue

                        # Personal junk heuristic
                        if is_personal_junk(repo):
                            continue

                        # At this point, we consider this repo as a candidate
                        collected_for_kw.append(repo)
                        sent_repo_ids.add(repo_id)  # reserve immediately to avoid dupes across keywords
                        per_keyword_sent_count[kw] += 1
                        new_repo_items.append((kw, repo))

                        print(f"[new] kw='{kw}' repo='{repo.get('full_name')}' id={repo_id}")

                        if per_keyword_sent_count[kw] >= PER_KEYWORD_MIN_RESULTS or len(collected_for_kw) >= PER_KEYWORD_LIMIT_PER_RUN:
                            break

                    # If we've reached our per-keyword cap, break paging & language loops
                    if per_keyword_sent_count[kw] >= PER_KEYWORD_MIN_RESULTS or len(collected_for_kw) >= PER_KEYWORD_LIMIT_PER_RUN:
                        break

                    # If GitHub says fewer than page size results then stop, else continue to next page
                    if len(items) < SEARCH_PAGE_SIZE:
                        break
                    page += 1
                    # Respect rate limits a little
                    time.sleep(0.2)

                # stop iterating languages if we've reached min results
                if per_keyword_sent_count[kw] >= PER_KEYWORD_MIN_RESULTS or len(collected_for_kw) >= PER_KEYWORD_LIMIT_PER_RUN:
                    break

        print(f"[summary] keyword='{kw}' found_new={per_keyword_sent_count[kw]}")

    # Prepare and send messages
    if not new_repo_items:
        msg = "😴 No truly new and unsent repos found for today's run."
        print(msg)
        send_telegram(msg)
    else:
        # Group messages into batches to avoid hitting Telegram message length limits
        MAX_REPOS_PER_MSG = 12
        batched = [new_repo_items[i:i+MAX_REPOS_PER_MSG] for i in range(0, len(new_repo_items), MAX_REPOS_PER_MSG)]
        for batch in batched:
            text_lines = []
            for kw, repo in batch:
                full_name = repo.get("full_name")
                html = repo.get("html_url")
                desc = repo.get("description") or ""
                stars = repo.get("stargazers_count", 0)
                language = repo.get("language") or ""
                line = f"🔥 [{full_name}]({html})\nKeyword: {kw}\nLang: {language} ⭐ {stars}\n{desc}\n"
                text_lines.append(line)

            message = "\n".join(text_lines)
            # Send as Markdown (Telegram default). Escape characters lightly is omitted for brevity; if you see issues, switch to plain text.
            send_telegram(message)
            time.sleep(0.15)

    # Save sent repo ids locally and attempt to commit back if in CI with GH_PAT
    save_sent_repo_ids_local(set(sent_repo_ids), SENT_REPOS_PATH)

    # If running in GitHub Actions and we have a PAT, commit the updated file back to the repository so next run avoids repeats.
    if GITHUB_REPOSITORY and GH_PAT:
        try:
            commit_and_push_sent_file(SENT_REPOS_PATH)
        except Exception as e:
            print(f"Failed to commit sent ids back to repo: {e}")


# --------------------------
# Helpers for committing state in CI
# --------------------------
def commit_and_push_sent_file(file_path: str):
    """
    Commit the changed sent_repo_ids.json back to the repo when running in GitHub Actions.
    This uses simple git commands and the GH_PAT to push.
    """
    if not GH_PAT:
        raise RuntimeError("GH_PAT not set - cannot push changes back to repo.")

    # Confirm that we're running inside Actions
    repo = GITHUB_REPOSITORY
    if not repo:
        print("GITHUB_REPOSITORY not set - assuming local run. Skipping commit back.")
        return

    # Configure git and push
    print(f"[git] Preparing to commit {file_path} back to {repo}")
    # Use system git. Ensure we have git in PATH (Actions runner will).
    # We'll set remote URL to include token for push
    push_url = f"https://x-access-token:{GH_PAT}@github.com/{repo}.git"
    run_cmd = lambda cmd: os.system(cmd)  # simple helper

    # Basic git operations
    run_cmd('git config --global user.email "{}"'.format(GIT_COMMIT_EMAIL))
    run_cmd('git config --global user.name "{}"'.format(GIT_COMMIT_NAME))
    # Ensure the file is tracked and commit
    run_cmd(f'git add "{file_path}" || true')
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    run_cmd(f'git commit -m "web3-scout-bot: update sent_repo_ids ({timestamp})" || true')
    # Push using token
    rc = os.system(f'git push "{push_url}" HEAD:main || true')
    if rc != 0:
        print("[git] Push returned non-zero code (note: git commands above use || true to avoid failing the job).")
    else:
        print("[git] Pushed sent_repo_ids back to repository.")


# --------------------------
# Entrypoint
# --------------------------
if __name__ == "__main__":
    try:
        scan_and_alert()
    except KeyboardInterrupt:
        print("Interrupted")
    except Exception as e:
        print(f"Fatal error: {e}")
        # Notify in Telegram (best effort)
        try:
            send_telegram(f"🚨 web3-scout-bot error: {e}")
        except Exception:
            pass
        sys.exit(1)
