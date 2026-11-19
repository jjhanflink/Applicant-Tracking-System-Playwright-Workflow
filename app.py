import os
import re
import json
import hashlib
import sqlite3
import asyncio
import subprocess
import sys
import requests
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import streamlit as st
from playwright.async_api import async_playwright

st.set_page_config(page_title="Llama 3.3 ATS Auto-Applier", page_icon="\u26a1", layout="wide")


@st.cache_resource
def ensure_playwright_browsers():
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    except Exception as e:
        print(f"Error ensuring Playwright browsers: {e}")
    return True


ensure_playwright_browsers()

DB_FILE = "ats_queue.db"
PROFILE_FILE = "candidate_profile.json"
SCREENSHOT_PATH = "latest_view.png"
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "gbraid", "gad_source", "gad_campaignid", "fbclid", "mc_cid", "mc_eid"
}


def sanitize_job_url(url: str) -> str:
    raw = url.strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    clean_query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return urlunparse(parsed._replace(query=urlencode(clean_query)))


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT,
            role TEXT,
            url TEXT UNIQUE,
            platform TEXT,
            status TEXT DEFAULT 'Pending',
            cover_letter_allowed TEXT DEFAULT 'Unknown',
            notes TEXT,
            unanswered_json TEXT DEFAULT '[]',
            custom_answers_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("PRAGMA table_info(applications)")
    existing_cols = [row[1] for row in c.fetchall()]
    needed_cols = {
        "cover_letter_allowed": "TEXT DEFAULT 'Unknown'",
        "unanswered_json": "TEXT DEFAULT '[]'",
        "custom_answers_json": "TEXT DEFAULT '{}'"
    }
    for col_name, col_type in needed_cols.items():
        if col_name not in existing_cols:
            try:
                c.execute(f"ALTER TABLE applications ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass
    conn.commit()
    conn.close()


def get_db_connection():
    return sqlite3.connect(DB_FILE)


def export_queue_json() -> str:
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r) for r in conn.execute(
            "SELECT company, role, url, platform, status, cover_letter_allowed, notes, unanswered_json, custom_answers_json FROM applications ORDER BY id ASC"
        ).fetchall()
    ]
    conn.close()
    return json.dumps(rows, indent=2)


def import_queue_json(raw_bytes: bytes) -> int:
    data = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(data, list):
        return 0
    conn = get_db_connection()
    added = 0
    for item in data:
        u = sanitize_job_url(str(item.get("url", "")))
        if not u:
            continue
        try:
            conn.execute("""
                INSERT INTO applications (company, role, url, platform, status, cover_letter_allowed, notes, unanswered_json, custom_answers_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    status=excluded.status,
                    notes=excluded.notes,
                    unanswered_json=excluded.unanswered_json,
                    custom_answers_json=excluded.custom_answers_json
            """, (
                item.get("company", parse_fallback_company(u)),
                item.get("role", "Engineering Candidate Role"),
                u,
                item.get("platform", detect_platform(u)),
                item.get("status", "Pending"),
                item.get("cover_letter_allowed", "Unknown"),
                item.get("notes", "Restored from backup"),
                item.get("unanswered_json", "[]"),
                item.get("custom_answers_json", "{}")
            ))
            added += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return added


def load_profile():
    default_profile = {
        "full_name": "Joshua Jason Hanflink",
        "first_name": "Joshua",
        "last_name": "Hanflink",
        "today_date": datetime.now().strftime("%m/%d/%Y"),
        "gpa": "3.45",
        "target_term": "Summer 2027"
    }
    if os.path.exists(PROFILE_FILE):
        try:
            with open(PROFILE_FILE, "r", encoding="utf-8-sig") as f:
                saved = json.load(f)
                default_profile.update(saved)
        except Exception:
            pass
    default_profile["today_date"] = datetime.now().strftime("%m/%d/%Y")
    return default_profile


init_db()


def detect_platform(url: str) -> str:
    u = url.lower()
    if "greenhouse.io" in u:
        return "Greenhouse"
    elif "lever.co" in u:
        return "Lever"
    elif "myworkdayjobs.com" in u or "workday" in u:
        return "Workday"
    elif "phenom" in u or "/apply?jobseqno=" in u or "careers.geaerospace.com" in u or "careers.cisco.com" in u:
        return "Phenom / Enterprise"
    elif "ashbyhq.com" in u:
        return "Ashby"
    return "Company Direct Portal"


def parse_fallback_company(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    domain = re.sub(r'^(www\.|careers\.|jobs\.|boards\.)', '', domain)
    cand = domain.split('.')[0]
    return cand.capitalize() if cand else "Engineering Employer"


def ask_llama_to_resolve_all(form_schema: list, profile: dict, api_key: str) -> dict:
    if not api_key or not form_schema:
        return {}
    prompt = f"""You are an autonomous job application form filler for Joshua Jason Hanflink.
Candidate Profile:
{json.dumps(profile, indent=2)}

Strict Rules:
1. Ethnicity/Race: ALWAYS choose 'Hispanic or Latino'.
2. Citizenship / Work Auth / US Person / Export Control: Choose 'A United States Citizen', 'U.S. Citizen', or 'Yes'.
3. Visa Sponsorship / Relatives / Former Employee: Choose 'No'.
4. Checkboxes for Authorization, Certifications, Terms, Proprietary Info, Burden Statements: Return 'CHECK'.
5. Target Internship Term: Summer 2027 (availability only; return 'NEEDS_HUMAN' if asked for exact company-specific start dates).
6. Address Routing: Default to Raleigh, NC 27606 unless the prompt explicitly says 'Permanent' or 'Home' address (then use 701 Blanton Place, Greensboro, NC 27408).
7. MANUAL FALLBACK: If a question asks for a specific job location preference, club/group affiliation, how you heard about the role, past degrees completed, or ANY short-answer essays (technical accomplishments, mission, values), return 'NEEDS_HUMAN'. Do NOT guess these.
8. Return the exact option string from the 'options' list for dropdowns or radio groups.

Questions:
{json.dumps(form_schema, indent=2)}

Return ONLY a JSON object mapping each question 'qid' to the string answer."""

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "response_format": {"type": "json_object"}
            },
            timeout=25
        )
        if resp.status_code == 200:
            return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception:
        pass
    return {}


