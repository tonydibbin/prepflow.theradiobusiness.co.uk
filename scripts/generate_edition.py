#!/usr/bin/env python3
"""
Prepflow — autonomous daily edition generator (zero-cost edition).

Runs in CI (GitHub Actions) every day at 14:59 UK time. Produces tomorrow's
edition as HTML + PDF, named after tomorrow's day of week.

Sources — all free, no API keys required:
  - BBC News, Politics, Entertainment & Arts and Sport RSS feeds
  - Wikipedia REST API for "on this day" (birthdays, events, deaths, holidays)
  - National Lottery results page (jackpot estimates)
  - content_bank.json in this folder for the deterministic rotating content

Env (optional):
  PREPFLOW_OUT_DIR     — output dir, defaults to ../editions relative to this script
  PREPFLOW_TARGET_DATE — ISO date (YYYY-MM-DD) override; defaults to tomorrow UK time
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML
from pypdf import PdfReader


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

UK = ZoneInfo("Europe/London")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = SCRIPT_DIR.parent / "editions"
TEMPLATE_NAME = "template_edition.html.j2"
BANK_PATH = SCRIPT_DIR / "content_bank.json"

USER_AGENT = "Prepflow/1.0 (+https://prepflow.theradiobusiness.co.uk)"

BBC_FEEDS = {
    "news_top": "https://feeds.bbci.co.uk/news/uk/rss.xml",
    "news_main": "https://feeds.bbci.co.uk/news/rss.xml",
    "news_politics": "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "news_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "showbiz": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    "showbiz_extra": "https://feeds.bbci.co.uk/news/newsbeat/rss.xml",
    "sport": "https://feeds.bbci.co.uk/sport/rss.xml",
    "sport_football": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "sport_cricket": "https://feeds.bbci.co.uk/sport/cricket/rss.xml",
    "sport_f1": "https://feeds.bbci.co.uk/sport/formula1/rss.xml",
    "sport_tennis": "https://feeds.bbci.co.uk/sport/tennis/rss.xml",
    "sport_rugby": "https://feeds.bbci.co.uk/sport/rugby-union/rss.xml",
    "sport_golf": "https://feeds.bbci.co.uk/sport/golf/rss.xml",
}


# ----------------------------------------------------------------------------
# HTTP helper
# ----------------------------------------------------------------------------

def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ----------------------------------------------------------------------------
# Date helpers
# ----------------------------------------------------------------------------

def target_date() -> dt.date:
    override = os.environ.get("PREPFLOW_TARGET_DATE", "").strip()
    if override:
        return dt.date.fromisoformat(override)
    return (dt.datetime.now(UK) + dt.timedelta(days=1)).date()


def fmt_full(d: dt.date) -> str:
    return d.strftime("%A, %-d %B %Y") if sys.platform != "win32" else d.strftime("%A, %#d %B %Y")


def fmt_short(d: dt.date) -> str:
    return d.strftime("%a %-d %b %Y") if sys.platform != "win32" else d.strftime("%a %#d %b %Y")


def day_meta(d: dt.date) -> dict:
    return {
        "iso": d.isoformat(),
        "day_name": d.strftime("%A").lower(),
        "full_date": fmt_full(d),
        "short_date": fmt_short(d),
        "day_of_year": int(d.strftime("%j")),
        "days_remaining": (dt.date(d.year, 12, 31) - d).days,
        "year": d.year,
        "month": d.month,
        "day": d.day,
    }


# ----------------------------------------------------------------------------
# Deterministic per-day pick from a list
# ----------------------------------------------------------------------------

def pick_index(iso_date: str, salt: str, modulo: int) -> int:
    """Return a stable integer index for `salt` keyed off the date."""
    # Use a simple hash so the same date always gets the same content.
    import hashlib
    h = hashlib.sha256(f"{iso_date}:{salt}".encode("utf-8")).hexdigest()
    return int(h[:8], 16) % modulo


def pick_one(items: list, iso_date: str, salt: str):
    if not items:
        return None
    return items[pick_index(iso_date, salt, len(items))]


def pick_unique(items: list, iso_date: str, salt: str, n: int) -> list:
    """Pick n unique items deterministically. n must be <= len(items)."""
    if not items:
        return []
    n = min(n, len(items))
    # Shuffle deterministically: stable-sort by per-item hash.
    import hashlib
    keyed = [
        (hashlib.sha256(f"{iso_date}:{salt}:{i}".encode("utf-8")).digest(), item)
        for i, item in enumerate(items)
    ]
    keyed.sort(key=lambda t: t[0])
    return [it for _, it in keyed[:n]]


# ----------------------------------------------------------------------------
# Cross-edition no-repeat ledger
# ----------------------------------------------------------------------------
# A small JSON file (committed by the daily workflow) remembering which items
# each section used and when, so nothing reappears within its window. When the
# fresh pool is exhausted it falls back to the least-recently-used items.

import hashlib as _hashlib

LEDGER_PATH = SCRIPT_DIR / "used_history.json"

# Days an item stays blocked from reuse, per section.
LEDGER_WINDOWS = {
    "news": 12, "showbiz": 16, "sport": 12,
    "newsbrief": 45, "survey": 75, "talk_topic": 75,
    "true_false": 75, "facts": 45,
}
LEDGER_MAX_AGE = 150  # prune ledger entries older than this many days


def item_key(item) -> str:
    """Stable identity for a bank item or a raw RSS dict."""
    if isinstance(item, dict) and item.get("title"):
        basis = re.sub(r"\s+", " ", str(item["title"]).strip().lower())
    else:
        basis = json.dumps(item, sort_keys=True, ensure_ascii=False)
    return _hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def load_ledger() -> dict:
    try:
        with LEDGER_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_ledger(ledger: dict, iso_date: str) -> None:
    cutoff = (dt.date.fromisoformat(iso_date)
              - dt.timedelta(days=LEDGER_MAX_AGE)).isoformat()
    pruned = {}
    for sec, entries in ledger.items():
        if not isinstance(entries, dict) or any(not isinstance(v, str) for v in entries.values()):
            pruned[sec] = entries  # e.g. "_ai_recent" holds lists, keep as-is
            continue
        pruned[sec] = {k: v for k, v in entries.items() if v >= cutoff}
    LEDGER_PATH.write_text(
        json.dumps(pruned, indent=1, ensure_ascii=False), encoding="utf-8")


def _blocked_keys(ledger: dict, section: str, iso_date: str) -> set:
    window = LEDGER_WINDOWS.get(section, 30)
    cutoff = (dt.date.fromisoformat(iso_date)
              - dt.timedelta(days=window)).isoformat()
    return {k for k, last in ledger.get(section, {}).items() if last >= cutoff}


def _record(ledger: dict, section: str, iso_date: str, keys: list) -> None:
    ledger.setdefault(section, {})
    for k in keys:
        ledger[section][k] = iso_date


def pick_unique_fresh(items, iso_date, salt, n, ledger, section, key_fn=item_key):
    """Pick n items, preferring ones not used within the section's window.
    Falls back to least-recently-used items when not enough fresh remain."""
    if not items:
        return []
    n = min(n, len(items))
    blocked = _blocked_keys(ledger, section, iso_date)
    fresh = [it for it in items if key_fn(it) not in blocked]
    chosen = pick_unique(fresh, iso_date, salt, n) if fresh else []
    if len(chosen) < n:
        last = ledger.get(section, {})
        stale = [it for it in items if it not in chosen]
        stale.sort(key=lambda it: last.get(key_fn(it), ""))  # oldest first
        for it in stale:
            if len(chosen) >= n:
                break
            chosen.append(it)
    chosen = chosen[:n]
    _record(ledger, section, iso_date, [key_fn(it) for it in chosen])
    return chosen


def pick_one_fresh(items, iso_date, salt, ledger, section, key_fn=item_key):
    res = pick_unique_fresh(items, iso_date, salt, 1, ledger, section, key_fn)
    return res[0] if res else None


# ----------------------------------------------------------------------------
# BBC RSS
# ----------------------------------------------------------------------------

def parse_rss(xml_text: str) -> list[dict]:
    """Return list of {title, description, pubDate, link, source}."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = (it.findtext("description") or "").strip()
        # Strip HTML tags from description
        desc = re.sub(r"<[^>]+>", "", desc)
        desc = html.unescape(desc)
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        if title:
            items.append({"title": title, "description": desc, "link": link, "pubDate": pub})
    return items


