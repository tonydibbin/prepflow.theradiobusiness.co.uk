"""
deck_chart_map.py
-----------------
Mapping table for Audienceflow's quarterly "Programming Update" deck.

For every native PowerPoint chart in the template, this module records:
  - which Audienceflow STATE.stations[].id (or list of ids for competitive sets)
  - which metric to pull ('reach' | 'hours' | 'share' | 'dayparts')
  - how many trailing quarters to show

The chart parts in template.pptx are named /ppt/charts/chartN.xml — extracted by
inspecting the template once with python-pptx (the inspector run is in this
module's git history). Adjust if you re-arrange slides in the template.

Quarter labels in the template use "Q4 2025" calendar-quarter style. STATE.quarters
uses RAJAR publication-wave style ("DEC 2025", "MAR 2026"). Convert with
quarter_to_calendar() below.
"""

from typing import NamedTuple


class ChartSpec(NamedTuple):
    chart_id: str           # e.g. "chart1"
    slide:    int           # 1-indexed slide number, for logging only
    station:  str           # primary station id in STATE
    metric:   str           # 'reach' | 'hours' | 'share' | 'dayparts'
    extras:   tuple = ()    # for competitive charts: additional station ids


# ──────────────────────────────────────────────────────────────────────────────
# Wave-label conversion. RAJAR publishes four waves per year:
#   MAR YYYY = published mid-May    = "Q1 YYYY" in the deck's calendar labels
#   JUN YYYY = published mid-August = "Q2 YYYY"
#   SEP YYYY = published mid-Nov    = "Q3 YYYY"
#   DEC YYYY = published mid-Feb+1  = "Q4 YYYY"
WAVE_TO_QUARTER = {"MAR": 1, "JUN": 2, "SEP": 3, "DEC": 4}
QUARTER_TO_WAVE = {v: k for k, v in WAVE_TO_QUARTER.items()}


def quarter_to_calendar(wave_label: str) -> str:
    """`'DEC 2025'` → `'Q4 2025'`. Returns the input unchanged if it doesn't match."""
    parts = wave_label.split()
    if len(parts) != 2 or parts[0] not in WAVE_TO_QUARTER:
        return wave_label
    return f"Q{WAVE_TO_QUARTER[parts[0]]} {parts[1]}"


def calendar_to_quarter(cal_label: str) -> str:
    """`'Q4 2025'` → `'DEC 2025'`. Returns the input unchanged on mismatch."""
    parts = cal_label.split()
    if len(parts) != 2 or not parts[0].startswith("Q"):
        return cal_label
    try:
        n = int(parts[0][1:])
    except ValueError:
        return cal_label
    if n not in QUARTER_TO_WAVE:
        return cal_label
    return f"{QUARTER_TO_WAVE[n]} {parts[1]}"


# ──────────────────────────────────────────────────────────────────────────────
# The deck has 29 native charts. Mapping built from inspecting slide titles:
#   slide N  /ppt/charts/chartM.xml  STATION  METRIC
# (the comment after each entry mirrors the slide title for readability).
#
# Station IDs use Audienceflow's slug convention. Where the deck's station label
# doesn't match Audienceflow's catalogue exactly we map to the closest match.

# Metric tokens:
#   reach     →  STATE.stations[s].metrics[q].reach
#   hours     →  STATE.stations[s].metrics[q].hours
#   dayparts  →  STATE.stations[s].dayparts        (breakfast/daytime/drive/evening/weekend)
#   compset   →  reach values across a list of station ids (current quarter only)

