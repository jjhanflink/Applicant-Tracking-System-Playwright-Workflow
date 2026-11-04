import os
import re
import json
import sqlite3
import asyncio
import subprocess
import sys
import requests
from datetime import datetime
from urllib.parse import urlparse
import streamlit as st
from playwright.async_api import async_playwright

def ensure_playwright_browsers():
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    except Exception as e:
        print(f"Error ensuring Playwright browsers: {e}")

ensure_playwright_browsers()

DB_FILE = "ats_queue.db"
PROFILE_FILE = "candidate_profile.json"
SCREENSHOT_PATH = "latest_view.png"

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
    if "unanswered_json" not in existing_cols:
        c.execute("ALTER TABLE applications ADD COLUMN unanswered_json TEXT DEFAULT '[]'")
    if "custom_answers_json" not in existing_cols:
        c.execute("ALTER TABLE applications ADD COLUMN custom_answers_json TEXT DEFAULT '{}'")
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect(DB_FILE)

def load_profile():
    default_profile = {
        "full_name": "Joshua Jason Hanflink",
        "signature_name": "Joshua Hanflink",
        "first_name": "Joshua",
        "middle_name": "Jason",
        "last_name": "Hanflink",
        "email": "jjhanflink@gmail.com",
        "phone": "3369296685",
        "location": "Greensboro, NC, USA",
        "city": "Greensboro",
        "state": "North Carolina",
        "zip": "27410",
        "linkedin": "http://www.linkedin.com/in/joshua-hanflink",
        "github": "https://github.com/jjhanflink",
        "portfolio": "https://jjhanflink.github.io",
        "school": "North Carolina State University",
        "degree": "B.S. in Electrical Engineering & Computer Engineering",
        "major": "Electrical and Computer Engineering",
        "degree_level": "Bachelor's degree",
        "academic_year": "Senior",
        "gpa": "3.5",
        "degree_start": "08/2023",
        "grad_date": "12/2027",
        "preferred_location": "Dallas, Texas",
        "work_location_ack": "Dallas, Texas",
        "us_citizen_status": "A United States Citizen",
        "how_did_you_hear": "Campus visit",
        "technical_accomplishments": "1) Automated TCAD microfabrication workflow with Python & Streamlit converting GDSII layouts directly into simulation decks. 2) Hardware-software integration in the Active Robotic Sensing Lab developing 3D imaging, lighting, and embedded sensor pipelines.",
        "mission_alignment": "I am deeply inspired by Shield AI's mission to protect service members through autonomous systems, and I bring a rigorous engineering work ethic and collaborative mindset to advance mission-critical hardware.",
        "gender": "Male",
        "ethnicity": "Hispanic or Latino",
        "race_fallback": "White (Not Hispanic or Latino)",
        "veteran_status": "I am not a protected veteran",
        "disability_status": "Yes, I have a disability, or have had one in the past",
        "us_person": "Yes",
        "work_authorized": "Yes",
        "sponsorship_needed": "No",
        "relatives_employed": "No",
        "work_onsite": "Yes",
        "relocate": "Yes",
        "over_18": "Yes",
        "today_date": datetime.now().strftime("%m/%d/%Y")
    }
    if os.path.exists(PROFILE_FILE):
        try:
            with open(PROFILE_FILE, "r", encoding="utf-8") as f:
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
1. Ethnicity/Race: ALWAYS choose 'Hispanic or Latino' if available. Fallback to 'White' only if Hispanic is not present.
2. Degree: Choose 'Bachelor\'s degree' or 'B.S.'.
3. Location: Prefer 'Dallas, Texas' if listed in options, or the primary job location.
4. Citizenship / Work Auth / US Person: Choose 'A United States Citizen' or 'Yes'.
5. Visa Sponsorship / Relatives: Choose 'No'.
6. Academic Year: 'Senior'. Overall GPA: '3.5' or numeric bracket containing 3.5.
7. Degree Start: '08/2023'. Degree Completion: '12/2027'.
8. How did you hear: 'Campus visit' or 'University Career Center/Handshake'.
9. Checkboxes for Authorization, Certifications, Terms, Proprietary Info: Return 'CHECK'.
10. Conditional follow-up text fields (e.g., 'If yes, please specify', 'If you selected Other...', 'Optional Comments on GPA'): Return '' (blank).
11. Return exact option string from the 'options' list for dropdowns or radio groups.

