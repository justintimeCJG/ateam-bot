"""
Study alert bot: watches paid clinical-study sites and posts new and reopened
studies to a Telegram group, in the same style as StudyScoopsBot.

Commands
  python studybot.py --find-chat     show the chat ID of groups your bot is in
  python studybot.py --check         read every source once and PRINT the posts
                                     it would make (nothing is sent or saved)
  python studybot.py --check icon_sa only check one source (use its key)
  python studybot.py --dump          save each source's page to dumps/
  python studybot.py                 run for real: post new/reposted studies

On the first real run every study already listed is recorded silently so the
group isn't flooded; only studies that appear after that get posted.
Add --post-existing to post everything currently listed instead.
"""

import asyncio
import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
# On GitHub the token comes from a hidden secret instead of config.json
import os
if os.environ.get("TELEGRAM_BOT_TOKEN"):
    CONFIG["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]

# Oracle server only: its Ubuntu image is missing system libraries that
# Playwright/Chromium need, and the bot has no admin rights, so the bot
# downloads those packages itself and unpacks them into libroot/.
SERVER_SETUP = r"""
set -x
mkdir -p debs libroot
cd debs
PKGS="libatomic1 libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64
 libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3
 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3
 libxkbcommon0 libxrandr2 libfontconfig1 libfreetype6 fonts-liberation"
ALL=$(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
  --no-breaks --no-replaces --no-enhances $PKGS 2>/dev/null | grep '^[a-z0-9]' | sort -u)
for p in $ALL; do dpkg -s "$p" >/dev/null 2>&1 || apt-get download "$p"; done
for f in *.deb; do dpkg -x "$f" ../libroot; done
cd ..
venv/bin/python -m playwright install chromium && touch libroot/.ok2
"""
SERVER = HERE.resolve().as_posix() == "/opt/studybot"
if SERVER:
    import subprocess
    sys.stdout.reconfigure(line_buffering=True)  # log lines show up right away
    _lib = HERE / "libroot"
    os.environ["LD_LIBRARY_PATH"] = ":".join(
        [str(_lib / "usr/lib/aarch64-linux-gnu"), str(_lib / "lib/aarch64-linux-gnu"),
         str(_lib / "usr/lib")] + [p for p in [os.environ.get("LD_LIBRARY_PATH")] if p])
    if not (_lib / ".ok2").exists():
        subprocess.run(["bash", "-c", SERVER_SETUP], cwd=HERE)
    # the server has no fonts installed; point Chromium at the unpacked ones
    _fc = _lib / "fonts.conf"
    _fc.write_text(f"""<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>
<include ignore_missing="yes">/etc/fonts/fonts.conf</include>
<dir>{_lib}/usr/share/fonts</dir><cachedir>{HERE}/fontcache</cachedir></fontconfig>
""")
    os.environ["FONTCONFIG_FILE"] = str(_fc)
    # keep the bot's Telegram token where the always-on checker can read it
    # (only the bot's own user account can open this file)
    _tok = HERE / ".token"
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        if not _tok.exists() or _tok.read_text().strip() != os.environ["TELEGRAM_BOT_TOKEN"]:
            _tok.write_text(os.environ["TELEGRAM_BOT_TOKEN"])
        os.chmod(_tok, 0o600)
    elif _tok.exists():
        CONFIG["bot_token"] = _tok.read_text().strip()
DB = sqlite3.connect(HERE / "studybot.db")
DB.execute("""CREATE TABLE IF NOT EXISTS items(
    source TEXT, key TEXT, present INTEGER, missing INTEGER, sig TEXT,
    first_seen REAL, last_posted REAL, title TEXT, PRIMARY KEY(source, key))""")
DB.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
SCHEMA = "2"
if (DB.execute("SELECT v FROM meta WHERE k='schema'").fetchone() or [None])[0] != SCHEMA:
    DB.execute("DELETE FROM items")  # readers changed: start fresh
    DB.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA,))
    DB.commit()

# --------------------------------------------------------------------------
# Sources (checked against each live site on Nov 14, 2026).
#   mode "cards": each study is one element on the list page (card = CSS selector)
#   mode "links": studies are links matching link_re; the bot reads the text around each
#   mode "text":  page text between start/end, split into studies on `split`
#   detail:       also open each NEW study's own page for extra details
# --------------------------------------------------------------------------
ICON = dict(mode="cards", card=".studies-card__inner", link="a.studies-card__inner-link",
            title=".studies-card__title", study=".studies-card__number-inner",
            detail=True, detail_only=["bmi"], style="icon", calendars=True)
SOURCES = [
    dict(ICON, key="icon_sa", header=["Icon VCT, San Antonio, TX", "(210)225-5437. Online appointment page is more efficient"],
         list_urls=["https://iconstudies.com/sanantonio/"]),
    dict(ICON, key="icon_lenexa", header=["Icon VCT, Lenexa, KS", "(913)410-2900"],
         list_urls=["https://iconstudies.com/Lenexa/"], style="icon_ks"),
    dict(ICON, key="icon_slc", header=["Icon VCT, Salt Lake City, UT", "(801)269-8200"],
         list_urls=["https://iconstudies.com/SLC/"]),
    dict(key="icon_sa_screenings", header=["Screenings Available", "Icon San Antonio"],
         list_urls=["https://www.appointmentquest.com/scheduler/2110084437.1"], mode="cards",
         card="tr:has(> td > table td.LABEL h4)", title="h4", key_from="title",
         exclude=r"study done|appts? cx|cancel", style="screenings", no_date=True),
    dict(key="parexel_bal", header=["(877)617-8839", "Parexel Baltimore"], style="parexel",
         list_urls=["https://pxlcrs.my.site.com/portal/s/trials-listing-bal?loc=BAL&language=en_US"],
         mode="text", start="find a suitable trial:", end="Get Started", split=r"\n\s*Learn More\s*\n",
         key_re=r"\b(\d{6})\b", wait=8000, detail="click"),
    dict(key="parexel_la", header=["8882287425", "Parexel Glendale"], style="parexel",
         list_urls=["https://pxlcrs.my.site.com/portal/s/triallistla?loc=LA&language=en_US"],
         mode="text", start="find a suitable trial:", end="Get Started", split=r"\n\s*Learn More\s*\n",
         key_re=r"\b(\d{6})\b", wait=8000, detail="click"),
    dict(key="fortrea", header=["Fortrea", "{status}: (866)429-3700"], status_in_header=True, style="fortrea",
         list_urls=[f"https://www.fortreaclinicaltrials.com/en-us/clinical-research/browse-studies?page={i}"
                    for i in range(4)],
         mode="cards", card="div.view-studies-us tbody tr", link="td.views-field-title a",
         title="td.views-field-title a", study_re=r"\n(\d{6,7}[^\n]*)\n", detail=True),
    dict(key="abbvie", header=["{status}: 800-827-2778", "Abbvie, NonVCT, Grayslake IL"], status_in_header=True,
         no_date=True, style="plain", list_urls=["https://abbviephase1.com/#/available-trials"],
         mode="links", link_re=r"InputFiles/Files/.+\.pdf", detail="pdf"),
    dict(key="celerion", header=["{STATUS} CELERION STUDY:", "Online Registration (quickest):",
                                 "https://eregportal.helpresearch.com/PivotalUX/", "Phone: (866)445-7033."],
         status_in_header=True, style="celerion",
         list_urls=[f"https://helpresearch.com/studies?page={i}" for i in range(4)],
         mode="cards", card="div.inner-container", link="a.study-page-link", detail=True,
         study_re=r"\b(C[A-Z]\d{5}-\w+)", skip_re=r"^starts in"),
    dict(key="trialmed", header=["{STATUS} Trialmed formerly PPD", "(800)773-2782"], status_in_header=True,
         include=r"No major health conditions|Non-childbearing potential|Healthy (?:volunteers|adults|men|women)",
         style="trialmed", list_urls=[f"https://trialmed.com/find-a-study/?paged={i}" for i in range(1, 7)],
         mode="cards", card="div.wp-study-card", link="a.wp-study-card-title", title="a.wp-study-card-title",
         study="div.wp-study-card-study-id", detail=True),
    dict(key="alta_kc", header=["{status}", "Altasciences KS (913)213-2970", "", "Altasciences", "VCT and RSVP",
                                "Overland Park, KS"], status_in_header=True, no_date=True, style="plain",
         list_urls=["https://participantskc.altasciences.com/available-studies"],
         mode="links", link_re=r"/etudes/\d+", detail=True,
         closed_notice="{title} - Recruitment Complete In Altasciences KS is done recruiting"),
    dict(key="alta_la", header=["{status}:", "Altasciences, Cypress CA", "(866)461-2526"], status_in_header=True,
         no_date=True, style="plain", list_urls=["https://participantsla.altasciences.com/current-studies"],
         mode="cards", card="div.current-studies-tbody.views-row", link="a[href*='/current-studies/']",
         study="div.studynumber", detail=True, skip_re=r"^age\b"),
    dict(key="medpace", header=["{status}: (513)366-3222 Medpace, Cincinatti"], status_in_header=True,
         style="plain", list_urls=["https://www.medpace.com/study-participants/browse-studies/"],
         mode="cards", card="a.em-study-card", title=".em-study-card__title", detail=True),
    dict(key="drvince", header=["{status}:", "Dr Vince (913)-333-3000"], status_in_header=True, no_date=True,
         style="plain", list_urls=["https://drvince.com/current-studies/"],
         mode="cards", card=".card--study", key_re=r"Study Indicator:\s*(\w+)", study_re=r"Study Indicator:\s*(\w+)"),
    dict(key="frontage", header=["{brand}, Frontage, Seacaucus NJ, NonVCT", "877-298-9071"], status_in_header=True,
         no_date=True, style="passthrough", list_urls=["https://www.frontagelab.com/clinical-studies/"],
         mode="links", link_re=r"frontagelab\.com/clinical-studies/(?!referral)[\w-]+/?$", detail=True,
         post_url="https://www.frontagelab.com/clinical-studies/",
         fresh=False,  # this site answers "page not found" if anything is added to the address
         need_money=False),
]
# Worldwide Clinical Trials' study page now forwards to Fortrea, which is covered above.

