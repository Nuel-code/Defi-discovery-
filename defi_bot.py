from __future__ import annotations

import os
import sys
import time
import json
import logging
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Dict, Set, Tuple, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from html import escape as html_escape

# --------------------------
# Configuration
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("Web3Scout")

# Secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GH_PAT = os.getenv("GH_PAT")

# Paths
DATA_DIR = "data"
RUN_HISTORY_DIR = f"{DATA_DIR}/runs"
SENT_REPOS_PATH = f"{DATA_DIR}/sent_repo_ids.json"
LATEST_RUN_PATH = f"{DATA_DIR}/latest_run.json"
ALL_STARTUPS_PATH = f"{DATA_DIR}/all_startups.json"

# Tuning
CREATED_DAYS_AGO = 90
MIN_SCORE_THRESHOLD = 10
SEARCH_PAGE_SIZE = 100
PER_KEYWORD_LIMIT = 15
PAGES_PER_KEYWORD = 2

# “Unknown gems” constraints
MAX_STARS = 80
MAX_FORKS = 40
MIN_SIZE_KB = 200
MAX_INACTIVE_DAYS = 21
REQUIRE_LICENSE = True
REQUIRE_DESCRIPTION = True

USER_AGENT = "web3-scout-v6"

# --------------------------
# The "Brain" (Keywords & Scoring)
# --------------------------
TRASH_TERMS = [
    "tutorial", "demo", "example", "test", "playground", "sample",
    "starter", "boilerplate", "course", "assignment", "homework",
    "learning", "practice", "101", "hello-world", "my-first",
    "scaffold", "template", "roadmap", "interview", "challenge",
    "curated list", "collection", "awesome", "personal site",
]

PRO_TERMS = [
    "protocol", "finance", "defi", "dex", "swap", "dao", "chain", "network",
    "labs", "ventures", "foundation", "exchange", "market", "arbitrage",
    "mev", "flashloan", "bot", "solana", "ethereum", "zk", "rollup",
    "lending", "borrowing", "bridge", "cross-chain", "stablecoin",
]

KEYWORDS = [
    "defi", "decentralized exchange", "automated market maker", "btc",
    "yield farming", "yield aggregator", "lending protocol",
    "borrowing protocol", "liquidity pool", "staking", "perpetual futures",
    "stablecoin", "rollup", "optimistic rollup",
    "bridge cross-chain", "cross-chain bridge",
    "token", "dex", "dex aggregator", "wallet",
    "rust blockchain", "layer 2",
]

PRIORITY_LANGUAGES = ["Solidity", "Rust", "TypeScript", "JavaScript", "Go", "Python"]

# --------------------------
# Infrastructure
# --------------------------
def get_github_session() -> requests.Session:
    s = requests.Session()

    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))

    headers = {
        "Accept": "application/vnd.github.mercy-preview+json",
        "User-Agent": USER_AGENT,
    }
    if GH_PAT:
        headers["Authorization"] = f"Bearer {GH_PAT}"

    s.headers.update(headers)
    return s


def ensure_data_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(RUN_HISTORY_DIR, exist_ok=True)