class ATSAutomator:
    def __init__(self, headless: bool = True, groq_api_key: str = ""):
        self.headless = headless
        self.groq_api_key = groq_api_key

    async def accept_cookies(self, page):
        for sel in [
            "button:has-text('Accept All')", "button:has-text('Accept all')",
            "button:has-text('Accept Cookies')", "button:has-text('Accept')",
            "#accept-cookies", ".cookie-accept", "#onetrust-accept-btn-handler"
        ]:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.evaluate("el => el.click()")
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

    async def stepped_scroll(self, page):
        try:
            total_height = await page.evaluate("document.body.scrollHeight")
            current_pos = 0
            while current_pos < total_height:
                current_pos += 450
                await page.evaluate(f"window.scrollTo(0, {current_pos})")
                await asyncio.sleep(0.18)
                total_height = await page.evaluate("document.body.scrollHeight")
            await asyncio.sleep(0.5)
        except Exception:
            pass

    async def _navigate_to_form(self, page, url: str):
        if "jobs.lever.co" in url and not url.rstrip("/").endswith("/apply"):
            apply_url = url.rstrip("/") + "/apply"
            try:
                await page.goto(apply_url, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(2)
                await self.accept_cookies(page)
                return page
            except Exception:
                pass

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
        await self.accept_cookies(page)

        has_form = await page.query_selector("input[type='text'], input[type='email'], input[name='name'], input[name='firstName' i]")
        if not has_form:
            apply_selectors = [
                "a:has-text('Apply for this job')", "a:has-text('Apply Now')", "button:has-text('Apply Now')",
                "a:has-text('Apply now')", "button:has-text('Apply now')", "a:has-text('Apply')",
                "[data-ph-at-id='apply-text']", ".ph-apply-button", ".apply-button", "a.apply-link"
            ]
            for sel in apply_selectors:
                try:
                    btns = await page.query_selector_all(sel)
                    clicked = False
                    for btn in btns:
                        if await btn.is_visible():
                            pages_before = len(page.context.pages)
                            await btn.evaluate("el => el.click()")
                            await asyncio.sleep(4)
                            if len(page.context.pages) > pages_before:
                                page = page.context.pages[-1]
                                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                                await asyncio.sleep(2)
                            clicked = True
                            break
                    if clicked:
                        break
                except Exception:
                    pass
        return page

    async def classify_pipeline(self, page, url: str) -> str:
        u = (url + " " + page.url).lower()
        if any(p in u for p in ["jobs.lever.co", "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.ashbyhq.com"]):
            return "single_page"
        if any(p in u for p in ["myworkdayjobs.com", "phenom", "/apply?jobseqno=", "careers.geaerospace.com", "careers.cisco.com"]):
            return "enterprise_wizard"
        is_wizard = await page.evaluate(r"""() => {
            return Boolean(
                document.querySelector('[data-automation-id], [data-ph-at-id], [role="progressbar"], .wd-popup, .step-indicator') ||
                Array.from(document.querySelectorAll('button')).some(b => /^(next|save and continue)$/i.test((b.innerText || '').trim()))
            );
        }""")
        return "enterprise_wizard" if is_wizard else "single_page"

    # =========================================================================
    # PIPELINE 1: SINGLE-PAGE FORMS (Lever, Greenhouse, Ashby) - PRESERVED
    # =========================================================================
    async def _extract_clean_dom_schema(self, page):
        schema = await page.evaluate(r"""() => {
            const results = [];
            let qCounter = 0;
            const radioGroups = {};

            function extractPrompt(el) {
                const container = el.closest('.application-question, .custom-question, fieldset, li, .application-field, div.field, label, div.form-group, .posting-requirement, div.section-wrapper');
                if (!container) return el.name || el.placeholder || el.getAttribute('aria-label') || 'Question';
                const labelEl = container.querySelector('.application-label, .text, legend, .section-headline, b, h4, .posting-req-label');
                if (labelEl) {
                    const t = labelEl.innerText.replace(/\s+/g, ' ').trim();
                    if (t.length > 2) return t;
                }
                const clone = container.cloneNode(true);
                clone.querySelectorAll('select, input, ul, .application-field, .options-list, .resume-upload, .help-text').forEach(n => n.remove());
                const clean = clone.innerText.replace(/\s+/g, ' ').trim();
                return clean.length > 2 ? clean : (el.name || el.placeholder || 'Question');
            }

            document.querySelectorAll('select').forEach(sel => {
                if (sel.offsetParent === null) return;
                const qid = 'q_' + (qCounter++);
                sel.setAttribute('data-ats-qid', qid);
                const opts = Array.from(sel.options).map(o => o.text.trim()).filter(t => t && !['select...', 'select one', '--'].includes(t.toLowerCase()));
                const curTxt = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text.trim() : '';
                const isFilled = sel.value && curTxt && !['select...', 'select one', '--'].includes(curTxt.toLowerCase());
                results.push({ qid, type: 'select', label: extractPrompt(sel), options: opts, filled: Boolean(isFilled) });
            });

            document.querySelectorAll('input[type="radio"], input[type="checkbox"]:not([name*="consent"]):not([required]):not([id*="consent"])').forEach(rad => {
                if (rad.offsetParent === null) return;
                const name = rad.name || ('anon_' + extractPrompt(rad));
                if (!radioGroups[name]) radioGroups[name] = [];
                radioGroups[name].push(rad);
            });

            Object.keys(radioGroups).forEach(name => {
                const group = radioGroups[name];
                const qid = 'q_' + (qCounter++);
                const opts = [];
                let isFilled = false;
                let isMulti = false;
                group.forEach(el => {
                    el.setAttribute('data-ats-qid', qid);
                    if (el.checked) isFilled = true;
                    if (el.type === 'checkbox') isMulti = true;
                    const rLbl = el.closest('label') || document.querySelector(`label[for="${el.id}"]`);
                    const oTxt = rLbl ? rLbl.innerText.trim().split('\n')[0] : el.value;
                    if (oTxt) opts.push(oTxt);
                });
                results.push({ qid, type: isMulti ? 'multiselect' : 'radio', label: extractPrompt(group[0]), options: opts, filled: isFilled });
            });

            document.querySelectorAll('input[type="text"]:not([data-core="true"]), input[type="number"], textarea').forEach(tInp => {
                if (tInp.offsetParent === null) return;
                const qid = 'q_' + (qCounter++);
                tInp.setAttribute('data-ats-qid', qid);
                results.push({ qid, type: 'text', label: extractPrompt(tInp), options: [], filled: Boolean(tInp.value && tInp.value.trim().length > 0) });
            });

            document.querySelectorAll('input[type="checkbox"][name*="consent"], input[type="checkbox"][required], input[type="checkbox"][id*="consent"]').forEach(cb => {
                if (cb.offsetParent === null) return;
                const qid = 'q_' + (qCounter++);
                cb.setAttribute('data-ats-qid', qid);
                results.push({ qid, type: 'checkbox', label: extractPrompt(cb), options: ['CHECK', 'SKIP'], filled: cb.checked });
            });

            return results;
        }""")
        return schema

    async def _fill_single_page(self, page, profile: dict, custom_answers: dict):
        filled = 0
        await self.accept_cookies(page)

        if os.path.exists("resume.pdf"):
            file_inputs = await page.query_selector_all("input[type='file']")
            for f_in in file_inputs:
                try:
                    p_txt = (await f_in.evaluate("el => el.closest('div, label, fieldset')?.innerText || ''")).lower()
                    if "cover letter" not in p_txt:
                        await f_in.set_input_files("resume.pdf")
                        filled += 1
                        try:
                            await page.wait_for_selector("text='Analyzing resume'", state="visible", timeout=3500)
                            await page.wait_for_selector("text='Analyzing resume'", state="hidden", timeout=15000)
                        except Exception:
                            pass
                        await asyncio.sleep(6.0)
                        break
                except Exception:
                    pass

        await self.stepped_scroll(page)

        core_map = [
            ("input[name='name'], input[aria-label*='Full Name' i]", profile.get("full_name", "Joshua Jason Hanflink")),
            ("input[name='email'], input[type='email']", profile.get("email", "")),
            ("input[name='phone'], input[type='tel']", profile.get("phone", "")),
            ("input[name='org'], input[name*='company' i]", profile.get("school", "")),
            ("input[name='urls[LinkedIn]'], input[name*='linkedin' i]", profile.get("linkedin", "")),
            ("input[name='urls[GitHub]'], input[name*='github' i]", profile.get("github", "")),
            ("input[name='urls[Portfolio]'], input[name*='portfolio' i]", profile.get("portfolio", ""))
        ]
        for sel, val in core_map:
            try:
                elements = await page.query_selector_all(sel)
                for el in elements:
                    if await el.is_visible():
                        await el.evaluate("el => el.setAttribute('data-core', 'true')")
                        await el.fill(val)
                        filled += 1
                        break
            except Exception:
                pass

        loc_els = await page.query_selector_all("input[name*='location' i], input[id*='location' i]")
        for loc_el in loc_els:
            if await loc_el.is_visible():
                try:
                    await loc_el.evaluate("el => el.setAttribute('data-core', 'true')")
                    await loc_el.click()
                    await loc_el.fill("")
                    await loc_el.type(profile.get("location", "Raleigh, NC, USA"), delay=45)
                    await asyncio.sleep(1.2)
                    await loc_el.press("ArrowDown")
                    await loc_el.press("Enter")
                    await loc_el.press("Tab")
                    filled += 1
                    break
                except Exception:
                    pass

        schema = await self._extract_clean_dom_schema(page)
        unfilled_items = [item for item in schema if not item.get("filled")]
        qid_answers = {}
        today_str = datetime.now().strftime("%m/%d/%Y")

        for item in unfilled_items:
            qid = item["qid"]
            qtype = item["type"]
            lbl = item["label"]
            low = lbl.lower()
            opts = item.get("options", [])

            for c_q, c_v in custom_answers.items():
                if c_v and (c_q.lower()[:30] in low or low[:30] in c_q.lower()):
                    qid_answers[qid] = c_v
                    break
            if qid in qid_answers:
                continue

            if "legal name" in low or low in ["name", "name *"]:
                qid_answers[qid] = profile.get("full_name")
            elif "authorized to work" in low:
                qid_answers[qid] = profile.get("work_authorized", "Yes")
            elif "school or university" in low:
                for o in opts:
                    if "north carolina state" in o.lower() or "nc state" in o.lower():
                        qid_answers[qid] = o
                        break
            elif "gpa" in low:
                if qtype in ["select", "radio"]:
                    for o in opts:
                        if "3.5" in o or "3.45" in o or "3.0" in o or "3.4" in o:
                            qid_answers[qid] = o
                            break
                elif qtype == "text" and not any(skip in low for skip in ["comment", "explain", "optional"]):
                    qid_answers[qid] = profile.get("gpa", "3.45")
            elif any(k in low for k in ["citizenship", "citizen status", "immigration", "u.s. person", "export control"]):
                for o in opts:
                    if "united states citizen" in o.lower() or "u.s. citizen" in o.lower() or o.lower() == "yes":
                        qid_answers[qid] = o
                        break
            elif any(k in low for k in ["if you selected", "if yes", "optional", "explain further", "comments on gpa"]):
                qid_answers[qid] = ""
            elif qtype in ["select", "radio"]:
                if any(k in low for k in ["hispanic", "latino", "ethnicity", "race"]):
                    for o in opts:
                        if "hispanic or latino" in o.lower() and "not hispanic" not in o.lower():
                            qid_answers[qid] = o
                            break
                    if qid not in qid_answers:
                        for o in opts:
                            if o.lower().startswith("white"):
                                qid_answers[qid] = o
                                break
                elif "gender" in low:
                    for o in opts:
                        if o.lower() in ["male", "man"]:
                            qid_answers[qid] = o
                            break
                elif "veteran" in low:
                    for o in opts:
                        if any(k in o.lower() for k in ["not a protected veteran", "i am not", "no"]):
                            qid_answers[qid] = o
                            break
                elif "disability" in low:
                    for o in opts:
                        if "yes" in o.lower() or "have a disability" in o.lower():
                            qid_answers[qid] = o
                            break
                elif any(k in low for k in ["sponsor", "visa", "relative", "family member", "former employee"]):
                    for o in opts:
                        if o.lower() == "no" or o.lower().startswith("no,"):
                            qid_answers[qid] = o
                            break
            elif qtype == "checkbox":
                if any(k in low for k in ["certify", "acknowledge", "agree", "proprietary", "authorization", "terms", "privacy", "burden statement"]):
                    qid_answers[qid] = "CHECK"
            elif qtype == "text":
                if "signature" in low or "legal name" in low:
                    qid_answers[qid] = profile.get("signature_name", "Joshua Hanflink")
                elif low in ["date", "date *"] or "today's date" in low:
                    qid_answers[qid] = today_str

        remaining = [it for it in unfilled_items if it["qid"] not in qid_answers]
        llama_count = 0
        if remaining and self.groq_api_key:
            l_map = ask_llama_to_resolve_all(remaining, profile, self.groq_api_key)
            for k_qid, v_ans in l_map.items():
                if v_ans and v_ans != "NEEDS_HUMAN":
                    qid_answers[k_qid] = v_ans
                    llama_count += 1

        for item in unfilled_items:
            qid = item["qid"]
            qtype = item["type"]
            ans = qid_answers.get(qid)
            if ans is None or ans == "":
                continue
            try:
                if qtype == "select":
                    sel_el = await page.query_selector(f"select[data-ats-qid='{qid}']")
                    if sel_el:
                        matched = await page.evaluate(r"""([el, target]) => {
                            const tLow = target.toLowerCase().trim();
                            for (const opt of el.options) {
                                const oLow = opt.text.toLowerCase().trim();
                                if (oLow === tLow || oLow.includes(tLow) || tLow.includes(oLow)) {
                                    el.value = opt.value;
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                    return true;
                                }
                            }
                            return false;
                        }""", [sel_el, str(ans)])
                        if matched:
                            filled += 1
                elif qtype == "radio":
                    clicked = await page.evaluate(r"""([qid, target]) => {
                        const tLow = target.toLowerCase().trim();
                        const radios = document.querySelectorAll(`input[type="radio"][data-ats-qid="${qid}"]`);
                        for (const r of radios) {
                            const lbl = r.closest('label') || document.querySelector(`label[for="${r.id}"]`);
                            const oTxt = (lbl ? lbl.innerText : (r.value || '')).toLowerCase().trim();
                            const firstLine = oTxt.split('\n')[0].trim();
                            if (firstLine === tLow || oTxt.includes(tLow) || tLow.includes(firstLine)) {
                                r.click();
                                r.checked = true;
                                r.dispatchEvent(new Event('change', { bubbles: true }));
                                return true;
                            }
                        }
                        return false;
                    }""", [qid, str(ans)])
                    if clicked:
                        filled += 1
                elif qtype == "multiselect":
                    vals = ans if isinstance(ans, list) else [ans]
                    for a in vals:
                        await page.evaluate(r"""([qid, target]) => {
                            const tLow = target.toLowerCase().trim();
                            const checkboxes = document.querySelectorAll(`input[type="checkbox"][data-ats-qid="${qid}"]`);
                            for (const cb of checkboxes) {
                                const lbl = cb.closest('label') || document.querySelector(`label[for="${cb.id}"]`);
                                const oTxt = (lbl ? lbl.innerText : (cb.value || '')).toLowerCase().trim();
                                if (oTxt.includes(tLow) || tLow.includes(oTxt)) {
                                    if (!cb.checked) {
                                        cb.click();
                                        cb.checked = true;
                                        cb.dispatchEvent(new Event('change', { bubbles: true }));
                                    }
                                }
                            }
                        }""", [qid, str(a)])
                    filled += 1
                elif qtype == "checkbox" and str(ans).upper() == "CHECK":
                    cb_el = await page.query_selector(f"input[type='checkbox'][data-ats-qid='{qid}']")
                    if cb_el and not await cb_el.is_checked():
                        await cb_el.evaluate("el => { el.click(); el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true })); }")
                        filled += 1
                elif qtype == "text" and str(ans).strip():
                    t_el = await page.query_selector(f"[data-ats-qid='{qid}']")
                    if t_el:
                        await t_el.fill(str(ans).strip())
                        filled += 1
            except Exception:
                pass

        return filled, llama_count

    async def _scrape_unanswered_questions(self, page):
        schema = await self._extract_clean_dom_schema(page)
        unanswered = []
        seen = set()
        for item in schema:
            if item.get("filled"):
                continue
            lbl = item.get("label", "").strip()
            low = lbl.lower()
            if any(skip in low for skip in ["if you selected", "if yes", "please specify", "additional information", "optional", "cover letter"]):
                continue
            if item["type"] == "checkbox":
                continue
            if lbl and lbl not in seen:
                seen.add(lbl)
                unanswered.append({
                    "label": lbl,
                    "type": item["type"],
                    "options": item.get("options", [])
                })
        return unanswered

    # =========================================================================
    # PIPELINE 2: ENTERPRISE MULTI-STEP WIZARD (Workday / Phenom / GE / Cisco)
    # =========================================================================
    async def _wait_for_react_stability(self, page):
        spinner_sel = "[data-automation-id='loading'], [aria-busy='true'], .ph-loader, .spinner, .skeleton-loader"
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        try:
            await page.wait_for_selector(spinner_sel, state="hidden", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(1.0)

    async def _is_review_page(self, page) -> bool:
        return await page.evaluate(r"""() => {
            const header = (document.querySelector('h1, h2, [data-automation-id="pageHeaderTitle"], .step-active')?.innerText || '').toLowerCase();
            const hasReviewHeading = /\b(review|summary|review and submit)\b/.test(header);
            const buttons = Array.from(document.querySelectorAll('button:not([disabled]), input[type="submit"]'))
                .filter(b => b.offsetParent !== null)
                .map(b => (b.innerText || b.value || '').toLowerCase().trim());
            const hasSubmitBtn = buttons.some(t => /^submit(\s+application)?$/.test(t));
            const hasNextBtn = buttons.some(t => /^(next|continue|save and continue)$/.test(t));
            return hasReviewHeading || (hasSubmitBtn && !hasNextBtn);
        }""")

    async def _scrape_validation_errors(self, page) -> list:
        return await page.evaluate(r"""() => {
            const errs = [];
            document.querySelectorAll('[role="alert"], .error-message, .field-error, [data-automation-id="errorMessage"], .invalid-feedback').forEach(el => {
                if (el.offsetParent !== null) {
                    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
                    if (t && !errs.includes(t)) errs.push(t);
                }
            });
            return errs.slice(0, 5);
        }""")

    async def _try_enterprise_resume_upload(self, page) -> bool:
        file_inputs = await page.query_selector_all("input[type='file']")
        for f_in in file_inputs:
            try:
                p_txt = (await f_in.evaluate("el => (el.closest('div, section, form')?.innerText || '').toLowerCase()"))
                if "cover letter" in p_txt:
                    continue
                await f_in.set_input_files("resume.pdf")
                await asyncio.sleep(3.5)
                return True
            except Exception:
                pass
        return False

    async def _human_type(self, locator, value: str):
        await locator.scroll_into_view_if_needed()
        await locator.click()
        await locator.press("Control+A")
        await locator.press("Backspace")
        await locator.press_sequentially(str(value), delay=35)
        await locator.press("Tab")

    async def _select_react_dropdown(self, page, trigger_locator, target_text: str) -> bool:
        try:
            await trigger_locator.scroll_into_view_if_needed()
            await trigger_locator.click()
            await asyncio.sleep(0.45)

            tag = await trigger_locator.evaluate("el => el.tagName")
            if tag == "INPUT":
                await trigger_locator.press("Control+A")
                await trigger_locator.press("Backspace")
                await trigger_locator.press_sequentially(target_text[:14], delay=40)
                await asyncio.sleep(0.8)

            options = page.locator("[role='option']:visible, [data-automation-id='promptOption']:visible, li.dropdown-item:visible")
            count = await options.count()
            t_low = target_text.lower().strip()
            best_match = None

            for i in range(count):
                opt = options.nth(i)
                txt = (await opt.inner_text()).lower().strip()
                if txt == t_low:
                    await opt.click()
                    return True
                if t_low in txt or txt in t_low:
                    best_match = opt

            if best_match is not None:
                await best_match.click()
                return True

            await page.keyboard.press("Escape")
        except Exception:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
        return False

    async def _extract_enterprise_schema(self, page) -> list:
        return await page.evaluate(r"""() => {
            const results = [];
            let qCounter = 0;
            const radioGroups = {};
            const BLANK_OPTS = ['select...', 'please select', 'select one', 'select', '--', 'choose...'];

            function getLabel(el) {
                if (el.id) {
                    const forLbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (forLbl && forLbl.innerText.trim()) return forLbl.innerText.replace(/\s+/g, ' ').trim();
                }
                const labelledBy = el.getAttribute('aria-labelledby');
                if (labelledBy) {
                    const txt = labelledBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').replace(/\s+/g, ' ').trim();
                    if (txt) return txt;
                }
                const aria = el.getAttribute('aria-label');
                if (aria && aria.trim().length > 1) return aria.trim();
                const wrap = el.closest('fieldset, .form-group, .field-wrapper, [data-automation-id], tr, li, div.row, div');
                if (wrap) {
                    const lbl = wrap.querySelector('label, legend, .field-label, .control-label, span.label');
                    if (lbl && lbl.innerText.trim()) return lbl.innerText.replace(/\s+/g, ' ').trim();
                }
                return el.getAttribute('data-automation-id') || el.name || el.placeholder || 'Field';
            }

            function isVisible(el) {
                const r = el.getBoundingClientRect();
                return el.offsetParent !== null && r.width > 0 && r.height > 0 && !el.disabled;
            }

            // 1. Native <select>
            document.querySelectorAll('select').forEach(sel => {
                if (!isVisible(sel)) return;
                const qid = 'ent_' + (qCounter++);
                sel.setAttribute('data-ent-qid', qid);
                const opts = Array.from(sel.options).map(o => o.text.trim()).filter(t => t && !BLANK_OPTS.includes(t.toLowerCase()));
                const curTxt = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text.trim() : '';
                const filled = Boolean(sel.value && curTxt && !BLANK_OPTS.includes(curTxt.toLowerCase()));
                const lbl = getLabel(sel);
                const req = Boolean(sel.required || sel.getAttribute('aria-required') === 'true' || lbl.includes('*'));
                results.push({ qid, type: 'select', label: lbl, options: opts, filled, required: req, auto_id: sel.getAttribute('data-automation-id') || sel.name || '' });
            });

            // 2. Custom ARIA dropdowns (Workday / React comboboxes)
            document.querySelectorAll('button[aria-haspopup="listbox"], [role="combobox"]:not(select)').forEach(el => {
                if (!isVisible(el) || el.hasAttribute('data-ent-qid')) return;
                const qid = 'ent_' + (qCounter++);
                el.setAttribute('data-ent-qid', qid);
                const valTxt = (el.tagName === 'INPUT' ? el.value : el.innerText || '').trim();
                const filled = Boolean(valTxt && !BLANK_OPTS.includes(valTxt.toLowerCase()));
                const lbl = getLabel(el);
                const req = Boolean(el.getAttribute('aria-required') === 'true' || lbl.includes('*'));
                results.push({ qid, type: 'aria_select', label: lbl, options: [], filled, required: req, auto_id: el.getAttribute('data-automation-id') || el.name || '' });
            });

            // 3. Radio buttons
            document.querySelectorAll('input[type="radio"]').forEach(rad => {
                if (!isVisible(rad)) return;
                const gName = rad.name || ('rad_' + getLabel(rad));
                if (!radioGroups[gName]) radioGroups[gName] = [];
                radioGroups[gName].push(rad);
            });
            Object.keys(radioGroups).forEach(gName => {
                const group = radioGroups[gName];
                const qid = 'ent_' + (qCounter++);
                const opts = [];
                let filled = false;
                group.forEach(el => {
                    el.setAttribute('data-ent-qid', qid);
                    if (el.checked) filled = true;
                    const rLbl = el.closest('label') || (el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null);
                    const oTxt = rLbl ? rLbl.innerText.trim().split('\n')[0] : el.value;
                    if (oTxt) opts.push(oTxt);
                });
                const lbl = getLabel(group[0]);
                const req = Boolean(group.some(e => e.required) || lbl.includes('*'));
                results.push({ qid, type: 'radio', label: lbl, options: opts, filled, required: req, auto_id: gName });
            });

            // 4. Text / Email / Tel / Textarea
            document.querySelectorAll('input[type="text"], input[type="email"], input[type="tel"], input[type="number"], input:not([type]), textarea').forEach(inp => {
                if (!isVisible(inp) || inp.hasAttribute('data-ent-qid')) return;
                if (inp.type === 'hidden' || inp.readOnly) return;
                const qid = 'ent_' + (qCounter++);
                inp.setAttribute('data-ent-qid', qid);
                const lbl = getLabel(inp);
                const req = Boolean(inp.required || inp.getAttribute('aria-required') === 'true' || lbl.includes('*'));
                results.push({ qid, type: 'text', label: lbl, options: [], filled: Boolean(inp.value && inp.value.trim().length > 0), required: req, auto_id: inp.getAttribute('data-automation-id') || inp.name || inp.id || '' });
            });

            // 5. Consent / Certification Checkboxes
            document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                if (!isVisible(cb)) return;
                const qid = 'ent_' + (qCounter++);
                cb.setAttribute('data-ent-qid', qid);
                const lbl = getLabel(cb);
                const req = Boolean(cb.required || cb.getAttribute('aria-required') === 'true' || lbl.includes('*'));
                results.push({ qid, type: 'checkbox', label: lbl, options: ['CHECK', 'SKIP'], filled: cb.checked, required: req, auto_id: cb.name || cb.id || '' });
            });

            return results;
        }""")

    async def _get_step_fingerprint(self, page, schema: list) -> str:
        header = await page.evaluate(r"""() => {
            const h = document.querySelector('h1, h2, [data-automation-id="pageHeaderTitle"], .step-active');
            return h ? h.innerText.trim() : '';
        }""")
        labels = "|".join(sorted(q["label"] for q in schema))
        raw = f"{page.url}::{header}::{labels}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    async def _click_next_button(self, page) -> bool:
        next_selectors = [
            "[data-automation-id='bottom-navigation-next-button']",
            "button:has-text('Save and Continue')",
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "input[type='submit'][value*='Next' i]"
        ]
        for sel in next_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                    await btn.scroll_into_view_if_needed()
                    await btn.click()
                    await asyncio.sleep(2.5)
                    return True
            except Exception:
                pass
        return False

    async def _fill_enterprise_step(self, page, schema: list, profile: dict, custom_answers: dict):
        filled = 0
        llama_count = 0
        unfilled = [it for it in schema if not it.get("filled")]
        qid_answers = {}
        today_str = datetime.now().strftime("%m/%d/%Y")

        for item in unfilled:
            qid = item["qid"]
            qtype = item["type"]
            lbl = item["label"]
            low = (lbl + " " + item.get("auto_id", "")).lower()
            opts = item.get("options", [])
            is_permanent = any(k in low for k in ["permanent", "home address", "tax address"])

            # 1. Saved manual custom answers first
            for c_q, c_v in custom_answers.items():
                if c_v and (c_q.lower()[:30] in lbl.lower() or lbl.lower()[:30] in c_q.lower()):
                    qid_answers[qid] = c_v
                    break
            if qid in qid_answers:
                continue

            # 2. Skip explicitly optional fields we don't want to pollute
            if any(skip in low for skip in ["address line 2", "apt", "suite", "middle name", "preferred name", "suffix", "if yes", "if you selected", "optional", "comments"]):
                if not item.get("required"):
                    qid_answers[qid] = ""
                    continue

            # 3. Deterministic split-field & dual-address mapping
            if any(k in low for k in ["first name", "firstname", "legalnamesection_firstname"]):
                qid_answers[qid] = profile.get("first_name", "Joshua")
            elif any(k in low for k in ["last name", "lastname", "family name", "legalnamesection_lastname"]):
                qid_answers[qid] = profile.get("last_name", "Hanflink")
            elif "full name" in low or "legal name" in low or "signature" in low:
                qid_answers[qid] = profile.get("full_name", "Joshua Jason Hanflink")
            elif "email" in low:
                qid_answers[qid] = profile.get("email", "")
            elif "phone device" in low or "device type" in low:
                qid_answers[qid] = profile.get("phone_device_type", "Mobile")
            elif "phone code" in low or "country code" in low:
                qid_answers[qid] = profile.get("phone_country_code", "United States of America (+1)")
            elif "phone" in low and "extension" not in low:
                qid_answers[qid] = profile.get("phone", "3369296685")
            elif any(k in low for k in ["address line 1", "street address", "address1"]):
                qid_answers[qid] = profile.get("permanent_street", "701 Blanton Place") if is_permanent else profile.get("street", "NC State University")
            elif "city" in low and "ethnicity" not in low:
                qid_answers[qid] = profile.get("permanent_city", "Greensboro") if is_permanent else profile.get("city", "Raleigh")
            elif any(k in low for k in ["postal", "zip code", "zipcode"]):
                qid_answers[qid] = profile.get("permanent_zip", "27408") if is_permanent else profile.get("zip", "27606")
            elif re.search(r'\bstate\b|\bprovince\b|\bregion\b', low) and "united states" not in low and "statement" not in low:
                qid_answers[qid] = profile.get("permanent_state", "North Carolina") if is_permanent else profile.get("state", "North Carolina")
            elif "country" in low and "phone" not in low:
                qid_answers[qid] = profile.get("country", "United States of America")
            elif "linkedin" in low:
                qid_answers[qid] = profile.get("linkedin", "")
            elif "github" in low:
                qid_answers[qid] = profile.get("github", "")
            elif "portfolio" in low or "website" in low:
                qid_answers[qid] = profile.get("portfolio", "")
            elif any(k in low for k in ["school", "university", "institution"]):
                qid_answers[qid] = profile.get("school", "North Carolina State University")
            elif "gpa" in low:
                qid_answers[qid] = profile.get("gpa", "3.45")
            elif any(k in low for k in ["former employee", "previously worked", "previously employed", "ever worked for", "sponsor", "visa", "relative", "family member"]):
                qid_answers[qid] = "No"
            elif any(k in low for k in ["authorized to work", "legally authorized", "u.s. person", "us citizen", "citizenship", "over 18", "at least 18"]):
                if opts:
                    for o in opts:
                        if any(k in o.lower() for k in ["united states citizen", "u.s. citizen", "yes"]):
                            qid_answers[qid] = o
                            break
                if qid not in qid_answers:
                    qid_answers[qid] = "Yes"
            elif any(k in low for k in ["hispanic", "latino", "ethnicity", "race"]):
                qid_answers[qid] = profile.get("ethnicity", "Hispanic or Latino")
            elif "gender" in low or "sex" in low:
                qid_answers[qid] = profile.get("gender", "Male")
            elif "veteran" in low:
                qid_answers[qid] = profile.get("veteran_status", "I am not a protected veteran")
            elif "disability" in low:
                qid_answers[qid] = profile.get("disability_status", "Yes, I have a disability, or have had one in the past")
            elif qtype == "checkbox" and any(k in low for k in ["certify", "acknowledge", "agree", "terms", "privacy", "consent", "authorization"]):
                qid_answers[qid] = "CHECK"
            elif qtype == "text" and ("today's date" in low or lbl.strip().lower() in ["date", "date *"]):
                qid_answers[qid] = today_str

        # 4. Ask Llama 3.3 for remaining fields on this step
        remaining = [it for it in unfilled if it["qid"] not in qid_answers]
        if remaining and self.groq_api_key:
            l_map = ask_llama_to_resolve_all(remaining, profile, self.groq_api_key)
            for k_qid, v_ans in l_map.items():
                if v_ans and v_ans != "NEEDS_HUMAN":
                    qid_answers[k_qid] = v_ans
                    llama_count += 1

        # 5. Inject answers using human-simulated typing & React dropdown handling
        for item in unfilled:
            qid = item["qid"]
            qtype = item["type"]
            ans = qid_answers.get(qid)
            if ans is None or ans == "":
                continue
            try:
                loc = page.locator(f"[data-ent-qid='{qid}']").first
                if qtype == "text":
                    await self._human_type(loc, str(ans).strip())
                    filled += 1
                elif qtype == "select":
                    sel_el = await page.query_selector(f"select[data-ent-qid='{qid}']")
                    if sel_el:
                        matched = await page.evaluate(r"""([el, target]) => {
                            const tLow = target.toLowerCase().trim();
                            for (const opt of el.options) {
                                const oLow = opt.text.toLowerCase().trim();
                                if (oLow === tLow || oLow.includes(tLow) || tLow.includes(oLow)) {
                                    el.value = opt.value;
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                    return true;
                                }
                            }
                            return false;
                        }""", [sel_el, str(ans)])
                        if matched:
                            filled += 1
                elif qtype == "aria_select":
                    if await self._select_react_dropdown(page, loc, str(ans)):
                        filled += 1
                elif qtype == "radio":
                    clicked = await page.evaluate(r"""([qid, target]) => {
                        const tLow = target.toLowerCase().trim();
                        const radios = document.querySelectorAll(`input[type="radio"][data-ent-qid="${qid}"]`);
                        for (const r of radios) {
                            const lbl = r.closest('label') || (r.id ? document.querySelector(`label[for="${CSS.escape(r.id)}"]`) : null);
                            const oTxt = (lbl ? lbl.innerText : (r.value || '')).toLowerCase().trim();
                            if (oTxt === tLow || oTxt.includes(tLow) || tLow.includes(oTxt)) {
                                r.click();
                                r.checked = true;
                                r.dispatchEvent(new Event('change', { bubbles: true }));
                                return true;
                            }
                        }
                        return false;
                    }""", [qid, str(ans)])
                    if clicked:
                        filled += 1
                elif qtype == "checkbox" and str(ans).upper() == "CHECK":
                    if not await loc.is_checked():
                        await loc.click(force=True)
                        filled += 1
            except Exception:
                pass

        # 6. Re-check schema on this step to surface unanswered required fields to Manual Bridge
        post_schema = await self._extract_enterprise_schema(page)
        step_unanswered = []
        seen = set()
        for it in post_schema:
            if it.get("filled") or it["type"] == "checkbox":
                continue
            lbl = it["label"].strip()
            low = lbl.lower()
            if any(skip in low for skip in ["address line 2", "apt", "suite", "middle name", "preferred name", "optional", "if yes", "if you selected"]):
                continue
            if it.get("required") or "how did you hear" in low or len(it.get("options", [])) > 0:
                if lbl and lbl not in seen:
                    seen.add(lbl)
                    step_unanswered.append({
                        "label": lbl,
                        "type": "select" if it["type"] in ["select", "aria_select"] and it.get("options") else ("radio" if it["type"] == "radio" else "text"),
                        "options": it.get("options", [])
                    })

        return filled, llama_count, step_unanswered

    async def _execute_enterprise_wizard(self, page, profile: dict, custom_answers: dict):
        MAX_STEPS = 8
        visited_hashes = set()
        resume_uploaded = False
        total_filled = 0
        total_llama = 0
        step_logs = []

        for step_idx in range(1, MAX_STEPS + 1):
            await self._wait_for_react_stability(page)
            await self.accept_cookies(page)

            # Guard A: Auth / Sign-In Wall Detection
            if await page.locator("input[type='password']:visible").count() > 0:
                step_logs.append(f"Step {step_idx}: Login wall detected.")
                return {
                    "status": "Manual Action Needed",
                    "error": "Auth wall detected (Enterprise/Workday sign-in required). Open link in browser or run locally in Headed mode.",
                    "fields_filled": total_filled,
                    "llama_mapped": total_llama,
                    "unanswered": [],
                    "submitted": False,
                    "step_logs": step_logs
                }

            # Guard B: Hard Stop on Review & Submit Page (NEVER auto-submits)
            if await self._is_review_page(page):
                step_logs.append(f"Step {step_idx}: Reached Review page - Hard Stop engaged.")
                return {
                    "status": "Ready for Review",
                    "error": None,
                    "fields_filled": total_filled,
                    "llama_mapped": total_llama,
                    "unanswered": [],
                    "submitted": False,
                    "step_logs": step_logs
                }

            # Guard C: Single Resume Upload across wizard
            if not resume_uploaded and os.path.exists("resume.pdf"):
                if await self._try_enterprise_resume_upload(page):
                    resume_uploaded = True
                    total_filled += 1
                    step_logs.append(f"Step {step_idx}: Uploaded resume.pdf.")
                    await self._wait_for_react_stability(page)

            schema = await self._extract_enterprise_schema(page)
            step_hash = await self._get_step_fingerprint(page, schema)

            # Guard D: Anti-Infinite Loop (stayed on same page after Next)
            if step_hash in visited_hashes:
                val_errors = await self._scrape_validation_errors(page)
                err_txt = "; ".join(val_errors) if val_errors else "Portal blocked progression (check screenshot for required fields)."
                return {
                    "status": "Manual Action Needed",
                    "error": f"Blocked on Step {step_idx}: {err_txt}",
                    "fields_filled": total_filled,
                    "llama_mapped": total_llama,
                    "unanswered": [],
                    "submitted": False,
                    "step_logs": step_logs
                }
            visited_hashes.add(step_hash)

            s_filled, s_llama, s_unanswered = await self._fill_enterprise_step(page, schema, profile, custom_answers)
            total_filled += s_filled
            total_llama += s_llama
            step_logs.append(f"Step {step_idx}: Filled {s_filled} fields, {len(s_unanswered)} need manual input.")

            if s_unanswered:
                return {
                    "status": "Manual Action Needed",
                    "error": f"Paused on Step {step_idx} for Manual Bridge input ({len(s_unanswered)} remaining).",
                    "fields_filled": total_filled,
                    "llama_mapped": total_llama,
                    "unanswered": s_unanswered,
                    "submitted": False,
                    "step_logs": step_logs
                }

            if not await self._click_next_button(page):
                return {
                    "status": "Manual Action Needed",
                    "error": f"No Next/Continue button found on Step {step_idx}.",
                    "fields_filled": total_filled,
                    "llama_mapped": total_llama,
                    "unanswered": [],
                    "submitted": False,
                    "step_logs": step_logs
                }

        return {
            "status": "Manual Action Needed",
            "error": "Reached MAX_STEPS safety limit.",
            "fields_filled": total_filled,
            "llama_mapped": total_llama,
            "unanswered": [],
            "submitted": False,
            "step_logs": step_logs
        }

    # =========================================================================
    # PUBLIC ENTRYPOINTS
    # =========================================================================
    async def extract_title_and_probe(self, url: str):
        clean_url = sanitize_job_url(url)
        result = {
            "title": "",
            "company": parse_fallback_company(clean_url),
            "role": "Engineering Candidate Role",
            "platform": detect_platform(clean_url),
            "cover_letter_allowed": "No",
            "error": None
        }
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=self.headless, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    viewport={'width': 1280, 'height': 1080}
                )
                page = await context.new_page()
                page = await self._navigate_to_form(page, clean_url)
                await self.stepped_scroll(page)
                page_title = await page.title()
                result["title"] = page_title.strip()
                if "access denied" in page_title.lower() or "forbidden" in page_title.lower():
                    result["error"] = "Access Denied by WAF (Akamai/Cloudflare). Manual submission or local Headed mode required."
                else:
                    if " at " in page_title:
                        p_parts = page_title.split(" at ")
                        result["role"] = p_parts[0].strip()
                        result["company"] = p_parts[1].split("-")[0].split("|")[0].strip()
                    elif " - " in page_title:
                        p_parts = page_title.split(" - ")
                        result["company"] = p_parts[0].strip()
                        if len(p_parts) > 1:
                            result["role"] = p_parts[1].strip()
                    elif page_title:
                        result["role"] = page_title.strip()
                await page.screenshot(path=SCREENSHOT_PATH, full_page=True, animations="disabled")
                await browser.close()
        except Exception as e:
            result["error"] = str(e)
        return result

    async def execute_full_application(self, url: str, profile: dict, custom_answers: dict = None, auto_submit: bool = False):
        clean_url = sanitize_job_url(url)
        custom_answers = dict(custom_answers or {})
        result = {
            "status": "In Progress",
            "error": None,
            "fields_filled": 0,
            "unanswered": [],
            "llama_mapped": 0,
            "submitted": False,
            "pipeline": "single_page"
        }
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=self.headless, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    viewport={'width': 1280, 'height': 1080}
                )
                page = await context.new_page()
                page = await self._navigate_to_form(page, clean_url)
                pipeline = await self.classify_pipeline(page, clean_url)
                result["pipeline"] = pipeline

                if pipeline == "enterprise_wizard":
                    wiz_res = await self._execute_enterprise_wizard(page, profile, custom_answers)
                    result.update(wiz_res)
                else:
                    filled, llama_mapped = await self._fill_single_page(page, profile, custom_answers)
                    result["fields_filled"] = filled
                    result["llama_mapped"] = llama_mapped

                    unanswered = await self._scrape_unanswered_questions(page)
                    result["unanswered"] = unanswered

                    if auto_submit and not unanswered:
                        submit_selectors = [
                            "button[type='submit']",
                            "input[type='submit']",
                            "button:has-text('Submit application')",
                            "button:has-text('Submit Application')",
                            "button:has-text('SUBMIT APPLICATION')",
                            "[data-qa='btn-submit']",
                            "#submit_app"
                        ]
                        for sub_sel in submit_selectors:
                            sub_btn = await page.query_selector(sub_sel)
                            if sub_btn and await sub_btn.is_visible():
                                await sub_btn.evaluate("el => el.click()")
                                await asyncio.sleep(4)
                                break

                        body_txt = (await page.inner_text("body")).lower()
                        cur_url = page.url.lower()
                        if any(k in body_txt for k in ["application submitted", "thank you for applying", "thanks for applying", "received your application"]) or "confirmation" in cur_url or "thanks" in cur_url:
                            result["submitted"] = True
                            result["status"] = "Submitted"
                        else:
                            result["status"] = "Manual Action Needed"
                    else:
                        result["status"] = "Manual Action Needed" if unanswered else "Pending"

                await asyncio.sleep(2.0)
                await page.screenshot(path=SCREENSHOT_PATH, full_page=True, animations="disabled")
                await browser.close()
        except Exception as e:
            result["error"] = str(e)
            result["status"] = "Manual Action Needed"
        return result


