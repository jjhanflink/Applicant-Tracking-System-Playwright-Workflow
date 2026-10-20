import os
import re
import json
import sqlite3
import asyncio
from urllib.parse import urlparse
import streamlit as st
from playwright.async_api import async_playwright

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect(DB_FILE)

def load_profile():
    if os.path.exists(PROFILE_FILE):
        try:
            with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "full_name": "Joshua Hanflink",
        "first_name": "Joshua",
        "last_name": "Hanflink",
        "email": "jjhanflink@gmail.com",
        "phone": "727-555-0199",
        "linkedin": "https://www.linkedin.com/in/joshua-hanflink",
        "github": "https://github.com/jjhanflink",
        "portfolio": "https://jjhanflink.github.io",
        "school": "North Carolina State University",
        "degree": "B.S. Electrical & Computer Engineering",
        "grad_date": "12/2027",
        "us_authorized": True,
        "sponsorship_needed": False,
        "work_onsite": True
    }

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

class ATSAutomator:
    def __init__(self, headless: bool = True):
        self.headless = headless

    async def _navigate_to_form(self, page, url: str):
        # Handle Lever 2-step application pages
        if "jobs.lever.co" in url and not url.rstrip("/").endswith("/apply"):
            apply_url = url.rstrip("/") + "/apply"
            try:
                await page.goto(apply_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)
                return
            except Exception:
                pass

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        # Handle Workday initial apply buttons (Shadow-DOM elements)
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
                        await btn.click()
                        await asyncio.sleep(4)
                        return
                    except Exception:
                        pass

        # Handle standard generic Apply buttons if no form is immediately visible
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
                if btn:
                    try:
                        await btn.click()
                        await asyncio.sleep(3)
                        break
                    except Exception:
                        pass

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
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
                await self._navigate_to_form(page, url)

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

                cl_elements = await page.query_selector_all(
                    "input[type='file'], textarea, label:has-text('cover letter'), label:has-text('Cover Letter')"
                )
                for el in cl_elements:
                    text_content = (await el.inner_text()).lower() if el else ""
                    if "cover letter" in text_content or "additional documents" in text_content:
                        result["cover_letter_allowed"] = "Yes"
                        break

                await page.screenshot(path=SCREENSHOT_PATH)
                await browser.close()
        except Exception as e:
            result["error"] = str(e)
            result["company"] = parse_fallback_company(url)
            result["role"] = "Manual Review Required"
        return result

    async def fill_application(self, url: str, profile: dict):
        result = {"status": "In Progress", "error": None, "fields_filled": 0}
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
                await self._navigate_to_form(page, url)

                filled_count = 0

                full_name_input = await page.query_selector("input[name='name'], input[aria-label*='Full Name' i]")
                if full_name_input:
                    await full_name_input.fill(profile.get("full_name", "Joshua Hanflink"))
                    filled_count += 1
                else:
                    fn = await page.query_selector("input[name*='first_name' i], input[id*='first_name' i]")
                    if fn:
                        await fn.fill(profile.get("first_name", "Joshua"))
                        filled_count += 1
                    ln = await page.query_selector("input[name*='last_name' i], input[id*='last_name' i]")
                    if ln:
                        await ln.fill(profile.get("last_name", "Hanflink"))
                        filled_count += 1

                em = await page.query_selector("input[name='email'], input[type='email'], input[id*='email' i]")
                if em:
                    await em.fill(profile.get("email", ""))
                    filled_count += 1

                ph = await page.query_selector("input[name='phone'], input[type='tel'], input[id*='phone' i]")
                if ph:
                    await ph.fill(profile.get("phone", ""))
                    filled_count += 1

                org = await page.query_selector("input[name='org'], input[id*='org' i], input[name*='company' i]")
                if org:
                    await org.fill(profile.get("school", "North Carolina State University"))
                    filled_count += 1

                li = await page.query_selector("input[name='urls[LinkedIn]'], input[name*='linkedin' i]")
                if li:
                    await li.fill(profile.get("linkedin", ""))
                    filled_count += 1

                gh = await page.query_selector("input[name='urls[GitHub]'], input[name*='github' i]")
                if gh:
                    await gh.fill(profile.get("github", ""))
                    filled_count += 1

                pf = await page.query_selector("input[name='urls[Portfolio]'], input[name='urls[Other]'], input[name*='portfolio' i], input[name*='website' i]")
                if pf:
                    await pf.fill(profile.get("portfolio", ""))
                    filled_count += 1

                if os.path.exists("resume.pdf"):
                    f_input = await page.query_selector("input[type='file']")
                    if f_input:
                        try:
                            await f_input.set_input_files("resume.pdf")
                            filled_count += 1
                        except Exception:
                            pass

                yes_radios = await page.query_selector_all("input[type='radio'][value*='yes' i], input[type='radio'][value='1']")
                for yr in yes_radios:
                    try:
                        parent_text = (await yr.evaluate("el => el.closest('div, fieldset')?.innerText || ''")).lower()
                        if "authorized" in parent_text or "us person" in parent_text or "on-site" in parent_text or "onsite" in parent_text:
                            await yr.check()
                            filled_count += 1
                    except Exception:
                        pass

                await asyncio.sleep(2)
                await page.screenshot(path=SCREENSHOT_PATH)
                result["fields_filled"] = filled_count
                result["status"] = "Paused for Review"
                await browser.close()
        except Exception as e:
            result["error"] = str(e)
            result["status"] = "Failed"
        return result

def run_async_coro(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)

st.set_page_config(page_title="Intelligent ATS Auto-Applier", page_icon="⚡", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 2rem; padding-bottom: 2rem; }
    .stButton>button { border-radius: 8px; font-family: monospace; font-weight: 600; }
