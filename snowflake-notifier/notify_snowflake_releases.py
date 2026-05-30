#!/usr/bin/env python3
"""
Snowflake Release Notifier
Spec: SNOWFLAKE_RELEASE_NOTIFIER_SPEC_20260505.md Rev 2.1

Changelog:
  2026-05-09  Added digest page generation (guide: Snowflake_Notifier_Digest_Page_20260509.md)
  2026-05-25  Fix: bypass 24h time filter for html_scrape sources (release notes now dedupe-gated only)
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NTFY_URL       = os.environ["NTFY_URL"]
NTFY_TOKEN     = os.environ["NTFY_TOKEN"]
OLLAMA_URL     = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
KEYWORDS_FILE  = os.environ.get("KEYWORDS_FILE", "/etc/snowflake-notifier/keywords.json")
SOURCES_FILE   = os.environ.get("SOURCES_FILE", "/etc/snowflake-notifier/sources.json")
STATE_DIR      = Path(os.environ.get("STATE_DIR",
                  str(Path.home() / ".local/share/snowflake-notifier")))

SUMMARY_CACHE_FILE  = STATE_DIR / "summary_cache.json"
NOTIFIED_URLS_FILE  = STATE_DIR / "notified_urls.json"
RUN_STATE_FILE      = STATE_DIR / "run_state.json"

OLLAMA_TIMEOUT      = 240   # seconds per generate call
NTFY_RETRY_DELAYS   = [2, 4, 8]

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception as e:
        log.warning("Could not read %s: %s", path, e)
    return default

def save_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        log.error("Could not write %s: %s — attempting /tmp fallback", path, e)
        try:
            fallback = Path("/tmp") / path.name
            fallback.write_text(json.dumps(data, indent=2, default=str))
            log.warning("Saved to fallback: %s", fallback)
        except Exception as e2:
            log.error("Fallback write also failed: %s", e2)

def prune_dict_by_date(items: dict, max_days: int) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).date().isoformat()
    return {k: v for k, v in items.items() if str(v) >= cutoff}

# ---------------------------------------------------------------------------
# Keyword scoring
# ---------------------------------------------------------------------------

def load_keywords(path: str) -> list:
    """Returns list of (weight, [keywords]) tuples."""
    data = json.loads(Path(path).read_text())
    tiers = []
    for tier_data in data.values():
        tiers.append((tier_data["weight"], [kw.lower() for kw in tier_data["keywords"]]))
    return tiers

def score_item(title: str, text: str, tiers: list) -> tuple:
    combined = (title + " " + text).lower()
    score = 0
    matched = set()
    for weight, keywords in tiers:
        for kw in keywords:
            if kw in combined:
                score += weight
                matched.add(kw)
    return score, sorted(matched)

def stars_for_score(score: int) -> str:
    if score >= 3:
        return "⭐⭐⭐"
    if score == 2:
        return "⭐⭐"
    if score == 1:
        return "⭐"
    return ""

# ---------------------------------------------------------------------------
# Text sanitization
# ---------------------------------------------------------------------------

def sanitize_for_ollama(text: str, max_chars: int = 2000) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]

# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_rss(source: dict) -> list:
    url = source["url"]
    log.info("Fetching RSS: %s", url)
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        log.warning("RSS fetch failed for %s: %s", url, e)
        return []

    items = []
    for entry in feed.entries:
        title = getattr(entry, "title", "")
        link  = getattr(entry, "link", "")
        if not link:
            continue

        # Publish time
        pub = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if pub is None:
            log.warning("No date for entry '%s' in %s — skipping", title, url)
            continue
        published_at = datetime(*pub[:6], tzinfo=timezone.utc)

        # Body text
        if hasattr(entry, "content") and entry.content:
            raw_text = entry.content[0].value
        elif hasattr(entry, "summary"):
            raw_text = entry.summary
        else:
            raw_text = ""

        items.append({
            "title": title,
            "url": link,
            "published_at": published_at,
            "raw_text": raw_text,
            "source": source["name"],
            "source_type": "rss",
            "emoji": source["emoji"],
        })

    log.info("  fetched %d entries from %s", len(items), source["name"])
    return items


def extract_date_from_title(title: str):
    """
    Handles four date formats found in Snowflake release notes:

    1. Feature update (current):   "May 28, 2026 - Gemini 3.5 Flash..."
    2. Weekly bundle (current):    "May 22-27, 2026 - 10.19 Release Notes"
    3. Announcement (legacy):      "May 5, 2026: Some feature (GA)"
    4. Bundle range (legacy):      "10.15 Release Notes: Apr 24, 2026-May 2, 2026"

    Formats 1 and 3 are unified (dash or colon after the date).
    Format 2 uses a date range; the start date is returned.
    """
    # Patterns 1 + 3: single date at start followed by " - " or ":"
    m = re.search(r'^(\w+ \d+,\s*\d{4})\s*[-:]', title)
    if m:
        return m.group(1)
    # Pattern 2: date range at start "Month DD-DD, YYYY - ..."
    m = re.search(r'^(\w+ \d+)-\d+,\s*(\d{4})\s+-', title)
    if m:
        return f"{m.group(1)}, {m.group(2)}"
    # Pattern 4 (legacy): single date at end after dash
    m = re.search(r'-\s*(\w+ \d+,\s*\d{4})\s*$', title)
    if m:
        return m.group(1)
    return None

def fetch_html(source: dict) -> list:
    url = source["url"]
    rules = source.get("parse_rules", {})
    log.info("Fetching HTML: %s", url)
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "snowflake-notifier/1.0"})
        resp.raise_for_status()
    except Exception as e:
        log.warning("HTML fetch failed for %s: %s", url, e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    items = []

    container_sel         = rules.get("selector_item", "")
    title_sel             = rules.get("selector_title", "")
    content_sel           = rules.get("selector_content", "")
    date_from_title       = rules.get("date_from_title", False)
    href_must_contain     = rules.get("href_must_contain", "")
    href_must_not_contain = rules.get("href_must_not_contain", "")

    if not container_sel or container_sel.startswith("TBD"):
        log.warning("parse_rules not filled in for %s — skipping HTML scrape", source["name"])
        return []

    # Derive base URL from the source URL so root-relative hrefs resolve correctly.
    # Hardcoding a path prefix breaks when the docs site restructures its URLs.
    parsed_source = urlparse(url)
    base_url = f"{parsed_source.scheme}://{parsed_source.netloc}"

    for container in soup.select(container_sel):
        title_el = container.select_one(title_sel) if title_sel else None
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        # Build absolute URL
        link = ""
        if title_el and title_el.name == "a" and title_el.get("href"):
            href = title_el["href"]
            link = href if href.startswith("http") else base_url + "/" + href.lstrip("/")
        if not link:
            link = url + "#" + re.sub(r"\W+", "-", title.lower())[:60]

        # Apply href allow-list / deny-list filters.
        # href_must_contain="/release-notes/2" captures year-versioned content
        # while excluding navigation items, driver changelogs, etc.
        if href_must_contain and href_must_contain not in link:
            continue
        if href_must_not_contain and href_must_not_contain in link:
            continue

        # Extract date
        if date_from_title:
            date_str = extract_date_from_title(title)
            if not date_str:
                log.warning("Could not extract date from title '%s' — skipping", title[:80])
                continue
        else:
            date_el = container.select_one(rules.get("selector_date", "")) \
                      if rules.get("selector_date") else None
            date_str = date_el.get_text(strip=True) if date_el else ""

        try:
            published_at = dateutil_parser.parse(date_str, fuzzy=True)
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
        except Exception:
            log.warning("Could not parse date '%s' for '%s' — skipping", date_str, title[:60])
            continue

        content_el = container.select_one(content_sel) if content_sel else None
        raw_text = content_el.get_text(separator=" ", strip=True) if content_el else ""

        items.append({
            "title":        title,
            "url":          link,
            "published_at": published_at,
            "raw_text":     raw_text,
            "source":       source["name"],
            "source_type":  "html_scrape",
            "emoji":        source["emoji"],
        })

    log.info("  fetched %d entries from %s", len(items), source["name"])
    if len(items) == 0:
        log.warning(
            "  html_scrape source '%s' returned 0 entries — CSS selector may be stale. "
            "Run the selector diagnostic in the guide.",
            source["name"],
        )
    return items


# ---------------------------------------------------------------------------
# Filter pipeline (order matters per spec § 2)
# ---------------------------------------------------------------------------

def filter_items(items: list, notified_urls: dict) -> list:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    # html_scrape sources bypass the time gate: the page lists the full index on
    # every scrape, so "new" means not yet in notified_urls, not "published today".
    # RSS sources keep the 24h gate (timestamps are accurate; avoids surfacing
    # weeks-old blog posts if the feed fills up with backfill).
    after_time = []
    rss_skipped = 0
    for i in items:
        if i.get("source_type") == "html_scrape":
            after_time.append(i)
        elif i["published_at"] >= cutoff:
            after_time.append(i)
        else:
            rss_skipped += 1
    log.info("After time filter: %d / %d items (%d RSS item(s) too old, html_scrape bypasses)",
             len(after_time), len(items), rss_skipped)

    after_dedup = [i for i in after_time if i["url"] not in notified_urls.get("items", {})]
    log.info("After dedupe filter: %d / %d items", len(after_dedup), len(after_time))

    return after_dedup

# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def ollama_healthy() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        return r.status_code == 200
    except Exception:
        return False

def ollama_summarize(text: str) -> str:
    sanitized = sanitize_for_ollama(text)
    prompt = (
        "You are summarizing a technical article for a daily Snowflake digest.\n"
        "Write 1-2 sentences in plain English. Keep technical terms "
        "(Cortex, semantic view, lineage, etc.) verbatim.\n"
        "Do not start with 'This article' or 'The post'. Do not add a preamble.\n\n"
        f"Article:\n{sanitized}\n\nSummary:"
    )
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 100},
    }
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            timeout=OLLAMA_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        raise RuntimeError(f"Ollama generate failed: {e}") from e

# ---------------------------------------------------------------------------
# Notification format
# ---------------------------------------------------------------------------

def build_body(top_5: list, additional: list, degraded: bool) -> str:
    date_str = datetime.now().strftime("%B %-d, %Y")
    lines = [f"🎉 SNOWFLAKE PULSE — {date_str}", ""]

    if top_5:
        lines += ["━" * 39, "TOP TODAY", "━" * 39, ""]
        for i, item in enumerate(top_5, 1):
            stars = stars_for_score(item["score"])
            kws   = ", ".join(item["matched"])
            lines.append(f"{i}. {item['emoji']} {item['title']}")
            lines.append(item["summary"])
            lines.append(f"{stars} {kws}")
            lines.append(f"→ {item['url']}")
            lines.append("")

    if additional:
        lines += ["━" * 39, "ALSO TODAY", "━" * 39, ""]
        for i, item in enumerate(additional, len(top_5) + 1):
            lines.append(f"{i}. {item['emoji']} {item['title']}")
            lines.append(f"   → {item['url']}")
            lines.append("")

    if degraded:
        lines.append("⚠️ Summaries unavailable (Ollama offline)")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# ntfy POST with retry
# ---------------------------------------------------------------------------

def post_ntfy(body: str) -> bool:
    headers = {
        "Authorization": f"Bearer {NTFY_TOKEN}",
        "Title": "Snowflake Pulse",
        "Priority": "default",
        "Tags": "snowflake,data",
    }
    for attempt, delay in enumerate([0] + NTFY_RETRY_DELAYS, 1):
        if delay:
            log.warning("ntfy retry %d in %ds...", attempt, delay)
            time.sleep(delay)
        try:
            r = requests.post(NTFY_URL, data=body.encode(), headers=headers, timeout=15)
            if r.status_code in (200, 201):
                log.info("ntfy POST succeeded (attempt %d)", attempt)
                return True
            if r.status_code in (401, 403):
                log.error("ntfy auth error %d — token invalid, not retrying", r.status_code)
                return False
            log.warning("ntfy POST %d on attempt %d", r.status_code, attempt)
        except Exception as e:
            log.warning("ntfy POST exception on attempt %d: %s", attempt, e)
    log.error("ntfy POST failed after all retries")
    return False

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    run_start = time.time()
    log.info("=== snowflake-notifier run start ===")

    # Load config
    tiers       = load_keywords(KEYWORDS_FILE)
    sources_cfg = json.loads(Path(SOURCES_FILE).read_text())["feeds"]

    # Load state
    summary_cache  = load_json(SUMMARY_CACHE_FILE, {"version": 1, "items": {}})
    notified_urls  = load_json(NOTIFIED_URLS_FILE, {"version": 1, "items": {}})

    # Prune stale state
    summary_cache["items"] = prune_dict_by_date(summary_cache.get("items", {}), 90)
    notified_urls["items"] = prune_dict_by_date(notified_urls.get("items", {}), 30)

    # Fetch all enabled sources
    all_items = []
    for src in sources_cfg:
        if not src.get("enabled", False):
            log.info("Skipping disabled source: %s", src["name"])
            continue
        src_type = src.get("type", "")
        if src_type == "rss":
            all_items.extend(fetch_rss(src))
        elif src_type == "html_scrape":
            all_items.extend(fetch_html(src))
        else:
            log.info("Skipping unsupported source type '%s': %s", src_type, src["name"])

    fetched_count = len(all_items)
    log.info("Total items fetched: %d", fetched_count)

    # Filter pipeline (order per spec: 24h -> dedupe -> score-zero)
    filtered = filter_items(all_items, notified_urls)

    # Score
    for item in filtered:
        item["score"], item["matched"] = score_item(
            item["title"], sanitize_for_ollama(item["raw_text"]), tiers
        )

    # Drop zero-score items
    scored = [i for i in filtered if i["score"] > 0]
    log.info("After score-zero drop: %d / %d items", len(scored), len(filtered))

    # Sort by score desc
    scored.sort(key=lambda x: x["score"], reverse=True)

    # Partition
    top_5      = scored[:5]
    additional = scored[5:15]

    if not top_5 and not additional:
        log.info("No items survived filtering — skipping notification")
        save_run_state(run_start, "no_items", fetched_count, 0, 0, 0, 0, 0)
        sys.exit(0)

    # Summarize
    ollama_ok = ollama_healthy()
    degraded  = False
    ollama_calls = 0
    cache_hits   = 0

    if not ollama_ok:
        log.warning("Ollama unreachable — falling back to title + excerpt for top 5")
        degraded = True

    for item in top_5:
        url = item["url"]
        if url in summary_cache.get("items", {}):
            item["summary"] = summary_cache["items"][url]["summary"]
            cache_hits += 1
            log.info("Cache hit: %s", item["title"][:60])
        elif ollama_ok:
            try:
                item["summary"] = ollama_summarize(item["raw_text"])
                summary_cache.setdefault("items", {})[url] = {
                    "title": item["title"],
                    "summary": item["summary"],
                    "summary_date": datetime.now(timezone.utc).isoformat(),
                    "source": item["source"],
                    "keywords_matched": item["matched"],
                }
                ollama_calls += 1
                log.info("Summarized: %s", item["title"][:60])
            except RuntimeError as e:
                log.warning("Ollama failed for item '%s': %s — using excerpt", item["title"][:60], e)
                item["summary"] = sanitize_for_ollama(item["raw_text"])[:200]
                degraded = True
        else:
            item["summary"] = sanitize_for_ollama(item["raw_text"])[:200]

    # Build and post notification
    body = build_body(top_5, additional, degraded)
    posted = post_ntfy(body)

    if posted:
        today = datetime.now(timezone.utc).date().isoformat()
        for item in top_5 + additional:
            notified_urls.setdefault("items", {})[item["url"]] = today
        save_json(SUMMARY_CACHE_FILE, summary_cache)
        save_json(NOTIFIED_URLS_FILE, notified_urls)
        log.info("State files updated")
    else:
        save_json(SUMMARY_CACHE_FILE, summary_cache)
        log.error("ntfy POST failed — notified_urls NOT updated (items remain eligible tomorrow)")

    status = "success" if posted else "ntfy_failed"
    save_run_state(
        run_start, status, fetched_count,
        len(scored), len(top_5), len(additional),
        ollama_calls, cache_hits,
    )

    log.info("=== run complete: %s | %.1fs ===", status, time.time() - run_start)
    sys.exit(0 if posted else 1)

def save_run_state(start, status, fetched, after_filter, top5, additional, calls, hits):
    duration = round(time.time() - start, 1)
    data = {
        "last_run_at": datetime.now(timezone.utc).isoformat(),
        "last_run_status": status,
        "last_run_items_fetched": fetched,
        "last_run_items_after_filter": after_filter,
        "last_run_top_5_count": top5,
        "last_run_additional_count": additional,
        "last_run_ollama_calls": calls,
        "last_run_ollama_cache_hits": hits,
        "last_run_duration_seconds": duration,
    }
    save_json(RUN_STATE_FILE, data)

if __name__ == "__main__":
    main()
