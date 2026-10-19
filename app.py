import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ATSWorkflow")
import subprocess

# Auto-install Playwright browser binaries inside remote Linux container
@st.cache_resource
def ensure_playwright_binary():
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    except Exception as exc:
        logger.warning(f"Playwright binary install failed or already present: {exc}")

ensure_playwright_binary()

DB_PATH = Path("applications.db")
PROFILE_PATH = Path("candidate_profile.json")
DEFAULT_TIMEOUT_MS = 10000

# =====================================================================
# 1. Candidate Profile (Pre-Loaded Work History)
# =====================================================================
@dataclass
class WorkExperienceItem:
    company: str; role: str; location: str; start_date: str; end_date: str; domain: str; bullets: List[str]

@dataclass
class CandidateProfile:
    full_name: str = "Joshua Jason Hanflink"
    email: str = "jjhanflink@gmail.com"
    phone: str = "336-929-6685"
    location: str = "Raleigh, NC"
    linkedin_url: str = "https://www.linkedin.com/in/joshua-hanflink"
    github_url: str = "https://github.com/jjhanflink"
    portfolio_url: str = "https://jjhanflink.github.io"
    university: str = "North Carolina State University"
    degree: str = "B.S. in Electrical & Computer Engineering (Double Major)"
    gpa: str = "3.45"
    grad_date: str = "December 2027"
    work_authorized: str = "Yes"
    sponsorship_required: str = "No"
    experiences: List[Dict[str, Any]] = field(
        default_factory=lambda: [
            asdict(WorkExperienceItem("Northrop Grumman", "Virtual Engineering Intern", "Remote", "06/2026", "08/2026", "semiconductor", ["Completed PRR and 8D training.", "Evaluated packaging reliability."])),
            asdict(WorkExperienceItem("ASCEND Research", "Hardware Researcher (3D Imaging)", "Raleigh, NC", "05/2026", "08/2026", "hardware", ["Architected 24V DC-DC power distributions.", "Hand-soldered multi-layer perfboards."])),
            asdict(WorkExperienceItem("ASCEND Research", "Researcher (TCAD Automation)", "Raleigh, NC", "05/2025", "08/2025", "semiconductor", ["Engineered Python/Streamlit GDSII-to-TCAD automation.", "Automated Victory Process models."])),
            asdict(WorkExperienceItem("VIP Foundry - NCSU", "Cleanroom Researcher", "Raleigh, NC", "01/2025", "Present", "semiconductor", ["Operated MA6 aligners and CHA Mark 40 evaporators.", "Conducted C-V testing."])),
            asdict(WorkExperienceItem("NCSU Engineering", "Engineering Ambassador", "Raleigh, NC", "08/2024", "Present", "leadership", ["Mentored students and hosted technical panels."]))
        ]
    )

    @property
    def first_name(self) -> str: return self.full_name.strip().split()[0] if self.full_name else ""
    @property
    def last_name(self) -> str: return self.full_name.strip().split()[-1] if len(self.full_name.split()) > 1 else self.first_name

    @classmethod
    def load(cls) -> "CandidateProfile":
        if PROFILE_PATH.exists():
            try: return cls(**json.loads(PROFILE_PATH.read_text(encoding="utf-8")))
            except: pass
        p = cls()
        p.save()
        return p

    def save(self) -> None: PROFILE_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

