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
            detail=True, detail_only=["bmi"], style="icon")
SOURCES = [
    dict(ICON, key="icon_sa", header=["Icon VCT, San Antonio, TX", "(210)225-5437. Online appointment page is more efficient"],
         list_urls=["https://iconstudies.com/sanantonio/"]),
    dict(ICON, key="icon_lenexa", header=["Icon VCT, Lenexa, KS", "(913)410-2900"],
         list_urls=["https://iconstudies.com/Lenexa/"]),
    dict(ICON, key="icon_slc", header=["Icon VCT, Salt Lake City, UT", "(801)269-8200"],
         list_urls=["https://iconstudies.com/SLC/"]),
    dict(key="icon_sa_screenings", header=["Screenings Available", "Icon San Antonio"],
         list_urls=["https://www.appointmentquest.com/scheduler/2110084437.1"], mode="cards",
         card="tr:has(> td > table td.LABEL h4)", title="h4", key_from="title",
         exclude=r"study done|appts? cx|cancel", style="screenings", no_date=True),
    dict(key="parexel_bal", header=["(877)617-8839", "Parexel Baltimore"], style="parexel",
         list_urls=["https://pxlcrs.my.site.com/portal/s/trials-listing-bal?loc=BAL&language=en_US"],
         mode="text", start="find a suitable trial:", end="Get Started", split=r"\n\s*Learn More\s*\n",
         key_re=r"\b(\d{6})\b", wait=8000),
    dict(key="parexel_la", header=["8882287425", "Parexel Glendale"], style="parexel",
         list_urls=["https://pxlcrs.my.site.com/portal/s/triallistla?loc=LA&language=en_US"],
         mode="text", start="find a suitable trial:", end="Get Started", split=r"\n\s*Learn More\s*\n",
         key_re=r"\b(\d{6})\b", wait=8000),
    dict(key="fortrea", header=["Fortrea", "{status}: (866)429-3700"], status_in_header=True, style="plain",
         list_urls=[f"https://www.fortreaclinicaltrials.com/en-us/clinical-research/browse-studies?page={i}"
                    for i in range(4)],
         mode="cards", card="div.view-studies-us tbody tr", link="td.views-field-title a",
         title="td.views-field-title a", study_re=r"\n(\d{6,7}[^\n]*)\n"),
    dict(key="abbvie", header=["{status}: 800-827-2778", "Abbvie, NonVCT, Grayslake IL"], status_in_header=True,
         no_date=True, style="plain", list_urls=["https://abbviephase1.com/#/available-trials"],
         mode="links", link_re=r"InputFiles/Files/.+\.pdf"),
    dict(key="celerion", header=["{STATUS} CELERION STUDY:", "Online Registration (quickest):",
                                 "https://eregportal.helpresearch.com/PivotalUX/", "Phone: (866)445-7033."],
         status_in_header=True, style="plain",
         list_urls=[f"https://helpresearch.com/studies?page={i}" for i in range(4)],
         mode="cards", card="div.inner-container", link="a.study-page-link", detail=True,
         study_re=r"\b(C[A-Z]\d{5}-\w+)", skip_re=r"^starts in"),
    dict(key="trialmed", header=["{STATUS} Trialmed formerly PPD", "(800)773-2782"], status_in_header=True,
         style="plain", list_urls=[f"https://trialmed.com/find-a-study/?paged={i}" for i in range(1, 7)],
         mode="cards", card="div.wp-study-card", link="a.wp-study-card-title", title="a.wp-study-card-title",
         detail=True),
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
    dm = [m for m in DATES.finditer(text)]
    best = next((m for m in dm if re.match(r"(start date|check[- ]?in|admission|clinic stay)", m.group(0), re.I)),
                dm[0] if dm else None)
    if best:
        f["dates"] = re.sub(r"\s*\n\s*", " ", best.group(0)).strip()
    return f


def lines_of(text):
    return [re.sub(r"[ \t ﻿]+", " ", l).strip() for l in (text or "").splitlines()
            if l.strip() and l.strip() not in ("/", "|")]


def fmt_money(v):
    return f"${v:,}"


# --------------------------------------------------------------------------
# Building posts
# --------------------------------------------------------------------------
def build_post(src, status, item):
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
        return "\n".join(lines + ["\n".join(lines_of(detail or card))[:1500], url]).strip()

    # details: list card first; the study's own page fills gaps
    f = extract(card)
    if detail:
        d = extract(detail)
        for k, v in d.items():
            if src.get("detail_only") and k not in src["detail_only"]:
                continue
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
    if "dates" in f and style != "icon":
        lines.append(f["dates"])
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
    found = {}
    try:
        for n, list_url in enumerate(src["list_urls"]):
            await load(page, list_url, src.get("wait", 2500))
            if dump:
                d = HERE / "dumps"
                d.mkdir(exist_ok=True)
                (d / f"{src['key']}_{n}.txt").write_text(await page.inner_text("body"), encoding="utf-8")
            if src["mode"] == "cards":
                cards = await page.evaluate(CARDS_JS, {k: src.get(k) for k in ("card", "link", "title", "study")})
            elif src["mode"] == "links":
                cards = await page.evaluate(LINKS_JS, src["link_re"])
            else:  # text
                body = await page.inner_text("body")
                lo = body.lower()
                i = lo.find(src["start"].lower())
                j = lo.find(src["end"].lower(), i + 1)
                body = body[i + len(src["start"]) if i >= 0 else 0: j if j > 0 else None]
                cards = [dict(text=b.strip(), href=list_url, title="", study="")
                         for b in re.split(src["split"], body) if b.strip()]
            before = len(found)
            for c in cards:
                if src.get("exclude") and re.search(src["exclude"], c["text"], re.I):
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
                                         title=c["title"], study=c["study"]))
            if n > 0 and len(found) == before:  # past the last page
                break
    finally:
        await page.close()
    return found


