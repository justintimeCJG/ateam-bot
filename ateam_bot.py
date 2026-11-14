#!/usr/bin/env python3
"""
ateam_bot.py - A Team Bot: watches paid clinical-study sites and posts to Telegram.

Sources (same sites the Quicker Study Scoops bot links to):
  - ICON San Antonio screening slots (AppointmentQuest scheduler)
  - ICON study pages (San Antonio, Salt Lake City, Lenexa)
  - Celerion (helpresearch.com)  - Trialmed  - Medpace  - Fortrea
  - Altasciences Kansas City     - Parexel Baltimore (needs Playwright)

Setup:
  pip install requests beautifulsoup4
  pip install playwright && playwright install chromium   # only for Parexel
  Put your settings in a file called .env next to this script:
      TELEGRAM_BOT_TOKEN=123456:ABC...
      TELEGRAM_CHAT_ID=-5432816105
      POST_FOOTER=Please mention <your name> when asked how you heard about this study.

Usage:
  python ateam_bot.py --dry-run      # print everything it finds, post nothing
  python ateam_bot.py                # check once (first run just records what exists)
  python ateam_bot.py --loop 15      # keep checking every 15 minutes
  python ateam_bot.py --only icon_slots,celerion   # run some sources only
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "ateam_state.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}


# ---------------------------------------------------------------------------
# Settings (.env file or environment variables)
# ---------------------------------------------------------------------------
def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


load_env()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
THREAD_ID = os.environ.get("TELEGRAM_THREAD_ID", "")
FOOTER = os.environ.get("POST_FOOTER", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_soup(url, session=None):
    r = (session or requests).get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def clean(t):
    return " ".join((t or "").replace("\xa0", " ").split())


def txt(el, sel):
    found = el.select_one(sel) if el else None
    return clean(found.get_text(" ")) if found else ""


def study(source, key, title, url, **kw):
    """Standard study record every scraper returns."""
    d = {"source": source, "key": f"{source}:{key}", "title": title, "url": url,
         "code": "", "location": "", "pay": "", "dates": "", "ages": "", "details": ""}
    d.update({k: clean(v) if isinstance(v, str) else v for k, v in kw.items()})
    return d


# ---------------------------------------------------------------------------
# SOURCE: ICON screening slots (AppointmentQuest)
# ---------------------------------------------------------------------------
ICON_SCHEDULERS = {"Icon San Antonio": "https://www.appointmentquest.com/scheduler/2110084437.1"}
MONTHS_AHEAD = 3
MONTH_RE = re.compile(r"(January|February|March|April|May|June|July|August|"
                      r"September|October|November|December) \d{4}")
SHOWN_DAY_RE = re.compile(r"Available appointment times \(\w+\) for \w+, \w+ (\d{1,2})")


def _press(session, page_url, button):
    form = button.find_parent("form")
    action = urljoin(page_url, form.get("action"))
    if button.get("type") == "image":
        data = {f"{button['name']}.x": "5", f"{button['name']}.y": "5"}
    else:
        data = {button["name"]: button.get("value") or button.get_text(strip=True)}
    r = session.post(action, data=data, headers=UA, timeout=30)
    r.raise_for_status()
    return r.url, BeautifulSoup(r.text, "html.parser")


def _open_days(soup):
    return [i for i in soup.select('td input[type="submit"]')
            if re.fullmatch(r"\d{1,2}", (i.get("value") or "").strip())]


def _icon_slots_for(session, sched_url, index):
    r = session.get(sched_url, headers=UA, timeout=30)
    url, soup = r.url, BeautifulSoup(r.text, "html.parser")
    url, soup = _press(session, url, soup.select('input[value="Make Appointment"]')[index])
    slots = []
    for month_no in range(MONTHS_AHEAD):
        m = MONTH_RE.search(soup.get_text(" ", strip=True))
        month = m.group(1) if m else "?"
        for day in [d.get("value").strip() for d in _open_days(soup)]:
            shown = SHOWN_DAY_RE.search(soup.get_text(" ", strip=True))
            if not (shown and shown.group(1) == day):   # first open day is pre-selected
                btn = next(d for d in _open_days(soup) if d.get("value").strip() == day)
                url, soup = _press(session, url, btn)
            slots += [f"{month} {day} {b.get_text(strip=True)}"
                      for b in soup.select("button.INLINESUBMITBUTTON")]
        if month_no == MONTHS_AHEAD - 1:
            break
        nxt = next((i for i in soup.select('input[type="image"]')
                    if "ra.gif" in (i.get("src") or "")), None)
        if not nxt:
            break
        url, soup = _press(session, url, nxt)
    return slots


def scrape_icon_slots():
    out = []
    for location, url in ICON_SCHEDULERS.items():
        session = requests.Session()
        soup = get_soup(url, session)
        for i, btn in enumerate(soup.select('input[value="Make Appointment"]')):
            row = btn.find_parent("tr")
            name_row = row.find_previous_sibling("tr")
            name = clean(name_row.get_text(" ")) if name_row else f"Study {i+1}"
            try:
                slots = _icon_slots_for(session, url, i)
            except Exception as e:
                print(f"    {name}: {e}", file=sys.stderr)
                continue
            out.append(study("icon_slots", name, name, url, location=location,
                             details=txt(row, "blockquote"), slots=slots))
            time.sleep(1)
    return out


# ---------------------------------------------------------------------------
# SOURCE: ICON study pages (new studies)
# ---------------------------------------------------------------------------
ICON_PAGES = {"San Antonio, TX": "https://iconstudies.com/sanantonio/",
              "Salt Lake City, UT": "https://iconstudies.com/SLC/",
              "Lenexa, KS": "https://iconstudies.com/Lenexa/"}
ICON_LINK = re.compile(r"/Clinical-Research-Study/(\d+)/?$", re.I)


def scrape_icon_pages():
    out = []
    for location, page in ICON_PAGES.items():
        soup = get_soup(page)
        seen = set()
        for a in soup.find_all("a", href=True):
            m = ICON_LINK.search(a["href"])
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            card = a
            for _ in range(8):   # climb to the card holding this one study
                p = card.parent
                if p is None or len({ICON_LINK.search(x["href"]).group(1) for x in p.find_all("a", href=True)
                                     if ICON_LINK.search(x["href"])}) > 1:
                    break
                card = p
                if "Up to $" in card.get_text(" "):
                    break
            text = clean(card.get_text(" "))
            title = txt(card, "h1,h2,h3,h4,h5")
            grab = lambda pat: (re.search(pat, text, re.I) or [None, ""])[1]
            out.append(study("icon", f"{location}:{m.group(1)}", title, urljoin(page, a["href"]),
                             code=title.split(":")[0] if ":" in title else "",
                             location=f"ICON {location}", pay=grab(r"Up to (\$[\d,]+)"),
                             ages=grab(r"(\d{2}\s*[-–]\s*\d{2})"),
                             details=grab(r"(Participation in this study includes[^.]*)")))
    return out


# ---------------------------------------------------------------------------
# SOURCE: Celerion (helpresearch.com) - server-rendered cards, ?page=N
# ---------------------------------------------------------------------------
def scrape_celerion():
    out, seen = [], set()
    for page in range(10):
        soup = get_soup(f"https://helpresearch.com/studies?page={page}")
        cards = soup.select("fieldset.container-link")
        new = 0
        for c in cards:
            a = c.select_one("a.study-page-link")
            if not a or a["href"] in seen:
                continue
            seen.add(a["href"]); new += 1
            start = txt(c, ".views-field-field-csapi-start-date")
            end = txt(c, ".views-field-field-csapi-end-date")
            code = txt(c, ".views-field-field-csapi-studygroup")
            out.append(study("celerion", code or a["href"], txt(c, ".views-field-field-csapi-shortdesc-eng"),
                             urljoin("https://helpresearch.com/", a["href"]), code=code,
                             location="Celerion " + txt(c, ".views-field-field-csapi-sitename"),
                             pay=txt(c, ".views-field-field-csapi-stipend"),
                             dates=f"{start} - {end}" if start else "",
                             ages=txt(c, ".views-field-field-csapi-maxage"),
                             details=txt(c, ".views-field-field-csapi-start-date-1")))
        if not new:
            break
    return out


# ---------------------------------------------------------------------------
# SOURCE: Trialmed - WordPress JSON API, healthy-volunteer studies
# ---------------------------------------------------------------------------
TRIALMED_HEALTHY_AREA = 525   # "Healthy volunteers" therapy area; set None for all studies


def scrape_trialmed():
    r = requests.get("https://trialmed.com/wp-json/wp/v2/studies",
                     params={"per_page": 100, "_fields": "slug,link,title,meta"},
                     headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for s in r.json():
        m = s.get("meta") or {}
        if TRIALMED_HEALTHY_AREA and TRIALMED_HEALTHY_AREA not in (m.get("trialmed_study_therapy_areas") or []):
            continue
        locs = [l for l in (m.get("trialmed_study_locations") or [])
                if l.get("status") in ("enrolling-now", "coming-soon")]
        if not locs:
            continue
        pay = m.get("trialmed_study_compensation") or 0
        visits = (f"{m.get('trialmed_study_overnight', 0)} nights, "
                  f"{m.get('trialmed_study_outpatient', 0)} opvs, {m.get('trialmed_study_calls', 0)} calls")
        out.append(study("trialmed", s["slug"], clean(BeautifulSoup(s["title"]["rendered"], "html.parser").get_text()),
                         s["link"], code=m.get("trialmed_study_id", ""),
                         location="Trialmed " + ", ".join(l["name"] for l in locs),
                         pay=("$" + format(pay, ",")) if pay else "",
                         dates="Starts " + locs[0].get("startDate", "") if locs[0].get("startDate") else "",
                         ages=f"{m.get('trialmed_study_age_min')}-{m.get('trialmed_study_age_max')}",
                         details=f"BMI {m.get('trialmed_study_bmi_min')}-{m.get('trialmed_study_bmi_max')}, {visits}"
                                 + (" (coming soon)" if locs[0].get("status") == "coming-soon" else "")))
    return out


# ---------------------------------------------------------------------------
# SOURCE: Medpace - study cards
# ---------------------------------------------------------------------------
def scrape_medpace():
    # The page loads its cards from this JSON feed with JavaScript, so read the feed directly.
    r = requests.get("https://www.medpace.com/study-participants/wp-json/em/v1/post_factory",
                     params={"post_type": "em_study", "posts_per_page": 50, "term_counts": "false",
                             "partial": "card-study", "orderby": "date", "order": "desc", "paged": 1},
                     headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, str):   # the feed double-encodes its JSON
        data = json.loads(data)
    soup = BeautifulSoup("".join(data.get("posts") or []), "html.parser")
    out = []
    for a in soup.select("a.em-study-card"):
        details = clean(a.select_one(".em-study-card__details").get_text(" ")) if a.select_one(".em-study-card__details") else ""
        excerpt = clean(a.select_one(".em-study-card__excerpt").find_next("p").get_text(" ")) \
            if a.select_one(".em-study-card__excerpt") and a.select_one(".em-study-card__excerpt").find_next("p") else ""
        label = txt(a, ".em-study-card__label")
        out.append(study("medpace", a["href"].rstrip("/").split("/")[-1], txt(a, ".em-study-card__title"),
                         a["href"], location="Medpace Cincinnati, OH",
                         pay=(re.search(r"\$[\d,]+", label) or [""])[0],
                         ages=(re.search(r"Age:\s*([\d\-+ ]+)", details) or [None, ""])[1],
                         details=f"{excerpt} {details}".strip()))
    return out


# ---------------------------------------------------------------------------
# SOURCE: Fortrea - Drupal table, ?page=N
# ---------------------------------------------------------------------------
def scrape_fortrea():
    base = "https://www.fortreaclinicaltrials.com/en-us/clinical-research/browse-studies"
    out, seen = [], set()
    for page in range(10):
        soup = get_soup(f"{base}?page={page}")
        new = 0
        for tr in soup.select("table tbody tr"):
            a = tr.select_one("td.views-field-title a")
            if not a:
                continue
            cell = clean(tr.select_one("td.views-field-title").get_text(" "))
            title = clean(a.get_text())
            code = clean(cell.replace(title, "", 1))
            key = f"{a['href']}|{code}"
            if key in seen:
                continue
            seen.add(key); new += 1
            out.append(study("fortrea", key, title, urljoin(base, a["href"]), code=code,
                             location="Fortrea " + txt(tr, "td.views-field-field-location"),
                             pay=txt(tr, "td.views-field-field-compensation"),
                             dates=txt(tr, "td.views-field-field-dates-1"),
                             ages=txt(tr, "td.views-field-field-age-freetext"),
                             details=txt(tr, "td.views-field-field-study-design")
                                     + (" Smokers OK." if txt(tr, "td.views-field-field-smoker") == "Yes" else "")))
        if not new:
            break
    return out


# ---------------------------------------------------------------------------
# SOURCE: Altasciences Kansas City - div.summary cards
# ---------------------------------------------------------------------------
def scrape_altasciences():
    base = "https://participantskc.altasciences.com/"
    soup = get_soup(base + "available-studies")
    out = []
    for c in soup.select("div.summary"):
        a = c.select_one("a[href*='etudes/']")
        if not a:
            continue
        sid = a["href"].rstrip("/").split("/")[-1]
        out.append(study("altasciences", sid, txt(c, ".title"), urljoin(base, a["href"]),
                         location="Altasciences Overland Park, KS",
                         pay=(lambda d: ("$" + format(int(d), ",")) if d else "")(re.sub(r"\D", "", txt(c, ".compensation"))),
                         dates=", ".join(clean(li.get_text(" ")) for li in c.select(".date-list li")),
                         ages=txt(c, ".age"),
                         details=f"{txt(c, '.gender')}. {txt(c, '.nbVisits')}. {txt(c, '.smoke-status')}"))
    return out


# ---------------------------------------------------------------------------
# SOURCE: Parexel Baltimore - Salesforce site, needs a real browser (Playwright)
# ---------------------------------------------------------------------------
PAREXEL_URL = "https://pxlcrs.my.site.com/portal/s/trials-listing-bal?loc=BAL&language=en_US"


def scrape_parexel():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("    (skipping Parexel: run 'pip install playwright && playwright install chromium')")
        return []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA["User-Agent"])
        page.goto(PAREXEL_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("text=Learn More", timeout=45000)   # wait for the study list
        page.wait_for_timeout(2000)
        text = page.inner_text("body")
        browser.close()
    body = text.split("Filter your criteria below to find a suitable trial:", 1)[-1]
    body = body.split("Get Started", 1)[0]
    out = []
    for block in body.split("Learn More"):
        lines = [clean(l) for l in block.splitlines() if clean(l)]
        if len(lines) < 3:
            continue
        title = lines[0]
        code = lines[1].lstrip("﻿")
        rest = " ".join(lines[2:])
        out.append(study("parexel", code, title, PAREXEL_URL, code=code,
                         location="Parexel Baltimore, MD",
                         pay=(re.search(r"\$[\d,]+", rest) or [""])[0],
                         ages=(re.search(r"\b(\d{2}\s*[-+]\s*(?:\d{2})?)", rest) or [None, ""])[1],
                         details=rest.split("Study Information:", 1)[-1][:400]))
    return out


SOURCES = {
    "icon_slots": scrape_icon_slots,
    "icon": scrape_icon_pages,
    "celerion": scrape_celerion,
    "trialmed": scrape_trialmed,
    "medpace": scrape_medpace,
    "fortrea": scrape_fortrea,
    "altasciences": scrape_altasciences,
    "parexel": scrape_parexel,
}
NAMES = {"icon": "ICON", "celerion": "CELERION", "trialmed": "TRIALMED", "medpace": "MEDPACE",
         "fortrea": "FORTREA", "altasciences": "ALTASCIENCES", "parexel": "PAREXEL"}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_slots_post(s):
    lines = [f"<b>{esc(s['title'])}</b>", f"<b>{len(s['slots'])}</b>", "Screenings Available",
             esc(s["location"]), esc(s["details"]), ""]
    if FOOTER:
        lines.append(esc(FOOTER))
    lines.append(s["url"])
    return "\n".join(lines)


def format_study_post(s, tag="NEW"):
    today = dt.date.today().strftime("%B-%d-%Y")
    lines = [today, f"<b>{tag} {NAMES.get(s['source'], s['source'].upper())} STUDY:</b>",
             esc(s["location"])]
    if s["pay"]:
        lines.append(f"💵 {esc(s['pay'])}")
    if s["code"]:
        lines.append(f" - {esc(s['code'])}")
    if s["title"]:
        lines.append(esc(s["title"]))
    if s["dates"]:
        lines.append(f"Dates: {esc(s['dates'])}")
    if s["ages"]:
        lines.append(f"Ages: {esc(s['ages'])}")
    if s["details"]:
        lines.append(esc(s["details"]))
    lines.append(s["url"])
    if FOOTER:
        lines.append(esc(FOOTER))
    return "\n".join(lines)


def send(text, dry_run):
    if dry_run:
        print(text + "\n" + "-" * 40)
        return
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID - put them in the .env file.")
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if THREAD_ID:
        payload["message_thread_id"] = int(THREAD_ID)
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=30)
        if r.status_code == 429:   # rate limited - wait as told
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
            continue
        if not r.ok:
            print("Telegram error:", r.text, file=sys.stderr)
        break
    time.sleep(1.5)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_once(only, post_all, dry_run):
    first_run = not STATE_FILE.exists()
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    quiet = first_run and not post_all and not dry_run
    if quiet:
        print("First run: recording what's listed now without posting. New studies will post from next run.")

    for name, fn in SOURCES.items():
        if only and name not in only:
            continue
        print(f"[{name}] checking…")
        try:
            items = fn()
        except Exception as e:
            print(f"[{name}] failed: {e}", file=sys.stderr)
            traceback.print_exc(limit=1)
            continue   # keep old state for this source
        print(f"[{name}] {len(items)} found")
        old = state.get(name, {})
        new_state = {}
        for s in items:
            prev = old.get(s["key"])
            if name == "icon_slots":
                new_state[s["key"]] = s["slots"]
                gained = set(s["slots"]) - set(prev or [])
                if s["slots"] and (post_all or dry_run or (gained and not quiet)):
                    send(format_slots_post(s), dry_run)
            else:
                new_state[s["key"]] = {"pay": s["pay"], "dates": s["dates"]}
                if post_all or dry_run or (prev is None and not quiet):
                    send(format_study_post(s), dry_run)
                elif prev and prev.get("pay") != s["pay"] and not quiet:
                    send(format_study_post(s, tag=f"PAY CHANGE (was {prev.get('pay')})"), dry_run)
        state[name] = new_state

    if not dry_run:
        STATE_FILE.write_text(json.dumps(state, indent=2))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="print instead of posting")
    p.add_argument("--all", action="store_true", help="post everything currently listed")
    p.add_argument("--loop", type=int, metavar="MIN", help="repeat every N minutes")
    p.add_argument("--only", help="comma-separated sources: " + ",".join(SOURCES))
    a = p.parse_args()
    only = set(a.only.split(",")) if a.only else None
    while True:
        run_once(only, a.all, a.dry_run)
        if not a.loop:
            break
        print(f"Sleeping {a.loop} min…")
        time.sleep(a.loop * 60)


if __name__ == "__main__":
    main()