# --------------------------------------------------------------------------
# Reading study details out of text
# --------------------------------------------------------------------------
MONEY = re.compile(r"\$\s?(\d{1,3}(?:[,\s]\d{3})+|\d{3,6})(?:\.\d{2})?")
AGES = re.compile(r"\bages?\b[^0-9]{0,15}(\d{2})\s*(?:-|–|to)\s*(\d{2}|99\+)|(\d{2})\s*(?:-|–|to)\s*(\d{2})\s*years", re.I)
BMI = re.compile(r"\bbmi\b[^0-9]{0,25}(\d{2}(?:\.\d+)?)\s*(?:-|–|to)\s*(\d{2}(?:\.\d+)?)", re.I)
NIGHTS = re.compile(r"(\d+)\s*-?\s*(?:nights?|overnights?)\b", re.I)
OPV = re.compile(r"(\d+)\s*(?:in-person\s+)?(?:outpatient|out-patient|opvs?|follow[- ]?ups?(?!\s+(?:phone|call))|returns?|"
                 r"clinic visits?|return visits?)\b", re.I)
STAYS = re.compile(r"[^\n]*\b\d+\s*(?:-?\s*nights?|night stay|overnight)\b[^\n]*", re.I)
DATES = re.compile(r"(?:check[- ]?in|admission|inpatient|clinic stay|dates?|start date)[ \t]*:?[ \t]*\n?[ \t]*"
                   r"(?![A-Za-z ]+:)(?=[^\n$]*\d)[^\n$]{3,80}", re.I)
SEX = re.compile(r"\b(males?\s*(?:and|&|/)\s*females?|men\s*(?:and|&)\s*women|females?\s+only|males?\s+only|"
                 r"healthy\s+(?:males?|females?|women|men)\b[^\n,.]{0,40}|male/female|postmenopausal\s+females?)", re.I)


# Start/end dates, most specific first. Each returns the text after "Dates: ".
DATE_RULES = [
    (r"START DATE:\s*([A-Z][a-z]+ \d{1,2}, \d{4})[\s\S]{0,80}?END DATE:\s*([A-Z][a-z]+ \d{1,2}, \d{4})", "{} - {}"),
    (r"\bDates\s*:?\s*\n*\s*([A-Z][a-z]{2} \d{1,2},? \d{4})\s*[-\u2013]\s*([A-Z][a-z]{2} \d{1,2},? \d{4})", "{} - {}"),
    (r"Admissions?\s*:\s*([^\n]+)", "{}"),
    (r"Trial Dates\s*\n+\s*([^\n]+)", "{}"),
    (r"Clinic stay\s*\n*\s*([^\n]+)", "{}"),
    (r"Check[- ]?In:?[ \t]*\n?[ \t]*([^\n]*\d[^\n]*?)\s*(?:\n|$)[\s\S]{0,40}?Check[- ]?Out\s*:?[ \t]*\n?[ \t]*([^\n]*\d[^\n]*)", "{} - {}"),
    (r"Check[- ]?In:?[ \t]*\n?[ \t]*([^\n]*\d[^\n]*)", "Check-in {}"),
    (r"\bDates?:[ \t]*([^\n$]*\d[^\n$]*)", "{}"),
]


def find_dates(text):
    for rx, fmt in DATE_RULES:
        m = re.search(rx, text, re.I)
        if m:
            return re.sub(r"\s+", " ", fmt.format(*[g.strip(" ,") for g in m.groups()])).strip()
    return None


def money_values(text):
    out = []
    for m in MONEY.finditer(text or ""):
        try:
            out.append(int(re.sub(r"[,\s]", "", m.group(1))))
        except ValueError:
            pass
    return [v for v in out if v >= 300]


WORDNUM = dict(one=1, two=2, three=3, four=4, five=5, six=6, seven=7, eight=8, nine=9, ten=10)
_N = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)"


def _num(x):
    return int(x) if x.isdigit() else WORDNUM.get(x.lower(), 1)


def total_nights_text(t):
    """Rewrite '2 stays of 8 nights' / 'three 4-night stays' as '16 nights' / '12 nights'."""
    t = re.sub(_N + r"\s+(?:separate\s+|in-house\s+)*stays?\s+of\s+(?:up-?to\s+)?(?:\d+\s*-?\s*days?\s*/\s*)?"
               r"(\d+)\s*-?\s*nights?(?:\s+each)?",
               lambda m: f"{_num(m.group(1)) * int(m.group(2))} nights", t, flags=re.I)
    t = re.sub(_N + r"\s+(?:separate\s+)?(\d+)\s*-\s*night\s+stays",
               lambda m: f"{_num(m.group(1)) * int(m.group(2))} nights", t, flags=re.I)
    return t


def extract(text):
    f = {}
    counted = total_nights_text(text)
    if m := AGES.search(text):
        a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        f["ages"] = f"{a} - {b}"
    if m := BMI.search(text):
        f["bmi"] = f"{m.group(1)} - {m.group(2)}"
    elif m := re.search(r"\bbmi\b[^0-9]{0,25}(\d{2}(?:\.\d+)?)\s*(\+|or\s+(?:above|higher|more|greater|over))", text, re.I):
        f["bmi"] = f"{m.group(1)}+"
    # count from the one line that describes the stays when there is one,
    # so pages that repeat the schedule aren't counted twice
    stay_line = next((l for l in counted.splitlines() if NIGHTS.search(l)), "")
    nights = sum(int(n) for n in NIGHTS.findall(stay_line or counted) if 0 < int(n) < 120)
    opv_src = stay_line if OPV.search(stay_line) else counted
    opv = sum(int(n) for n in OPV.findall(opv_src) if int(n) < 60)
    if nights:
        f["nights"] = nights
    if opv:
        f["opv"] = opv
    if m := SEX.search(text):
        f["sex"] = m.group(0).strip()
    if m := STAYS.search(text):
        f["stays"] = m.group(0).strip(" •o-")
    d = find_dates(text)
    if d:
        f["dates"] = d
    return f


def lines_of(text):
    return [re.sub(r"[ \t ﻿]+", " ", l).strip() for l in (text or "").splitlines()
            if l.strip() and l.strip() not in ("/", "|")]


def fmt_money(v):
    return f"${v:,}"


# --------------------------------------------------------------------------
# Building posts
# --------------------------------------------------------------------------
def _md(d):
    return f"{d.month}/{d.day}"


def _span(a, b):
    """11/18-21, or 11/30-12/3 when the month changes."""
    if (a.month, a.day) == (b.month, b.day):
        return _md(a)
    return f"{_md(a)}-{b.day}" if a.month == b.month else f"{_md(a)}-{_md(b)}"


def _date_runs(dates):
    """[11/13, 11/16, 11/17] -> '11/13 & 16-17'"""
    from datetime import timedelta
    dates = sorted(set(dates))
    runs, out, last_month = [], [], None
    for d in dates:
        if runs and d - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(d)
        else:
            runs.append([d])
    for r in runs:
        a, b = r[0], r[-1]
        first = _md(a) if a.month != last_month else str(a.day)
        out.append(first + (f"-{b.day}" if b != a and b.month == a.month else (f"-{_md(b)}" if b != a else "")))
        last_month = b.month
    return " & ".join(out)