def fetch_bbc(feed_key: str) -> list[dict]:
    try:
        xml = http_get(BBC_FEEDS[feed_key])
        return parse_rss(xml)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  ! BBC RSS fetch failed for {feed_key}: {e}", file=sys.stderr)
        return []


def is_political(item: dict) -> bool:
    s = (item.get("title", "") + " " + item.get("description", "")).lower()
    keywords = ["starmer", "labour", "tory", "tories", "conservative", "reform uk",
                "lib dem", "downing street", "westminster", "prime minister",
                "chancellor", "parliament", "general election", "polling", "mp ",
                "by-election", "no 10", "no. 10", "cabinet"]
    return any(k in s for k in keywords)


# ───────────────────────────────────────────────────────────────────────────
# Past-event filter
# ───────────────────────────────────────────────────────────────────────────
# Drop stories whose body explicitly references an event/date that's already
# passed by the edition's target date. The classic case: a BBC story written
# on Friday at 5pm previewing Saturday night's Champions League final —
# perfectly fresh at build time, hopelessly stale by Sunday morning's edition.
#
# We look for two signals in the title+description text:
#   (a) Time-relative words tied to the build day ("tonight", "this evening",
#       "today", "tomorrow") — these always refer to a day before the
#       edition's target date, because the edition covers tomorrow.
#   (b) Dotted/short date references that resolve to before the target date.

_MONTH_LONG = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,"sep":9,"sept":9,
    "oct":10,"nov":11,"dec":12,
}

_PAST_WORDS = re.compile(
    r"\b(tonight|this evening|earlier today|earlier this evening|yesterday|"
    r"last night|moments ago|just hours ago)\b",
    re.IGNORECASE,
)


def _explicit_dates(text: str, ref_year: int) -> list[dt.date]:
    """Pull any date the text spells out (e.g. '30 May', 'Saturday 30 May 2026')."""
    found: list[dt.date] = []
    # "30 May" / "30 May 2026"
    for m in re.finditer(r"\b(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?\b", text):
        mon = _MONTH_LONG.get(m.group(2).lower())
        if not mon:
            continue
        try:
            year = int(m.group(3)) if m.group(3) else ref_year
            found.append(dt.date(year, mon, int(m.group(1))))
        except ValueError:
            pass
    # "May 30" / "May 30, 2026"
    for m in re.finditer(r"\b([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?\b", text):
        mon = _MONTH_LONG.get(m.group(1).lower())
        if not mon:
            continue
        try:
            year = int(m.group(3)) if m.group(3) else ref_year
            found.append(dt.date(year, mon, int(m.group(2))))
        except ValueError:
            pass
    return found


def is_past_event_story(item: dict, target_date: dt.date) -> bool:
    """True if the story is clearly tied to a moment that's already gone by
    the time the edition's target date arrives."""
    text = ((item.get("title") or "") + " " + (item.get("description") or "")).strip()
    if not text:
        return False
    # (a) Time-relative words on the BUILD day (= day before target_date).
    if _PAST_WORDS.search(text):
        return True
    # (b) Explicit date references before target_date.
    #     We allow ±1 year wrap by trying both target_date.year and prev/next.
    for cand in _explicit_dates(text, target_date.year):
        if cand < target_date:
            return True
    return False