Questions:
{json.dumps(form_schema, indent=2)}

Return ONLY a JSON mapping the question 'qid' to the string answer."""

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
            timeout=20
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
        for sel in ["button:has-text('Accept All')", "button:has-text('Accept all')", "button:has-text('Accept')", "#accept-cookies", ".cookie-accept"]:
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
                return
            except Exception:
                pass

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
        await self.accept_cookies(page)

        has_form = await page.query_selector("input[type='text'], input[type='email'], input[name='name']")
        if not has_form:
            for sel in ["a:has-text('Apply for this job')", "a:has-text('Apply Now')", "button:has-text('Apply Now')", ".postings-btn"]:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    try:
                        await btn.evaluate("el => el.click()")
                        await asyncio.sleep(3)
                        break
                    except Exception:
                        pass

    async def _extract_clean_dom_schema(self, page):
        """Extracts the real question prompts using Lever's specific .application-label hierarchy."""
        schema = await page.evaluate("""() => {
            const results = [];
            let qCounter = 0;

            function extractPrompt(container, fallbackEl) {
                if (!container) return fallbackEl.name || fallbackEl.placeholder || 'Question';
                const labelEl = container.querySelector('.application-label, .text, label, legend, .section-headline, b, h4');
                if (labelEl) {
                    const t = labelEl.innerText.replace(/\\s+/g, ' ').trim();
                    if (t.length > 2) return t.slice(0, 200);
                }
                const clone = container.cloneNode(true);
                clone.querySelectorAll('select, input, ul, .application-field, .options-list').forEach(n => n.remove());
                const clean = clone.innerText.replace(/\\s+/g, ' ').trim();
                return clean.length > 2 ? clean.slice(0, 200) : (fallbackEl.name || 'Question');
            }

            // 1. Process all .application-question or field containers
            const questionBlocks = document.querySelectorAll('.application-question, .custom-question, fieldset, li.application-question');
            questionBlocks.forEach(block => {
                const selects = block.querySelectorAll('select');
                selects.forEach(sel => {
                    const qid = 'q_' + (qCounter++);
                    sel.setAttribute('data-ats-qid', qid);
                    const opts = Array.from(sel.options).map(o => o.text.trim()).filter(t => t && !['select...', 'select one', '--'].includes(t.toLowerCase()));
                    const curTxt = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text.trim() : '';
                    const isFilled = sel.value && curTxt && !['select...', 'select one', '--'].includes(curTxt.toLowerCase());
                    results.push({
                        qid: qid,
                        type: 'select',
                        label: extractPrompt(block, sel),
                        options: opts,
                        filled: Boolean(isFilled)
                    });
                });

                const radios = block.querySelectorAll('input[type="radio"]');
                if (radios.length > 0) {
                    const qid = 'q_' + (qCounter++);
                    const opts = [];
                    let isFilled = false;
                    radios.forEach(rad => {
                        rad.setAttribute('data-ats-qid', qid);
                        if (rad.checked) isFilled = true;
                        const rLbl = rad.closest('label') || document.querySelector(`label[for="${rad.id}"]`);
                        const oTxt = rLbl ? rLbl.innerText.trim().split('\\n')[0] : rad.value;
                        if (oTxt) opts.push(oTxt);
                    });
                    results.push({
                        qid: qid,
                        type: 'radio',
                        label: extractPrompt(block, radios[0]),
                        options: opts,
                        filled: isFilled
                    });
                }

                const textInputs = block.querySelectorAll('input[type="text"]:not([data-core="true"]), input[type="number"], textarea');
                textInputs.forEach(tInp => {
                    const qid = 'q_' + (qCounter++);
                    tInp.setAttribute('data-ats-qid', qid);
                    results.push({
                        qid: qid,
                        type: 'text',
                        label: extractPrompt(block, tInp),
                        options: [],
                        filled: Boolean(tInp.value && tInp.value.trim().length > 0)
                    });
                });
            });

            // 2. Collect standalone legal checkboxes
            document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                const qid = 'q_' + (qCounter++);
                cb.setAttribute('data-ats-qid', qid);
                const cBlock = cb.closest('li, div.application-question, fieldset, label');
                const prompt = extractPrompt(cBlock, cb);
                results.push({
                    qid: qid,
                    type: 'checkbox',
                    label: prompt,
                    options: ['CHECK', 'SKIP'],
                    filled: cb.checked
                });
            });

            return results;
        }""")
        return schema

    async def _fill_single_page(self, page, profile: dict, custom_answers: dict):
        filled = 0
        await self.accept_cookies(page)

        # 1. Upload Resume
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
                        except Exception:
                            pass
                        try:
                            await page.wait_for_selector("text='Analyzing resume'", state="hidden", timeout=15000)
                        except Exception:
                            pass
                        await asyncio.sleep(2.0)
                        break
                except Exception:
                    pass

        await self.stepped_scroll(page)

        # 2. Tag and Fill Core Profile Info
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
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                try:
                    await el.evaluate("el => el.setAttribute('data-core', 'true')")
                    await el.fill(val)
                    filled += 1
                except Exception:
                    pass

        loc_el = await page.query_selector("input[name*='location' i], input[id*='location' i]")
        if loc_el and await loc_el.is_visible():
            try:
                await loc_el.evaluate("el => el.setAttribute('data-core', 'true')")
                await loc_el.click()
                await loc_el.fill("")
                await loc_el.type(profile.get("location", "Greensboro, NC, USA"), delay=45)
                await asyncio.sleep(1.2)
                await loc_el.press("ArrowDown")
                await loc_el.press("Enter")
                await loc_el.press("Tab")
                filled += 1
            except Exception:
                pass

        # 3. Extract and Process the Question Blocks
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

            # Check user-saved custom answers first
            for c_q, c_v in custom_answers.items():
                if c_v and (c_q.lower()[:30] in low or low[:30] in c_q.lower()):
                    qid_answers[qid] = c_v
                    break
            if qid in qid_answers:
                continue

            # Deterministic mapping based on verified clean prompts
            if "which location are you applying for" in low or "location" in low and opts:
                for target in ["dallas", "remote", "austin"]:
                    for o in opts:
                        if target in o.lower():
                            qid_answers[qid] = o
                            break
                    if qid in qid_answers:
                        break

            elif "school or university" in low:
                for o in opts:
                    if "north carolina state" in o.lower() or "nc state" in o.lower():
                        qid_answers[qid] = o
                        break

            elif "degree" in low and "program" not in low and ("level" in low or "type" in low or opts):
                for o in opts:
                    if "bachelor" in o.lower() or "b.s." in o.lower():
                        qid_answers[qid] = o
                        break

            elif "gpa" in low:
                if qtype in ["select", "radio"]:
                    for o in opts:
                        if "3.5" in o or "3.5 - 3.9" in o or "3.5 - 4.0" in o or "3.0 - 3.5" in o:
                            qid_answers[qid] = o
                            break
                elif qtype == "text":
                    if not any(skip in low for skip in ["comment", "explain", "optional"]):
                        qid_answers[qid] = profile.get("gpa", "3.5")

            elif "how did you hear" in low or "campus visit" in low:
                for o in opts:
                    if any(h in o.lower() for h in ["campus visit", "career fair", "university", "handshake"]):
                        qid_answers[qid] = o
                        break

            elif any(k in low for k in ["citizenship", "citizen status", "immigration", "u.s. person"]):
                for o in opts:
                    if "united states citizen" in o.lower() or "u.s. citizen" in o.lower() or o.lower() == "yes":
                        qid_answers[qid] = o
                        break

            elif any(k in low for k in ["when did you begin", "start date"]):
                qid_answers[qid] = profile.get("degree_start", "08/2023")

            elif any(k in low for k in ["expect to complete", "graduation date", "completion date"]):
                qid_answers[qid] = profile.get("grad_date", "12/2027")

            elif "strongest technical accomplishment" in low:
                qid_answers[qid] = profile.get("technical_accomplishments", "")

            elif "mission" in low or "values" in low:
                qid_answers[qid] = profile.get("mission_alignment", "")

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
                elif any(k in low for k in ["sponsor", "visa", "relative", "family member"]):
                    for o in opts:
                        if o.lower() == "no" or o.lower().startswith("no,"):
                            qid_answers[qid] = o
                            break

            elif qtype == "checkbox":
                if any(k in low for k in ["certify", "acknowledge", "agree", "proprietary", "authorization", "terms", "privacy"]):
                    qid_answers[qid] = "CHECK"

            elif qtype == "text":
                if low in ["name", "name *"] or "signature" in low:
                    qid_answers[qid] = profile.get("signature_name", "Joshua Hanflink")
                elif low in ["date", "date *"] or "today's date" in low:
                    qid_answers[qid] = today_str

        # Send remaining unresolved to Llama 3.3
        remaining = [it for it in unfilled_items if it["qid"] not in qid_answers]
        llama_count = 0
        if remaining and self.groq_api_key:
            l_map = ask_llama_to_resolve_all(remaining, profile, self.groq_api_key)
            for k_qid, v_ans in l_map.items():
                if v_ans and v_ans != "NEEDS_HUMAN":
                    qid_answers[k_qid] = v_ans
                    llama_count += 1

        # Apply Answers by data-ats-qid
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
                        matched = await page.evaluate("""([el, target]) => {
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
                    clicked = await page.evaluate("""([qid, target]) => {
                        const tLow = target.toLowerCase().trim();
                        const radios = document.querySelectorAll(`input[type="radio"][data-ats-qid="${qid}"]`);
                        for (const r of radios) {
                            const lbl = r.closest('label') || document.querySelector(`label[for="${r.id}"]`);
                            const oTxt = (lbl ? lbl.innerText : (r.value || '')).toLowerCase().trim();
                            const firstLine = oTxt.split('\\n')[0].trim();
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
                    "label": lbl[:140],
                    "type": "select" if item["type"] in ["select", "radio"] else "text",
                    "options": item.get("options", [])[:25]
                })
        return unanswered

    async def extract_title_and_probe(self, url: str):
        result = {
            "title": "",
            "company": parse_fallback_company(url),
            "role": "Engineering Candidate Role",
            "platform": detect_platform(url),
            "cover_letter_allowed": "No",
            "error": None
        }
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
                await self._navigate_to_form(page, url)
                await self.stepped_scroll(page)

                page_title = await page.title()
                result["title"] = page_title.strip()

                if "access denied" in page_title.lower() or "forbidden" in page_title.lower():
                    result["error"] = "Access Denied by WAF (Akamai/Cloudflare). Manual submission required."
                else:
                    if " at " in page_title:
                        p_parts = page_title.split(" at ")
                        result["role"] = p_parts[0].strip()
                        result["company"] = p_parts[1].split("-")[0].split("|")[0].strip()
                    elif " - " in page_title:
                        p_parts = page_title.split(" - ")
                        result["company"] = p_parts[0].strip()
                        result["role"] = p_parts[1].strip()
                    elif page_title:
                        result["role"] = page_title.strip()

                await page.screenshot(path=SCREENSHOT_PATH, full_page=True)
                await browser.close()
        except Exception as e:
            result["error"] = str(e)
        return result

    async def execute_full_application(self, url: str, profile: dict, custom_answers: dict = None, auto_submit: bool = True):
        custom_answers = dict(custom_answers or {})
        result = {
            "status": "In Progress",
            "error": None,
            "fields_filled": 0,
            "unanswered": [],
            "llama_mapped": 0,
            "submitted": False
        }
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
                await self._navigate_to_form(page, url)

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

                await asyncio.sleep(1.0)
                await page.screenshot(path=SCREENSHOT_PATH, full_page=True)
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

st.set_page_config(page_title="Llama 3.3 ATS Auto-Applier", page_icon="⚡", layout="wide")

st.sidebar.title("⚙️ Llama 3.3 ATS Engine")
default_groq_key = st.secrets.get("GROQ_API_KEY", "") if hasattr(st, "secrets") else os.environ.get("GROQ_API_KEY", "")
groq_key = st.sidebar.text_input("🦙 Free Groq Llama 3.3 API Key:", value=default_groq_key, type="password")

engine_mode = st.sidebar.radio("Browser Execution Mode:", ["Headless (Cloud Safe)", "Headed (Visible Window - Local Only)"], index=0)
is_headless = "Headless" in engine_mode

profile = load_profile()
with st.sidebar.expander("👤 Candidate & EEO Profile", expanded=False):
    st.json(profile)

conn = get_db_connection()
total_apps = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
pending_apps = conn.execute("SELECT COUNT(*) FROM applications WHERE status='Pending'").fetchone()[0]
manual_apps = conn.execute("SELECT COUNT(*) FROM applications WHERE status='Manual Action Needed'").fetchone()[0]
submitted_apps = conn.execute("SELECT COUNT(*) FROM applications WHERE status='Submitted'").fetchone()[0]
conn.close()

st.sidebar.markdown(f"**Total Tracked:** `{total_apps}`")
st.sidebar.markdown(f"🟢 **Submitted:** `{submitted_apps}`")
st.sidebar.markdown(f"🟡 **Pending:** `{pending_apps}`")
st.sidebar.markdown(f"🔴 **Manual Action Needed:** `{manual_apps}`")

if st.sidebar.button("🧹 Purge Database (Full Reset)"):
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

st.title("⚡ Meta Llama 3.3 ATS Auto-Applier & Question Bridge")
st.caption("Clean .application-label Hierarchy • Shield AI & Lever Multi-Choice Calibration")

tab1, tab2, tab3 = st.tabs(["1. Add Jobs to Queue", "2. Process Queue & Llama Bridge", "3. Application Tracker"])

with tab1:
    st.subheader("1. Add Applications to Queue")
    input_url = st.text_input("Paste Direct Job Requisition Link (Lever, Greenhouse, Workday, Ashby):")
    col_a, col_b = st.columns([1, 1])
    with col_a:
        custom_company = st.text_input("Override Company Name (Optional):")
    with col_b:
        custom_role = st.text_input("Override Role Title (Optional):")

    if st.button("💾 Auto-Extract & Add to Queue", use_container_width=True):
        if not input_url.strip():
            st.error("Please enter a valid job URL.")
        else:
            with st.spinner("Probing URL and resolving application form..."):
                automator = ATSAutomator(headless=is_headless, groq_api_key=groq_key)
                extracted = run_async_coro(automator.extract_title_and_probe(input_url.strip()))

                final_company = custom_company.strip() or extracted["company"]
                final_role = custom_role.strip() or extracted["role"]
                final_platform = extracted["platform"]
                cl_status = extracted["cover_letter_allowed"]
                notes = extracted["error"] if extracted["error"] else "Form ready for Llama 3.3 Zero-Touch execution"

                try:
                    conn = get_db_connection()
                    conn.execute("""
                        INSERT INTO applications (company, role, url, platform, status, cover_letter_allowed, notes, unanswered_json, custom_answers_json)
                        VALUES (?, ?, ?, ?, 'Pending', ?, ?, '[]', '{}')
                    """, (final_company, final_role, input_url.strip(), final_platform, cl_status, notes))
                    conn.commit()
                    conn.close()
                    st.success(f"Added to Queue: **{final_role}** at **{final_company}**")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.warning("This job URL is already in your database.")

with tab2:
    st.subheader("2. Zero-Touch Cloud Execution & Llama Question Bridge")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT id, company, role, url, platform, status, cover_letter_allowed, notes, unanswered_json, custom_answers_json
        FROM applications WHERE status IN ('Pending', 'Manual Action Needed')
    """)
    pending_rows = c.fetchall()
    conn.close()

    if not pending_rows:
        st.info("🎉 Your actionable queue is empty! All queued jobs have been submitted.")
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
            <h4 style="color:#94a3b8; margin-bottom: 0.4rem;">{company} • <span style="color:#a855f7;">{platform}</span></h4>
            <p style="font-size: 0.85rem; color:#cbd5e1; margin-bottom: 0.3rem;"><b>Status:</b> <code>{status}</code></p>
            <p style="font-size: 0.85rem; color:#94a3b8; margin-bottom: 0;"><b>Diagnostics:</b> {notes}</p>
        </div>
        """, unsafe_allow_html=True)

        col_link, col_del = st.columns([3, 1])
        with col_link:
            st.markdown(f"""
            <a href="{url}" target="_blank" style="display:inline-block; width:100%; text-align:center; padding: 0.6rem 1rem; background-color: #0284c7; color: white; border-radius: 8px; font-weight: bold; text-decoration: none; font-family: monospace;">
                🔗 Open Job Application in New Tab ↗
            </a>
            """, unsafe_allow_html=True)
        with col_del:
            if st.button("🗑️ Delete Job", key=f"del_{job_id}", use_container_width=True):
                conn = get_db_connection()
                conn.execute("DELETE FROM applications WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                st.rerun()

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("⚡ Llama Auto-Fill & Submit", use_container_width=True):
                with st.spinner("Uploading resume, tagging DOM, running Llama 3.3, and submitting..."):
                    automator = ATSAutomator(headless=is_headless, groq_api_key=groq_key)
                    res = run_async_coro(automator.execute_full_application(url, profile, saved_custom_answers, auto_submit=True))
                    conn = get_db_connection()
                    new_notes = res["error"] if res["error"] else f"Filled {res['fields_filled']} fields (Llama mapped {res['llama_mapped']}). Unanswered: {len(res['unanswered'])}"
                    conn.execute(
                        "UPDATE applications SET status=?, notes=?, unanswered_json=? WHERE id=?",
                        (res["status"], new_notes, json.dumps(res["unanswered"]), job_id)
                    )
                    conn.commit()
                    conn.close()
                    st.rerun()
        with c2:
            if st.button("🔍 Fill & Verify Only (No Submit)", use_container_width=True):
                with st.spinner("Uploading resume, extracting clean questions, and checking fields..."):
                    automator = ATSAutomator(headless=is_headless, groq_api_key=groq_key)
                    res = run_async_coro(automator.execute_full_application(url, profile, saved_custom_answers, auto_submit=False))
                    conn = get_db_connection()
                    new_notes = f"Verified {res['fields_filled']} fields (Llama mapped {res['llama_mapped']}). Unanswered: {len(res['unanswered'])}"
                    conn.execute(
                        "UPDATE applications SET status='Manual Action Needed', notes=?, unanswered_json=? WHERE id=?",
                        (new_notes, json.dumps(res["unanswered"]), job_id)
                    )
                    conn.commit()
                    conn.close()
                    st.rerun()
        with c3:
            if st.button("✅ Mark as Submitted", use_container_width=True):
                conn = get_db_connection()
                conn.execute("UPDATE applications SET status='Submitted' WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                st.rerun()

        if unanswered_list:
            st.markdown("---")
            st.warning(f"⚠️ **Manual Input Needed ({len(unanswered_list)} Questions):** Answer below and click **Inject & Submit**.")
            with st.form(key=f"bridge_form_{job_id}"):
                updated_custom = dict(saved_custom_answers)
                for idx, q_item in enumerate(unanswered_list):
                    q_lbl = q_item.get("label", f"Question {idx+1}")
                    q_type = q_item.get("type", "text")
                    q_opts = q_item.get("options", [])
                    default_val = updated_custom.get(q_lbl, "")

                    if q_type == "select" and q_opts:
                        opts_with_blank = ["-- Select Answer --"] + q_opts
                        sel_idx = opts_with_blank.index(default_val) if default_val in opts_with_blank else 0
                        chosen = st.selectbox(f"📌 {q_lbl}", opts_with_blank, index=sel_idx, key=f"q_{job_id}_{idx}")
                        if chosen != "-- Select Answer --":
                            updated_custom[q_lbl] = chosen
                    else:
                        typed = st.text_input(f"📌 {q_lbl}", value=default_val, key=f"q_{job_id}_{idx}")
                        if typed.strip():
                            updated_custom[q_lbl] = typed.strip()

                if st.form_submit_button("⚡ Inject Custom Answers & Auto-Submit Now", use_container_width=True):
                    with st.spinner("Injecting answers and submitting..."):
                        automator = ATSAutomator(headless=is_headless, groq_api_key=groq_key)
                        res = run_async_coro(automator.execute_full_application(url, profile, updated_custom, auto_submit=True))
                        conn = get_db_connection()
                        new_notes = f"Injected {res['fields_filled']} fields. Remaining unanswered: {len(res['unanswered'])}"
                        conn.execute(
                            "UPDATE applications SET status=?, notes=?, unanswered_json=?, custom_answers_json=? WHERE id=?",
                            (res["status"], new_notes, json.dumps(res["unanswered"]), json.dumps(updated_custom), job_id)
                        )
                        conn.commit()
                        conn.close()
                        st.rerun()

        st.markdown("---")
        if os.path.exists(SCREENSHOT_PATH):
            st.markdown("##### 📸 Full-Page Cloud Browser Verification Capture:")
            st.image(SCREENSHOT_PATH, use_container_width=True)

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
            stat_color = "#3b82f6" if i_stat == "Pending" else "#22c55e" if i_stat == "Submitted" else "#eab308"

            with st.container():
                c_info, c_btn1, c_btn2 = st.columns([6, 1.5, 1.5])
                with c_info:
                    st.markdown(f"**#{i_id} • {i_comp}** — *{i_role}* <span style='color:{stat_color}; font-weight:bold;'>[{i_stat}]</span>", unsafe_allow_html=True)
                    st.caption(f"Platform: {i_plat} | Notes: {i_notes} | [View Posting ↗]({i_url})")
                with c_btn1:
                    if i_stat != 'Submitted':
                        if st.button("✅ Done", key=f"tbl_done_{i_id}", use_container_width=True):
                            conn = get_db_connection()
                            conn.execute("UPDATE applications SET status='Submitted' WHERE id=?", (i_id,))
                            conn.commit()
                            conn.close()
                            st.rerun()
                with c_btn2:
                    if st.button("🗑️ Del", key=f"tbl_del_{i_id}", use_container_width=True):
                        conn = get_db_connection()
                        conn.execute("DELETE FROM applications WHERE id=?", (i_id,))
                        conn.commit()
                        conn.close()
                        st.rerun()
                st.markdown("<hr style='margin: 0.4rem 0; border-color: #1e293b;'/>", unsafe_allow_html=True)
