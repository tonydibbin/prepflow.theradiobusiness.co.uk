# Prepflow

The Prep Flow module of The Radio Business.
Daily radio show prep — news, sport, showbiz, talkback prompts and day notes — built fresh every weekday, ready by 3pm for the next day's show.

Lives at: **https://prepflow.theradiobusiness.co.uk**

---

## Stack

Identical to Podflow and Clockflow: plain HTML + Tailwind CSS via CDN, no framework, no build step.

- HTML5
- Tailwind CSS via `https://cdn.tailwindcss.com` (inline config sets theme tokens)
- Google Fonts: Fraunces (serif), Inter (sans), JetBrains Mono (mono)
- Inline SVG logo
- Static — no server-side code

No Node, no npm, no bundler. Edit files in place, push, done.

---

## File structure

```
site/
├── index.html               Homepage / landing / preview
├── editions/
│   ├── 2026-05-13.html      Standalone edition view (clean URL: /editions/2026-05-13)
│   └── 2026-05-13.pdf       Downloadable PDF (8 pages, A4, page-break per section)
├── assets/
│   ├── favicon.svg          Browser tab icon
│   └── logo.svg             Main logo (P + dot + Prepflow wordmark)
└── README.md                You are here
```

URLs as deployed:

| Path | What it is |
| --- | --- |
| `/` | Landing page with hero, "what's inside" overview, today's lead, editions library, family-of-tools cross-links |
| `/editions/2026-05-13.html` | The 13 May 2026 edition (full 11 sections) |
| `/editions/2026-05-13.pdf` | The same edition as a printable PDF |
| `/assets/favicon.svg` | Tab favicon |
| `/assets/logo.svg` | Logo asset |

---

## Deploy to Cloudflare Pages (recommended — matches Podflow/Clockflow if that's where they live)

1. Create the subdomain in Cloudflare DNS:
   - Type: CNAME
   - Name: `prepflow`
   - Target: whatever your Pages project URL is (e.g. `prepflow-radiobusiness.pages.dev`)
   - Proxy: ON (orange cloud)

2. In Cloudflare dashboard → Pages → Create application → Direct upload:
   - Project name: `prepflow-radiobusiness`
   - Drag the entire `site/` folder onto the upload zone
   - Hit Deploy

3. In the Pages project → Custom domains → Set up a custom domain → `prepflow.theradiobusiness.co.uk`. Cloudflare will verify and route automatically.

4. (Optional) Connect a Git repo so future edits auto-deploy:
   - Push `site/` to a GitHub repo (`prepflow-radiobusiness` say)
   - Cloudflare Pages → Connect to Git → choose the repo
   - Build settings: leave build command empty, output directory `/`
   - Branch: `main`

That's it. No build step, no node_modules.

---

## Deploy alternatives

### Netlify
- Drag the `site/` folder onto netlify.app deploy zone, or `netlify deploy --dir=site --prod`
- Add the custom domain in Site settings → Domain management.

### Vercel
- `vercel deploy site --prod`
- Add custom domain in the project settings.

### Plain VPS / shared hosting
- Just upload the contents of `site/` to the document root for the subdomain. No special configuration needed.

---

## URL convention — day-of-week (rolling 7-day cycle)

Editions are named after the day of the week they cover, not the date. This gives stable URLs that any presenter can bookmark for their shift days.

| URL | What it is |
| --- | --- |
| `/editions/monday.html` (and .pdf) | This coming Monday's prep — rewritten every Sunday at 14:59 |
| `/editions/tuesday.html`           | Tuesday's prep — rewritten every Monday at 14:59 |
| `/editions/wednesday.html`         | Wednesday's prep — rewritten every Tuesday at 14:59 |
| `/editions/thursday.html`          | Thursday's prep — rewritten every Wednesday at 14:59 |
| `/editions/friday.html`            | Friday's prep — rewritten every Thursday at 14:59 |
| `/editions/saturday.html`          | Saturday's prep — rewritten every Friday at 14:59 |
| `/editions/sunday.html`            | Sunday's prep — rewritten every Saturday at 14:59 |

Each file lives for 7 days before being overwritten by the next cycle. The homepage (`index.html`) is dynamic JavaScript and figures out the current day on its own — no rebuild needed when the day rolls over.