def celerion_post(item):
    from datetime import datetime as dt
    card, detail = item.get("text") or "", item.get("detail") or ""
    g = lambda rx, t=detail: (re.search(rx, t, re.I | re.M) or [None, None])[1]
    pays = money_values(card) or money_values(detail)
    rows = [l.split("\t") for l in detail.splitlines() if l.count("\t") >= 5]
    def day(x):
        try:
            return dt.strptime(x.strip().split()[0], "%m/%d/%Y")
        except Exception:
            return None
    # the page can list the same schedule table twice: keep each row once
    rows = list({tuple(c.strip() for c in r): r for r in rows}.values())
    stays = [r for r in rows if r[0].strip().lower().startswith("check-in")]
    returns = [r for r in rows if r[0].strip().lower() == "return"]
    site = (stays[0][-1].strip() if stays else None) or next(
        (l for l in lines_of(card) if l in ("Lincoln", "Phoenix", "Belfast")), "")
    study = g(r"STUDY NUMBER:\s*(\S+)")
    group = g(r"GROUP NUMBER:\s*(\S+)")
    name = g(r"REFER A FRIEND\s*\n\s*([^\n*]+)") or (lines_of(card) or [""])[-1]
    who = (g(r"^\s*([A-Z][A-Z &,'-]*(?:MALES?|FEMALES?)[A-Z &,'-]*)\s*$") or
           next((l for l in lines_of(card) if re.search(r"males?|females?", l, re.I)), "")).lower()
    sex = "M/F" if ("male" in who.replace("female", "") and "female" in who) else (
        "F" if "female" in who else ("M" if "male" in who else ""))
    if re.search(r"non-?child|post-?menopaus|sterile", who):
        sex += " (pm/ss)"
    age = g(r"AGE:\s*(\d+\s*-\s*\d+)")
    bmi = g(r"BMI:\s*([\d.]+\s*-\s*[\d.]+)")
    lines = [f"{fmt_money(max(pays)) if pays else ''} Celerion {site}".strip()]
    if study:
        lines.append(f"Study #{study}" + (f" G{group}" if group else ""))
    if name:
        lines.append(f"Study name: {name.strip()}")
    if sex:
        lines.append(sex)
    if age:
        lines.append(f"Age: {re.sub(r'\s+', '', age)}")
    if bmi:
        lines.append(f"Bmi: {re.sub(r'\s+', '', bmi)}")
    spans = [_span(day(r[2]), day(r[4])) for r in stays if day(r[2]) and day(r[4])]
    if spans:
        opv = [_md(day(r[2])) for r in returns if day(r[2])]
        spans, opv = list(dict.fromkeys(spans)), list(dict.fromkeys(opv))
        lines += ["In \U0001f3e1:", ", ".join(spans) + (f" with opv: {', '.join(opv)}" if opv else "")]
    m = re.search(r"SCREENING DATES:([\s\S]*?)(?:DOCTOR EXAM|STUDY DATES|$)", detail)
    screen = sorted(set(d for d in (day(x) for x in re.findall(r"\d{1,2}/\d{1,2}/\d{4}", m.group(1) if m else "")) if d))
    if screen:
        lines += ["Screening \U0001f5d3\ufe0f:", _date_runs(screen)]
    return "\n".join(lines)


def icon_post(src, status, item):
    """ICON style, e.g.
    November-06-2026 / New: / Icon VCT, San Antonio, TX / phone / 3705-0020S Enrolling / Cohort 3 /
    $15200 / $660.87 per day IP and OPV / Healthy Volunteers. / Male/Female Age 18 - 65 /
    bmi: 30.0+ or 27.0+ / 1 screening visit, 1 stay of 4 nights and 19 follow-up visits. / link"""
    card, detail = item.get("text") or "", item.get("detail") or ""
    cl = lines_of(card)
    lines = [datetime.now().strftime("%B-%d-%Y"), f"{status}:"] + list(src["header"])
    study = item.get("study", "").strip()
    if study:
        lines.append(f"{study} Enrolling")
    cohorts = re.findall(r"\b(Cohort\s+[\w-]+)\s+(?:Dates|Calendar)\b", detail, re.I)
    if cohorts:
        lines.append(" & ".join(dict.fromkeys(cohorts)))
    pays = money_values(card) or money_values(detail)
    sched = next((l for l in cl if re.match(r"Participation in this study includes", l, re.I)), "")
    sched = re.sub(r"^Participation in this study includes\s*", "", sched, flags=re.I).strip()
    if pays:
        pay = max(pays)
        lines.append(f"${pay}")
        f = extract(card)
        days = f.get("nights", 0) + f.get("opv", 0)
        if days:
            lines.append(f"${pay / days:.2f} per day IP and OPV")
    title = (item.get("title") or "").strip()
    if title:
        lines.append(title if title.endswith(".") else title + ".")
    sex = next((l for l in cl if re.fullmatch(r"(?:Male/Female|Female|Male|Females?|Males?)", l, re.I)), "")
    age = next((l for l in cl if re.match(r"Age\s+\d", l)), "")
    if sex or age:
        lines.append(f"{sex} {age}".strip())
    bmi = re.search(r"\bBMI\s*:?\s*([^\n]*\d[^\n]*)", detail, re.I)
    if bmi:
        lines.append(f"bmi: {bmi.group(1).strip()}")
    named = [(n, read_calendar(t)) for n, t in item.get("calendars") or [] if t]
    named = [(re.sub(r"\s*(?:Dates|Calendar)\s*$", "", n, flags=re.I), c) for n, c in named if c["stays"] or c["opvs"]]
    lines += calendar_lines([c for _, c in named], [n for n, _ in named])
    if sched:
        lines.append(sched[0].upper() + sched[1:] if sched[0].isalpha() else sched)
    if item.get("url"):
        lines.append(item["url"])
    return "\n".join(lines).strip()


MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
CAL_DATE = (r"\d{1,2}-[A-Z][a-z]{2}-\d{2,4}"                       # 19-Nov-26
            r"|" + MONTHS + r"\s+\d{1,2},?\s+\d{4}"                # December 14, 2026
            r"|\d{1,2}/\d{1,2}/\d{2,4}")                            # 12/14/2026
CAL_LABEL = (r"Check[- ]?in|Admit\w*|Admission|Check[- ]?out|Discharge\w*|Follow\s*-?\s*Up|"
             r"Out-?patient|\bOP\b|Return|End of Study|EOS|Screening")


def _cal_day(x):
    from datetime import datetime as dt
    x = re.sub(r"\s+", " ", x.replace(",", " ").replace(".", "")).strip()
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.strptime(x, fmt)
        except ValueError:
            pass
    if x.lower().startswith("sept"):
        return _cal_day("Sep" + x[4:])
    return None


def read_calendar(text):
    """ICON calendar PDF text -> group, stays [(in, out)] and outpatient dates.
    Works for 'Check-in Day -1 19-Nov-26' tables (Lenexa) and
    'Day -1 Admit Sunday, December 13, 2026' tables (San Antonio)."""
    toks = re.findall(f"({CAL_LABEL})|({CAL_DATE})", text, re.I)
    queue, rows = [], []
    for label, date in toks:
        if label:
            queue.append(label.lower())
        elif queue:
            rows.append((queue.pop(0), _cal_day(date)))
    # calendars sometimes carry the wrong year on a later date (e.g. 5-Jan-26 after
    # 29-Dec-26): keep the visits in calendar order by moving such dates forward a year
    from datetime import timedelta
    prev = None
    for i, (label, d) in enumerate(rows):
        if d and prev:
            while d < prev - timedelta(days=60):
                d = d.replace(year=d.year + 1)
            rows[i] = (label, d)
        if d and not label.startswith(("eos", "screening")):
            prev = d
    stays, opvs, cin = [], [], None
    for label, d in rows:
        if not d:
            continue
        if label.startswith(("admi", "check-in", "checkin", "check in")):
            cin = d
        elif label.startswith(("check", "discharge")):
            if cin:
                stays.append((cin, d))
                cin = None
        elif label.startswith(("follow", "out", "op", "return", "end of study")):
            opvs.append(d)
    if cin:
        stays.append((cin, cin))
    g = re.search(r"Group\s+([A-Za-z]?\d+[A-Za-z]?)", text)
    part = re.search(r"\bPart\s+([A-Z])\b", text)
    group = g.group(1) if g else ""
    if part and group[:1].upper() == part.group(1):
        group = group[1:]
    head = re.search(r"(\d{4}/\d{3,4})\s+(Part\s+[A-Z])?", text)
    return dict(group=group, stays=stays, opvs=opvs,
                study=head.group(1) if head else "", part=(head.group(2) or "") if head else "")


def calendar_lines(cals, names):
    """In 🏡 block for each calendar: '11/19-25 with opvs:' / '11/27, 12/1, 4, & 11'."""
    out = []
    for c, name in zip(cals, names):
        out.append("In \U0001f3e1:" if len(cals) == 1 else f"In \U0001f3e1 ({name}):")
        spans = ", ".join(_span(a, b) for a, b in c["stays"])
        if c["opvs"]:
            out += [f"{spans} with opv{'s' if len(c['opvs']) > 1 else ''}:".strip(), _opv_list(c["opvs"])]
        elif spans:
            out.append(spans)
    return out


def _opv_list(dates):
    out, last = [], None
    for d in sorted(dates):
        out.append(_md(d) if d.month != last else str(d.day))
        last = d.month
    if len(out) > 2:
        out[-1] = "& " + out[-1]
        return ", ".join(out)
    return " & ".join(out)