def run_async_coro(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)


# =============================================================================
# STREAMLIT UI
# =============================================================================
st.sidebar.title("\u2699\ufe0f Llama 3.3 ATS Engine")
default_groq_key = st.secrets.get("GROQ_API_KEY", "") if hasattr(st, "secrets") else os.environ.get("GROQ_API_KEY", "")
groq_key = st.sidebar.text_input("\U0001f999 Free Groq Llama 3.3 API Key:", value=default_groq_key, type="password")
engine_mode = st.sidebar.radio("Browser Execution Mode:", ["Headless (Cloud Safe)", "Headed (Visible Window - Local Only)"], index=0)
is_headless = "Headless" in engine_mode

profile = load_profile()
with st.sidebar.expander("\U0001f464 Candidate & EEO Profile", expanded=False):
    st.json(profile)

conn = get_db_connection()
total_apps = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
pending_apps = conn.execute("SELECT COUNT(*) FROM applications WHERE status='Pending'").fetchone()[0]
manual_apps = conn.execute("SELECT COUNT(*) FROM applications WHERE status='Manual Action Needed'").fetchone()[0]
review_apps = conn.execute("SELECT COUNT(*) FROM applications WHERE status='Ready for Review'").fetchone()[0]
submitted_apps = conn.execute("SELECT COUNT(*) FROM applications WHERE status='Submitted'").fetchone()[0]
conn.close()

