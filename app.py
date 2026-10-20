import os
import re
import json
import sqlite3
import asyncio
from datetime import datetime
from urllib.parse import urlparse
import streamlit as st
from playwright.async_api import async_playwright

# ==========================================
# 1. DATABASE & STORAGE MANAGEMENT
# ==========================================
DB_FILE = "applications.db"
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
        "first_name": "Joshua",
        "last_name": "Hanflink",
        "email": "jjhanflink@gmail.com",
        "phone": "727-555-0199",
        "linkedin": "https://www.linkedin.com/in/joshua-hanflink",
        "github": "https://github.com/jjhanflink",
        "portfolio": "https://jjhanflink.github.io",
        "school": "North Carolina State University",
        "degree": "B.S. Electrical & Computer Engineering"
    }

init_db()

# ==========================================
# 2. SMART EXTRACTION & PLATFORM DETECTION
# ==========================================
def detect_platform(url: str) -> str:
    url_lower = url.lower()
    if "greenhouse.io" in url_lower or "boards.greenhouse.io" in url_lower:
        return "Greenhouse"
    elif "lever.co" in url_lower or "jobs.lever.co" in url_lower:
        return "Lever"
    elif "myworkdayjobs.com" in url_lower or "workday" in url_lower:
        return "Workday"
    elif "ashbyhq.com" in url_lower:
        return "Ashby"
    elif "taleo.net" in url_lower:
        return "Taleo"
    elif "tesla.com" in url_lower:
        return "Tesla (Akamai WAF Protected)"
    elif "honeywell.com" in url_lower:
        return "Honeywell Enterprise Portal"
    return "Company Direct Portal"