def load_history() -> Set[int]:
    ensure_data_dirs()

    if not os.path.exists(SENT_REPOS_PATH):
        return set()

    try:
        with open(SENT_REPOS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except Exception:
        try:
            os.rename(SENT_REPOS_PATH, f"{SENT_REPOS_PATH}.bak")
        except Exception:
            pass
        return set()


def save_history(history: Set[int]) -> None:
    ensure_data_dirs()
    try:
        with open(SENT_REPOS_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(history), f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save history: {e}")


def parse_utc(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def handle_rate_limit(resp: requests.Response) -> bool:
    """
    Returns True if we slept due to rate limit and should retry.
    """
    if resp.status_code not in (403, 429):
        return False

    remaining = resp.headers.get("X-RateLimit-Remaining")
    reset = resp.headers.get("X-RateLimit-Reset")
    msg = resp.text[:200].replace("\n", " ")

    if remaining == "0" and reset:
        try:
            reset_ts = int(reset)
            now_ts = int(time.time())
            sleep_s = max(5, (reset_ts - now_ts) + 3)
            logger.warning(f"Rate limit hit. Sleeping {sleep_s}s. ({msg})")
            time.sleep(sleep_s)
            return True
        except Exception:
            pass

    logger.warning(f"Rate/abuse limit hit. Cooling down 20s. ({msg})")
    time.sleep(20)
    return True


# --------------------------
# Hard Filters (stop junk early)
# --------------------------
def passes_hard_filters(repo: Dict) -> Tuple[bool, str]:
    if repo.get("fork") or repo.get("archived"):
        return False, "fork/archived"

    desc = (repo.get("description") or "").strip()
    if REQUIRE_DESCRIPTION and not desc:
        return False, "no_description"

    if REQUIRE_LICENSE and not repo.get("license"):
        return False, "no_license"

    stars = int(repo.get("stargazers_count", 0) or 0)
    forks = int(repo.get("forks_count", 0) or 0)
    size_kb = int(repo.get("size", 0) or 0)

    if stars > MAX_STARS or forks > MAX_FORKS:
        return False, "too_popular"

    if size_kb < MIN_SIZE_KB:
        return False, "too_small"

    pushed = parse_utc(repo.get("pushed_at"))
    if not pushed:
        return False, "no_pushed_at"

    if pushed < (datetime.utcnow() - timedelta(days=MAX_INACTIVE_DAYS)):
        return False, "inactive"

    return True, "ok"


# --------------------------
# Scoring (refined)
# --------------------------
def calculate_quality_score(repo: Dict) -> Tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []

    name = (repo.get("name") or "").lower()
    desc = (repo.get("description") or "").lower()
    topics = [t.lower() for t in (repo.get("topics") or [])]
    owner_type = repo.get("owner", {}).get("type", "User")
    homepage = (repo.get("homepage") or "").strip()
    has_license = repo.get("license") is not None
    size_kb = int(repo.get("size", 0) or 0)
    stars = int(repo.get("stargazers_count", 0) or 0)
    lang = (repo.get("language") or "").strip()

    text_corpus = f"{name} {desc} {' '.join(topics)}"

    if any(bad in text_corpus for bad in TRASH_TERMS):
        score -= 50
        reasons.append("Contains trash terms")

    if owner_type == "Organization":
        score += 12
        reasons.append("🏛️ Organization")

    if homepage:
        score += 6
        reasons.append("🔗 Has website")

    if has_license:
        score += 8
        reasons.append("📜 Licensed")

    if lang in PRIORITY_LANGUAGES:
        score += 6
        reasons.append(f"🧠 {lang}")

    pro_hits = sum(1 for pro in PRO_TERMS if pro in text_corpus)
    if pro_hits >= 2:
        score += 8
        reasons.append("🧩 DeFi/protocol signals")
    elif pro_hits == 1:
        score += 3

    if owner_type == "User" and stars < 3:
        score -= 8
        reasons.append("Likely personal repo")

    if size_kb >= 800:
        score += 4
        reasons.append("📦 Substantial codebase")

    if stars >= 10:
        score += 2
        reasons.append("⭐ Some traction")

    return score, reasons


# --------------------------
# JSON Export
# --------------------------
def build_repo_record(
    repo: Dict,
    score: int,
    reasons: List[str],
    keyword: str,
    telegram_sent: bool,
) -> Dict:
    return {
        "repo_id": repo.get("id"),
        "name": repo.get("name"),
        "full_name": repo.get("full_name"),
        "html_url": repo.get("html_url"),
        "description": repo.get("description"),
        "language": repo.get("language"),
        "stars": int(repo.get("stargazers_count", 0) or 0),
        "forks": int(repo.get("forks_count", 0) or 0),
        "size_kb": int(repo.get("size", 0) or 0),
        "score": score,
        "reasons": reasons,
        "found_via": keyword,
        "homepage": repo.get("homepage"),
        "topics": repo.get("topics") or [],
        "created_at": repo.get("created_at"),
        "updated_at": repo.get("updated_at"),
        "pushed_at": repo.get("pushed_at"),
        "telegram_sent": telegram_sent,
        "links": {
            "github": repo.get("html_url"),
            "website": repo.get("homepage") or None,
        },
        "owner": {
            "login": repo.get("owner", {}).get("login"),
            "type": repo.get("owner", {}).get("type"),
        },
        "license": (repo.get("license") or {}).get("spdx_id"),
    }


def save_run_json(results: List[Dict]) -> None:
    ensure_data_dirs()

    generated_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    timestamp_for_file = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")

    sent_count = sum(1 for r in results if r.get("telegram_sent"))

    payload = {
        "generated_at": generated_at,
        "summary": {
            "match_count": len(results),
            "telegram_sent_count": sent_count,
            "keywords_scanned": len(KEYWORDS),
        },
        "startups": results,
    }

    try:
        with open(LATEST_RUN_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        history_path = f"{RUN_HISTORY_DIR}/run_{timestamp_for_file}.json"
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        logger.info(f"Saved latest run JSON to {LATEST_RUN_PATH}")
        logger.info(f"Saved history run JSON to {history_path}")
    except Exception as e:
        logger.error(f"Failed to save run JSON: {e}")


def load_all_startups() -> List[Dict]:
    ensure_data_dirs()

    if not os.path.exists(ALL_STARTUPS_PATH):
        return []

    try:
        with open(ALL_STARTUPS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("startups"), list):
                return data["startups"]
            return []
    except Exception as e:
        logger.error(f"Failed to load all startups: {e}")
        return []


def save_all_startups(startups: List[Dict]) -> None:
    ensure_data_dirs()

    payload = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {
            "total_count": len(startups),
        },
        "startups": startups,
    }

    try:
        with open(ALL_STARTUPS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved cumulative startup history to {ALL_STARTUPS_PATH}")
    except Exception as e:
        logger.error(f"Failed to save all startups: {e}")


def merge_startups(existing: List[Dict], new_results: List[Dict]) -> List[Dict]:
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    merged = {item["repo_id"]: item for item in existing if item.get("repo_id") is not None}

    for item in new_results:
        repo_id = item.get("repo_id")
        if repo_id is None:
            continue

        if repo_id in merged:
            old = merged[repo_id]
            item["first_seen_at"] = old.get("first_seen_at", now)
            item["last_seen_at"] = now
            item["seen_count"] = int(old.get("seen_count", 1)) + 1
        else:
            item["first_seen_at"] = now
            item["last_seen_at"] = now
            item["seen_count"] = 1

        merged[repo_id] = item

    return sorted(
        merged.values(),
        key=lambda x: x.get("last_seen_at") or "",
        reverse=True,
    )


# --------------------------
# Telegram (HTML, safe)
# --------------------------
def send_telegram_card(repo: Dict, score: int, reasons: List[str], keyword: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured (missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID).")
        return False

    name = repo.get("full_name") or repo.get("name") or "Unnamed"
    url = repo.get("html_url") or ""
    desc = (repo.get("description") or "No description provided.").strip()
    if len(desc) > 220:
        desc = desc[:217] + "..."

    lang = repo.get("language") or "Code"
    stars = int(repo.get("stargazers_count", 0) or 0)
    forks = int(repo.get("forks_count", 0) or 0)

    pushed_at = parse_utc(repo.get("pushed_at"))
    freshness = pushed_at.strftime("%d %b") if pushed_at else "Unknown"

    display_tags = [r for r in reasons if not any(x in r.lower() for x in ["trash", "personal"])]
    tags_str = " • ".join(display_tags[:3]) if display_tags else "New discovery"

    msg = (
        f"💎 <b>{html_escape(name)}</b> <code>{html_escape(lang)}</code>\n"
        f"<i>{html_escape(desc)}</i>\n\n"
        f"📊 <code>{stars}⭐</code>  <code>{forks}🍴</code>  <code>{html_escape(freshness)}📅</code>\n"
        f"🧠 Score: <b>{score}</b>\n"
        f"✅ {html_escape(tags_str)}\n"
        f"🔎 Found via <code>{html_escape(keyword)}</code>\n"
        f"🔗 <a href=\"{html_escape(url)}\">View on GitHub</a>"
    )

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        r = requests.post(api_url, json=payload, timeout=12)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


# --------------------------
# Main
# --------------------------
def run_scout() -> None:
    logger.info("--- Starting Web3 Scout v6 (Hard Filters + Persistent Cache + JSON Export + History) ---")

    ensure_data_dirs()
    session = get_github_session()
    sent_ids = load_history()

    created_since = (datetime.utcnow() - timedelta(days=CREATED_DAYS_AGO)).strftime("%Y-%m-%d")
    new_count = 0
    matched_results: List[Dict] = []

    for kw in KEYWORDS:
        logger.info(f"Scanning: {kw}")
        kw_hits = 0

        for page in range(1, PAGES_PER_KEYWORD + 1):
            if kw_hits >= PER_KEYWORD_LIMIT:
                break

            q = f'{kw} created:>{created_since} fork:false'
            encoded_q = urllib.parse.quote_plus(q)
            url = (
                "https://api.github.com/search/repositories"
                f"?q={encoded_q}&sort=updated&order=desc&per_page={SEARCH_PAGE_SIZE}&page={page}"
            )

            try:
                resp = session.get(url, timeout=18)

                if handle_rate_limit(resp):
                    continue

                resp.raise_for_status()
                data = resp.json()
                items = data.get("items", []) or []
                if not items:
                    break

                for repo in items:
                    if kw_hits >= PER_KEYWORD_LIMIT:
                        break

                    rid = repo.get("id")
                    if not rid:
                        continue

                    if rid in sent_ids:
                        continue

                    ok, _why = passes_hard_filters(repo)
                    if not ok:
                        continue

                    score, reasons = calculate_quality_score(repo)
                    if score < MIN_SCORE_THRESHOLD:
                        continue

                    full_name = repo.get("full_name") or repo.get("name") or "unknown"
                    logger.info(f"   [MATCH] {full_name} | score={score} | reasons={reasons}")

                    success = send_telegram_card(repo, score, reasons, kw)

                    record = build_repo_record(
                        repo=repo,
                        score=score,
                        reasons=reasons,
                        keyword=kw,
                        telegram_sent=success,
                    )
                    matched_results.append(record)

                    if success:
                        sent_ids.add(rid)
                        save_history(sent_ids)
                        kw_hits += 1
                        new_count += 1
                        time.sleep(0.6)

                time.sleep(1.5)

            except Exception as e:
                logger.error(f"Error on keyword '{kw}': {e}")
                time.sleep(6)

    save_run_json(matched_results)

    existing = load_all_startups()
    merged = merge_startups(existing, matched_results)
    save_all_startups(merged)

    logger.info(f"🏁 Scout finished. Found {new_count} new gems. Total matched: {len(matched_results)}")


if __name__ == "__main__":
    try:
        run_scout()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.critical(f"Main crash: {e}")
        sys.exit(1)