---

## Automation — totally autonomous, totally free, runs at 14:59 daily

The daily generation runs as a **GitHub Action** on GitHub's cloud runners. Nothing on your Mac is required — the schedule fires whether your computer is on or off. **No API keys, no LLM costs.** Total monthly running cost: £0.

```
.github/workflows/prepflow-daily.yml    GitHub Actions workflow (cron + commit)
scripts/generate_edition.py             Python generator
scripts/template_edition.html.j2        Jinja2 template for the edition
scripts/content_bank.json               Rotating curated content
scripts/requirements.txt                Python deps (3 packages, all open source)
```

### Sources — all free, no keys required

| Section | Source |
| --- | --- |
| News (3 stories, max 1 political) | BBC News UK, World and Politics RSS feeds, rewritten in radio voice via **Gemini 2.5 Flash** (free tier) |
| Showbiz (3 forward-looking items) | BBC Entertainment & Arts RSS feed, rewritten via Gemini |
| Sport (3–4 items) | BBC Sport RSS feed, rewritten via Gemini |
| Newsbrief (8 light items) | `content_bank.json` — deterministic daily pick from a pool of 20 |
| Talkback (survey + topic + true/false) | `content_bank.json` — 7 sets of each, rotated daily |
| Lottery jackpot estimates | Scraped from lottery.co.uk public homepage |
| Facts of the Day | `content_bank.json` — 7 sets of 8 facts, rotated daily |
| Today's & Tomorrow's Day Notes | Wikipedia REST API (`/feed/v1/wikipedia/en/onthisday/all/MM/DD`) — birthdays, events, deaths, holidays |
| Film Forward Planning | `content_bank.json` — manually curated, edit when you need to refresh |
| Sport Forward Planning | `content_bank.json` — manually curated, edit when you need to refresh |

The rotating content is **deterministic by date** — the same date always gets the same picks — so today's edition is reproducible and the entire 365-day cycle is varied without random surprises.

**About the Gemini polish step.** The BBC RSS feeds give us factual, current copy but it reads like newswire — not like a presenter. The generator makes a single API call to Google's free-tier Gemini 2.5 Flash to rewrite the news/showbiz/sport leads into conversational British radio voice. It's one call per day, well inside the free quota (250 requests/day on the free tier). If the key is missing, the API errors, or the response is malformed, the script silently falls back to the raw BBC text — the build never fails because of Gemini.

### What happens each run

1. GitHub Actions fires the workflow at **13:59 UTC and 14:59 UTC** every day. The first run handles BST (UK summer = UTC+1, so 14:59 local). The second handles GMT (UK winter = UTC, so 14:59 local). Both runs check the UK local date inside the script — only one does real work on any given day; the other regenerates the same file harmlessly.
2. `generate_edition.py` works out tomorrow's UK-local date and day-of-week name.
3. It pulls live news/sport/showbiz from BBC RSS, day notes from Wikipedia, and lottery estimates from lottery.co.uk.
4. It picks the day's newsbrief / talkback / facts / forward calendars from `content_bank.json` using a date-keyed hash so each day is different but reproducible.
5. Jinja2 renders the data through `template_edition.html.j2` to produce the edition HTML.
6. WeasyPrint converts the HTML to PDF.
7. Both files are written to `editions/{day_name}.html` and `editions/{day_name}.pdf`, overwriting the previous week's copy.
8. The action commits and pushes. Cloudflare Pages picks up the commit and redeploys within ~30 seconds.

### One-time setup (~15 minutes)

You'll do these once, then the system runs itself forever.

**1. Put `site/` in a GitHub repo.**

```sh
cd "/Users/tonydibbin/prep flow/site"
git init
git add .
git commit -m "Prepflow v1"
git branch -M main
git remote add origin git@github.com:tonydibbin/prepflow-radiobusiness.git   # create the repo first on github.com
git push -u origin main
```

**2. Get a free Gemini API key and add it to GitHub.**

Go to [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey) → sign in with your Google account → "Create API key" → "Create API key in new project". Copy the key (starts with `AIza...`).

In GitHub → your repo → Settings → Secrets and variables → Actions → New repository secret:
- Name: `GEMINI_API_KEY`
- Value: paste the key