def parse_fallback_company(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    domain = re.sub(r'^(www\.|careers\.|jobs\.|boards\.)', '', domain)
    company_candidate = domain.split('.')[0]
    return company_candidate.capitalize() if company_candidate else "Engineering Employer"

# ==========================================
# 3. PLAYWRIGHT AUTOMATION ENGINE
# ==========================================
class ATSAutomator:
    def __init__(self, headless: bool = True):
        self.headless = headless

    async def extract_title_and_probe(self, url: str):
        result = {
            "title": "",
            "company": parse_fallback_company(url),
            "role": "Engineering Candidate Role",
            "platform": detect_platform(url),
            "cover_letter_allowed": "No",
            "screenshot": None,
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
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)

                page_title = await page.title()
                result["title"] = page_title.strip()

                if "access denied" in page_title.lower() or "forbidden" in page_title.lower():
                    result["error"] = "Access Denied by WAF (Akamai/Cloudflare). Manual submission required."
                    result["company"] = parse_fallback_company(url)
                    result["role"] = "WAF Protected Posting"
                else:
                    if " at " in page_title:
                        parts = page_title.split(" at ")
                        result["role"] = parts[0].strip()
                        result["company"] = parts[1].split("-")[0].split("|")[0].strip()
                    elif " - " in page_title:
                        parts = page_title.split(" - ")
                        result["role"] = parts[0].strip()
                        result["company"] = parts[1].strip()
                    elif page_title:
                        result["role"] = page_title.strip()

                # Check for cover letter field indicators
                cl_elements = await page.query_selector_all(
                    "input[type='file'], textarea, label:has-text('cover letter'), label:has-text('Cover Letter')"
                )
                for el in cl_elements:
                    text_content = (await el.inner_text()).lower() if el else ""
                    if "cover letter" in text_content or "additional documents" in text_content:
                        result["cover_letter_allowed"] = "Yes"
                        break

                await page.screenshot(path=SCREENSHOT_PATH)
                result["screenshot"] = SCREENSHOT_PATH
                await browser.close()
        except Exception as e:
            result["error"] = str(e)
            result["company"] = parse_fallback_company(url)
            result["role"] = "Manual Review Required"
        return result

    async def fill_application(self, url: str, profile: dict):
        result = {"status": "In Progress", "screenshot": None, "error": None}
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                await asyncio.sleep(3)

                # Attempt standard candidate field injections
                field_selectors = {
                    "first_name": ["input[name*='first_name' i]", "input[id*='first_name' i]", "input[aria-label*='first name' i]"],
                    "last_name": ["input[name*='last_name' i]", "input[id*='last_name' i]", "input[aria-label*='last name' i]"],
                    "email": ["input[name*='email' i]", "input[type='email' i]", "input[id*='email' i]"],
                    "phone": ["input[name*='phone' i]", "input[type='tel' i]", "input[id*='phone' i]"],
                    "linkedin": ["input[name*='linkedin' i]", "input[aria-label*='linkedin' i]"],
                    "github": ["input[name*='github' i]", "input[aria-label*='github' i]"]
                }

                for key, selectors in field_selectors.items():
                    val = profile.get(key, "")
                    if not val:
                        continue
                    for selector in selectors:
                        input_elem = await page.query_selector(selector)
                        if input_elem:
                            try:
                                await input_elem.fill(val)
                                break
                            except Exception:
                                pass

                await page.screenshot(path=SCREENSHOT_PATH)
                result["screenshot"] = SCREENSHOT_PATH
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

# ==========================================
# 4. STREAMLIT USER INTERFACE
# ==========================================
st.set_page_config(
    page_title="Intelligent ATS Auto-Applier",
    page_icon="⚡",
    layout="wide"
)

# Custom Styling
st.markdown("""
<style>
    .block-container { padding-top: 2rem; padding-bottom: 2rem; }
    .job-card { background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; padding: 1.5rem; margin-bottom: 1rem; }
    .stButton>button { border-radius: 8px; font-family: monospace; font-weight: 600; }
</style>
""", unsafe_allow_html=True)

# Sidebar Configuration
st.sidebar.title("⚙️ ATS Engine Settings")
engine_mode = st.sidebar.radio(
    "Browser Execution Mode:",
    ["Headless (Cloud Safe)", "Headed (Visible Window - Local Only)"],
    index=0,
    help="Use Headless on Streamlit Cloud. Use Headed only when running locally on your computer."
)
is_headless = "Headless" in engine_mode

profile = load_profile()
with st.sidebar.expander("👤 Candidate Profile Preview", expanded=False):
    st.json(profile)

conn = get_db_connection()
total_apps = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
pending_apps = conn.execute("SELECT COUNT(*) FROM applications WHERE status='Pending'").fetchone()[0]
conn.close()

st.sidebar.markdown(f"**Total Applications Tracked:** `{total_apps}`")
st.sidebar.markdown(f"**Pending Processing:** `{pending_apps}`")

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

# Main Application Title
st.title("⚡ ATS Application Engine & Queue Manager")
st.caption("Playwright Async Auto-Applier with Direct Job Review & Anti-WAF Fallback")

tab1, tab2, tab3 = st.tabs(["1. Add Jobs to Queue", "2. Process Queue & Review", "3. Application Tracker"])

# ==========================================
# TAB 1: ADD JOBS TO QUEUE
# ==========================================
with tab1:
    st.subheader("1. Add Applications to Queue")
    input_url = st.text_input(
        "Paste Job Application Link (Greenhouse, Lever, Workday, or Corporate Portal):",
        placeholder="https://boards.greenhouse.io/... or https://jobs.lever.co/..."
    )

    col_a, col_b = st.columns([1, 1])
    with col_a:
        custom_company = st.text_input("Override Company Name (Optional):", placeholder="Auto-detected if blank")
    with col_b:
        custom_role = st.text_input("Override Role Title (Optional):", placeholder="Auto-detected if blank")

    if st.button("💾 Auto-Extract & Add to Queue", use_container_width=True):
        if not input_url.strip():
            st.error("Please enter a valid job URL.")
        else:
            with st.spinner("Probing URL with Playwright engine..."):
                automator = ATSAutomator(headless=is_headless)
                extracted = run_async_coro(automator.extract_title_and_probe(input_url.strip()))

                final_company = custom_company.strip() or extracted["company"]
                final_role = custom_role.strip() or extracted["role"]
                final_platform = extracted["platform"]
                cl_status = extracted["cover_letter_allowed"]
                notes = extracted["error"] if extracted["error"] else "Extracted successfully"

                try:
                    conn = get_db_connection()
                    conn.execute("""
                        INSERT INTO applications (company, role, url, platform, status, cover_letter_allowed, notes)
                        VALUES (?, ?, ?, ?, 'Pending', ?, ?)
                    """, (final_company, final_role, input_url.strip(), final_platform, cl_status, notes))
                    conn.commit()
                    conn.close()
                    st.success(f"Added to Queue: **{final_role}** at **{final_company}** [{final_platform}]")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.warning("This job URL is already in your database.")
                except Exception as e:
                    st.error(f"Database error: {e}")

# ==========================================
# TAB 2: PROCESS QUEUE & REVIEW
# ==========================================
with tab2:
    st.subheader("2. Active Application Review & Execution Gate")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, company, role, url, platform, status, cover_letter_allowed, notes FROM applications WHERE status='Pending'")
    pending_rows = c.fetchall()
    conn.close()

    if not pending_rows:
        st.info("🎉 Your pending queue is currently empty! Add new jobs in Tab 1.")
    else:
        # Build selection dict
        job_options = {f"#{row[0]} | {row[1]} - {row[2]} ({row[4]})": row for row in pending_rows}
        selected_label = st.selectbox("Select Pending Job to Process:", list(job_options.keys()))
        job = job_options[selected_label]

        job_id, company, role, url, platform, status, cl_allowed, notes = job

        # Card Display
        st.markdown(f"""
        <div style="background: rgba(15, 23, 42, 0.8); border: 1px solid #334155; padding: 1.5rem; border-radius: 12px; margin-bottom: 1.5rem;">
            <h3 style="margin-top:0; color:#38bdf8;">{role}</h3>
            <h4 style="color:#94a3b8; margin-bottom: 0.5rem;">{company} • <span style="color:#a855f7;">{platform}</span></h4>
            <p style="font-size: 0.85rem; color:#64748b; margin-bottom: 0.5rem;"><b>Status:</b> {status} | <b>Cover Letter Allowed:</b> {cl_allowed}</p>
            <p style="font-size: 0.85rem; color:#e2e8f0; margin-bottom: 1rem;"><b>Probe Diagnostics:</b> {notes}</p>
        </div>
        """, unsafe_allow_html=True)

        # High-Impact Action Bar
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
                st.success(f"Deleted job #{job_id} from queue.")
                st.rerun()

        st.markdown("---")

        # Visual Capture Preview
        if os.path.exists(SCREENSHOT_PATH):
            st.markdown("##### 📸 Live Browser Capture (What the Cloud Engine Sees):")
            st.image(SCREENSHOT_PATH, use_container_width=True)

        st.markdown("#### Automation & Final Submission Gate")
        st.caption("For sites that block cloud workers (Tesla/Akamai), open the link above and submit in your browser, then mark as Submitted.")

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("🚀 Run Playwright Auto-Fill", use_container_width=True):
                with st.spinner("Injecting candidate credentials into form fields..."):
                    automator = ATSAutomator(headless=is_headless)
                    res = run_async_coro(automator.fill_application(url, profile))
                    if res["error"]:
                        st.error(f"Auto-fill error: {res['error']}")
                    else:
                        st.success("Form fields mapped and injected! Check the screenshot above.")
                        st.rerun()

        with c2:
            if st.button("✅ Mark as Submitted", use_container_width=True):
                conn = get_db_connection()
                conn.execute("UPDATE applications SET status='Submitted' WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                st.success("Marked as Submitted!")
                st.rerun()

        with c3:
            if st.button("⚠️ Mark as Manual Action Needed", use_container_width=True):
                conn = get_db_connection()
                conn.execute("UPDATE applications SET status='Manual Action Needed' WHERE id=?", (job_id,))
                conn.commit()
                conn.close()
                st.warning("Marked as Manual Action Needed.")
                st.rerun()

# ==========================================
# TAB 3: APPLICATION TRACKER & QUEUE MANAGEMENT
# ==========================================
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
            with st.container():
                c_info, c_link, c_btn = st.columns([4, 2, 1])
                with c_info:
                    st.markdown(f"**#{i_id} • {i_comp}** — *{i_role}* `[{i_stat}]`")
                    st.caption(f"Platform: {i_plat} | Created: {i_date}")
                with c_link:
                    st.markdown(f"[View Posting ↗]({i_url})")
                with c_btn:
                    if st.button("🗑️ Remove", key=f"tbl_del_{i_id}"):
                        conn = get_db_connection()
                        conn.execute("DELETE FROM applications WHERE id=?", (i_id,))
                        conn.commit()
                        conn.close()
                        st.rerun()
                st.markdown("<hr style='margin: 0.4rem 0; border-color: #1e293b;'/>", unsafe_allow_html=True)