def icon_ks_post(src, status, item):
    """ICON Lenexa style, e.g.
    $10,500 ICON Lenexa / Study #0009/1171 Part A G4 / RE-RELEASED / M/F (all) / Age: 18-55 /
    Bmi: 27-34.9 / In 🏡: / 11/19-25 with opvs: / 11/27, 12/1, 4, 11, 18, & 29"""
    card, detail = item.get("text") or "", item.get("detail") or ""
    cl = lines_of(card)
    pays = money_values(card) or money_values(detail)
    cals = [read_calendar(t) for _, t in item.get("calendars") or [] if t]
    cals = [c for c in cals if c["stays"] or c["opvs"]]
    lines = [f"{fmt_money(max(pays)) if pays else ''} ICON Lenexa".strip(), "(913)410-2900"]
    if cals:
        c0 = cals[0]
        groups = " & ".join(f"G{c['group']}" for c in cals if c["group"])
        lines.append(" ".join(x for x in ["Study #" + (c0["study"] or item.get("study", "")), c0["part"], groups] if x))
    elif item.get("study"):
        lines.append(f"Study #{item['study'].strip()}")
    lines.append("RE-RELEASED" if status == "Reposted" else "NEW")
    sex = next((l for l in cl if re.fullmatch(r"Male/Female|Females?|Males?", l, re.I)), "").lower()
    if sex:
        lines.append("M/F (all)" if "/" in sex else ("F" if sex.startswith("f") else "M"))
    age = re.search(r"Age\s+(\d+)\s*-\s*(\d+)", card)
    if age:
        lines.append(f"Age: {age.group(1)}-{age.group(2)}")
    nz = lambda x: re.sub(r"\.0\b", "", x)
    bmi = re.search(r"\bBMI\s*:?\s*([\d.]+)\s*(?:-|\u2013|to)\s*([\d.]+)", detail, re.I)
    bmi_plus = re.search(r"\bBMI\s*:?\s*([^\n]*\d[^\n]*)", detail, re.I)
    if bmi:
        lines.append(f"Bmi: {nz(bmi.group(1))}-{nz(bmi.group(2))}")
    elif bmi_plus:
        lines.append(f"Bmi: {nz(bmi_plus.group(1).strip())}")
    lines += calendar_lines(cals, [f"G{c['group']}" for c in cals])
    if not cals:
        sched = next((l for l in cl if re.match(r"Participation in this study includes", l, re.I)), "")
        if sched:
            lines.append(re.sub(r"^Participation in this study includes\s*", "", sched, flags=re.I))
    return "\n".join(lines)


def parexel_post(src, status, item):
    """Parexel style, e.g.
    November-10-2026 / Reposted: / (877)617-8839 / Parexel Baltimore / 287970 / Healthy Volunteers (Prediabetic) /
    $24,400 / $3485.71 per day IP and OPV / Admissions: ... Phone Calls: ... Follow ups: ... /
    Ages 18-60 / BMI 35-50 / Population: ... This study involves up to: ... / link"""
    card, detail = item.get("text") or "", item.get("detail") or ""
    cl, dl = lines_of(card), lines_of(detail)
    study = item.get("study", "")
    lines = [datetime.now().strftime("%B-%d-%Y"), f"{status}:"] + list(src["header"])
    if study:
        lines.append(study)
    title = next((l for l in cl if study not in l and not money_values(l)), "")
    if title:
        lines.append(title)
    info_at = next((i for i, l in enumerate(cl) if re.match(r"Study Information", l, re.I)), len(cl))
    info = [l for l in cl[info_at + 1:] if not re.fullmatch(r"Ages?\s*\d+\+?(?:\s*-\s*\d+)?", l, re.I)
            and not re.search(r"\brefer", l, re.I)]  # never post referral info
    info_text = " ".join(info)
    pays = money_values(card) or money_values(detail)
    if pays:
        pay = max(pays)
        lines.append(fmt_money(pay))
        # per day = pay / days spent in-house ("1 in-house stay of 4 days/3 nights")
        stay_rx = _N + r"\s+in-?house\s+stays?\s+of\s+(?:up[- ]?to\s+)?(\d+)\s*-?\s*days?"
        days = sum(_num(n) * int(d) for n, d in re.findall(stay_rx, info_text, re.I) or re.findall(stay_rx, detail, re.I))
        if not days:
            f = extract(card)
            days = f.get("nights", 0) + f.get("opv", 0)
        if days:
            lines.append(f"${pay / days:.2f} per day IP and OPV")
    sched = [l for l in dl if re.match(r"(Admissions?|Phone Calls?|Follow[- ]?ups?|Outpatients?(?: visits?)?|OPVs?|"
                                        r"Check[- ]?ins?|Dates?)\s*:", l, re.I)]
    if sched:
        lines.append(" ".join(sched))
    ages = re.search(r"\bAges\s*\n+\s*(\d[^\n]*)", detail) or re.search(r"\bAges?\s*(\d+\s*(?:-\s*\d+|\+))", card)
    if ages:
        lines.append(f"Ages {re.sub(chr(32), '', ages.group(1).strip())}")
    bmi = re.search(r"\bBMI\s*:?\s*(\d[^\n]*)", detail)
    if bmi:
        lines.append(f"BMI {bmi.group(1).strip()}")
    if info_text:
        lines.append(info_text)
    if item.get("url"):
        lines.append(item["url"])
    return "\n".join(lines).strip()


FT_KEY = (r"(?P<key>Check[- ]?in|Check[- ]?out|Follow[- ]?up\s+visits?|Out[- ]?patient\s+visits?|"
          r"Phone\s+Call\s+Dates|Phone\s+calls?|Calls?)\s*:?")
FT_GROUP = r"(?P<group>\b(?:Cohort|Group)\s+[\w-]+(?:\s+Dates)?\s*:?|\bOR\b|(?<![\w/])[A-Z][a-z]?\d*[a-z]?:(?=\s*Check))"
FT_DATE = (r"(?P<num>\d{1,2}/\d{1,2}/\d{2,4})|(?P<mon>" + MONTHS + r")(?:\s+(?P<yr>\d{4}))?\s*:?\s*(?P<d1>\d{1,2})?"
           r"|(?P<day>\b\d{1,2}\b)")


def read_fortrea_dates(detail, start=None):
    """Fortrea 'Additional Details' -> [(cohort, stays, opvs, calls)].
    Handles 'Check-in: Nov 24 & Dec 6', 'Group A5: Check-in: Dec 28', 'Cohort 2a ... OR Cohort 3 ...',
    'Period 2 Check-in: Jan 5', 'Check-in: Sat. 11/28/26', 'Follow-up Visits: Dec: 21, 28 Jan 4, 11'."""
    from datetime import datetime as dt, timedelta
    a = detail.find("Additional Details")
    if a < 0:
        return []
    b = detail.find("BMI Calculator", a)
    text = detail[a + len("Additional Details"): b if b > 0 else None]
    text = "\n".join(l for l in text.splitlines() if not re.search(r"[{};]", l))     # drop CSS
    text = re.sub(r"\([^)]*\)", " ", text)                                             # (Please call us ...)
    text = re.sub(r"\bPeriod\s+\d+\b|\b(?:Mon|Tues?|Wed|Thu|Thurs|Fri|Sat|Sun)\.", " ", text, flags=re.I)
    text = re.sub(r"(\d)(?=" + MONTHS + ")", r"\1 ", text)                             # '31June' -> '31 June'
    year = start.year if start else datetime.now().year
    last, month = start, None
    cohorts, cur, key = [], None, None

    def new_cohort(name=""):
        nonlocal last, year, month
        last, month = start, None                       # each cohort's dates start from the study start
        year = start.year if start else datetime.now().year
        c = dict(name=name.strip().rstrip(":").replace(" Dates", "").strip(), cin=[], cout=[], opv=[], calls=[])
        cohorts.append(c)
        return c

    def add(d):
        nonlocal last
        bucket = {"check-in": "cin", "check-out": "cout", "follow": "opv", "out": "opv"}.get(key, "calls")
        prev = cur[bucket][-1] if cur[bucket] else last    # calls are listed separately, so compare
        if prev and d < prev - timedelta(days=60):         # within the same kind of visit
            d = d.replace(year=d.year + 1)
        last = d
        cur[bucket].append(d)

    for m in re.finditer(FT_KEY + "|" + FT_GROUP + "|" + FT_DATE, text, re.I):
        if m.group("key"):
            k = re.sub(r"\s+", " ", m.group("key").lower())
            key = ("check-in" if re.match(r"check[- ]?in", k) else "check-out" if k.startswith("check")
                   else "follow" if k.startswith("follow") else "out" if k.startswith("out") else "calls")
            if cur is None:
                cur = new_cohort()
            continue
        if m.group("group"):
            g = m.group("group")
            if g.upper() == "OR":
                cur, key = None, None
            elif cur is None or cur["cin"] or cur["opv"]:
                cur, key = new_cohort(g), None
            else:
                cur["name"] = g.strip().rstrip(":").replace(" Dates", "").strip()
            continue
        if not key or cur is None:
            continue
        try:
            if m.group("num"):
                mm, dd, yy = m.group("num").split("/")
                yy = int(yy) + (2000 if len(yy) == 2 else 0)
                d = dt(yy, int(mm), int(dd))
                month, year = d.month, d.year
                last = None
                add(d)
            elif m.group("mon"):
                month = _cal_day(m.group("mon")[:3] + " 1 2000").month
                if m.group("yr"):
                    year, last = int(m.group("yr")), None
                if m.group("d1"):
                    add(dt(year, month, int(m.group("d1"))))
            elif m.group("day") and month:
                add(dt(year, month, int(m.group("day"))))
        except ValueError:
            pass
    out = []
    for c in cohorts:
        stays = list(zip(c["cin"], c["cout"]))
        if stays or c["opv"]:
            out.append((c["name"], stays, sorted(c["opv"]), sorted(c["calls"])))
    return out