st.sidebar.markdown(f"**Total Tracked:** `{total_apps}`")
st.sidebar.markdown(f"\U0001f7e2 **Submitted:** `{submitted_apps}`")
st.sidebar.markdown(f"\U0001f535 **Ready for Review:** `{review_apps}`")
st.sidebar.markdown(f"\U0001f7e1 **Pending:** `{pending_apps}`")
st.sidebar.markdown(f"\U0001f534 **Manual Action Needed:** `{manual_apps}`")

with st.sidebar.expander("\U0001f4be Backup / Restore Queue", expanded=False):
    st.download_button(
        "Export Queue Backup (.json)",
        data=export_queue_json(),
        file_name=f"ats_queue_backup_{datetime.now().strftime('%Y%m%d')}.json",
        mime="application/json",
        width="stretch"
    )
    uploaded_backup = st.file_uploader("Upload Backup JSON", type=["json"], key="queue_restore_file")
    if uploaded_backup is not None:
        if st.button("Restore Uploaded Backup", width="stretch"):
            restored = import_queue_json(uploaded_backup.getvalue())
            st.success(f"Restored {restored} applications.")
            st.rerun()

if st.sidebar.button("\U0001f9f9 Purge Database (Full Reset)", width="stretch"):
    conn = get_db_connection()
    conn.execute("DELETE FROM applications")
    conn.commit()
    conn.close()
    if os.path.exists(SCREENSHOT_PATH):
        try:
            os.remove(SCREENSHOT_PATH)
        except Exception:
            pass
    st.rerun()