This unlocks the radio-voice rewrite step. The free tier gives you 250 requests/day; Prepflow uses 1 per day so you'll never see a bill or a quota wall. **No card on file required.**

If you skip this step entirely, the system still works — it just publishes the BBC headlines verbatim instead of in a polished radio voice.

**3. Connect Cloudflare Pages to the repo.**

Cloudflare dashboard → Workers & Pages → Create application → Pages → Connect to Git → choose the prepflow repo.
- Production branch: `main`
- Build command: (leave empty)
- Build output directory: `/`
- Save and Deploy.

Then in the Pages project → Custom domains → add `prepflow.theradiobusiness.co.uk`. Cloudflare proves the domain and adds it.

**4. Enable Actions in the repo.**

GitHub → your repo → Actions tab → "I understand my workflows, go ahead and enable them" if prompted.

**5. Smoke-test by manually triggering the workflow.**

GitHub → Actions → "Prepflow daily edition" → Run workflow → leave inputs blank → Run workflow.

In ~2 minutes you'll see a green tick and a fresh commit on `main` with tomorrow's edition files. Cloudflare auto-redeploys.

That's it. **No payment method. No monthly bill. Free quota is more than 250× what you'll ever use.**

### Editing the content bank

`scripts/content_bank.json` is the rotating content source. Edit it freely — the script picks deterministically from the pools you provide. Keep the JSON valid and the shapes intact. Specifically:

- `newsbrief` — array of `{lead, detail}`. 7 are picked per day.
- `survey` — array of pairs of `{question, answer}`. One pair is picked per day.
- `talk_topic` — array of pairs of `{lead, detail}`. One pair per day.
- `true_false` — array of `{fact_label, fact, fiction}`. One per day.
- `facts` — array of 8-item lists of strings. One list per day.
- `film_calendar` / `sport_calendar` — static `{left, right}` columns of months. Update by hand when calendars change (every few weeks).

Add more entries any time — the pools just grow and the rotation gets more varied. The minimum pool size to keep variety is **~7** items.

### Manual back-fill or date override

In the GitHub Actions UI, "Run workflow" → set `target_date` to a YYYY-MM-DD value to generate that specific day's edition. Useful for catching up after a missed run or testing.

You can also run locally:

```sh
cd "/Users/tonydibbin/prep flow/site"
PREPFLOW_TARGET_DATE=2026-05-20 python scripts/generate_edition.py
```

### Monitoring & failure handling

- Each run posts a green/red tick to the Actions tab.
- On failure, GitHub emails the repo owner by default.
- If BBC's RSS is briefly down, the script just generates fewer items in that section — it doesn't fail the build. The previous week's edition stays in place if the run does fail.
- The script is non-fatal on individual source failures: each source has a fallback (default lottery amounts, empty section if RSS unreachable, etc.).

### Upgrading the rewrite later

The current radio-voice rewrite uses Gemini 2.5 Flash (free). If you ever want a more sophisticated voice (longer detail, sharper phrasing, deeper context), you can swap in:

- **Gemini 2.5 Pro** — same free tier, slightly different limits, higher quality. Change the model string in `scripts/generate_edition.py` (`model="gemini-2.5-pro"`).
- **Anthropic Claude API** — paid, ~£0.50–£1.20/day on Opus. Worth it only if you find the Gemini voice falls short.

The whole polish step is one function (`polish_with_gemini`) so swapping providers is a single-file change.

### Cowork local fallback (already disabled)

A Cowork scheduled task at `~/Documents/Claude/Scheduled/prepflow-daily-edition/` was the very first generator. It has been **disabled** now that the free GitHub Action exists. To re-enable it as a manual local backup, open the Scheduled section in Cowork → toggle it on.

---

## Roadmap

**v1 — now.** Marketing/preview page + working sample edition + PDF + day-of-week URL convention + Cowork scheduled daily refresh.

**v1.1.** Move the schedule off Cowork onto GitHub Actions or Cloudflare Workers cron for full server-side automation.

**v1.2.** Subscriber list and morning email digest.

**v2.** Login, archive search (date-stamped historical editions kept after rotation), per-presenter customisation (drop a section, change tone, custom calendars), team accounts.

---

## Contact

tony@tonydibbin.com — early access list and product feedback.
