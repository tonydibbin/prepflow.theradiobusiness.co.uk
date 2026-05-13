#!/usr/bin/env python3
"""
generate_deck.py — Audienceflow quarterly "Programming Update" deck regenerator.

Fetches the current shared Audienceflow dataset, opens the master template, and
patches:
  * every native chart's cached data (so when PowerPoint opens it, the bars are
    drawn from current STATE rather than the static numbers baked in months ago)
  * the title slide's quarter label
  * the "Qtr X YYYY" source-line on every chart slide
  * the per-station commentary paragraphs (auto-written from QoQ deltas)

Outputs a single .pptx ready for download.

Usage
-----
  AUDIENCEFLOW_URL=https://… \\
  PREPFLOW_OUT_DIR=./dist \\
  python scripts/generate_deck.py

Environment
-----------
  AUDIENCEFLOW_URL   — JSON endpoint returning the dataset (default tries the
                       live `audienceflow.theradiobusiness.co.uk/api/data.php`,
                       which requires the GH Actions runner to have a session
                       cookie / token — see workflow YAML for how that's set).
  AUDIENCEFLOW_TOKEN — optional bearer token sent as `Authorization: Bearer …`.
                       Provided by the export-deck PHP endpoint when triggering
                       the workflow.
  PREPFLOW_OUT_DIR   — where to write the output .pptx (default: ./dist).
  DECK_TEMPLATE      — path to the master template (default: the bundled one).
  STATE_FILE         — load STATE from a local JSON file instead of fetching
                       (useful for local development).
"""

from __future__ import annotations

import json
import os
import re
import sys
import shutil
import urllib.request
import urllib.error
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

# Local module — chart→station/metric mapping
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from deck_chart_map import (
    CHARTS,
    TITLE_SLIDE,
    COMMENTARY_SLIDES,
    quarter_to_calendar,
    WAVE_TO_QUARTER,
)


C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
ET.register_namespace("c", C_NS)
ET.register_namespace("a", A_NS)


# ──────────────────────────────────────────────────────────────────────────────
# Inputs