</style>
""", unsafe_allow_html=True)

st.sidebar.title("⚙️ ATS Engine Settings")
engine_mode = st.sidebar.radio("Browser Execution Mode:", ["Headless (Cloud Safe)", "Headed (Visible Window - Local Only)"], index=0)
is_headless = "Headless" in engine_mode

profile = load_profile()
with st.sidebar.expander("👤 Candidate Profile Preview", expanded=False):
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
st.sidebar.markdown(f"🔴 **Manual Action:** `{manual_apps}`")

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

st.title("⚡ ATS Application Engine & Queue Manager")
st.caption("Playwright Async Auto-Applier with Multi-Step Transition & Form Mapping")

tab1, tab2, tab3 = st.tabs(["1. Add Jobs to Queue", "2. Process Queue & Review", "3. Application Tracker"])

with tab1:
    st.subheader("1. Add Applications to Queue")
    input_url = st.text_input("Paste Job Application Link (Greenhouse, Lever, Workday, or Corporate Portal):")
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
                notes = extracted["error"] if extracted["error"] else "Form detected successfully"

                try:
                    conn = get_db_connection()
                    conn.execute("""
                        INSERT INTO applications (company, role, url, platform, status, cover_letter_allowed, notes)
                        VALUES (?, ?, ?, ?, 'Pending', ?, ?)
                    """, (final_company, final_role, input_url.strip(), final_platform, cl_status, notes))
                    conn.commit()
                    conn.close()
                    st.success(f"Added to Queue: **{final_role}** at **{final_company}**")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.warning("This job URL is already in your database.")

with tab2:
    st.subheader("2. Active Application Review & Execution Gate")
    
    # FIX: Now fetching BOTH Pending and Manual Action Needed jobs
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, company, role, url, platform, status, cover_letter_allowed, notes FROM applications WHERE status IN ('Pending', 'Manual Action Needed')")
    pending_rows = c.fetchall()
    conn.close()

    if not pending_rows:
        st.info("🎉 Your actionable queue is currently empty!")
    else:
        job_options = {f"#{row[0]} | {row[1]} - {row[2]} [{row[5]}]": row for row in pending_rows}
        selected_label = st.selectbox("Select Actionable Job to Process:", list(job_options.keys()))
        job = job_options[selected_label]

        job_id, company, role, url, platform, status, cl_allowed, notes = job

        st.markdown(f"""
        <div style="background: rgba(15, 23, 42, 0.8); border: 1px solid #334155; padding: 1.5rem; border-radius: 12px; margin-bottom: 1.5rem;">
            <h3 style="margin-top:0; color:#38bdf8;">{role}</h3>
            <h4 style="color:#94a3b8; margin-bottom: 0.5rem;">{company} • <span style="color:#a855f7;">{platform}</span></h4>
            <p style="font-size: 0.85rem; color:#64748b; margin-bottom: 0.5rem;"><b>Status:</b> {status} | <b>Cover Letter Allowed:</b> {cl_allowed}</p>
            <p style="font-size: 0.85rem; color:#e2e8f0; margin-bottom: 1rem;"><b>Probe Diagnostics:</b> {notes}</p>
        </div>
        """, unsafe_allow_html=True)

        col_link, col_del = st.columns([3, 1])
        with col_link:
            st.markdown(f"""
            <a href="{url}" target="_blank" style="display:inline-block; width:100%; text-align:center; padding: 0.65rem 1rem; background-color: #0284c7; color: white; border-radius: 8px; font-weight: bold; text-decoration: none; font-family: monospace;">
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

        st.markdown("---")
        if os.path.exists(SCREENSHOT_PATH):
            st.markdown("##### 📸 Live Browser Capture (Current View):")
            st.image(SCREENSHOT_PATH, use_container_width=True)

        st.markdown("#### Automation & Final Submission Gate")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("🚀 Run Playwright Auto-Fill", use_container_width=True):
                with st.spinner("Transitioning to form and injecting details..."):
                    automator = ATSAutomator(headless=is_headless)
                    res = run_async_coro(automator.fill_application(url, profile))
                    if res["error"]:
                        st.error(f"Auto-fill error: {res['error']}")
                    else:
                        st.success(f"Injected {res['fields_filled']} fields! Check screenshot.")
                        st.rerun()
        with c2:
            if st.button("✅ Mark as Submitted", use_container_width=True):
                conn = get_db_connection()
                conn.execute("UPDATE applications SET status='Submitted' WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                st.rerun()
        with c3:
            if st.button("⚠️ Mark as Manual Action Needed", use_container_width=True):
                conn = get_db_connection()
                conn.execute("UPDATE applications SET status='Manual Action Needed' WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                st.rerun()

with tab3:
    st.subheader("3. Comprehensive Application Tracker")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, company, role, platform, status, url, created_at FROM applications ORDER BY id DESC")
    all_rows = c.fetchall()
    conn.close()

    if not all_rows:
        st.info("No applications in the tracker yet.")
    else:
        for item in all_rows:
            i_id, i_comp, i_role, i_plat, i_stat, i_url, i_date = item
            
            # Dynamic styling for status pills
            stat_color = "#3b82f6" if i_stat == "Pending" else "#22c55e" if i_stat == "Submitted" else "#eab308"
            
            with st.container():
                c_info, c_btn1, c_btn2 = st.columns([6, 1.5, 1.5])
                with c_info:
                    st.markdown(f"**#{i_id} • {i_comp}** — *{i_role}* <span style='color:{stat_color}; font-weight:bold;'>[{i_stat}]</span>", unsafe_allow_html=True)
                    st.caption(f"Platform: {i_plat} | Created: {i_date} | [View Posting ↗]({i_url})")
                
                # Inline quick actions
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