st.title("\u26a1 Meta Llama 3.3 ATS Auto-Applier")
st.caption("Pipeline 1 (Single-Page ATS) + Pipeline 2 (Enterprise Multi-Step Wizard w/ Hard Stop)")

tab1, tab2, tab3 = st.tabs(["1. Add Jobs to Queue", "2. Process Queue & Llama Bridge", "3. Application Tracker"])

with tab1:
    st.subheader("1. Add Applications to Queue")
    input_url = st.text_input("Paste Direct Job Requisition Link (Lever, Greenhouse, Workday, Phenom, Ashby):")
    col_a, col_b = st.columns([1, 1])
    with col_a:
        custom_company = st.text_input("Override Company Name (Optional):")
    with col_b:
        custom_role = st.text_input("Override Role Title (Optional):")

    if st.button("\U0001f4be Auto-Extract & Add to Queue", width="stretch"):
        clean_u = sanitize_job_url(input_url)
        if not clean_u:
            st.error("Please enter a valid job URL.")
        else:
            with st.spinner("Sanitizing URL, probing portal, and detecting pipeline..."):
                automator = ATSAutomator(headless=is_headless, groq_api_key=groq_key)
                extracted = run_async_coro(automator.extract_title_and_probe(clean_u))
                final_company = custom_company.strip() or extracted["company"]
                final_role = custom_role.strip() or extracted["role"]
                try:
                    conn = get_db_connection()
                    conn.execute("""
                        INSERT INTO applications (company, role, url, platform, status, cover_letter_allowed, notes, unanswered_json, custom_answers_json)
                        VALUES (?, ?, ?, ?, 'Pending', ?, ?, '[]', '{}')
                    """, (final_company, final_role, clean_u, extracted["platform"], extracted["cover_letter_allowed"], extracted["error"] or "Ready"))
                    conn.commit()
                    conn.close()
                    st.success(f"Added to Queue: **{final_role}** at **{final_company}** ({extracted['platform']})")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.warning("This job URL is already in your database.")

    with st.expander("\U0001f4cb Bulk Paste Multiple Summer Links (1 URL per line)", expanded=False):
        bulk_urls = st.text_area("Paste URLs here (tracking params stripped automatically):", height=140)
        if st.button("Add All Valid Links to Queue", width="stretch"):
            lines = [sanitize_job_url(l) for l in bulk_urls.splitlines() if l.strip().startswith("http")]
            added_cnt = 0
            conn = get_db_connection()
            for u in lines:
                try:
                    conn.execute("""
                        INSERT INTO applications (company, role, url, platform, status, cover_letter_allowed, notes, unanswered_json, custom_answers_json)
                        VALUES (?, ?, ?, ?, 'Pending', 'Unknown', 'Queued via Bulk Import', '[]', '{}')
                    """, (parse_fallback_company(u), "Summer 2027 Engineering Role", u, detect_platform(u)))
                    added_cnt += 1
                except sqlite3.IntegrityError:
                    pass
            conn.commit()
            conn.close()
            st.success(f"Queued {added_cnt} new links.")
            if added_cnt > 0:
                st.rerun()

