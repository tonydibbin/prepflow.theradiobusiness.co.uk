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


def _select_news_raw(iso_date: str) -> list[dict]:
    """Return chosen raw RSS items for News (3 items, max 1 political)."""
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

    non_pol = [it for it in unique(pool) if not is_political(it)]
    pol = unique(politics)

    chosen = []
    if pol:
        chosen.append(pol[pick_index(iso_date, "news_pol", min(len(pol), 5))])
    needed = 3 - len(chosen)
    np_picks = pick_unique(non_pol[:20], iso_date, "news_np", needed) if non_pol else []
    chosen.extend(np_picks)
    return chosen[:3]


def _select_showbiz_raw(iso_date: str) -> list[dict]:
    items = fetch_bbc("showbiz")
    return pick_unique(items[:25], iso_date, "showbiz", 3)


def _select_sport_raw(iso_date: str) -> list[dict]:
    items = fetch_bbc("sport")
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

    # Live sources — fetch raw, format as fallback, then optionally polish via Gemini
    print("  · Fetching BBC News, Politics, World...")
    news_raw = _select_news_raw(iso_date)
    print(f"    → {len(news_raw)} news items")
    print("  · Fetching BBC Entertainment & Arts...")
    showbiz_raw = _select_showbiz_raw(iso_date)
    print(f"    → {len(showbiz_raw)} showbiz items")
    print("  · Fetching BBC Sport...")
    sport_raw = _select_sport_raw(iso_date)
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
        "film_calendar": bank["film_calendar"],
        "sport_calendar": bank["sport_calendar"],
    }


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
