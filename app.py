import os
import re
import json
import sqlite3
import asyncio
from datetime import datetime
from urllib.parse import urlparse
import streamlit as st
from playwright.async_api import async_playwright

DB_FILE = "ats_queue.db"
PROFILE_FILE = "candidate_profile.json"
SCREENSHOT_PATH = "latest_view.png"

# ==========================================
# 1. DATABASE & AUTO-MIGRATION
# ==========================================
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
        "first_name": "Joshua",
        "middle_name": "Jason",
        "last_name": "Hanflink",
        "email": "jjhanflink@gmail.com",
        "phone": "336-929-6685",
        "location": "Raleigh, North Carolina",
        "city": "Raleigh",
        "state": "North Carolina",
        "zip": "27606",
        "linkedin": "https://www.linkedin.com/in/joshua-hanflink",
        "github": "https://github.com/jjhanflink",
        "portfolio": "https://jjhanflink.github.io",
        "school": "North Carolina State University",
        "degree": "B.S. in Electrical Engineering & Computer Engineering",
        "major": "Electrical and Computer Engineering",
        "grad_date": "12/2027",
        "grad_month": "December",
        "grad_year": "2027",
        "gpa": "3.8",
        "gender": "Male",
        "ethnicity": "Hispanic or Latino",
        "race_fallback": "White",
        "veteran_status": "I am not a protected veteran",
        "disability_status": "Yes, I have a disability, or have had one in the past",
        "us_citizen": "Yes",
        "work_authorized": "Yes",
        "sponsorship_needed": "No",
        "work_onsite": "Yes",
        "relocate": "Yes",
        "over_18": "Yes"
    }
    if os.path.exists(PROFILE_FILE):
        try:
            with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                default_profile.update(saved)
        except Exception:
            pass
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
    elif "tesla.com" in u:
        return "Tesla (Akamai WAF Protected)"
    return "Company Direct Portal"