def _select_news_raw(iso_date: str, target_date: dt.date, ledger: dict) -> list[dict]:
    """Return chosen raw RSS items for News (3 items, max 1 political).

    Past-event stories (anything tied to 'tonight', a date before target_date,
    etc.) are stripped first — they read as stale on tomorrow's prep doc.
    """
    pool = fetch_bbc("news_top") + fetch_bbc("news_world")
    politics = fetch_bbc("news_politics")

    seen = set()
    def unique(items):
        out = []
        for it in items:
            key = it["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out

    fresh = lambda items: [it for it in items if not is_past_event_story(it, target_date)]
    non_pol = fresh([it for it in unique(pool) if not is_political(it)])
    pol     = fresh(unique(politics))

    chosen = []
    if pol:
        chosen += pick_unique_fresh(pol, iso_date, "news_pol", 1, ledger, "news")
    needed = 3 - len(chosen)
    if non_pol and needed > 0:
        chosen += pick_unique_fresh(non_pol[:25], iso_date, "news_np", needed, ledger, "news")
    return chosen[:3]


def _select_showbiz_raw(iso_date: str, target_date: dt.date, ledger: dict) -> list[dict]:
    items = [it for it in fetch_bbc("showbiz") if not is_past_event_story(it, target_date)]
    return pick_unique_fresh(items[:25], iso_date, "showbiz", 3, ledger, "showbiz")


def _select_sport_raw(iso_date: str, target_date: dt.date, ledger: dict) -> list[dict]:
    items = [it for it in fetch_bbc("sport") if not is_past_event_story(it, target_date)]
    return pick_unique_fresh(items[:30], iso_date, "sport", 4, ledger, "sport")


def _format_raw_news(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        tag = "Politics · UK" if is_political(it) else "UK · News"
        out.append({
            "lead": it["title"].rstrip(".") + ".",
            "detail": (it["description"] or "More to follow.").strip(),
            "tag": tag,
        })
    return out


def _format_raw_showbiz(items: list[dict]) -> list[dict]:
    return [{
        "lead": it["title"].rstrip(".") + ".",
        "detail": (it["description"] or "More to follow.").strip(),
        "tag": "Showbiz · UK",
    } for it in items]


def _format_raw_sport(items: list[dict]) -> list[dict]:
    return [{
        "lead": it["title"].rstrip(".") + ".",
        "detail": (it["description"] or "More to follow.").strip(),
        "tag": "Sport · UK",
    } for it in items]


# ----------------------------------------------------------------------------
# Wide candidate pools + AI curation for News / Showbiz / Sport
# ----------------------------------------------------------------------------

def _dedupe_items(items):
    seen, out = set(), []
    for it in items:
        k = (it.get("title") or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out


def _fresh_first(items, target_date, ledger, section, iso_date, n):
    items = [it for it in _dedupe_items(items) if not is_past_event_story(it, target_date)]
    blocked = _blocked_keys(ledger, section, iso_date)
    fresh = [it for it in items if item_key(it) not in blocked]
    stale = [it for it in items if item_key(it) in blocked]
    return (fresh + stale)[:n]


def _pool_news(target_date, ledger, iso_date, n=22):
    pool = (fetch_bbc("news_top") + fetch_bbc("news_main")
            + fetch_bbc("news_world") + fetch_bbc("news_politics"))
    return _fresh_first(pool, target_date, ledger, "news", iso_date, n)


def _pool_showbiz(target_date, ledger, iso_date, n=22):
    pool = fetch_bbc("showbiz") + fetch_bbc("showbiz_extra")
    return _fresh_first(pool, target_date, ledger, "showbiz", iso_date, n)


def _pool_sport(target_date, ledger, iso_date, n=30):
    pool = (fetch_bbc("sport") + fetch_bbc("sport_football")
            + fetch_bbc("sport_cricket") + fetch_bbc("sport_f1")
            + fetch_bbc("sport_tennis") + fetch_bbc("sport_rugby")
            + fetch_bbc("sport_golf"))
    return _fresh_first(pool, target_date, ledger, "sport", iso_date, n)


def _format_candidates(items, mark_political=False):
    if not items:
        return "(none available)"
    lines = []
    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").strip()
        desc = (it.get("description") or "").strip()
        if len(desc) > 240:
            desc = desc[:237].rstrip() + "..."
        flag = " [POLITICAL]" if (mark_political and is_political(it)) else ""
        lines.append(f"{i}.{flag} {title}\n   {desc}")
    return "\n".join(lines)


CURATE_SYSTEM = (
    "You are Prepflow's senior editor, choosing and writing the top News, "
    "Showbiz and Sport items for British radio presenter Tony Dibbin's daily "
    "prep. Be ruthless about picking the BIGGEST, most talkable, UK-relevant "
    "stories listeners are actually discussing - never minor, niche or filler. "
    "Write in a warm, conversational on-air British voice, never tabloid. Never "
    "invent facts not in the source. Never quote more than 12 words verbatim. "
    "Never include song lyrics."
)

CURATE_PROMPT = """From the candidate pools below, SELECT and REWRITE the strongest items for tomorrow's Prepflow edition ({full_date}).

Return ONE JSON object, no commentary, EXACTLY this shape:

{{
  "news":    [{{"source": 1, "lead": "...", "detail": "...", "angle": "...", "tag": "Topic \u00b7 UK"}}],
  "showbiz": [{{"source": 1, "lead": "...", "detail": "...", "angle": "...", "tag": "Topic \u00b7 When"}}],
  "sport":   [{{"source": 1, "lead": "...", "detail": "...", "angle": "...", "tag": "Sport \u00b7 UK"}}]
}}

news = exactly 5 items. showbiz = exactly 5 items. sport = exactly 5 items.

For every item:
- "source": the NUMBER of the candidate you chose (so we can track it). Pick distinct, strong candidates; ignore weak ones.
- "lead": one punchy sentence the presenter reads first (max 25 words).
- "detail": 2-3 short sentences of context.
- "angle": ONE on-air talking point OR a question to throw to listeners - the thing that makes it land on radio.
- "tag": short categorisation pill.

Selection rules:
- NEWS: the 3 biggest UK-relevant stories people are actually talking about. AT MOST ONE political (items marked [POLITICAL]). Skip minor or local-only items unless genuinely striking.
- SHOWBIZ: forward-looking and upbeat ONLY - what is COMING UP (releases, returns, tours, premieres, awards, castings). NEVER a death, obituary, tragedy, court case or pure recap. If the pool is thin, choose the most positive, entertaining options.
- SPORT: what matters THIS WEEK - real fixtures, results and talking points with UK relevance (football, cricket, F1, tennis, rugby, golf, racing). Avoid generic features and health explainers.

=== NEWS CANDIDATES ===
{news}

=== SHOWBIZ CANDIDATES ===
{showbiz}

=== SPORT CANDIDATES ===
{sport}
"""


def _fallback_select(pool, n, tagger):
    chosen = pool[:n]
    items = [{
        "lead": (it.get("title") or "").rstrip(".") + ".",
        "detail": (it.get("description") or "More to follow.").strip(),
        "angle": "",
        "tag": tagger(it),
    } for it in chosen]
    keys = [item_key(it) for it in chosen]
    return items, keys


def curate_sections_with_gemini(news_pool, showbiz_pool, sport_pool, ledger, iso_date, full_date):
    """Pick + write the strongest News/Showbiz/Sport via Gemini, with an on-air
    angle per item. Falls back to the top BBC items on any problem."""
    fb_news, k_news = _fallback_select(
        news_pool, 5, lambda it: "Politics · UK" if is_political(it) else "UK · News")
    fb_showbiz, k_showbiz = _fallback_select(showbiz_pool, 5, lambda it: "Showbiz · UK")
    fb_sport, k_sport = _fallback_select(sport_pool, 5, lambda it: "Sport · UK")

    def use_fallback(msg):
        if msg:
            print(msg)
        _record(ledger, "news", iso_date, k_news)
        _record(ledger, "showbiz", iso_date, k_showbiz)
        _record(ledger, "sport", iso_date, k_sport)
        return fb_news, fb_showbiz, fb_sport

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return use_fallback("  · No GEMINI_API_KEY - using top BBC items (no AI curation)")
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        return use_fallback(f"  ! google-genai not installed: {e}")

    prompt = CURATE_PROMPT.format(
        full_date=full_date,
        news=_format_candidates(news_pool, mark_political=True),
        showbiz=_format_candidates(showbiz_pool),
        sport=_format_candidates(sport_pool),
    )
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=CURATE_SYSTEM,
                response_mime_type="application/json",
                temperature=0.6,
                max_output_tokens=6000,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = (response.text or "").strip()
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
        data = json.loads(text)

        def shape(section, pool, want_min):
            arr = data.get(section)
            if not isinstance(arr, list):
                raise ValueError(f"{section} not a list")
            out, keys = [], []
            for x in arr:
                if not (isinstance(x, dict) and x.get("lead") and x.get("detail")):
                    continue
                out.append({
                    "lead": str(x["lead"]).strip(),
                    "detail": str(x["detail"]).strip(),
                    "angle": str(x.get("angle") or "").strip(),
                    "tag": (str(x.get("tag") or "").strip() or "UK"),
                })
                si = x.get("source")
                if isinstance(si, int) and 1 <= si <= len(pool):
                    keys.append(item_key(pool[si - 1]))
            if len(out) < want_min:
                raise ValueError(f"{section} too few valid items")
            return out, keys

        n_out, n_keys = shape("news", news_pool, 2)
        s_out, s_keys = shape("showbiz", showbiz_pool, 2)
        sp_out, sp_keys = shape("sport", sport_pool, 2)
        _record(ledger, "news", iso_date, n_keys)
        _record(ledger, "showbiz", iso_date, s_keys)
        _record(ledger, "sport", iso_date, sp_keys)
        print(f"  · Gemini curated sections ({len(n_out)} news, {len(s_out)} showbiz, {len(sp_out)} sport)")
        return n_out, s_out, sp_out
    except Exception as e:
        return use_fallback(f"  ! Gemini curation failed ({type(e).__name__}: {e})")


# ----------------------------------------------------------------------------
# Weather — Open-Meteo (free, no API key)
# ----------------------------------------------------------------------------

WEATHER_CITIES = [
    ("London",     51.5074,  -0.1278),
    ("Birmingham", 52.4862,  -1.8904),
    ("Manchester", 53.4808,  -2.2426),
    ("Glasgow",    55.8642,  -4.2518),
]

# WMO weather codes → readable conditions
WEATHER_CONDITIONS = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Foggy",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorms", 96: "Storms with hail", 99: "Severe storms",
}


def fetch_weather(target_date: dt.date) -> list[dict]:
    """Return tomorrow's forecast for the 4 UK cities. Empty list on failure."""
    out = []
    iso = target_date.isoformat()
    for name, lat, lon in WEATHER_CITIES:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min"
            f"&timezone=Europe/London&start_date={iso}&end_date={iso}"
        )
        try:
            data = json.loads(http_get(url, timeout=10))
            d = data.get("daily", {})
            if not d.get("time") or d["time"][0] != iso:
                continue
            code = d["weather_code"][0]
            out.append({
                "city": name,
                "condition": WEATHER_CONDITIONS.get(code, "Mixed"),
                "high": round(d["temperature_2m_max"][0]),
                "low": round(d["temperature_2m_min"][0]),
            })
        except Exception as e:
            print(f"  ! Weather fetch failed for {name}: {e}", file=sys.stderr)
    return out


# ----------------------------------------------------------------------------
# Wikipedia "On this day"
# ----------------------------------------------------------------------------

def fetch_wiki_onthisday(month: int, day: int) -> dict:
    """Return {births, events, deaths, holidays} for the given month/day."""
    url = f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/{month:02d}/{day:02d}"
    try:
        text = http_get(url)
        return json.loads(text)
    except Exception as e:
        print(f"  ! Wikipedia onthisday fetch failed: {e}", file=sys.stderr)
        return {}


def _pages_have_topic(pages: list, keywords: list[str]) -> bool:
    for p in pages or []:
        title = (p.get("titles", {}).get("normalized") or p.get("title", "")).lower()
        if any(k in title for k in keywords):
            return True
    return False


def _format_event(year: int, text: str) -> str:
    # Truncate very long descriptions
    text = text.strip()
    if len(text) > 280:
        text = text[:277].rstrip() + "..."
    return f"{year} — {text}"


# ───────────────────────────────────────────────────────────────────────────
# Birthday relevance filter
# ───────────────────────────────────────────────────────────────────────────
# We keep UK people of any showbiz/sport/media celebrity role, plus US
# people who are *movie or music stars*. Anyone else (politicians, royalty,
# scientists, non-UK sportspeople, etc.) is dropped — they don't earn space
# on a UK breakfast radio prep doc.

_UK_TAGS = (
    "british", "english", "scottish", "welsh",
    "northern irish", "irish", "n. irish",
)
_US_TAGS = ("american", "u.s.", "us-")

_UK_ROLES = (
    "actor", "actress", "singer", "musician", "rapper", "band ", "songwriter",
    "comedian", "comic", "presenter", "broadcaster", "dj ", "disc jockey",
    "tv host", "tv personality", "television personality", "radio host",
    "model", "footballer", "cricketer", "rugby", "boxer", "athlete",
    "olympian", "tennis player", "golfer", "darts player", "snooker",
    "film director", "film producer", "screenwriter", "novelist", "author",
    "chef", "celebrity", "youtuber", "influencer", "drag queen",
)
_US_ROLES = (
    "actor", "actress", "singer", "musician", "rapper", "band ", "songwriter",
    "film director", "film producer", "screenwriter", "filmmaker",
    "movie star", "pop star", "rock star", "guitarist", "drummer", "bassist",
    "vocalist", "composer",
)


def _birth_is_relevant(item: dict) -> bool:
    """True if the person looks like a UK celebrity or US movie/music star.

    We only inspect Wikipedia's short `description` field (e.g. "American
    actress and producer", "French politician"). The longer `extract` is
    skipped on purpose: it tends to mention every country a person ever
    worked in, which lets French actors and New Zealand models leak through.
    The description is consistently the *primary* nationality + role tag and
    is the strongest single signal we've got.
    """
    pages = item.get("pages") or []
    if not pages:
        return False
    for p in pages:
        desc = (p.get("description") or "").lower()
        if not desc:
            continue
        if any(t in desc for t in _UK_TAGS) and any(r in desc for r in _UK_ROLES):
            return True
        if any(t in desc for t in _US_TAGS) and any(r in desc for r in _US_ROLES):
            return True
    return False


def _format_birth(item: dict) -> str | None:
    year = item.get("year")
    text = item.get("text", "")
    if not year or not text:
        return None
    # Skip pre-1900 births (less recognisable to radio audiences)
    try:
        if int(year) < 1900:
            return None
    except (TypeError, ValueError):
        return None
    # Drop anyone outside our target audience — UK celebs and US movie/music stars only.
    if not _birth_is_relevant(item):
        return None
    if len(text) > 200:
        text = text[:197].rstrip() + "..."
    return f"{year} — {text}"


def build_day_notes(d: dt.date, salt: str) -> dict:
    iso = d.isoformat()
    data = fetch_wiki_onthisday(d.month, d.day)

    # Birthdays — pick 8, prefer recent (post-1950) names
    births_all = data.get("births", [])
    formatted_births = []
    for b in births_all:
        f = _format_birth(b)
        if f:
            formatted_births.append(f)
    # Prefer 1950-onward over older
    recent = [b for b in formatted_births if any(year in b[:4] for year in [str(y) for y in range(1950, 2010)])]
    older = [b for b in formatted_births if b not in recent]
    pool = recent[:30] + older[:10]
    birthdays = pick_unique(pool, iso, f"{salt}-births", 8) if pool else []

    # Events
    events_all = data.get("events", [])
    formatted_events = []
    for e in events_all:
        year = e.get("year")
        text = e.get("text", "")
        if year and text:
            formatted_events.append(_format_event(year, text))
    events = pick_unique(formatted_events[:25], iso, f"{salt}-events", 3)

    # Music — filter events whose pages reference music
    music_candidates = []
    for e in events_all:
        year = e.get("year")
        text = e.get("text", "")
        pages = e.get("pages", [])
        if year and text and _pages_have_topic(pages, ["song", "album", "single ", "band", "music"]):
            music_candidates.append(_format_event(year, text))
    music = pick_unique(music_candidates[:15], iso, f"{salt}-music", 2)
    if len(music) < 2:
        music += pick_unique(formatted_events[:25], iso, f"{salt}-music-fb", 2 - len(music))
        music = music[:2]

    # History — events not classified as music
    history_candidates = [e for e in formatted_events if e not in music]
    history = pick_unique(history_candidates[:25], iso, f"{salt}-history", 3)

    # Holidays / national days
    holidays = data.get("holidays", []) or []
    holiday_titles = []
    for h in holidays[:6]:
        t = h.get("text", "").strip()
        if t:
            holiday_titles.append(t)
    holiday_str = "; ".join(holiday_titles[:4]) if holiday_titles else "no major UK observance listed"

    # Star sign & birthstone
    zodiac = zodiac_for(d)
    birthstone = birthstone_for(d.month)

    summary = (
        f"Day {int(d.strftime('%j'))} of the year, "
        f"{(dt.date(d.year, 12, 31) - d).days} days left in {d.year}. "
        f"Today {holiday_str}. "
        f"Star sign {zodiac}, birthstone {birthstone}."
    )

    return {
        "summary": summary,
        "birthdays": birthdays,
        "events": events,
        "music": music,
        "history": history,
    }


# ----------------------------------------------------------------------------
# Astrology helpers
# ----------------------------------------------------------------------------

def zodiac_for(d: dt.date) -> str:
    m, day = d.month, d.day
    signs = [
        ((1, 20),  "Capricorn"), ((2, 19),  "Aquarius"), ((3, 21),  "Pisces"),
        ((4, 20),  "Aries"),     ((5, 21),  "Taurus"),    ((6, 21),  "Gemini"),
        ((7, 23),  "Cancer"),    ((8, 23),  "Leo"),       ((9, 23),  "Virgo"),
        ((10, 23), "Libra"),     ((11, 22), "Scorpio"),   ((12, 22), "Sagittarius"),
        ((12, 31), "Capricorn"),
    ]
    for (mm, dd), name in signs:
        if (m, day) <= (mm, dd):
            return name
    return "Capricorn"


def birthstone_for(month: int) -> str:
    return [
        "Garnet", "Amethyst", "Aquamarine", "Diamond",
        "Emerald", "Pearl", "Ruby", "Peridot",
        "Sapphire", "Opal", "Topaz", "Turquoise",
    ][month - 1]


# ----------------------------------------------------------------------------
# Lottery — scrape lottery.co.uk for next jackpot estimates
# ----------------------------------------------------------------------------

def fetch_lottery_estimates() -> dict:
    """Best-effort scrape of the next EuroMillions and Lotto estimated jackpots.
    Falls back to sensible defaults if the page changes structure."""
    defaults = {
        "euromillions_amount": "£25 million",
        "euromillions_date": "Next Tuesday or Friday",
        "national_amount": "£3.8 million",
        "national_date": "Next Wednesday or Saturday",
    }

    try:
        page = http_get("https://www.lottery.co.uk/")
    except Exception as e:
        print(f"  ! Lottery scrape failed: {e}", file=sys.stderr)
        return defaults

    # Look for EuroMillions estimated jackpot amount
    em = re.search(r"EuroMillions[^£]*£([\d.]+)\s*(?:Million|m)?", page, re.IGNORECASE | re.DOTALL)
    lotto = re.search(r"Lotto[^£]*£([\d.]+)\s*(?:Million|m)?", page, re.IGNORECASE | re.DOTALL)

    out = dict(defaults)
    if em:
        out["euromillions_amount"] = f"£{em.group(1)} million"
    if lotto:
        out["national_amount"] = f"£{lotto.group(1)} million"
    return out


# ----------------------------------------------------------------------------
# Gemini polish — rewrite news/showbiz/sport in radio voice
# ----------------------------------------------------------------------------

GEMINI_SYSTEM = (
    "You are Prepflow's daily editor. You rewrite raw BBC News headlines and "
    "summaries into short, conversational British radio prep notes for the "
    "presenter Tony Dibbin. Tone: relaxed, on-air, lightly British, never "
    "tabloid. NEVER invent facts not in the source. NEVER quote more than 12 "
    "words from the source verbatim. NEVER include song lyrics. Keep each "
    "lead to one sentence (≤25 words) and each detail to 2–3 sentences. "
    "Showbiz items must be FORWARD-LOOKING — focus on what's coming up, not "
    "what already happened. Sport items must keep UK focus. For news items, "
    "include AT MOST ONE political story."
)

GEMINI_PROMPT_TEMPLATE = """Below are raw BBC RSS items for tomorrow's Prepflow edition. Rewrite each into the Prepflow radio voice.

Return a single JSON object exactly matching this shape, no commentary:

{{
  "news":    [{{"lead": "...", "detail": "...", "tag": "Topic · UK"}}, ...exactly 3 items, max 1 political...],
  "showbiz": [{{"lead": "...", "detail": "...", "tag": "Topic · When"}}, ...exactly 3 items, all forward-looking...],
  "sport":   [{{"lead": "...", "detail": "...", "tag": "Sport · UK"}}, ...3 or 4 items, UK relevance...]
}}

Rules:
- The `lead` is the bolded one-sentence hook a presenter would read first.
- The `detail` is 2–3 short sentences for context.
- The `tag` is a short categorisation pill (e.g. "Football · Sun 24 May", "Politics · Westminster", "Cannes · This week").
- Stay faithful to the BBC source. If an item is unclear or thin, write a shorter detail rather than inventing facts.
- If a showbiz item is purely a recap (e.g. "X happened yesterday"), reframe it forward ("X continues this week", "what to watch for next…") — or drop it entirely and pick another item from the showbiz pool.

=== RAW NEWS POOL ===
{news_raw}

=== RAW SHOWBIZ POOL ===
{showbiz_raw}

=== RAW SPORT POOL ===
{sport_raw}
"""


def _format_raw_pool(items: list[dict]) -> str:
    if not items:
        return "(no items available)"
    lines = []
    for i, it in enumerate(items[:12], 1):
        title = it.get("title", "").strip()
        desc = it.get("description", "").strip()
        if len(desc) > 280:
            desc = desc[:277].rstrip() + "..."
        lines.append(f"{i}. {title}\n   {desc}")
    return "\n".join(lines)


def polish_with_gemini(news_raw, showbiz_raw, sport_raw,
                      news_fallback, showbiz_fallback, sport_fallback):
    """Try to rewrite via Gemini. On any error, return the fallback (raw RSS) shapes."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("  · No GEMINI_API_KEY — using raw BBC content (no radio-voice polish)")
        return news_fallback, showbiz_fallback, sport_fallback

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        print(f"  ! google-genai not installed: {e} — using raw BBC content")
        return news_fallback, showbiz_fallback, sport_fallback

    prompt = GEMINI_PROMPT_TEMPLATE.format(
        news_raw=_format_raw_pool(news_raw),
        showbiz_raw=_format_raw_pool(showbiz_raw),
        sport_raw=_format_raw_pool(sport_raw),
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=GEMINI_SYSTEM,
                response_mime_type="application/json",
                temperature=0.7,
                max_output_tokens=4000,
            ),
        )
        text = (response.text or "").strip()
        # Strip code fences if present
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
        polished = json.loads(text)

        n = polished.get("news") or news_fallback
        s = polished.get("showbiz") or showbiz_fallback
        sp = polished.get("sport") or sport_fallback

        # Sanity-check shapes — must be lists of dicts with lead/detail/tag.
        for label, arr in (("news", n), ("showbiz", s), ("sport", sp)):
            if not isinstance(arr, list) or not all(
                isinstance(x, dict) and "lead" in x and "detail" in x for x in arr
            ):
                raise ValueError(f"Bad {label} shape from Gemini")

        print(f"  · Gemini polish applied ({len(n)} news, {len(s)} showbiz, {len(sp)} sport)")
        return n, s, sp

    except Exception as e:
        print(f"  ! Gemini polish failed ({type(e).__name__}: {e}) — using raw BBC content")
        return news_fallback, showbiz_fallback, sport_fallback


# ----------------------------------------------------------------------------
# Gemini bank generation — fresh Newsbrief / Talkback / Facts each day
# ----------------------------------------------------------------------------

GEMINI_BANK_SYSTEM = (
    "You are Prepflow's daily editor, writing light prep material for a British "
    "radio presenter, Tony Dibbin. Voice: warm, conversational, family-friendly, "
    "lightly British, never tabloid, never crude. Everything must be read-aloud "
    "friendly. CRITICAL RULES: every item in 'facts' and every 'fact' you label as "
    "true MUST be genuinely true and verifiable — if you are not sure, leave it out "
    "and use a safer well-known fact. Never include song lyrics. Never quote more "
    "than 12 words from any source. Keep it evergreen and UK-appropriate. Avoid "
    "anything political, medical, tragic, or controversial in these light sections."
)

GEMINI_BANK_PROMPT = """Write the light, evergreen sections of tomorrow's Prepflow edition for {full_date}.

Return ONE JSON object, no commentary, EXACTLY this shape:

{{
  "newsbrief": [{{"lead": "...", "detail": "..."}} , ... exactly 7 items ...],
  "survey":    [{{"question": "...", "answer": "..."}}, {{"question": "...", "answer": "..."}}],
  "talk_topic":[{{"lead": "...", "detail": "..."}}, {{"lead": "...", "detail": "..."}}],
  "true_false":{{"fact_label": "Topic", "fact": "...", "fiction": "..."}},
  "facts":     ["...", "...", "...", "...", "...", "...", "...", "..."]
}}

Section guidance:
- newsbrief: 7 short, light human-interest / lifestyle / quirky research bites. Each lead is one punchy sentence; each detail is 1-2 sentences. Upbeat, the sort of thing a presenter drops between records.
- survey: 2 playful "Our Survey Said" question + answer pairs in the classic radio style ("X% of us admit to... what is it?" with a funny everyday answer). These are entertainment, not real statistics.
- talk_topic: 2 phone-in conversation starters. Each has a 'lead' (a light hook) and a 'detail' (the talk-back question to listeners).
- true_false: one "True Story or Jackanory" pair. 'fact' is a GENUINELY TRUE, surprising-but-real story or fact. 'fiction' is an invented but believable story. 'fact_label' is a short topic word (e.g. "Animals", "Space", "Records").
- facts: 8 short, genuinely TRUE "did you know" trivia bullets. Each one sentence, accurate, and the kind that makes people go "ooh".

AVOID reusing any of these recently-used lines:
{avoid}

Keep everything fresh, varied, and different from the avoid-list. British spelling.
"""


def _ai_recent_lines(ledger: dict) -> str:
    rec = ledger.get("_ai_recent", {})
    lines = []
    for sec in ("newsbrief", "survey", "talk_topic", "facts"):
        for txt in rec.get(sec, [])[-12:]:
            lines.append(f"- {txt}")
    return "\n".join(lines) if lines else "(none yet)"


def _remember_ai(ledger: dict, ai: dict) -> None:
    rec = ledger.setdefault("_ai_recent", {})
    def add(sec, vals):
        cur = rec.setdefault(sec, [])
        cur.extend(vals)
        rec[sec] = cur[-40:]  # keep last 40
    add("newsbrief", [x.get("lead", "") for x in ai.get("newsbrief", [])])
    add("survey", [x.get("question", "") for x in ai.get("survey", [])])
    add("talk_topic", [x.get("lead", "") for x in ai.get("talk_topic", [])])
    add("facts", list(ai.get("facts", []))[:8])


def generate_bank_with_gemini(iso_date: str, full_date: str, ledger: dict):
    """Return a dict with fresh newsbrief/survey/talk_topic/true_false/facts, or
    None on any problem (caller then falls back to the static banks)."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        print(f"  ! google-genai not installed: {e} — using static banks")
        return None

    prompt = GEMINI_BANK_PROMPT.format(full_date=full_date, avoid=_ai_recent_lines(ledger))
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=GEMINI_BANK_SYSTEM,
                response_mime_type="application/json",
                temperature=0.95,
                max_output_tokens=6000,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = (response.text or "").strip()
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
        data = json.loads(text)

        # ---- strict shape validation; any failure -> None (use banks) ----
        nb = data.get("newsbrief")
        sv = data.get("survey")
        tt = data.get("talk_topic")
        tf = data.get("true_false")
        fa = data.get("facts")
        ok = (
            isinstance(nb, list) and len(nb) >= 5
            and all(isinstance(x, dict) and x.get("lead") and x.get("detail") for x in nb)
            and isinstance(sv, list) and len(sv) >= 2
            and all(isinstance(x, dict) and x.get("question") and x.get("answer") for x in sv)
            and isinstance(tt, list) and len(tt) >= 2
            and all(isinstance(x, dict) and x.get("lead") and x.get("detail") for x in tt)
            and isinstance(tf, dict) and tf.get("fact") and tf.get("fiction")
            and isinstance(fa, list) and len(fa) >= 8
            and all(isinstance(x, str) and len(x) > 15 for x in fa)
        )
        if not ok:
            print("  ! Gemini bank output failed shape check — using static banks")
            return None
        tf.setdefault("fact_label", "True or False")
        result = {
            "newsbrief": nb[:7],
            "survey": sv[:2],
            "talk_topic": tt[:2],
            "true_false": tf,
            "facts": fa[:8],
        }
        print("  · Gemini generated fresh Newsbrief/Talkback/Facts")
        return result
    except Exception as e:
        print(f"  ! Gemini bank generation failed ({type(e).__name__}: {e}) — using static banks")
        return None


# ----------------------------------------------------------------------------
# Content bank
# ----------------------------------------------------------------------------

def load_bank() -> dict:
    with BANK_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_content(iso_date: str, target: dict, day_after: dict, ledger: dict) -> dict:
    bank = load_bank()

    # Try fresh AI generation first (when GEMINI_API_KEY is set); on any problem
    # fall back to the ledger-aware static banks below.
    print("  · Attempting Gemini fresh Newsbrief/Talkback/Facts...")
    ai = generate_bank_with_gemini(iso_date, target["full_date"], ledger)
    if ai:
        newsbrief = ai["newsbrief"]
        survey = ai["survey"]
        talk_topic = ai["talk_topic"]
        true_false = ai["true_false"]
        facts = ai["facts"]
        _remember_ai(ledger, ai)
    else:
        # Newsbrief: 7 unique items, avoiding anything used recently.
        newsbrief = pick_unique_fresh(bank["newsbrief"], iso_date, "newsbrief", 7, ledger, "newsbrief")
        # Survey, talk topic, true_false — one each, avoiding recent reuse.
        survey = pick_one_fresh(bank["survey"], iso_date, "survey", ledger, "survey")
        talk_topic = pick_one_fresh(bank["talk_topic"], iso_date, "talk_topic", ledger, "talk_topic")
        true_false = pick_one_fresh(bank["true_false"], iso_date, "true_false", ledger, "true_false")
        # Facts of the day: one set of 8, avoiding recent reuse.
        facts = pick_one_fresh(bank["facts"], iso_date, "facts", ledger, "facts")

    # Weather — tomorrow's UK overview (free, no key)
    print("  · Fetching weather (Open-Meteo)...")
    target_d = dt.date.fromisoformat(target["iso"])
    weather = fetch_weather(target_d)
    weather_note = None
    if not weather:
        weather_note = "Weather feed unavailable — see Met Office for the latest forecast."
    else:
        print(f"    → {len(weather)} cities")

    # Live sources — fetch raw, format as fallback, then optionally polish via Gemini.
    # All three selectors take the edition's target_date so they can drop stories
    # whose event has already passed by the time this edition is read.
    print("  · Building wide BBC candidate pools (news/showbiz/sport)...")
    news_pool = _pool_news(target_d, ledger, iso_date)
    showbiz_pool = _pool_showbiz(target_d, ledger, iso_date)
    sport_pool = _pool_sport(target_d, ledger, iso_date)
    print(f"    → pools: {len(news_pool)} news, {len(showbiz_pool)} showbiz, {len(sport_pool)} sport")
    print("  · Curating + writing the strongest stories (Gemini if available)...")
    news, showbiz, sport = curate_sections_with_gemini(
        news_pool, showbiz_pool, sport_pool, ledger, iso_date, target["full_date"])
    print("  · Fetching lottery estimates...")
    lottery_live = fetch_lottery_estimates()
    print("  · Fetching Wikipedia on-this-day for target date...")
    today_notes = build_day_notes(
        dt.date.fromisoformat(target["iso"]), "today")
    print("  · Fetching Wikipedia on-this-day for day after...")
    tomorrow_notes = build_day_notes(
        dt.date.fromisoformat(day_after["iso"]), "tomorrow")

    # Lede
    lede = (
        "Your daily radio show prep — three top news stories, forward-looking "
        "showbiz, sport that matters this week, talkback prompts, and day "
        "notes for today and tomorrow. Built fresh every weekday, ready by 3pm "
        "for tomorrow's show."
    )

    return {
        "lede": lede,
        "weather": weather,
        "weather_note": weather_note,
        "news": news,
        "showbiz": showbiz,
        "sport": sport,
        "newsbrief": newsbrief,
        "survey": survey or [],
        "talk_topic": talk_topic or [],
        "true_false": true_false or {},
        "lottery": lottery_live,
        "facts": facts or [],
        "today_notes": today_notes,
        "tomorrow_notes": tomorrow_notes,
        "film_calendar":  filter_calendar(bank["film_calendar"],  target_d),
        "sport_calendar": filter_calendar(bank["sport_calendar"], target_d),
    }


# ----------------------------------------------------------------------------
# Calendar date-filter
# ----------------------------------------------------------------------------
# Each calendar entry's `when` field is a short human string like:
#   "Fri 15", "Sat 30", "24 May – 7 Jun", "11 Jun – 19 Jul"
# We parse it relative to the month label on the surrounding card
# ("May 2026", "June 2026", etc.) and drop entries whose END date is strictly
# before the edition's target date. Items with date ranges survive as long as
# their right-hand end is in the future. Items we can't parse are kept (better
# to leave something in than to drop it on a regex miss).

_MONTH_NUM = {m: i+1 for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"]
)}