with tab2:
    st.subheader("2. Zero-Touch Execution & Llama Question Bridge")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, company, role, url, platform, status, cover_letter_allowed, notes, unanswered_json, custom_answers_json FROM applications WHERE status IN ('Pending', 'Manual Action Needed', 'Ready for Review')")
    pending_rows = c.fetchall()
    conn.close()

    if not pending_rows:
        st.info("\U0001f389 Your actionable queue is empty!")
    else:
        job_options = {f"#{row[0]} | {row[1]} - {row[2]} [{row[5]}]": row for row in pending_rows}
        selected_label = st.selectbox("Select Job to Process:", list(job_options.keys()))
        job = job_options[selected_label]
        job_id, company, role, url, platform, status, cl_allowed, notes, unanswered_raw, custom_ans_raw = job
        unanswered_list = json.loads(unanswered_raw) if unanswered_raw else []
        saved_custom_answers = json.loads(custom_ans_raw) if custom_ans_raw else {}

        st.markdown(f"""
        <div style="background: rgba(15, 23, 42, 0.85); border: 1px solid #334155; padding: 1.25rem; border-radius: 12px; margin-bottom: 1rem;">
            <h3 style="margin-top:0; color:#38bdf8;">{role}</h3>
            <h4 style="color:#94a3b8; margin-bottom: 0.4rem;">{company} &bull; <span style="color:#a855f7;">{platform}</span></h4>
            <p style="font-size: 0.85rem; color:#cbd5e1; margin-bottom: 0;"><b>Diagnostics:</b> {notes}</p>
        </div>
        """, unsafe_allow_html=True)

        col_link, col_del = st.columns([3, 1])
        with col_link:
            st.markdown(f'<a href="{url}" target="_blank" style="display:inline-block; width:100%; text-align:center; padding: 0.6rem 1rem; background-color: #0284c7; color: white; border-radius: 8px; font-weight: bold; text-decoration: none;">\U0001f517 Open Job Application in New Tab \u2197</a>', unsafe_allow_html=True)
        with col_del:
            if st.button("\U0001f5d1\ufe0f Delete Job", key=f"del_{job_id}", width="stretch"):
                conn = get_db_connection()
                conn.execute("DELETE FROM applications WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                st.rerun()

        c1, c2 = st.columns(2)
        with c1:
            if st.button("\U0001f50d Preview & Verify Data (No Submit)", width="stretch"):
                with st.spinner("Running routed ATS pipeline and capturing preview..."):
                    automator = ATSAutomator(headless=is_headless, groq_api_key=groq_key)
                    res = run_async_coro(automator.execute_full_application(url, profile, saved_custom_answers, auto_submit=False))
                    conn = get_db_connection()
                    pipe_tag = f"[{res.get('pipeline', 'single_page')}]"
                    detail = res["error"] if res.get("error") else f"Verified {res['fields_filled']} fields. Unanswered: {len(res['unanswered'])}"
                    new_notes = f"{pipe_tag} {detail}"
                    new_stat = res["status"] if res["status"] in ["Ready for Review", "Manual Action Needed"] else "Manual Action Needed"
                    conn.execute("UPDATE applications SET status=?, notes=?, unanswered_json=? WHERE id=?", (new_stat, new_notes, json.dumps(res["unanswered"]), job_id))
                    conn.commit()
                    conn.close()
                    st.rerun()
        with c2:
            if st.button("\U0001f680 FINAL: Submit Application", type="primary", width="stretch"):
                with st.spinner("Executing final pipeline (Enterprise wizards pause at Review Hard Stop)..."):
                    automator = ATSAutomator(headless=is_headless, groq_api_key=groq_key)
                    res = run_async_coro(automator.execute_full_application(url, profile, saved_custom_answers, auto_submit=True))
                    conn = get_db_connection()
                    pipe_tag = f"[{res.get('pipeline', 'single_page')}]"
                    detail = res["error"] if res.get("error") else f"Processed {res['fields_filled']} fields ({res['status']})."
                    new_notes = f"{pipe_tag} {detail}"
                    conn.execute("UPDATE applications SET status=?, notes=?, unanswered_json=? WHERE id=?", (res["status"], new_notes, json.dumps(res["unanswered"]), job_id))
                    conn.commit()
                    conn.close()
                    st.rerun()

        if unanswered_list:
            st.markdown("---")
            st.warning(f"\u26a0\ufe0f **Manual Input Needed ({len(unanswered_list)} Questions):** Select/type your answers below.")
            with st.form(key=f"bridge_form_{job_id}"):
                updated_custom = dict(saved_custom_answers)
                for idx, q_item in enumerate(unanswered_list):
                    q_lbl = q_item.get("label", f"Question {idx+1}")
                    q_type = q_item.get("type", "text")
                    q_opts = q_item.get("options", [])
                    default_val = updated_custom.get(q_lbl, "")

                    if q_type == "multiselect" and q_opts:
                        chosen = st.multiselect(f"\U0001f4cc {q_lbl}", q_opts, default=default_val if isinstance(default_val, list) else [], key=f"q_{job_id}_{idx}")
                        updated_custom[q_lbl] = chosen
                    elif q_type in ["select", "radio"] and q_opts:
                        opts_with_blank = ["-- Select Answer --"] + q_opts
                        sel_idx = opts_with_blank.index(default_val) if default_val in opts_with_blank else 0
                        chosen = st.selectbox(f"\U0001f4cc {q_lbl}", opts_with_blank, index=sel_idx, key=f"q_{job_id}_{idx}")
                        if chosen != "-- Select Answer --":
                            updated_custom[q_lbl] = chosen
                    else:
                        typed = st.text_area(f"\U0001f4cc {q_lbl}", value=default_val if isinstance(default_val, str) else "", key=f"q_{job_id}_{idx}", height=100)
                        if typed.strip():
                            updated_custom[q_lbl] = typed.strip()

                if st.form_submit_button("\U0001f4be Save Answers & Preview Again", width="stretch"):
                    with st.spinner("Saving answers and generating fresh preview screenshot..."):
                        automator = ATSAutomator(headless=is_headless, groq_api_key=groq_key)
                        res = run_async_coro(automator.execute_full_application(url, profile, updated_custom, auto_submit=False))
                        conn = get_db_connection()
                        new_notes = f"[{res.get('pipeline', 'single_page')}] Injected custom answers. Remaining unanswered: {len(res['unanswered'])}"
                        conn.execute("UPDATE applications SET status=?, notes=?, unanswered_json=?, custom_answers_json=? WHERE id=?", (res["status"], new_notes, json.dumps(res["unanswered"]), json.dumps(updated_custom), job_id))
                        conn.commit()
                        conn.close()
                        st.rerun()

        st.markdown("---")
        if os.path.exists(SCREENSHOT_PATH):
            st.markdown("##### \U0001f4f8 Full-Page Preview Screenshot:")
            st.image(SCREENSHOT_PATH, width="stretch")

with tab3:
    st.subheader("3. Comprehensive Application Tracker")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, company, role, platform, status, url, notes, created_at FROM applications ORDER BY id DESC")
    all_rows = c.fetchall()
    conn.close()
    if not all_rows:
        st.info("No applications in the tracker yet.")
    else:
        for item in all_rows:
            i_id, i_comp, i_role, i_plat, i_stat, i_url, i_notes, i_date = item
            stat_color = "#3b82f6" if i_stat == "Pending" else "#22c55e" if i_stat == "Submitted" else "#06b6d4" if i_stat == "Ready for Review" else "#eab308"
            with st.container():
                c_info, c_btn1, c_btn2 = st.columns([6, 1.5, 1.5])
                with c_info:
                    st.markdown(f"**#{i_id} &bull; {i_comp}** &mdash; *{i_role}* <span style='color:{stat_color}; font-weight:bold;'>[{i_stat}]</span>", unsafe_allow_html=True)
                with c_btn1:
                    if i_stat != 'Submitted':
                        if st.button("\u2705 Done", key=f"tbl_done_{i_id}", width="stretch"):
                            conn = get_db_connection()
                            conn.execute("UPDATE applications SET status='Submitted' WHERE id=?", (i_id,))
                            conn.commit()
                            conn.close()
                            st.rerun()
                with c_btn2:
                    if st.button("\U0001f5d1\ufe0f Del", key=f"tbl_del_{i_id}", width="stretch"):
                        conn = get_db_connection()
                        conn.execute("DELETE FROM applications WHERE id=?", (i_id,))
                        conn.commit()
                        conn.close()
                        st.rerun()
                st.markdown("<hr style='margin: 0.4rem 0; border-color: #1e293b;'/>", unsafe_allow_html=True)