def load_state() -> dict:
    """Load Audienceflow STATE either from a local file or the live endpoint."""
    local = os.environ.get("STATE_FILE")
    if local:
        log(f"Loading STATE from local file {local}")
        return json.loads(Path(local).read_text())

    url = os.environ.get(
        "AUDIENCEFLOW_URL",
        "https://audienceflow.theradiobusiness.co.uk/api/data.php",
    )
    token = os.environ.get("AUDIENCEFLOW_TOKEN")
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    log(f"Fetching STATE from {url}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def template_path() -> Path:
    p = Path(os.environ.get(
        "DECK_TEMPLATE",
        SCRIPT_DIR.parent / "assets" / "audienceflow-deck" / "template.pptx",
    ))
    if not p.exists():
        die(f"Template not found: {p}")
    return p


def output_path(latest_wave: str) -> Path:
    out_dir = Path(os.environ.get("PREPFLOW_OUT_DIR", SCRIPT_DIR.parent / "dist"))
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_wave = latest_wave.replace(" ", "-")
    return out_dir / f"AudienceflowDeck-{safe_wave}-{date.today().isoformat()}.pptx"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers

def log(msg: str): print(f"[deck] {msg}", flush=True)
def die(msg: str): log(f"FATAL: {msg}"); sys.exit(1)


def find_station(state: dict, sid: str) -> dict | None:
    for s in state.get("stations", []):
        if s["id"] == sid:
            return s
    return None


def trailing_quarters(state: dict, n: int = 9) -> list[str]:
    """Last `n` quarters from STATE.quarters, oldest → newest."""
    qs = state.get("quarters") or []
    return qs[-n:] if len(qs) >= n else qs


def metric_value(station: dict, quarter: str, metric: str) -> float:
    """Pull a per-quarter metric out of a station record. Returns 0 if missing."""
    m = (station.get("metrics") or {}).get(quarter) or {}
    v = m.get(metric)
    return float(v) if isinstance(v, (int, float)) else 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Chart XML patcher

def patch_chart_xml(xml_bytes: bytes, categories: list[str], values: list[float],
                    series_name: str | None = None) -> bytes:
    """Replace the cached categories and values in a chart's XML.

    The chart XML stores each series as:
        <c:ser>
          <c:tx><c:strRef><c:f>…</c:f><c:strCache><c:pt><c:v>NAME</c:v>…</c:strCache>…</c:tx>
          <c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>Q4 2023</c:v></c:pt>…</c:strCache></c:strRef></c:cat>
          <c:val><c:numRef><c:numCache><c:pt idx="0"><c:v>1153.8</c:v></c:pt>…</c:numCache></c:numRef></c:val>
        </c:ser>

    We replace the <c:pt> children inside the first series' <c:cat>/<c:val> caches.
    """
    tree = ET.fromstring(xml_bytes)
    ser = tree.find(f".//{{{C_NS}}}ser")
    if ser is None:
        return xml_bytes

    # Series display name (legend/tooltip)
    if series_name is not None:
        for v in ser.findall(f".//{{{C_NS}}}tx//{{{C_NS}}}v"):
            v.text = series_name
            break

    # Categories
    cat = ser.find(f".//{{{C_NS}}}cat")
    if cat is not None:
        cache = cat.find(f".//{{{C_NS}}}strCache") or cat.find(f".//{{{C_NS}}}numCache")
        if cache is not None:
            _replace_pts(cache, categories, as_str=True)

    # Values
    val = ser.find(f".//{{{C_NS}}}val")
    if val is not None:
        cache = val.find(f".//{{{C_NS}}}numCache")
        if cache is not None:
            _replace_pts(cache, [str(v) for v in values], as_str=False)

    return ET.tostring(tree, xml_declaration=True, encoding="UTF-8", default_namespace=None)


def _replace_pts(cache_elem: ET.Element, items: list, *, as_str: bool):
    """Replace all <c:pt><c:v>…</c:v></c:pt> children of a cache element."""
    # Clear existing points (but keep ptCount + formatCode children).
    pts = cache_elem.findall(f"{{{C_NS}}}pt")
    for pt in pts:
        cache_elem.remove(pt)

    # Update ptCount
    pt_count = cache_elem.find(f"{{{C_NS}}}ptCount")
    if pt_count is None:
        pt_count = ET.SubElement(cache_elem, f"{{{C_NS}}}ptCount")
        # ptCount lives at the start; reorder.
        cache_elem.remove(pt_count)
        cache_elem.insert(0, pt_count)
    pt_count.set("val", str(len(items)))

    # Append new points
    for i, item in enumerate(items):
        pt = ET.SubElement(cache_elem, f"{{{C_NS}}}pt")
        pt.set("idx", str(i))
        v = ET.SubElement(pt, f"{{{C_NS}}}v")
        v.text = str(item)


# ──────────────────────────────────────────────────────────────────────────────
# Slide text patcher

def patch_slide_text(xml_bytes: bytes, replacements: list[tuple[re.Pattern, str]]) -> bytes:
    """Apply regex replacements to every paragraph in a slide XML.

    Text in PowerPoint is split across one-or-more <a:r><a:t>…</a:t></a:r> runs
    inside a paragraph (so individual words can carry different formatting).
    Crucially that means a single visible string like "Source ... Qtr 4 2025"
    can land in *three* separate runs. We therefore work paragraph-by-paragraph:
    concatenate every <a:t> in the paragraph, apply the regex(es) to that whole
    string, then redistribute the result back into the runs (putting the new
    text in the first run and blanking the rest preserves the paragraph's
    leading run's formatting, which is fine for our small text swaps).
    """
    tree = ET.fromstring(xml_bytes)
    changed = False
    for p in tree.iter(f"{{{A_NS}}}p"):
        runs = list(p.iter(f"{{{A_NS}}}t"))
        if not runs:
            continue
        combined = "".join(r.text or "" for r in runs)
        new = combined
        for pat, repl in replacements:
            new = pat.sub(repl, new)
        if new == combined:
            continue
        runs[0].text = new
        for r in runs[1:]:
            r.text = ""
        changed = True
    return ET.tostring(tree, xml_declaration=True, encoding="UTF-8") if changed else xml_bytes


# ──────────────────────────────────────────────────────────────────────────────
# Commentary auto-writer

def auto_commentary(station: dict, q_now: str, q_prev: str | None,
                    q_yearago: str | None) -> str | None:
    """Single-paragraph commentary stitched from QoQ + YoY deltas. Returns None if
    we don't have enough numbers."""
    if not station:
        return None
    r_now    = metric_value(station, q_now,    "reach")
    h_now    = metric_value(station, q_now,    "hours")
    if r_now == 0 and h_now == 0:
        return None

    bits = []
    if q_prev:
        r_p = metric_value(station, q_prev, "reach")
        h_p = metric_value(station, q_prev, "hours")
        if h_p > 0:
            dh = (h_now - h_p) / h_p * 100
            bits.append(f"{'up' if dh >= 0 else 'down'} {abs(dh):.0f}% in listening hours on the previous quarter")
        if r_p > 0:
            dr = (r_now - r_p) / r_p * 100
            bits.append(f"reach {'+' if dr >= 0 else ''}{dr:.0f}% on Q-over-Q")
    if q_yearago:
        h_y = metric_value(station, q_yearago, "hours")
        if h_y > 0:
            dy = (h_now - h_y) / h_y * 100
            bits.append(f"{'+' if dy >= 0 else ''}{dy:.0f}% year-on-year hours")

    if not bits:
        return None
    return (f"{station['name']} delivers {r_now:,.0f}k weekly listeners "
            f"and {h_now:,.0f}k listening hours — "
            + "; ".join(bits) + ".")


# ──────────────────────────────────────────────────────────────────────────────
# Main

def main():
    state = load_state()
    qs = state.get("quarters") or []
    if not qs:
        die("STATE has no quarters — nothing to write into the deck.")
    q_now = qs[-1]                          # e.g. "JUN 2026"
    q_prev = qs[-2] if len(qs) >= 2 else None
    q_yearago = qs[-5] if len(qs) >= 5 else None  # 4 waves back = same quarter prior year
    cal_now = quarter_to_calendar(q_now)    # "Q2 2026"
    log(f"Latest wave in STATE: {q_now}  →  calendar label {cal_now}")

    tmpl = template_path()
    out  = output_path(q_now)
    log(f"Copying template → {out}")
    shutil.copy(tmpl, out)

    # Trailing 9 quarters (matching the template's column count) as calendar labels.
    show_quarters = trailing_quarters(state, n=9)
    cal_quarters  = [quarter_to_calendar(q) for q in show_quarters]
    log(f"Showing quarters: {cal_quarters}")

    # Build the source-line replacement: "Qtr 4 2025, Published Weighting" →
    # "Qtr {n} {YYYY}, Published Weighting" for the current quarter.
    new_qnum = WAVE_TO_QUARTER[q_now.split()[0]]
    new_year = q_now.split()[1]
    source_re = re.compile(r"Qtr\s+\d\s+20\d{2}", re.IGNORECASE)
    title_re  = re.compile(r"RAJAR\s+Q\d\s+20\d{2}", re.IGNORECASE)
    replacements = [
        (source_re, f"Qtr {new_qnum} {new_year}"),
        (title_re,  f"RAJAR Q{new_qnum} {new_year}"),
    ]

    # ── Edit the .pptx zip in place ────────────────────────────────────────────
    with zipfile.ZipFile(out, "r") as zin:
        contents = {n: zin.read(n) for n in zin.namelist()}

    # 1) Chart XML patches.
    for spec in CHARTS:
        path = f"ppt/charts/{spec.chart_id}.xml"
        if path not in contents:
            log(f"  ⚠ missing chart part {path}, skipping")
            continue
        station = find_station(state, spec.station)
        if not station:
            log(f"  ⚠ chart {spec.chart_id}: station '{spec.station}' not in STATE — chart left as-is")
            continue
        if spec.metric in ("reach", "hours"):
            values = [metric_value(station, q, spec.metric) for q in show_quarters]
            contents[path] = patch_chart_xml(
                contents[path],
                categories=cal_quarters,
                values=values,
                series_name=station["name"],
            )
            log(f"  ✓ {spec.chart_id} ({spec.station} {spec.metric}): "
                f"{[round(v) for v in values]}")
        elif spec.metric == "dayparts":
            dp = station.get("dayparts") or {}
            order = ["breakfast", "daytime", "drive", "evening", "weekend"]
            cats = [d.capitalize() for d in order]
            values = [float(dp.get(k) or 0) for k in order]
            if not any(values):
                log(f"  ⚠ {spec.chart_id} ({spec.station} dayparts): all zeros — drop a per-station PDF to populate")
            contents[path] = patch_chart_xml(
                contents[path], categories=cats, values=values,
                series_name=station["name"],
            )
            log(f"  ✓ {spec.chart_id} ({spec.station} dayparts): {[round(v) for v in values]}")
        elif spec.metric == "compset":
            # SKIPPED in MVP. The template's competitive-set charts carry
            # four-or-five historical series (one bar per quarter for each
            # competitor) and rebuilding them properly means rewriting every
            # series, not just one. Leave the original chart alone for now —
            # better than a half-updated chart that confuses Neil's audience.
            # TODO(phase 2): rebuild compset charts series-by-series.
            log(f"  · {spec.chart_id} (compset for {spec.station}): "
                f"left as-is (compset rebuild is phase 2)")

    # 2) Slide text patches — title slide + source-line on every chart slide +
    # all commentary slides.
    auto_commentaries: dict[int, str] = {}
    # Try to auto-write commentary for each commentary slide based on a "best
    # guess" station inferred from neighbouring chart slides.
    for c_slide in COMMENTARY_SLIDES:
        nearest = next(
            (c for c in CHARTS if c.slide > c_slide and c.slide - c_slide <= 3),
            None,
        )
        if nearest:
            text = auto_commentary(
                find_station(state, nearest.station), q_now, q_prev, q_yearago,
            )
            if text:
                auto_commentaries[c_slide] = text

    for name, data in list(contents.items()):
        m = re.match(r"ppt/slides/slide(\d+)\.xml$", name)
        if not m:
            continue
        slide_idx = int(m.group(1))
        per_slide = list(replacements)
        # Commentary slides additionally swap their first long paragraph for
        # the auto-written one. We do that by inserting a (text-of-first-long-
        # paragraph, new-text) replacement, only when we have new text.
        new_body = auto_commentaries.get(slide_idx)
        if new_body:
            contents[name] = _replace_first_long_paragraph(data, new_body) or data
        contents[name] = patch_slide_text(contents[name], per_slide)

    # 3) Rewrite the zip
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, b in contents.items():
            zout.writestr(n, b)

    log(f"Done. Wrote {out} ({out.stat().st_size:,} bytes)")


def _replace_first_long_paragraph(slide_xml: bytes, new_text: str) -> bytes | None:
    """Replace the first text paragraph >= 30 chars with new_text. Keeps the
    rest of the slide intact (logos, photos, decorative elements)."""
    tree = ET.fromstring(slide_xml)
    target = None
    for p in tree.iter(f"{{{A_NS}}}p"):
        text_in_p = "".join(t.text or "" for t in p.iter(f"{{{A_NS}}}t"))
        if len(text_in_p.strip()) >= 30:
            target = p
            break
    if target is None:
        return None
    runs = list(target.iter(f"{{{A_NS}}}t"))
    if not runs:
        return None
    # Put the new text in the first run; blank the rest so styling carries over.
    runs[0].text = new_text
    for r in runs[1:]:
        r.text = ""
    return ET.tostring(tree, xml_declaration=True, encoding="UTF-8")


if __name__ == "__main__":
    main()