def _parse_calendar_when(when: str, card_month: int, card_year: int) -> dt.date | None:
    """Return the END date of a `when` string, or None if we can't parse it.

    Examples we handle:
      "Fri 15"               -> day-of-month inside card_month
      "Sun 24"               -> day-of-month inside card_month
      "Thu 4"                -> day-of-month inside card_month
      "24 May – 7 Jun"       -> 7 June (card_year)
      "11 Jun – 19 Jul"      -> 19 July
      "11–14 Jun"            -> 14 June
      "Sat 30"               -> 30 inside card_month
    """
    if not when:
        return None
    s = when.replace("–", "-").replace("—", "-").strip()
    parts = [p.strip() for p in s.split("-")]
    tail = parts[-1].strip()  # whatever's after the last dash

    # Format A: "DD MMM" — e.g. "7 Jun" or "19 Jul" (date range tails).
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3,})", tail)
    if m:
        day = int(m.group(1))
        mon = _MONTH_NUM.get(m.group(2)[:3].lower())
        if mon:
            return dt.date(card_year, mon, day)

    # Format B: "Day DD" / "DD" — e.g. "Fri 15", "Sat 30", "14".
    # Pull the LAST integer in the tail; that's the day-of-month.
    nums = re.findall(r"\d{1,2}", tail)
    if nums:
        day = int(nums[-1])
        # If the tail is just "14 Jun" the regex above already handled it; this
        # path catches "Fri 15" / "Sat 30" / "11-14 Jun" (after dash split the
        # tail is "14 Jun" → first regex wins). For pure day-of-card-month
        # entries, we use card_month/card_year.
        # If the tail also names a month, prefer that.
        mm = re.search(r"([A-Za-z]{3,})", tail)
        if mm:
            mon = _MONTH_NUM.get(mm.group(1)[:3].lower())
            if mon:
                return dt.date(card_year, mon, day)
        return dt.date(card_year, card_month, day)
    return None