CHARTS = [
    # — Nation Radio South Wales —
    ChartSpec("chart1",  slide= 4, station="nation-radio-south-wales", metric="hours"),
    ChartSpec("chart2",  slide= 5, station="nation-radio-south-wales", metric="reach"),
    ChartSpec("chart3",  slide= 6, station="nation-radio-south-wales", metric="dayparts"),
    ChartSpec("chart4",  slide= 7, station="nation-radio-south-wales", metric="compset",
              extras=("dragon-radio-wales", "swansea-bay-radio", "heart-wales", "capital-south-wales", "smooth-radio-south-wales")),

    # — Swansea Bay Radio —
    ChartSpec("chart5",  slide= 9, station="swansea-bay-radio", metric="reach"),
    ChartSpec("chart6",  slide=10, station="swansea-bay-radio", metric="hours"),
    ChartSpec("chart7",  slide=11, station="swansea-bay-radio", metric="dayparts"),

    # — Dragon Radio Wales —
    ChartSpec("chart8",  slide=13, station="dragon-radio-wales", metric="reach"),
    ChartSpec("chart9",  slide=14, station="dragon-radio-wales", metric="hours"),

    # — Nation Radio Scotland —
    ChartSpec("chart10", slide=16, station="nation-radio-scotland-west",       metric="reach"),
    ChartSpec("chart11", slide=17, station="nation-radio-scotland-west",       metric="hours"),
    ChartSpec("chart12", slide=18, station="nation-radio-scotland-west",       metric="compset",
              extras=("clyde-1", "clyde-2", "heart-scotland", "capital-scotland", "smooth-radio-scotland")),

    # — Nation Radio South (Sussex/Hants/Dorset) —
    # Station name in deck = "NATION RADIO SOUTH". In Audienceflow it splits into
    # three regional stations (Hampshire, Sussex, Dorset). For the deck we use
    # the Hampshire row as the canonical line; the regenerator will fall back to
    # the next station that has data if Hampshire is missing.
    ChartSpec("chart13", slide=21, station="nation-radio-hampshire", metric="reach"),
    ChartSpec("chart14", slide=22, station="nation-radio-hampshire", metric="hours"),

    # — Easy Radio Hampshire —
    ChartSpec("chart15", slide=24, station="easy-radio-hampshire", metric="reach"),
    ChartSpec("chart16", slide=25, station="easy-radio-hampshire", metric="hours"),
    ChartSpec("chart17", slide=26, station="easy-radio-hampshire", metric="dayparts"),
    ChartSpec("chart18", slide=27, station="easy-radio-hampshire", metric="hours"),  # duplicate "Total Hours" slide

    # — Nation Radio Yorkshire —
    ChartSpec("chart19", slide=29, station="nation-radio-yorkshire", metric="reach"),
    ChartSpec("chart20", slide=30, station="nation-radio-yorkshire", metric="hours"),
    # chart21 is the Yorkshire LINE chart "Average half hours, Mon–Fri" — needs
    # half-hour data not summarised in STATE.dayparts, so skipped in MVP.
    # ChartSpec("chart21", slide=31, station="nation-radio-yorkshire", metric="halfhours"),

    # — Nation Radio North East —
    ChartSpec("chart22", slide=34, station="nation-radio-north-east", metric="reach"),
    ChartSpec("chart23", slide=35, station="nation-radio-north-east", metric="hours"),
    ChartSpec("chart24", slide=36, station="nation-radio-north-east", metric="dayparts"),

    # — Nation Radio London / London Digital —
    ChartSpec("chart25", slide=38, station="nation-radio-london",         metric="reach"),
    ChartSpec("chart26", slide=39, station="nation-radio-london-digital", metric="hours"),
    ChartSpec("chart27", slide=40, station="nation-radio-london-digital", metric="compset",
              extras=("capital-london", "heart-london", "smooth-london", "kiss-london", "lbc")),

    # — Nation Radio Suffolk —
    ChartSpec("chart28", slide=42, station="nation-radio-suffolk", metric="reach"),
    ChartSpec("chart29", slide=43, station="nation-radio-suffolk", metric="hours"),
]


# Slides whose body text mentions the wave/quarter and needs a refresh on
# regeneration. (chart slides get the "Qtr X YYYY" source line patched
# automatically; these are the prose slides + the title page.)
TITLE_SLIDE = 1
COMMENTARY_SLIDES = [3, 8, 15, 20, 23, 28, 33, 37, 45]