def fortrea_post(src, status, item):
    """Fortrea style, e.g.
    $10,784 Fortrea Madison / (866)429-3700 / Study #782640 G1 / NEW / MALES ONLY / Age: 18-55 /
    Bmi: 18-30 / In 🏡: / 12/3-23 with opv: 12/29"""
    from datetime import datetime as dt, timedelta
    card, detail = item.get("text") or "", item.get("detail") or ""
    cl = lines_of(card)
    loc = re.search(r"Location\s*\n+\s*([^\n,]+)", detail) or re.search(r"\n([A-Z][A-Za-z .]+),\s*[A-Z][a-z]+", card)
    city = loc.group(1).strip() if loc else ""
    pays = money_values(card) or money_values(detail)
    lines = [f"{fmt_money(max(pays)) if pays else ''} Fortrea {city}".strip(), "(866)429-3700"]
    sm = re.match(r"(\d{6,7})\s*(.*)", item.get("study", "").strip())
    if sm:
        grp = sm.group(2).strip()
        lines.append(f"Study #{sm.group(1)}" + (f" G{grp}" if re.fullmatch(r"\d+[a-z]?", grp) else f" {grp}" if grp else ""))
    lines.append("RE-RELEASED" if status == "Reposted" else "NEW")
    gender = re.search(r"Gender\s*\n+\s*([^\n]+)", detail)
    if gender:
        g = gender.group(1).lower()
        lines.append("M/F" if ("male" in g.replace("female", "") and "female" in g) else
                     "FEMALES ONLY" if "female" in g else "MALES ONLY" if "male" in g else gender.group(1).strip())
    age = re.search(r"\bAge\s*\n+\s*(\d+\s*-\s*\d+|\d+\+)", detail) or re.search(r"\n(\d{2}\s*-\s*\d{2})\n", card)
    if age:
        lines.append(f"Age: {re.sub(r'\s+', '', age.group(1))}")
    bmi = re.search(r"BMI of:\s*([\d.]+\s*(?:-\s*[\d.]+|\+))", detail)
    if bmi:
        lines.append(f"Bmi: {re.sub(r'\s+', '', bmi.group(1))}")
    rng = re.search(r"(" + MONTHS + r"\s+\d{1,2}\s+\d{4})\s*-\s*(" + MONTHS + r"\s+\d{1,2}\s+\d{4})", detail + "\n" + card)
    start = _cal_day(rng.group(1)) if rng else None
    end = _cal_day(rng.group(2)) if rng else None
    groups = read_fortrea_dates(detail, start)
    sched = next((l for l in cl if re.search(r"\bstays?\b|nights?|visits?", l, re.I)), "")
    if not groups and start:
        # no date list on the page: '1 stay of 20 nights & 1 follow-up visit' + 'Dec 03 2026 - Dec 29 2026'
        one = re.match(r"1 stay of (\d+) nights?(?:\s*(?:&|and|,|plus)\s*1 (?:follow[- ]?up|out-?patient) visit)?", sched, re.I)
        if one:
            cout = start + timedelta(days=int(one.group(1)))
            opv = [end] if end and "visit" in sched.lower() and end > cout else []
            groups = [("", [(start, cout)], opv, [])]
    for name, stays, opvs, calls in groups:
        lines.append("In \U0001f3e1:" if len(groups) == 1 or not name else f"In \U0001f3e1 ({name}):")
        spans = ", ".join(_span(x, y) for x, y in stays)
        if opvs:
            lines += [f"{spans} with opv{'s' if len(opvs) > 1 else ''}:".strip(), _opv_list(opvs)]
        elif spans:
            lines.append(spans)
        if calls:
            lines.append(f"Calls: {_opv_list(calls)}")
    if not groups:
        if sched:
            lines.append(sched)
        if rng:
            lines.append(f"Dates: {rng.group(1)} - {rng.group(2)}")
    return "\n".join(lines)


def abbvie_extra(text):
    """Information sheet PDF -> check-out, screening pay, outpatient visits, phone calls.
    The PDF text comes out with stray spaces ('No vember 2 026', '$ 26,15 0'), so it is read
    with all spaces removed and then tidied."""
    t = re.sub(r"\s+", "", text or "")
    if not t:
        return []

    def tidy(seg):
        seg = re.sub(r"(?i)(\d)(or|and|&)(?=\d)", r"\1 \2 ", seg)
        seg = re.sub(r"(\d)([A-Za-z])", r"\1 \2", seg)
        seg = re.sub(r"([A-Za-z])(\d)", r"\1 \2", seg)
        seg = re.sub(r"(?i)\b(or|and)(?=[A-Z0-9])", r"\1 ", seg)
        return re.sub(r",(?=\S)", ", ", seg).strip(" ,.;:")

    stop = r"(?=upon|atscheduled|atthe|\(|CHECK|FOLLOW|OUT-?PATIENT|PHONE|RETURN|Please|$)"
    lines = []
    co = re.search(r"CHECKOUT:?(.+?)" + stop, t)
    if co:
        lines.append(f"Check-out: {tidy(co.group(1))}")
    scr = re.search(r"Screening:\$([\d,]+(?:\.\d+)?)", t, re.I)
    if scr:
        amt = float(scr.group(1).replace(",", ""))
        lines.append(f"Screening: ${amt:,.0f}" if amt == int(amt) else f"Screening: ${amt:,.2f}")
    for label, rx in (("Outpatient visits", r"(?:FOLLOW-?UP|OUT-?PATIENT|RETURN)VISITS?(?:\(S\))?:?"),
                      ("Phone calls", r"PHONECALLS?(?:\(S\))?:?")):
        m = re.search(rx + r"(.+?)" + stop, t)        # headings are in capitals in the PDF
        if m and re.search(r"\d", m.group(1)):
            lines.append(f"{label}: {tidy(m.group(1))}")
    return lines


TM_DATE = r"\d{1,2}/\d{1,2}(?:/\d{2,4})?"
TM_WEEKDAY = r"\b(?:Mon|Tues?|Wed|Thu|Thurs|Fri|Sat|Sun)[a-z]*\.*\s*"


def _tm_dates(text):
    """'12/24*, 12/31, 1/14/27' -> '12/24, 31, 1/14' (month shown when it changes)."""
    out, last = [], None
    for d in re.findall(TM_DATE, text):
        m, day = d.split("/")[:2]
        out.append(f"{int(m)}/{int(day)}" if m != last else str(int(day)))
        last = m
    return ", ".join(out)


def _tm_stays(text):
    """'Wed. 11/18 - Fri. 11/20' -> '11/18-20'; '11/30 - 12/9' -> '11/30-12/9'."""
    text = re.sub(TM_WEEKDAY, " ", text)
    out = []
    for a, b in re.findall(f"({TM_DATE})\\s*[–—-]+\\s*({TM_DATE})", text):
        (am, ad), (bm, bd) = a.split("/")[:2], b.split("/")[:2]
        out.append(f"{int(am)}/{int(ad)}-{int(bd)}" if am == bm else f"{int(am)}/{int(ad)}-{int(bm)}/{int(bd)}")
    return ", ".join(out)


def read_trialmed(detail):
    """'What's the commitment?' -> counts and {group: {stay, opv, calls}} (group '' = not named)."""
    a = detail.find("commitment")
    if a < 0:
        return {}, {}
    b = detail.find("How do I participate", a)
    text = detail[a: b if b > 0 else None]
    text = re.sub(r"\([^)]*\)", " ", text)                       # (holiday, will likely change)
    counts, groups, order = {}, {}, []
    parts = re.split(r"(?m)^\s*(\d+)\s+(Overnight|Outpatient|Calls?)\b.*$", text)
    for i in range(1, len(parts) - 2, 3):
        n, kind, body = parts[i], parts[i + 1].lower(), parts[i + 2]
        kind = "stay" if kind.startswith("over") else "opv" if kind.startswith("out") else "calls"
        counts[kind] = int(n)
        bits = re.split(r"Group:?\s*([\w!]+)\s*:", body)
        chunks = [("", bits[0])] + [(bits[j], bits[j + 1]) for j in range(1, len(bits) - 1, 2)]
        unnamed = [c for c in re.split(r"\n\s*•", chunks[0][1]) if re.search(TM_DATE, c)]
        if len(chunks) == 1 and len(unnamed) > 1 and len(unnamed) == len(order):
            chunks = list(zip(order, unnamed))                   # bullets in the same order as the groups
        for g, chunk in chunks:
            g = g.replace("!", "1")
            if g and g not in order:
                order.append(g)
            # keep 'Part 1:' / 'Part 2:' labels with their dates
            pieces = re.split(r"(Part\s*\d+)\s*:", chunk)
            if kind == "stay":
                val = _tm_stays(chunk)
            elif len(pieces) > 1:
                val = "; ".join(f"{pieces[k]}: {_tm_dates(pieces[k + 1])}" for k in range(1, len(pieces) - 1, 2)
                                if re.search(TM_DATE, pieces[k + 1]))
            else:
                val = _tm_dates(chunk)
            if val:
                groups.setdefault(g, {})[kind] = val
    return counts, {g: groups[g] for g in ([""] if "" in groups else []) + order if g in groups}


