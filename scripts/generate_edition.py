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
    "news_politics": "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "news_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "showbiz": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    "sport": "https://feeds.bbci.co.uk/sport/rss.xml",
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


def _select_news_raw(iso_date: str, target_date: dt.date) -> list[dict]:
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
        chosen.append(pol[pick_index(iso_date, "news_pol", min(len(pol), 5))])
    needed = 3 - len(chosen)
    np_picks = pick_unique(non_pol[:20], iso_date, "news_np", needed) if non_pol else []
    chosen.extend(np_picks)
    return chosen[:3]


def _select_showbiz_raw(iso_date: str, target_date: dt.date) -> list[dict]:
    items = [it for it in fetch_bbc("showbiz") if not is_past_event_story(it, target_date)]
    return pick_unique(items[:25], iso_date, "showbiz", 3)


def _select_sport_raw(iso_date: str, target_date: dt.date) -> list[dict]:
    items = [it for it in fetch_bbc("sport") if not is_past_event_story(it, target_date)]
    return pick_unique(items[:30], iso_date, "sport", 4)


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
# Content bank
# ----------------------------------------------------------------------------

def load_bank() -> dict:
    with BANK_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_content(iso_date: str, target: dict, day_after: dict) -> dict:
    bank = load_bank()

    # Newsbrief: 7 unique items from the pool
    newsbrief = pick_unique(bank["newsbrief"], iso_date, "newsbrief", 7)

    # Survey, talk topic, true_false — pick one PAIR/triple from the array of options
    survey = pick_one(bank["survey"], iso_date, "survey")
    talk_topic = pick_one(bank["talk_topic"], iso_date, "talk_topic")
    true_false = pick_one(bank["true_false"], iso_date, "true_false")

    # Facts of the day: pick one set of 8
    facts = pick_one(bank["facts"], iso_date, "facts")

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
    print("  · Fetching BBC News, Politics, World...")
    news_raw = _select_news_raw(iso_date, target_d)
    print(f"    → {len(news_raw)} news items")
    print("  · Fetching BBC Entertainment & Arts...")
    showbiz_raw = _select_showbiz_raw(iso_date, target_d)
    print(f"    → {len(showbiz_raw)} showbiz items")
    print("  · Fetching BBC Sport...")
    sport_raw = _select_sport_raw(iso_date, target_d)
    print(f"    → {len(sport_raw)} sport items")

    # Raw (fallback) formatting — used as-is if Gemini is unavailable
    news_fb = _format_raw_news(news_raw)
    showbiz_fb = _format_raw_showbiz(showbiz_raw)
    sport_fb = _format_raw_sport(sport_raw)

    # Optional radio-voice polish via Gemini Flash (free tier).
    # Falls back to the raw BBC text silently if no key, no package, or any error.
    print("  · Attempting Gemini Flash radio-voice polish...")
    news, showbiz, sport = polish_with_gemini(
        news_raw, showbiz_raw, sport_raw,
        news_fb, showbiz_fb, sport_fb,
    )
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

    content = build_content(tgt["iso"], tgt, day_after)
    html_str = render_html(content, tgt, day_after["short_date"])

    html_path = out_dir / f"{tgt['day_name']}.html"
    pdf_path = out_dir / f"{tgt['day_name']}.pdf"
    html_path.write_text(html_str, encoding="utf-8")

    pages = render_pdf(html_path, pdf_path)
    size_kb = round(pdf_path.stat().st_size / 1024)

    lead = (content.get("news") or [{}])[0].get("lead", "(no lead)")
    print(f"✓ Saved {html_path.name} and {pdf_path.name} "
          f"({pages} pages, {size_kb} KB)")
    print(f"  Lead: {lead}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