def parse_fallback_company(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    domain = re.sub(r'^(www\.|careers\.|jobs\.|boards\.)', '', domain)
    cand = domain.split('.')[0]
    return cand.capitalize() if cand else "Engineering Employer"

# ==========================================
# 2. ZERO-TOUCH PLAYWRIGHT ENGINE
# ==========================================
class ATSAutomator:
    def __init__(self, headless: bool = True):
        self.headless = headless

    async def accept_cookies(self, page):
        cookie_selectors = [
            "button:has-text('Accept All')",
            "button:has-text('Accept all')",
            "button:has-text('ACCEPT')",
            "button:has-text('Accept Cookies')",
            "button:has-text('Accept')",
            "button:has-text('Allow All')",
            "button:has-text('I Agree')",
            "#onetrust-accept-btn-handler",
            "#accept-cookies",
            ".cookie-accept"
        ]
        for sel in cookie_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.evaluate("el => el.click()")
                    await asyncio.sleep(0.6)
                    break
            except Exception:
                pass

    async def stepped_scroll(self, page):
        try:
            total_height = await page.evaluate("document.body.scrollHeight")
            current_pos = 0
            step = 450
            while current_pos < total_height:
                current_pos += step
                await page.evaluate(f"window.scrollTo(0, {current_pos})")
                await asyncio.sleep(0.2)
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
                return
            except Exception:
                pass

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
        await self.accept_cookies(page)

        if "myworkdayjobs.com" in url:
            wd_selectors = [
                "[data-automation-id='applyExternal']",
                "[data-automation-id='applyWithWorkday']",
                "a[data-automation-id='applyExternal']"
            ]
            for wd_sel in wd_selectors:
                btn = await page.query_selector(wd_sel)
                if btn:
                    try:
                        await btn.evaluate("el => el.click()")
                        await asyncio.sleep(4)
                        return
                    except Exception:
                        pass

        has_form = await page.query_selector("input[type='text'], input[type='email'], input[name='name']")
        if not has_form:
            apply_selectors = [
                "a:has-text('Apply for this job')",
                "a:has-text('Apply Now')",
                "button:has-text('Apply Now')",
                "a:has-text('Apply')",
                "button:has-text('Apply')",
                ".postings-btn",
                "[data-qa='show-page-apply']"
            ]
            for sel in apply_selectors:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    try:
                        await btn.evaluate("el => el.click()")
                        await asyncio.sleep(3)
                        break
                    except Exception:
                        pass

    async def _match_and_select_option(self, select_el, target_keywords, exclude_keywords=None):
        exclude_keywords = exclude_keywords or []
        options = await select_el.query_selector_all("option")
        for opt in options:
            opt_text = (await opt.inner_text()).strip().lower()
            opt_val = await opt.get_attribute("value")
            if not opt_val or opt_text in ["select...", "select one", "choose", "none", "--", ""]:
                continue
            if any(ex in opt_text for ex in exclude_keywords):
                continue
            if any(tk in opt_text for tk in target_keywords):
                await select_el.select_option(value=opt_val)
                return True
        return False

    async def _fill_single_page(self, page, profile: dict, custom_answers: dict):
        filled = 0
        await self.accept_cookies(page)

        # 1. RESUME FIRST (Wait out Lever/Greenhouse parsing lock)
        if os.path.exists("resume.pdf"):
            file_inputs = await page.query_selector_all("input[type='file']")
            for f_in in file_inputs:
                try:
                    parent_txt = (await f_in.evaluate("el => el.closest('div, label, fieldset')?.innerText || ''")).lower()
                    if "cover letter" not in parent_txt:
                        await f_in.set_input_files("resume.pdf")
                        filled += 1
                        try:
                            await page.wait_for_selector("text='Analyzing resume'", state="hidden", timeout=12000)
                        except Exception:
                            pass
                        await asyncio.sleep(1.2)
                        break
                except Exception:
                    pass

        await self.stepped_scroll(page)

        # 2. STANDARD IDENTITY & URL INPUTS
        full_name_el = await page.query_selector("input[name='name'], input[aria-label*='Full Name' i]")
        if full_name_el:
            await full_name_el.fill(profile.get("full_name", "Joshua Jason Hanflink"))
            filled += 1
        else:
            fn = await page.query_selector("input[name*='first_name' i], input[id*='first_name' i], input[aria-label*='First Name' i]")
            if fn:
                await fn.fill(profile.get("first_name", "Joshua"))
                filled += 1
            ln = await page.query_selector("input[name*='last_name' i], input[id*='last_name' i], input[aria-label*='Last Name' i]")
            if ln:
                await ln.fill(profile.get("last_name", "Hanflink"))
                filled += 1

        field_map = [
            (["input[name='email']", "input[type='email']", "input[id*='email' i]"], profile.get("email", "")),
            (["input[name='phone']", "input[type='tel']", "input[id*='phone' i]"], profile.get("phone", "")),
            (["input[name='org']", "input[id*='org' i]", "input[name*='company' i]", "input[aria-label*='Current company' i]", "input[aria-label*='School' i]", "input[name*='school' i]"], profile.get("school", "")),
            (["input[name='urls[LinkedIn]']", "input[name*='linkedin' i]", "input[aria-label*='LinkedIn' i]"], profile.get("linkedin", "")),
            (["input[name='urls[GitHub]']", "input[name*='github' i]", "input[aria-label*='GitHub' i]"], profile.get("github", "")),
            (["input[name='urls[Portfolio]']", "input[name='urls[Other]']", "input[name*='portfolio' i]", "input[name*='website' i]"], profile.get("portfolio", ""))
        ]
        for selectors, val in field_map:
            if not val:
                continue
            for sel in selectors:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    try:
                        await el.fill(val)
                        filled += 1
                        break
                    except Exception:
                        pass

        # Location with autocomplete lock
        loc_el = await page.query_selector("input[name*='location' i], input[id*='location' i], input[aria-label*='location' i]")
        if loc_el and await loc_el.is_visible():
            try:
                await loc_el.fill(profile.get("location", "Raleigh, North Carolina"))
                await asyncio.sleep(0.8)
                await loc_el.press("ArrowDown")
                await loc_el.press("Enter")
                await loc_el.press("Tab")
                filled += 1
            except Exception:
                pass

        # 3. SMART QUESTION & EEO COMPLIANCE MATRIX (<select> Dropdowns)
        selects = await page.query_selector_all("select")
        for s_el in selects:
            try:
                if not await s_el.is_visible():
                    continue
                q_text = (await s_el.evaluate("""el => {
                    const id = el.id;
                    if (id) {
                        const lbl = document.querySelector(`label[for="${id}"]`);
                        if (lbl) return lbl.innerText;
                    }
                    const container = el.closest('div.application-question, div.field, fieldset, label, li, div');
                    return container ? container.innerText : '';
                }""")).strip()
                q_lower = q_text.lower()

                # Check user-supplied custom answers first
                matched_custom = False
                for c_question, c_val in custom_answers.items():
                    if c_val and c_question.lower()[:30] in q_lower:
                        await self._match_and_select_option(s_el, [c_val.lower()])
                        filled += 1
                        matched_custom = True
                        break
                if matched_custom:
                    continue

                # Ethnicity / Race Two-Tier Rule:
                # Priority 1: Select "Hispanic or Latino" if present
                # Priority 2: If asking about Hispanic/Latino as Yes/No, select "Yes"
                # Priority 3: If Race dropdown excludes Hispanic/Latino, select "White"
                if any(k in q_lower for k in ["hispanic", "latino", "ethnicity", "race", "racial"]):
                    found_hisp = await self._match_and_select_option(s_el, ["hispanic", "latino"])
                    if found_hisp:
                        filled += 1
                        continue
                    if "hispanic" in q_lower or "latino" in q_lower:
                        if await self._match_and_select_option(s_el, ["yes"], ["no"]):
                            filled += 1
                            continue
                    if await self._match_and_select_option(s_el, ["white", "caucasian"], ["non-white"]):
                        filled += 1
                        continue

                # Gender -> Male
                elif "gender" in q_lower or "sex" in q_lower:
                    if await self._match_and_select_option(s_el, ["male", "man"], ["female", "woman", "non-binary"]):
                        filled += 1

                # Veteran -> Not a protected veteran
                elif "veteran" in q_lower or "military" in q_lower:
                    if await self._match_and_select_option(s_el, ["not a protected veteran", "not a veteran", "no, i am not", "i am not", "no"], ["yes, i am"]):
                        filled += 1

                # Disability -> Yes, I have or have had a disability
                elif "disability" in q_lower or "handicap" in q_lower:
                    if await self._match_and_select_option(s_el, ["yes, i have a disability", "yes, i have", "have had one in the past", "yes"]):
                        filled += 1

                # Sponsorship -> No
                elif "sponsor" in q_lower or "h-1b" in q_lower or "visa" in q_lower:
                    if await self._match_and_select_option(s_el, ["no"], ["yes"]):
                        filled += 1

                # US Citizen / US Person / Work Authorized / Onsite / Relocate / 18+ -> Yes
                elif any(k in q_lower for k in ["authorized", "legally", "us person", "u.s. person", "citizen", "export", "on-site", "onsite", "relocat", "18 years", "fluent"]):
                    if await self._match_and_select_option(s_el, ["yes", "u.s. citizen", "us citizen", "authorized"], ["no", "not authorized"]):
                        filled += 1

                # Degree / Education
                elif "degree" in q_lower or "education" in q_lower:
                    if await self._match_and_select_option(s_el, ["bachelor", "b.s.", "bs", "undergraduate"]):
                        filled += 1
            except Exception:
                pass

        # 4. RADIO BUTTONS & CHECKBOXES MATRIX
        radios = await page.query_selector_all("input[type='radio'], input[type='checkbox']")
        for r_el in radios:
            try:
                group_txt = (await r_el.evaluate("""el => {
                    const container = el.closest('fieldset, div.application-question, div.field, li, ul, div');
                    return container ? container.innerText : '';
                }""")).lower()
                opt_label = (await r_el.evaluate("""el => {
                    const lbl = el.closest('label') || document.querySelector(`label[for="${el.id}"]`);
                    return lbl ? lbl.innerText : (el.value || '');
                }""")).strip().lower()

                # Custom answer match for radios
                for c_question, c_val in custom_answers.items():
                    if c_val and c_question.lower()[:30] in group_txt and c_val.lower() in opt_label:
                        await r_el.evaluate("el => el.click()")
                        filled += 1

                # Ethnicity / Race Radio Handling
                if any(k in group_txt for k in ["hispanic", "latino", "ethnicity", "race"]):
                    if "hispanic" in opt_label or "latino" in opt_label:
                        await r_el.evaluate("el => el.click()")
                        filled += 1
                    elif ("hispanic" in group_txt or "latino" in group_txt) and opt_label in ["yes", "true"]:
                        await r_el.evaluate("el => el.click()")
                        filled += 1
                    elif "hispanic" not in group_txt and ("white" in opt_label or "caucasian" in opt_label):
                        await r_el.evaluate("el => el.click()")
                        filled += 1

                # Veteran Radio Handling
                elif "veteran" in group_txt:
                    if any(k in opt_label for k in ["not a protected veteran", "not a veteran", "i am not", "no"]):
                        await r_el.evaluate("el => el.click()")
                        filled += 1

                # Disability Radio Handling
                elif "disability" in group_txt:
                    if "yes" in opt_label or "have a disability" in opt_label:
                        await r_el.evaluate("el => el.click()")
                        filled += 1

                # Gender Radio Handling
                elif "gender" in group_txt:
                    if opt_label in ["male", "man"]:
                        await r_el.evaluate("el => el.click()")
                        filled += 1

                # Sponsorship -> No
                elif "sponsor" in group_txt or "visa" in group_txt:
                    if opt_label == "no" or opt_label.startswith("no,"):
                        await r_el.evaluate("el => el.click()")
                        filled += 1

                # US Person / Authorized / Onsite -> Yes
                elif any(k in group_txt for k in ["authorized", "us person", "u.s. person", "citizen", "on-site", "onsite", "relocat", "18 years"]):
                    if opt_label == "yes" or opt_label.startswith("yes,") or "u.s. citizen" in opt_label:
                        await r_el.evaluate("el => el.click()")
                        filled += 1

                # Certify / Consent / Privacy Checkboxes
                elif any(k in group_txt for k in ["certify", "acknowledge", "agree", "privacy", "consent", "accurate"]):
                    if await r_el.get_attribute("type") == "checkbox" and not await r_el.is_checked():
                        await r_el.evaluate("el => el.click()")
                        filled += 1
            except Exception:
                pass

        # 5. TEXT INPUTS FOR DATES, GRADUATION, GPA & CUSTOM ANSWERS
        text_inputs = await page.query_selector_all("input[type='text'], input[type='number'], textarea")
        today_str = datetime.now().strftime("%m/%d/%Y")
        for t_el in text_inputs:
            try:
                if not await t_el.is_visible():
                    continue
                curr_val = (await t_el.input_value()).strip()
                q_label = (await t_el.evaluate("""el => {
                    const lbl = el.closest('label') || document.querySelector(`label[for="${el.id}"]`) || el.closest('div.application-question, div.field, li, div');
                    return lbl ? lbl.innerText : (el.placeholder || el.name || '');
                }""")).strip()
                q_low = q_label.lower()

                # Custom answer injection
                for c_question, c_val in custom_answers.items():
                    if c_val and c_question.lower()[:30] in q_low:
                        await t_el.fill(str(c_val))
                        filled += 1
                        curr_val = str(c_val)
                        break

                if curr_val:
                    continue

                if "graduation" in q_low or "grad date" in q_low or "expected completion" in q_low:
                    await t_el.fill(profile.get("grad_date", "12/2027"))
                    filled += 1
                elif "today's date" in q_low or "signature date" in q_low or ("date" in q_low and "birth" not in q_low and "start" not in q_low):
                    await t_el.fill(today_str)
                    filled += 1
                elif "start date" in q_low or "available" in q_low:
                    await t_el.fill("01/2027")
                    filled += 1
                elif "gpa" in q_low or "grade point" in q_low:
                    await t_el.fill(profile.get("gpa", "3.8"))
                    filled += 1
                elif "signature" in q_low or "full legal name" in q_low:
                    await t_el.fill(profile.get("full_name", "Joshua Jason Hanflink"))
                    filled += 1
                elif "city" in q_low:
                    await t_el.fill(profile.get("city", "Raleigh"))
                    filled += 1
                elif "postal" in q_low or "zip" in q_low:
                    await t_el.fill(profile.get("zip", "27606"))
                    filled += 1
            except Exception:
                pass

        return filled

    async def _scrape_unanswered_questions(self, page):
        unanswered = []
        seen_labels = set()

        # 1. Check unfilled <select> dropdowns
        selects = await page.query_selector_all("select")
        for s_el in selects:
            try:
                if not await s_el.is_visible():
                    continue
                val = await s_el.evaluate("el => el.value")
                sel_text = (await s_el.evaluate("el => el.options[el.selectedIndex]?.text || ''")).strip().lower()
                if not val or sel_text in ["select...", "select one", "choose", "none", "--", ""]:
                    q_label = (await s_el.evaluate("""el => {
                        const lbl = document.querySelector(`label[for="${el.id}"]`) || el.closest('div.application-question, div.field, fieldset, label, li');
                        return lbl ? lbl.innerText.split('\\n')[0] : (el.name || 'Dropdown Question');
                    }""")).strip()
                    opts = await s_el.evaluate("el => Array.from(el.options).map(o => o.text.trim()).filter(t => t && !['select...', 'select one', '--'].includes(t.toLowerCase()))")
                    if q_label and q_label not in seen_labels and opts:
                        seen_labels.add(q_label)
                        unanswered.append({"label": q_label[:120], "type": "select", "options": opts[:25]})
            except Exception:
                pass

        # 2. Check unfilled required text inputs / textareas
        inputs = await page.query_selector_all("input[type='text'], input[type='number'], textarea")
        for i_el in inputs:
            try:
                if not await i_el.is_visible():
                    continue
                val = (await i_el.input_value()).strip()
                if not val:
                    q_label = (await i_el.evaluate("""el => {
                        const lbl = document.querySelector(`label[for="${el.id}"]`) || el.closest('div.application-question, div.field, label, li');
                        return lbl ? lbl.innerText.split('\\n')[0] : (el.placeholder || el.name || '');
                    }""")).strip()
                    if q_label and q_label not in seen_labels and len(q_label) > 2:
                        seen_labels.add(q_label)
                        unanswered.append({"label": q_label[:120], "type": "text", "options": []})
            except Exception:
                pass

        return unanswered

    async def extract_title_and_probe(self, url: str):
        result = {
            "title": "",
            "company": parse_fallback_company(url),
            "role": "Engineering Candidate Role",
            "platform": detect_platform(url),
            "cover_letter_allowed": "No",
            "unanswered": [],
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
                        result["role"] = p_parts[0].strip()
                        result["company"] = p_parts[1].strip()
                    elif page_title:
                        result["role"] = page_title.strip()

                cl_elements = await page.query_selector_all("input[type='file'], textarea, label")
                for el in cl_elements:
                    txt = (await el.inner_text()).lower() if el else ""
                    if "cover letter" in txt:
                        result["cover_letter_allowed"] = "Yes"
                        break

                await page.screenshot(path=SCREENSHOT_PATH, full_page=True)
                await browser.close()
        except Exception as e:
            result["error"] = str(e)
            result["company"] = parse_fallback_company(url)
            result["role"] = "Manual Review Required"
        return result

    async def execute_full_application(self, url: str, profile: dict, custom_answers: dict = None, auto_submit: bool = True):
        custom_answers = custom_answers or {}
        result = {
            "status": "In Progress",
            "error": None,
            "fields_filled": 0,
            "unanswered": [],
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

                # Multi-page loop (up to 4 steps for Workday / multi-step portals)
                for _ in range(4):
                    filled = await self._fill_single_page(page, profile, custom_answers)
                    result["fields_filled"] += filled

                    next_btn = await page.query_selector(
                        "button:has-text('Next'), button:has-text('Continue'), button:has-text('Save and Continue'), [data-automation-id='bottom-navigation-next-button']"
                    )
                    if next_btn and await next_btn.is_visible():
                        await next_btn.evaluate("el => el.click()")
                        await asyncio.sleep(3)
                    else:
                        break

                # Scrape any remaining unanswered questions for the Streamlit UI Bridge
                unanswered = await self._scrape_unanswered_questions(page)
                result["unanswered"] = unanswered

                # Attempt Zero-Touch Cloud Submission if requested
                if auto_submit:
                    submit_selectors = [
                        "button[type='submit']",
                        "input[type='submit']",
                        "button:has-text('Submit application')",
                        "button:has-text('Submit Application')",
                        "button:has-text('Submit')",
                        "[data-qa='btn-submit']",
                        "#submit_app",
                        "[data-automation-id='bottom-navigation-next-button']"
                    ]
                    for sub_sel in submit_selectors:
                        sub_btn = await page.query_selector(sub_sel)
                        if sub_btn and await sub_btn.is_visible():
                            await sub_btn.evaluate("el => el.click()")
                            await asyncio.sleep(4)
                            break

                    body_txt = (await page.inner_text("body")).lower()
                    cur_url = page.url.lower()
                    if any(k in body_txt for k in ["application submitted", "thank you for applying", "thanks for applying", "received your application", "successfully submitted"]) or "confirmation" in cur_url or "thanks" in cur_url:
                        result["submitted"] = True
                        result["status"] = "Submitted"
                    elif unanswered:
                        result["status"] = "Manual Action Needed"
                    else:
                        result["status"] = "Manual Action Needed"
                else:
                    result["status"] = "Manual Action Needed" if unanswered else "Pending"

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

# ==========================================
# 3. STREAMLIT USER INTERFACE
# ==========================================
st.set_page_config(page_title="Zero-Touch ATS Auto-Applier", page_icon="⚡", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    .stButton>button { border-radius: 8px; font-family: monospace; font-weight: 600; }
</style>
""", unsafe_allow_html=True)

st.sidebar.title("⚙️ Zero-Touch ATS Engine")
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
    st.sidebar.success("Database wiped clean!")
    st.rerun()

st.title("⚡ Zero-Touch ATS Auto-Applier & Dynamic Question Bridge")
st.caption("Automated Cloud Submission • Hispanic/Latino Priority EEO Matrix • Live Unanswered Question Scraper")

tab1, tab2, tab3 = st.tabs(["1. Add Jobs to Queue", "2. Process Queue & Dynamic Bridge", "3. Application Tracker"])

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
                automator = ATSAutomator(headless=is_headless)
                extracted = run_async_coro(automator.extract_title_and_probe(input_url.strip()))

                final_company = custom_company.strip() or extracted["company"]
                final_role = custom_role.strip() or extracted["role"]
                final_platform = extracted["platform"]
                cl_status = extracted["cover_letter_allowed"]
                notes = extracted["error"] if extracted["error"] else "Form ready for Zero-Touch execution"

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
    st.subheader("2. Zero-Touch Cloud Execution & Unanswered Question Bridge")

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
            <p style="font-size: 0.85rem; color:#cbd5e1; margin-bottom: 0.3rem;"><b>Status:</b> <code>{status}</code> | <b>Cover Letter Allowed:</b> {cl_allowed}</p>
            <p style="font-size: 0.85rem; color:#94a3b8; margin-bottom: 0;"><b>Diagnostics:</b> {notes}</p>
        </div>
        """, unsafe_allow_html=True)

        col_link, col_del = st.columns([3, 1])
        with col_link:
            st.markdown(f"""
            <a href="{url}" target="_blank" style="display:inline-block; width:100%; text-align:center; padding: 0.6rem 1rem; background-color: #0284c7; color: white; border-radius: 8px; font-weight: bold; text-decoration: none; font-family: monospace;">
                🔗 Open Job Application in New Tab (Fallback) ↗
            </a>
            """, unsafe_allow_html=True)
        with col_del:
            if st.button("🗑️ Delete Job", key=f"del_{job_id}", use_container_width=True):
                conn = get_db_connection()
                conn.execute("DELETE FROM applications WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                st.rerun()

        st.markdown("#### 🚀 Primary Zero-Touch Cloud Controls")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("⚡ Zero-Touch Auto-Fill & Submit", use_container_width=True):
                with st.spinner("Executing Zero-Touch Cloud Fill, EEO Matrix, and Auto-Submit..."):
                    automator = ATSAutomator(headless=is_headless)
                    res = run_async_coro(automator.execute_full_application(url, profile, saved_custom_answers, auto_submit=True))
                    conn = get_db_connection()
                    new_notes = res["error"] if res["error"] else f"Filled {res['fields_filled']} fields. Unanswered: {len(res['unanswered'])}"
                    conn.execute(
                        "UPDATE applications SET status=?, notes=?, unanswered_json=? WHERE id=?",
                        (res["status"], new_notes, json.dumps(res["unanswered"]), job_id)
                    )
                    conn.commit()
                    conn.close()
                    st.rerun()
        with c2:
            if st.button("🔍 Fill & Scan Only (No Submit)", use_container_width=True):
                with st.spinner("Filling standard fields and scanning for unanswered questions..."):
                    automator = ATSAutomator(headless=is_headless)
                    res = run_async_coro(automator.execute_full_application(url, profile, saved_custom_answers, auto_submit=False))
                    conn = get_db_connection()
                    new_notes = f"Scanned {res['fields_filled']} fields. Found {len(res['unanswered'])} custom questions."
                    conn.execute(
                        "UPDATE applications SET status='Manual Action Needed', notes=?, unanswered_json=? WHERE id=?",
                        (new_notes, json.dumps(res["unanswered"]), job_id)
                    )
                    conn.commit()
                    conn.close()
                    st.rerun()
        with c3:
            if st.button("✅ Manually Mark as Submitted", use_container_width=True):
                conn = get_db_connection()
                conn.execute("UPDATE applications SET status='Submitted' WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                st.rerun()

        # DYNAMIC REMOTE-DOM BRIDGE FOR MANUAL ACTION NEEDED
        if unanswered_list:
            st.markdown("---")
            st.warning(f"⚠️ **Manual Input Needed ({len(unanswered_list)} Questions Detected):** Answer the scraped questions below and click **Inject Custom Answers & Auto-Submit**.")
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
                    with st.spinner("Re-launching cloud browser, injecting custom answers, and clicking Submit..."):
                        automator = ATSAutomator(headless=is_headless)
                        res = run_async_coro(automator.execute_full_application(url, profile, updated_custom, auto_submit=True))
                        conn = get_db_connection()
                        new_notes = f"Custom answers injected ({res['fields_filled']} fields). Remaining unanswered: {len(res['unanswered'])}"
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