def trialmed_post(src, status, item):
    """Trialmed healthy-volunteer style: study #, location, pay, age/sex/BMI, then for each group
    'In 🏡 (Group F): 11/18-20, 12/3-4' with its outpatient and phone-call dates."""
    card, detail = item.get("text") or "", item.get("detail") or ""
    cl = lines_of(card)
    lines = [h.replace("{STATUS}", status.upper()).replace("{status}", status) for h in src["header"]]
    st = (item.get("study") or "").split()                      # hidden card field: '3147540 AUS'
    loc = re.search(r"Location\s*\n+\s*([^\n]+)", detail)
    study = st[0] if st else ""
    place = loc.group(1).strip() if loc else (cl[0] if cl else "")
    lines.append(f"Study #{study}" + (f" - {place}" if place else ""))
    comp = re.search(r"Compensation\s*\n+\s*([^\n]+)", detail)
    pays = money_values(card) or money_values(detail)
    if comp and money_values(comp.group(1)):
        lines.append(comp.group(1).strip())
    elif pays:
        lines.append(f"Up to {fmt_money(max(pays))}")
    who = [l for l in cl if re.match(r"(Age|Sex|BMI):", l)]
    if who:
        lines.append(" | ".join(who))
    counts, groups = read_trialmed(detail)
    if counts:
        bits = [f"{counts['stay']} nights in-house" if counts.get("stay") else "",
                f"{counts['opv']} outpatient" if counts.get("opv") else "",
                f"{counts['calls']} call{'s' if counts['calls'] != 1 else ''}" if counts.get("calls") else ""]
        lines.append(", ".join(b for b in bits if b))
    shared = groups.pop("", {})
    named = list(groups.items()) or ([("", shared)] if shared else [])
    for g, v in named:
        v = dict(shared, **v) if g else v
        lines.append(f"In \U0001f3e1 (Group {g}):" if g else "In \U0001f3e1:")
        if v.get("stay"):
            lines.append(v["stay"])
        if v.get("opv"):
            lines.append(f"Outpatient: {v['opv']}")
        if v.get("calls"):
            lines.append(f"Phone call{'s' if ',' in v['calls'] or ';' in v['calls'] else ''}: {v['calls']}")
    if not named:
        start = re.search(r"Check-in date\s*\n+\s*([^\n]+)", detail)
        if start:
            lines.append(f"Check-in: {start.group(1).strip()}")
    if item.get("url"):
        lines.append(item["url"])
    return "\n".join(lines)


def frontage_body(detail):
    """The study description on a Frontage page: after the standard CDC notice, before the
    'Application for Clinical Research Study' form."""
    if not detail:
        return ""
    a = re.search(r"as recommended by the CDC\.?", detail)
    if a:
        start = a.end()
    else:
        s = re.search(r"IN THIS SECTION\s*\n[^\n]*\n", detail)
        start = s.end() if s else 0
    b = detail.find("Application for Clinical Research Study", start)
    text = "\n".join(l.rstrip() for l in detail[start: b if b > 0 else None].splitlines()).strip()
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text[:3500]


def build_post(src, status, item):
    """Build the post, then drop any line about referrals (never posted)."""
    text = _build_post(src, status, item)
    text = "\n".join(l for l in text.split("\n") if not re.search(r"\brefer", l, re.I)).strip()
    # every post ends with a link to the study's own page
    url = (item.get("url") or "").strip()
    if url.startswith("http") and url not in text:
        text += "\n" + url
    return text


def _build_post(src, status, item):
    if src["key"] == "drvince":
        # the site lists counts only (no dates): add the outpatient-visit and phone-call lines
        lines = _build_post(dict(src, key="drvince_base"), status, item).split("\n")
        extra = [l for l in lines_of(item.get("text"))
                 if re.match(r"\d+\s+(?:Outpatient visits?|Phone calls?|Follow[- ]?up visits?)\b", l, re.I)]
        at = next((i + 1 for i, l in enumerate(lines) if re.match(r"\d+\s+Stays?\b", l, re.I)),
                  next((i for i, l in enumerate(lines) if l.startswith("Dates:")), len(lines)))
        return "\n".join(lines[:at] + extra + lines[at:])
    if src["style"] == "trialmed":
        return trialmed_post(src, status, item)
    if src["key"] == "abbvie":
        base = _build_post(dict(src, key="abbvie_base"), status, dict(item, detail=""))
        lines = base.split("\n")
        extra = abbvie_extra(item.get("detail"))
        at = len(lines) - 1 if lines and lines[-1].startswith("http") else len(lines)
        return "\n".join(lines[:at] + extra + lines[at:])
    if src["style"] == "fortrea":
        return fortrea_post(src, status, item)
    if src["style"] == "parexel":
        return parexel_post(src, status, item)
    if src["style"] == "icon_ks":
        return icon_ks_post(src, status, item)
    if src["style"] == "celerion":
        return celerion_post(item)
    if src["style"] == "icon":
        return icon_post(src, status, item)
    card, detail = item.get("text") or "", item.get("detail") or ""
    brand = "Brand New" if status == "New" else "Reposted"
    head = [h.format(status=status, STATUS=status.upper(), brand=brand) for h in src["header"]]
    lines = [] if src.get("no_date") else [datetime.now().strftime("%B-%d-%Y")]
    if not src.get("status_in_header") and src["style"] != "screenings":
        lines.append(f"{status}:")
    lines += head
    url = item.get("url", "")
    style = src["style"]

    if style == "screenings":
        body = [l for l in lines_of(card) if l != item.get("title")]
        return "\n".join([item.get("title", "")] + head + body + ["", url]).strip()

    if style == "passthrough":
        # Frontage: header line, blank line, the study's own description, phone, studies page
        body = frontage_body(detail) or "\n".join(lines_of(detail or card))[:3500]
        top = [l for l in lines if l not in head[1:]]
        return "\n".join(top + ["", body] + head[1:] + [src.get("post_url", url)]).strip()

    # details: list card first; the study's own page fills gaps
    f = extract(card)
    if detail:
        d = extract(detail)
        for k, v in d.items():
            if src.get("detail_only") and k not in src["detail_only"]:
                continue
            if k == "dates":
                f[k] = v  # the study's own page has the fuller dates
            else:
                f.setdefault(k, v)
    pays = money_values(card) or money_values(detail)
    pay = max(pays) if pays else None
    up_to = bool(pay and re.search(r"up\s*(?:to|-to)[:\s]*\$", card + detail, re.I))
    study = item.get("study", "").strip()
    skip = src.get("skip_re", r"^$")
    title = item.get("title") or next((l for l in lines_of(card) if l != study and not money_values(l)
                                       and len(l) > 3 and not re.search(skip, l, re.I)), "")

    if style == "icon":
        if study:
            lines.append(f"{study} Enrolling")
    elif study:
        lines.append(study)
    if title:
        lines.append(title)
    if pay:
        lines.append(("Up to " if up_to else "") + fmt_money(pay))
        days = f.get("nights", 0) + f.get("opv", 0)
        if days:
            lines.append(f"${round(pay / days, 2)} per day" + (" IP and OPV" if style == "icon" else ""))
    if "sex" in f:
        lines.append(f["sex"])
    if "ages" in f:
        lines.append(f"Age {f['ages']}" if style == "icon" else f"Ages {f['ages']}")
    if "bmi" in f:
        lines.append(f"bmi {f['bmi']}" if style == "icon" else f"BMI {f['bmi']}")
    if "stays" in f:
        lines.append(f["stays"])
    if style != "icon":
        lines.append(f"Dates: {f['dates']}" if "dates" in f else "Dates: not listed")
    if url:
        lines.append(url)
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Reading the sites
# --------------------------------------------------------------------------
CARDS_JS = r"""(cfg) => {
  const q = (c, s) => s ? ((c.matches(s) ? c : c.querySelector(s))?.innerText || '').trim() : '';
  return [...document.querySelectorAll(cfg.card)].map(c => {
    let text;
    if (c.tagName === 'TR') {
      const ths = [...(c.closest('table')?.querySelectorAll('thead th') || [])].map(t => t.innerText.trim().split('\n')[0].trim());
      text = [...c.children].map((td, i) => (ths[i] ? ths[i] + ': ' : '') + td.innerText.trim()).join('\n');
    } else text = c.innerText.trim();
    const a = cfg.link ? (c.matches(cfg.link) ? c : c.querySelector(cfg.link))
                       : (c.tagName === 'A' ? c : c.querySelector('a[href]'));
    return {text, href: a ? a.href : '', title: q(c, cfg.title), study: q(c, cfg.study)};
  });
}"""

LINKS_JS = r"""(re) => {
  const rx = new RegExp(re, 'i'), out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    if (!rx.test(a.href) || seen.has(a.href)) continue;
    seen.add(a.href);
    let p = a;  // widen to the block describing this study
    while (p.parentElement && p.parentElement !== document.body &&
           (p.parentElement.innerText || '').length < 1500 &&
           [...p.parentElement.querySelectorAll('a[href]')].filter(x => rx.test(x.href) && x.href !== a.href).length === 0)
      p = p.parentElement;
    out.push({text: (p.innerText || '').trim(), href: a.href, title: '', study: ''});
  }
  return out;
}"""


async def load(page, url, wait=2500):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    await page.wait_for_timeout(wait)