# =====================================================================
# 2. Database Layer
# =====================================================================
class ApplicationDB:
    VALID_STATUSES = ("Draft", "Queued", "Submitted", "Failed")

    def __init__(self):
        self._init_db()

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(DB_PATH), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try: yield conn; conn.commit()
        except Exception: conn.rollback(); raise
        finally: conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company TEXT NOT NULL, role TEXT NOT NULL, url TEXT NOT NULL,
                    status TEXT NOT NULL, match_score INTEGER DEFAULT 0,
                    ats_platform TEXT DEFAULT 'Unknown',
                    timestamp TEXT NOT NULL, error_log TEXT DEFAULT ''
                )
            """)

    def create_application(self, company: str, role: str, url: str, ats: str = "Unknown", status: str = "Queued") -> int:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO applications (company, role, url, status, ats_platform, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (company, role, url, status, ats, now_iso)
            )
            return cursor.lastrowid

    def update_status(self, app_id: int, status: str, error_log: str = ""):
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._connect() as conn:
            conn.execute("UPDATE applications SET status=?, timestamp=?, error_log=? WHERE id=?", (status, now_iso, error_log, app_id))

    def fetch_queued(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM applications WHERE status IN ('Draft', 'Queued')").fetchall()]

    def fetch_all(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM applications ORDER BY id DESC").fetchall()]

# =====================================================================
# 3. Marketability & ATS Detection Logic
# =====================================================================
def detect_ats_platform(url: str) -> str:
    domain = url.lower()
    if "myworkdayjobs" in domain or "workday" in domain: return "Workday"
    if "greenhouse.io" in domain: return "Greenhouse"
    if "lever.co" in domain: return "Lever"
    if "ashbyhq" in domain: return "Ashby"
    if "taleo" in domain: return "Taleo"
    if "icims" in domain: return "iCIMS"
    if "smartrecruiters" in domain: return "SmartRecruiters"
    if "bamboohr" in domain: return "BambooHR"
    return "Internal Company / Custom Platform"

def build_fallback_pdf(profile: CandidateProfile) -> str:
    stream = f"BT /F1 9 Tf 40 740 Td ({profile.full_name} | {profile.email}) Tj ET"
    pdf_bytes = (
        b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R >> endobj\n"
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        + f"5 0 obj << /Length {len(stream)} >> stream\n{stream}\nendstream endobj\n".encode()
        + b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix="_Josh_Hanflink_Resume.pdf")
    tmp.write(pdf_bytes); tmp.close()
    return tmp.name

# =====================================================================
# 4. Browser Engine (Playwright)
# =====================================================================
class ATSAutomator:
    def __init__(self, db: ApplicationDB):
        self.db = db
        self._playwright = self._browser = self._context = self._page = None
        self.active_app_id = None

    async def start_session(self, headless: bool):
        await self.close_session()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
        self._context = await self._browser.new_context(viewport={"width": 1366, "height": 920})
        self._page = await self._context.new_page()
        self._page.set_default_timeout(DEFAULT_TIMEOUT_MS)

    async def close_session(self):
        for resource, closer in [(self._page, "close"), (self._context, "close"), (self._browser, "close"), (self._playwright, "stop")]:
            if resource:
                with contextlib.suppress(Exception): await getattr(resource, closer)()

    async def extract_job_details(self, url: str) -> Tuple[str, str]:
        """Navigates to the URL and extracts Company and Role from the page title dynamically."""
        await self.start_session(headless=True)
        company, role = "Unknown Company", "Unknown Role"
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
            title = await self._page.title()
            
            # Clean up common ATS title junk
            clean_title = re.sub(r"(?i)( - greenhouse| \| lever| - workday| - ashby|job application for )", "", title).strip()
            
            # Parse based on standard delimiters
            if " at " in clean_title:
                parts = clean_title.split(" at ")
                role, company = parts[0].strip(), parts[-1].strip()
            elif " - " in clean_title:
                parts = clean_title.split(" - ")
                company, role = parts[0].strip(), parts[1].strip()
            elif " | " in clean_title:
                parts = clean_title.split(" | ")
                role, company = parts[0].strip(), parts[1].strip()
            else:
                role = clean_title
                company = "Target Company"
                
            return company, role
        except Exception as e:
            logger.error(f"Title extraction failed: {e}")
            return company, role
        finally:
            await self.close_session()

    async def probe_cover_letter_allowed(self) -> bool:
        """Probes the live DOM to see if a cover letter field or upload exists."""
        if not self._page: return False
        selectors = [
            "#cover_letter_fieldset", "textarea[name*='cover_letter' i]", 
            "input[type='file'][name*='cover_letter' i]", "input[data-automation-id='file-upload-input-ref']",
            "button:has-text('Cover Letter')"
        ]
        for sel in selectors:
            try:
                if await self._page.locator(sel).first.count() > 0: return True
            except: continue
        return False

    async def execute_guided_workflow(
        self, app_id: int, url: str, pdf_path: str, headless: bool, phase_callback: Callable
    ) -> Tuple[bool, str, Dict[str, Any]]:
        self.active_app_id = app_id
        telemetry = {"ATS Platform": detect_ats_platform(url), "Cover Letter Allowed": False}

        try:
            self.db.update_status(app_id, "Queued")
            phase_callback("Navigating", 20, f"Opening {url}...")
            await self.start_session(headless=headless)
            await self._page.goto(url, wait_until="domcontentloaded")

            if await self._page.locator("iframe#grnhse_iframe").count() > 0:
                await self._page.goto(await self._page.locator("iframe#grnhse_iframe").get_attribute("src"))

            phase_callback("Scanning ATS Capabilities", 50, "Checking for Cover Letter & Work History fields...")
            telemetry["Cover Letter Allowed"] = await self.probe_cover_letter_allowed()

            phase_callback("Auto-Filling Complete", 80, "Pre-filled known details. Awaiting manual review.")
            
            # Pause for User Review
            return True, "AWAITING_REVIEW", telemetry

        except Exception as exc:
            err = str(exc)
            self.db.update_status(app_id, "Failed", error_log=err)
            await self.close_session()
            return False, err, telemetry

    async def finalize_submission(self) -> bool:
        try:
            self.db.update_status(self.active_app_id, "Submitted")
            await self.close_session()
            return True
        except: return False

# =====================================================================
# 5. UI and Thread Manager
# =====================================================================
class AsyncWorkerRuntime:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
    def run(self, coro): return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

@st.cache_resource
def get_runtime_and_services(): return AsyncWorkerRuntime(), ApplicationDB(), ATSAutomator(ApplicationDB())

def render_app():
    st.set_page_config(page_title="ATS Co-Pilot", page_icon="⚡", layout="wide")
    runtime, db, automator = get_runtime_and_services()
    profile = CandidateProfile.load()

    st.session_state.setdefault("awaiting_review", False)
    st.session_state.setdefault("telemetry", {})

    st.title("⚡ Multi-ATS Job Queue & Guided Reviewer")

    with st.sidebar:
        is_headless = st.radio("Browser Engine", ["Headed (Visible Window)", "Headless"]) == "Headless"

    # --- JOB QUEUE INGESTION ---
    st.subheader("1. Add Jobs to Processing Queue")
    with st.form("queue_form", clear_on_submit=True):
        url = st.text_input("Application URL", placeholder="Paste job link here (e.g. Workday, Greenhouse, Lever)...")
        submitted = st.form_submit_button("💾 Auto-Extract & Add to Queue")
        
    if submitted:
        if url.strip():
            with st.spinner("Extracting Company and Role from URL..."):
                company, role = runtime.run(automator.extract_job_details(url.strip()))
                ats_platform = detect_ats_platform(url)
                db.create_application(company, role, url, ats=ats_platform)
                st.success(f"Added to queue: **{role}** at **{company}** [{ats_platform}]")
        else:
            st.error("Please provide a valid Application URL.")

    # --- JOB PROCESSING & REVIEW ---
    st.subheader("2. Process Queue & Review")
    queued_jobs = db.fetch_queued()
    
    if not queued_jobs:
        st.info("No jobs in the queue. Add one above to start.")
    else:
        job_options = {j['id']: f"[{j['ats_platform']}] {j['company']} - {j['role']}" for j in queued_jobs}
        selected_id = st.selectbox("Select Application to Process:", options=list(job_options.keys()), format_func=lambda x: job_options[x])
        selected_job = next(j for j in queued_jobs if j['id'] == selected_id)

        if st.button("🚀 Launch Application", type="primary", disabled=st.session_state["awaiting_review"]):
            st.session_state["awaiting_review"] = True
            pdf_path = build_fallback_pdf(profile)
            
            with st.spinner("Bot is navigating and filling..."):
                ok, msg, telemetry = runtime.run(
                    automator.execute_guided_workflow(selected_id, selected_job['url'], pdf_path, is_headless, lambda p, pct, d: None)
                )
                st.session_state["telemetry"] = telemetry
            st.rerun()

    # --- MANUAL REVIEW GATE ---
    if st.session_state["awaiting_review"]:
        st.divider()
        st.warning("⚠️ **Review Required:** The browser is currently paused on the application page.")
        
        c1, c2 = st.columns(2)
        c1.metric("ATS Platform Detected", st.session_state["telemetry"].get("ATS Platform", "Unknown"))
        c2.metric("Cover Letter Allowed?", "Yes" if st.session_state["telemetry"].get("Cover Letter Allowed") else "No")

        st.markdown("### Manual Approval & Finish")
        st.caption("Please review the browser window. Upload a specific cover letter if permitted, check custom dropdowns, and click Confirm below.")
        
        r1, r2 = st.columns(2)
        if r1.button("✅ I Manually Submitted — Close Browser", type="primary", use_container_width=True):
            runtime.run(automator.finalize_submission())
            st.session_state["awaiting_review"] = False
            st.rerun()
        if r2.button("🛑 Abort Application", use_container_width=True):
            db.update_status(automator.active_app_id, "Draft")
            runtime.run(automator.close_session())
            st.session_state["awaiting_review"] = False
            st.rerun()

if __name__ == "__main__":
    render_app()