def filter_calendar(cal: dict, today: dt.date) -> dict:
    """Drop past items from every month card. Drop empty month cards entirely."""
    out = {"left": [], "right": []}
    for col in ("left", "right"):
        for card in cal.get(col, []):
            label = (card.get("month") or "").strip()        # "May 2026"
            m = re.match(r"([A-Za-z]+)\s+(\d{4})", label)
            if not m:
                # Unknown card; keep as-is.
                out[col].append(card)
                continue
            card_month = _MONTH_NUM.get(m.group(1)[:3].lower())
            card_year  = int(m.group(2))
            if not card_month:
                out[col].append(card); continue
            # Drop the whole month card if it's entirely before the edition month.
            if (card_year, card_month) < (today.year, today.month):
                continue
            # Future months: keep every item.
            if (card_year, card_month) > (today.year, today.month):
                out[col].append(card); continue
            # Current month: filter item-by-item.
            kept = []
            for item in card.get("items", []):
                end = _parse_calendar_when(item.get("when",""), card_month, card_year)
                if end is None or end >= today:
                    kept.append(item)
            if kept:
                out[col].append({"month": label, "items": kept})
    return out


# ----------------------------------------------------------------------------
# Render
# ----------------------------------------------------------------------------

def render_html(content: dict, target: dict, tomorrow_short_date: str) -> str:
    env = Environment(
        loader=FileSystemLoader(SCRIPT_DIR),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tpl = env.get_template(TEMPLATE_NAME)
    return tpl.render(
        day_name=target["day_name"],
        full_date=target["full_date"],
        short_date=target["short_date"],
        tomorrow_short_date=tomorrow_short_date,
        **content,
    )


def render_pdf(html_path: Path, pdf_path: Path) -> int:
    HTML(filename=str(html_path)).write_pdf(str(pdf_path))
    return len(PdfReader(str(pdf_path)).pages)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    out_dir = Path(os.environ.get("PREPFLOW_OUT_DIR") or DEFAULT_OUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    target_d = target_date()
    tgt = day_meta(target_d)
    day_after = day_meta(target_d + dt.timedelta(days=1))

    print(f"→ Generating Prepflow edition for {tgt['full_date']} "
          f"(→ {tgt['day_name']}.html / .pdf)")

    ledger = load_ledger()
    content = build_content(tgt["iso"], tgt, day_after, ledger)
    html_str = render_html(content, tgt, day_after["short_date"])

    html_path = out_dir / f"{tgt['day_name']}.html"
    pdf_path = out_dir / f"{tgt['day_name']}.pdf"
    html_path.write_text(html_str, encoding="utf-8")

    pages = render_pdf(html_path, pdf_path)
    size_kb = round(pdf_path.stat().st_size / 1024)

    save_ledger(ledger, tgt["iso"])
    lead = (content.get("news") or [{}])[0].get("lead", "(no lead)")
    print(f"✓ Saved {html_path.name} and {pdf_path.name} "
          f"({pages} pages, {size_kb} KB)")
    print(f"  Lead: {lead}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