def fresh(url):
    """Add a throwaway value to the address so a site's cache (Trialmed's keeps
    pages for up to 10 minutes) always hands over the current list."""
    if not CONFIG.get("skip_cache", True):
        return url
    base, hash_, frag = url.partition("#")
    return f"{base}{'&' if '?' in base else '?'}_={int(time.time())}{hash_}{frag}"


def make_key(src, c):
    if src.get("key_re"):
        m = re.search(src["key_re"], c["text"])
        if m:
            return m.group(1).upper()
    if src.get("key_from") == "title" and c.get("title"):
        return re.sub(r"\s+", " ", c["title"]).strip().upper()
    if c.get("href") and not src.get("key_re"):
        return c["href"].split("#")[0].rstrip("/").lower()
    return hashlib.sha1((src["key"] + (lines_of(c["text"]) or [""])[0]).encode()).hexdigest()[:16]


async def read_source(ctx, src, dump=False):
    """Return {key: item} for every study this source currently lists."""
    page = await ctx.new_page()
    found, raw = {}, set()
    try:
        for n, list_url in enumerate(src["list_urls"]):
            await load(page, fresh(list_url) if src.get("fresh", True) else list_url, src.get("wait", 2500))
            if dump:
                d = HERE / "dumps"
                d.mkdir(exist_ok=True)
                (d / f"{src['key']}_{n}.txt").write_text(await page.inner_text("body"), encoding="utf-8")
            for attempt in range(4):  # a slow page can show nothing at first: look again
                if src["mode"] == "cards":
                    cards = await page.evaluate(CARDS_JS, {k: src.get(k) for k in ("card", "link", "title", "study")})
                elif src["mode"] == "links":
                    cards = await page.evaluate(LINKS_JS, src["link_re"])
                else:
                    break
                if cards or n > 0:
                    break
                await page.wait_for_timeout(2000)
            if src["mode"] not in ("cards", "links"):  # text
                body = await page.inner_text("body")
                lo = body.lower()
                i = lo.find(src["start"].lower())
                j = lo.find(src["end"].lower(), i + 1)
                body = body[i + len(src["start"]) if i >= 0 else 0: j if j > 0 else None]
                cards = [dict(text=b.strip(), href=list_url, title="", study="", index=n_)
                         for n_, b in enumerate(re.split(src["split"], body)) if b.strip()]
            seen_raw = len(raw)
            raw.update(c.get("href") or c.get("text") for c in cards)
            for c in cards:
                if src.get("exclude") and re.search(src["exclude"], c["text"], re.I):
                    continue
                if src.get("include") and not re.search(src["include"], c["text"], re.I):
                    continue
                if src.get("need_money", True) and not money_values(c["text"]):
                    continue
                k = make_key(src, c)
                if src.get("mode") == "text" and not re.search(src.get("key_re", "."), c["text"]):
                    continue
                if src.get("style") == "parexel":
                    c["study"] = k
                if src.get("study_re") and not c.get("study"):
                    m = re.search(src["study_re"], c["text"])
                    c["study"] = m.group(1).strip() if m else ""
                found.setdefault(k, dict(text=c["text"], url=c["href"] or list_url,
                                         title=c["title"], study=c["study"], index=c.get("index", 0)))
            # past the last page (count every card, even ones filtered out above)
            if n > 0 and len(raw) == seen_raw:
                break
    finally:
        await page.close()
    return found


def pdf_text(url):
    try:
        from io import BytesIO
        from pypdf import PdfReader
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(r.content)).pages)
    except Exception as e:
        print(f"  couldn't read calendar {url}: {e}")
        return ""


async def fill_details(ctx, src, item):
    if not src.get("detail") or not item.get("url") or item.get("detail") is not None:
        return item
    if src["detail"] == "pdf":  # the study link is a PDF information sheet
        item["detail"] = pdf_text(item["url"])
        return item
    page = await ctx.new_page()
    try:
        if src["detail"] == "click":  # study pages only open by clicking "Learn More"
            await load(page, item["url"], src.get("wait", 2500))
            await page.get_by_text("Learn More", exact=True).nth(item["index"]).click()
            await page.wait_for_timeout(src.get("wait", 2500))
            item["detail"] = await page.inner_text("body")
            if "detail" in page.url.lower():
                item["url"] = page.url
            return item
        await load(page, item["url"])
        main = await page.query_selector("main, article, [role=main]")
        item["detail"] = await (main.inner_text() if main else page.inner_text("body"))
        if src.get("calendars"):
            links = await page.eval_on_selector_all(
                "a[href$='.pdf']", "els => els.filter(a => /calendar|dates|cohort|group|schedule/i.test(a.innerText)).map(a => [a.innerText.trim(), a.href])")
            item["calendars"] = [(label, pdf_text(href)) for label, href in links]
    except Exception as e:
        print(f"  couldn't open {item['url']}: {e}")
        item["detail"] = ""
    finally:
        await page.close()
    return item


# --------------------------------------------------------------------------
# Deciding what's new and posting
# --------------------------------------------------------------------------
def decide(src, current, missing_limit=3):
    rows = {r[0]: r for r in DB.execute(
        "SELECT key, present, missing FROM items WHERE source=?", (src["key"],))}
    todo, closed = [], []
    for k in current:
        if k not in rows:
            todo.append(("New", k))
        elif not rows[k][1]:
            todo.append(("Reposted", k))
    for k, (_, present, missing) in rows.items():
        if present and k not in current and missing + 1 >= missing_limit:
            closed.append(k)
    return todo, closed


def record(src, current, closed):
    now = time.time()
    for k, item in current.items():
        DB.execute("""INSERT INTO items(source,key,present,missing,sig,first_seen,last_posted,title)
                      VALUES(?,?,1,0,'',?,NULL,?)
                      ON CONFLICT(source,key) DO UPDATE SET present=1, missing=0""",
                   (src["key"], k, now, item.get("title") or (lines_of(item["text"]) or [""])[0]))
        if item.get("posted"):
            DB.execute("UPDATE items SET last_posted=? WHERE source=? AND key=?", (now, src["key"], k))
    DB.execute(f"""UPDATE items SET missing=missing+1 WHERE source=? AND present=1
                   AND key NOT IN ({','.join('?' * len(current))})""", (src["key"], *current))
    for k in closed:
        DB.execute("UPDATE items SET present=0 WHERE source=? AND key=?", (src["key"], k))
    DB.commit()


def send(text):
    token, chat = CONFIG["bot_token"], CONFIG["chat_id"]
    for _ in range(5):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, json=dict(
            chat_id=chat, text=text[:4096], disable_web_page_preview=True))
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            print("  Telegram error:", r.text)
            mig = r.json().get("parameters", {}).get("migrate_to_chat_id")
            if mig:  # group was upgraded to a supergroup: follow it
                CONFIG["chat_id"] = chat = mig
                (HERE / "config.json").write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
                continue
        return r.ok
    return False


async def run_once(ctx, only=None, check=False, dump=False, post_existing=False):
    # several sites are read at the same time (CONFIG "parallel"; 1 = one by one)
    sem = asyncio.Semaphore(max(1, int(CONFIG.get("parallel", 4 if SERVER else 1))))

    async def one(src):
        async with sem:
            await check_source(ctx, src, check, dump, post_existing)

    await asyncio.gather(*(one(s) for s in SOURCES if not only or s["key"] in only))


async def check_source(ctx, src, check, dump, post_existing):
    try:
        current = await read_source(ctx, src, dump=dump)
    except Exception as e:
        print(f"[{src['key']}] couldn't read: {e}")
        if not (check or dump):
            health(src, "error", str(e).split("\n")[0][:150])
        return
    print(f"[{src['key']}] {len(current)} studies listed")
    if not (check or dump):
        health(src, "ok" if current else "empty")
    if check or dump:
        for k, item in list(current.items())[:3] if check else []:
            await fill_details(ctx, src, item)
            print("-" * 40)
            print(build_post(src, "New", item))
        return
    if not current:  # site down or page changed: don't treat as "all closed"
        return
    first_run = not DB.execute("SELECT 1 FROM items WHERE source=? LIMIT 1", (src["key"],)).fetchone()
    todo, closed = decide(src, current)
    if first_run and not post_existing:
        print(f"[{src['key']}] first run: recorded {len(current)} existing studies without posting")
        todo = []
    for status, k in todo:
        item = current[k]
        try:
            await fill_details(ctx, src, item)
            if await asyncio.to_thread(send, build_post(src, status, item)):
                print(f"[{src['key']}] posted {status}: {item.get('title') or k}")
                item["posted"] = True
            await asyncio.sleep(CONFIG.get("seconds_between_posts", 3))
        except Exception as e:
            print(f"[{src['key']}] couldn't post {k}: {e}")
    if src.get("closed_notice") and CONFIG.get("closed_notices", True) and not first_run:
        for k in closed:
            row = DB.execute("SELECT title FROM items WHERE source=? AND key=?", (src["key"], k)).fetchone()
            send(src["closed_notice"].format(title=(row and row[0]) or k))
    record(src, current, closed)