async def fill_details(ctx, src, item):
    if not src.get("detail") or not item.get("url") or item.get("detail") is not None:
        return item
    page = await ctx.new_page()
    try:
        await load(page, item["url"])
        main = await page.query_selector("main, article, [role=main]")
        item["detail"] = await (main.inner_text() if main else page.inner_text("body"))
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
    for src in SOURCES:
        if only and src["key"] not in only:
            continue
        try:
            current = await read_source(ctx, src, dump=dump)
        except Exception as e:
            print(f"[{src['key']}] couldn't read: {e}")
            continue
        print(f"[{src['key']}] {len(current)} studies listed")
        if check or dump:
            for k, item in list(current.items())[:3] if check else []:
                await fill_details(ctx, src, item)
                print("-" * 40)
                print(build_post(src, "New", item))
            continue
        if not current:  # site down or page changed: don't treat as "all closed"
            continue
        first_run = not DB.execute("SELECT 1 FROM items WHERE source=? LIMIT 1", (src["key"],)).fetchone()
        todo, closed = decide(src, current)
        if first_run and not post_existing:
            print(f"[{src['key']}] first run: recorded {len(current)} existing studies without posting")
            todo = []
        for status, k in todo:
            item = current[k]
            try:
                await fill_details(ctx, src, item)
                if send(build_post(src, status, item)):
                    print(f"[{src['key']}] posted {status}: {item.get('title') or k}")
                    item["posted"] = True
                time.sleep(CONFIG.get("seconds_between_posts", 3))
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


async def main():
    args = sys.argv[1:]
    if "--find-chat" in args:
        return find_chat()
    check, dump = "--check" in args, "--dump" in args
    only = [a for a in args if not a.startswith("--")]
    if not (check or dump) and (not CONFIG.get("bot_token") or not CONFIG.get("chat_id")):
        sys.exit("Put your bot_token and chat_id in config.json first (see README.md).")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"))
        if check or dump:
            await run_once(ctx, only, check=check, dump=dump)
            return
        start, end = CONFIG.get("active_hours", [0, 24])
        post_existing = "--post-existing" in args
        if "--once" in args:  # GitHub Actions: one check per run
            if start <= datetime.now().hour < end:
                await run_once(ctx, only, post_existing=post_existing)
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