def find_chat():
    r = requests.get(f"https://api.telegram.org/bot{CONFIG['bot_token']}/getUpdates", timeout=30).json()
    if not r.get("ok"):
        print("Telegram said:", r.get("description"))
    chats = {}
    for u in r.get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = msg.get("chat")
        if c:
            chats[c["id"]] = c.get("title") or c.get("username")
    if not chats:
        print("No chats yet. Add the bot to your group, send any message in the group, then run this again.")
    for cid, name in chats.items():
        print(f"{cid}  {name}")


# --------------------------------------------------------------------------
# Alerts: a private message to the owner when something stops working.
# To turn them on, open a private chat with the bot in Telegram and send /alerts
# --------------------------------------------------------------------------
NAMES = dict(icon_sa="ICON San Antonio", icon_lenexa="ICON Lenexa", icon_slc="ICON Salt Lake City",
             icon_sa_screenings="ICON San Antonio screenings", parexel_bal="Parexel Baltimore",
             parexel_la="Parexel Los Angeles", fortrea="Fortrea", abbvie="AbbVie", celerion="Celerion",
             trialmed="Trialmed", alta_kc="Altasciences Kansas City", alta_la="Altasciences Cypress",
             medpace="Medpace", drvince="Dr. Vince", frontage="Frontage")


def meta_get(k):
    row = DB.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else None


def meta_set(k, v):
    if v is None:
        DB.execute("DELETE FROM meta WHERE k=?", (k,))
    else:
        DB.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (k, str(v)))
    DB.commit()


def tg(method, **data):
    try:
        return requests.post(f"https://api.telegram.org/bot{CONFIG['bot_token']}/{method}",
                             json=data, timeout=30).json()
    except Exception as e:
        print("Telegram error:", e)
        return {}


def check_alert_signup():
    """Pick up a private /alerts message to the bot and remember who sent it."""
    off = int(meta_get("tg_offset") or 0)
    r = tg("getUpdates", offset=off, timeout=0, allowed_updates=["message"])
    for u in r.get("result", []):
        off = max(off, u["update_id"] + 1)
        m = u.get("message") or {}
        chat = m.get("chat") or {}
        if chat.get("type") == "private" and (m.get("text") or "").strip().lower().startswith(("/alerts", "/start")):
            meta_set("alerts_chat", chat["id"])
            tg("sendMessage", chat_id=chat["id"], text=(
                "\u2705 A-Team bot alerts will come to this chat. You'll get a message if a study "
                "site keeps failing, if a site suddenly shows no studies, or if the always-on "
                "checker stops, and another one when it's working again."))
    if r.get("result"):
        meta_set("tg_offset", off)


def alert(text):
    chat = CONFIG.get("alerts_chat_id") or meta_get("alerts_chat")
    print("ALERT:", text)
    if not chat:
        return False
    return bool(tg("sendMessage", chat_id=chat, text=text, disable_web_page_preview=True).get("ok"))


def health(src, state, detail=""):
    """Track each site; alert after 15 min of errors or 30 min of no studies."""
    if not SERVER:
        return
    k, now, name = "health:" + src["key"], time.time(), NAMES.get(src["key"], src["key"])
    h = json.loads(meta_get(k) or "{}")
    if state == "ok":
        if h.get("alerted"):
            alert(f"\u2705 {name} is working again.")
        if h:
            meta_set(k, None)
        return
    if h.get("state") != state:
        h = dict(state=state, since=now, alerted=h.get("alerted", False))
    limit = 15 * 60 if state == "error" else 30 * 60
    if not h["alerted"] and now - h["since"] >= limit:
        mins = int((now - h["since"]) // 60)
        if state == "error":
            msg = (f"\u26a0\ufe0f {name}: the bot hasn't been able to read the site for {mins} minutes, "
                   f"so new studies there won't post.\nError: {detail or 'unknown'}")
        else:
            msg = (f"\u26a0\ufe0f {name}: the site has shown 0 studies for {mins} minutes. Either it really "
                   f"has none right now, or the site is blocking the bot.")
        h["alerted"] = alert(msg)
    meta_set(k, json.dumps(h))


def runner_health(alive):
    """Called by the 10-minute timer: is the always-on checker running?"""
    start, end = CONFIG.get("active_hours", [0, 24])
    flagged = meta_get("health:runner")
    if alive and flagged:
        alert("\u2705 The always-on checker is running again.")
        meta_set("health:runner", None)
    elif not alive and not flagged and start <= datetime.now().hour < end:
        if alert("\u26a0\ufe0f The always-on checker has stopped. The backup check (every 10 minutes, "
                 "about 1 minute slower) is posting studies in the meantime."):
            meta_set("health:runner", 1)


# --------------------------------------------------------------------------
# Oracle server: one always-on checker (started by cron every minute if it
# isn't already running), so there is no gap between checks
# --------------------------------------------------------------------------
_LOCKS = {}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def take_lock(name):
    import fcntl
    f = open(HERE / f".{name}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _LOCKS[name] = f
    return True


def beat():
    (HERE / ".heartbeat").write_text(str(time.time()))


def runner_alive():
    try:
        return time.time() - float((HERE / ".heartbeat").read_text()) < 150
    except Exception:
        return False


def install_cron():
    line = f"* * * * * cd {HERE} && venv/bin/python studybot.py --forever >> {HERE}/forever.log 2>&1"
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if "--forever" not in cur:
            subprocess.run(["crontab", "-"], input=cur + line + "\n", text=True, check=True)
            print("Installed the always-on checker.")
    except Exception as e:
        print("Couldn't install the always-on checker:", e)


def pulled_update():
    """Fetch bot changes from GitHub; True if studybot.py changed."""
    repo = Path("/home/studybot/repo")
    try:
        subprocess.run(["git", "-C", str(repo), "pull", "-q"], timeout=90)
        new = (repo / "studybot.py").read_bytes()
        if new != (HERE / "studybot.py").read_bytes():
            (HERE / "studybot.py").write_bytes(new)
            return True
    except Exception as e:
        print("update check failed:", e)
    return False


async def forever(only):
    if not take_lock("runner"):
        return  # already running
    log = HERE / "forever.log"
    if log.exists() and log.stat().st_size > 5_000_000:
        os.truncate(log, 0)
    while not take_lock("work"):  # let a timer run that's in progress finish
        beat()
        await asyncio.sleep(2)
    print(datetime.now().strftime("%m/%d %I:%M:%S %p"), "always-on checker started")
    started = last_pull = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA)
        while True:
            beat()
            cycle = time.time()
            start, end = CONFIG.get("active_hours", [0, 24])
            active = start <= datetime.now().hour < end
            if active:
                await run_once(ctx, only)
                beat()
            check_alert_signup()
            if time.time() - last_pull > 300:
                last_pull = time.time()
                if pulled_update():
                    print("bot updated from GitHub: restarting")
                    break
            if time.time() - started > 3 * 3600 or not browser.is_connected():
                break  # restart now and then (or if the browser crashed) with a fresh browser
            wait = CONFIG.get("check_every_seconds", 30) if active else 60
            await asyncio.sleep(max(5, wait - (time.time() - cycle)))
        try:
            await browser.close()
        except Exception:
            pass
    for f in _LOCKS.values():
        f.close()
    os.execv(sys.executable, [sys.executable, str(HERE / "studybot.py"), "--forever"])


async def main():
    args = sys.argv[1:]
    if "--find-chat" in args:
        return find_chat()
    if SERVER and "--forever" in args:
        return await forever([a for a in args if not a.startswith("--")])
    if SERVER and "--once" in args:  # the 10-minute timer: just make sure the checker is on
        install_cron()
        alive = runner_alive()
        runner_health(alive)
        if alive:
            print("Always-on checker is running; nothing to do.")
            return
        if not take_lock("work"):
            print("Another check is still running; nothing to do.")
            return
    check, dump = "--check" in args, "--dump" in args
    only = [a for a in args if not a.startswith("--")]
    if not (check or dump) and (not CONFIG.get("bot_token") or not CONFIG.get("chat_id")):
        sys.exit("Put your bot_token and chat_id in config.json first (see README.md).")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA)
        if check or dump:
            await run_once(ctx, only, check=check, dump=dump)
            return
        start, end = CONFIG.get("active_hours", [0, 24])
        post_existing = "--post-existing" in args
        if "--once" in args:  # one run of the server's 10-minute timer (or GitHub Actions)
            # On the server, keep re-checking for most of the 10 minutes instead of
            # checking once, so new studies are posted within about a minute.
            keep_going = SERVER and CONFIG.get("fast_checks", True)
            began = time.time()
            while start <= datetime.now().hour < end:
                cycle = time.time()
                await run_once(ctx, only, post_existing=post_existing)
                post_existing = False
                if not keep_going or time.time() - began > 8 * 60 or (SERVER and runner_alive()):
                    break
                await asyncio.sleep(max(0, CONFIG.get("check_every_seconds", 30) - (time.time() - cycle)))
            else:
                print("Outside active hours; nothing to do.")
            return
        while True:
            if start <= datetime.now().hour < end:
                print(datetime.now().strftime("%I:%M %p"), "checking...")
                await run_once(ctx, only, post_existing=post_existing)
                post_existing = False
            await asyncio.sleep(CONFIG.get("check_every_minutes", 10) * 60)


if __name__ == "__main__":
    asyncio.run(main())
