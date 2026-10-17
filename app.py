import asyncio
import contextlib
import logging
import os
import sqlite3
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

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

# Ensure Windows compatibility for Playwright subprocesses inside asyncio loops
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Configure structured application logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ATSWorkflow")

DB_PATH = Path("applications.db")
DEFAULT_TIMEOUT_MS = 10000


# =====================================================================
# Database Layer (SQLite)
# =====================================================================
class ApplicationDB:
    """Thread-safe SQLite data access layer for tracking job applications."""

    VALID_STATUSES = ("Draft", "Queued", "Submitted", "Failed")

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company TEXT NOT NULL,
                    role TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('Draft', 'Queued', 'Submitted', 'Failed')),
                    timestamp TEXT NOT NULL,
                    error_log TEXT DEFAULT ''
                )
                """
            )

    def create_application(
        self, company: str, role: str, url: str, status: str = "Draft"
    ) -> int:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Expected one of {self.VALID_STATUSES}")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO applications (company, role, url, status, timestamp, error_log)
                VALUES (?, ?, ?, ?, ?, '')
                """,
                (company.strip(), role.strip(), url.strip(), status, now_iso),
            )
            return int(cursor.lastrowid)

    def update_status(self, app_id: int, status: str, error_log: str = "") -> None:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Expected one of {self.VALID_STATUSES}")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE applications
                SET status = ?, timestamp = ?, error_log = ?
                WHERE id = ?
                """,
                (status, now_iso, error_log, app_id),
            )

    def fetch_all(self) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, company, role, url, status, timestamp, error_log FROM applications ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]


# =====================================================================
# Data Models & PDF Fallback Helper
# =====================================================================
@dataclass
class CandidatePayload:
    full_name: str
    email: str
    phone: str
    linkedin_url: str
    github_url: str
    portfolio_url: str
    resume_pdf_path: str
    tailored_text: str

    @property
    def first_name(self) -> str:
        parts = self.full_name.strip().split()
        return parts[0] if parts else ""

    @property
    def last_name(self) -> str:
        parts = self.full_name.strip().split()
        return " ".join(parts[1:]) if len(parts) > 1 else (parts[0] if parts else "")


def build_fallback_pdf(text_content: str) -> str:
    """Generates a valid minimal PDF file on disk if user only supplies raw resume text."""
    safe_text = (
        text_content.replace("\\", "\\\\")
        .replace("(", "\\(") .replace(")", "\\)")
        .replace("\r", " ")
        .replace("\n", " ")[:400]
    )
    stream = f"BT /F1 10 Tf 50 750 Td ({safe_text}) Tj ET"
    pdf_bytes = (
        "%PDF-1.4\n"
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        "3 0 obj << /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        "/MediaBox [0 0 612 792] /Contents 5 0 R >> endobj\n"
        "4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        f"5 0 obj << /Length {len(stream)} >> stream\n{stream}\nendstream endobj\n"
        "xref\n0 6\n0000000000 65535 f \n"
        "0000000009 00000 n \n"
        "0000000058 00000 n \n"
        "0000000115 00000 n \n"
        "0000000229 00000 n \n"
        "0000000297 00000 n \n"
        "trailer << /Size 6 /Root 1 0 R >>\nstartxref\n390\n%%EOF\n"
    ).encode("latin-1", errors="ignore")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix="_resume.pdf")
    tmp.write(pdf_bytes)
    tmp.close()
    return tmp.name


# =====================================================================
# Browser Automation Engine (Playwright)
# =====================================================================
class ATSAutomator:
    """
    Playwright automation engine optimized for Greenhouse and standard ATS forms.
    Maintains browser context across review steps via a dedicated background event loop.
    """

    # Prioritized selector cascades covering Greenhouse (legacy & React job-boards) and general ATS layouts
    FIELD_SELECTORS: Dict[str, List[str]] = {
        "first_name": [
            "#first_name",
            "input[name='job_application[first_name]']",
            "input[autocomplete='given-name']",
            "input[aria-label*='First Name' i]",
            "input[name*='first_name' i]",
        ],
        "last_name": [
            "#last_name",
            "input[name='job_application[last_name]']",
            "input[autocomplete='family-name']",
            "input[aria-label*='Last Name' i]",
            "input[name*='last_name' i]",
        ],
        "full_name": [
            "input[name='name']",
            "input[id='name']",
            "input[autocomplete='name']",
            "input[aria-label='Full Name' i]",
            "input[placeholder*='Full Name' i]",
        ],
        "email": [
            "#email",
            "input[name='job_application[email]']",
            "input[type='email']",
            "input[autocomplete='email']",
            "input[aria-label*='Email' i]",
            "input[name*='email' i]",
        ],
        "phone": [
            "#phone",
            "input[name='job_application[phone]']",
            "input[type='tel']",
            "input[autocomplete='tel']",
            "input[aria-label*='Phone' i]",
            "input[name*='phone' i]",
        ],
        "linkedin": [
            "input[id*='linkedin' i]",
            "input[name*='linkedin' i]",
            "input[aria-label*='LinkedIn' i]",
            "input[placeholder*='linkedin' i]",
            "label:has-text('LinkedIn') ~ input[type='text']",
            "label:has-text('LinkedIn') ~ input[type='url']",
            "div.field:has(label:has-text('LinkedIn')) input[type='text']",
        ],
        "github": [
            "input[id*='github' i]",
            "input[name*='github' i]",
            "input[aria-label*='GitHub' i]",
            "input[placeholder*='github' i]",
            "label:has-text('GitHub') ~ input[type='text']",
            "label:has-text('GitHub') ~ input[type='url']",
            "div.field:has(label:has-text('GitHub')) input[type='text']",
        ],
        "portfolio": [
            "input[id*='website' i]",
            "input[id*='portfolio' i]",
            "input[name*='website' i]",
            "input[name*='portfolio' i]",
            "input[aria-label*='Website' i]",
            "input[aria-label*='Portfolio' i]",
            "label:has-text('Website') ~ input[type='text']",
            "label:has-text('Portfolio') ~ input[type='text']",
            "div.field:has(label:has-text('Website')) input[type='text']",
            "div.field:has(label:has-text('Portfolio')) input[type='text']",
        ],
        "cover_letter_text": [
            "#cover_letter_text",
            "textarea[name='job_application[cover_letter_text]']",
            "textarea[id*='cover_letter' i]",
            "textarea[name*='cover_letter' i]",
            "textarea[aria-label*='Cover Letter' i]",
            "textarea[id*='additional' i]",
            "textarea[name*='comments' i]",
        ],
    }

    RESUME_FILE_SELECTORS: List[str] = [
        "#resume_fieldset input[type='file']",
        "input[type='file'][id*='resume' i]",
        "input[type='file'][name*='resume' i]",
        "input[type='file'][data-field='resume']",
        "div[data-field='resume'] input[type='file']",
        "input[type='file'][accept*='pdf' i]",
        "input[type='file']",
    ]

    SUBMIT_SELECTORS: List[str] = [
        "#submit_app",
        "button[type='submit']#submit_app",
        "button[type='submit']:has-text('Submit Application')",
        "button[type='submit']:has-text('Submit')",
        "input[type='submit'][value*='Submit' i]",
        "button[type='submit']",
    ]

    def __init__(self, db: ApplicationDB) -> None:
        self.db = db
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self.active_app_id: Optional[int] = None

    async def start_session(self, headless: bool) -> None:
        """Initializes Playwright and launches Chromium with realistic user-agent parameters."""
        await self.close_session()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(DEFAULT_TIMEOUT_MS)

    async def close_session(self) -> None:
        """Cleanly tears down open pages, contexts, browsers, and Playwright drivers."""
        for resource, closer in [
            (self._page, "close"),
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ]:
            if resource is not None:
                with contextlib.suppress(Exception):
                    await getattr(resource, closer)()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    async def _fill_first_matching(
        self, selectors: List[str], value: str, timeout: int = DEFAULT_TIMEOUT_MS
    ) -> bool:
        """Attempts to locate and fill the first visible field from a selector cascade."""
        if not value or not self._page:
            return False

        combined = ", ".join(selectors)
        try:
            # Wait explicitly for at least one matching candidate in the DOM
            await self._page.wait_for_selector(combined, state="visible", timeout=timeout)
        except PlaywrightTimeoutError:
            return False

        for sel in selectors:
            locator = self._page.locator(sel).first
            try:
                if await locator.count() > 0 and await locator.is_visible():
                    await locator.scroll_into_view_if_needed(timeout=timeout)
                    await locator.fill(value, timeout=timeout)
                    return True
            except PlaywrightError:
                continue
        return False

    async def _populate_fields(self, payload: CandidatePayload) -> Dict[str, bool]:
        """Populates standard ATS candidate fields with fast fallback discovery."""
        if not self._page:
            raise RuntimeError("Browser page is not initialized.")

        results: Dict[str, bool] = {}

        # Wait for the primary application form container to settle
        form_anchor = (
            "#application_form, #application, form[id*='application' i], "
            "form[action*='applications' i], input[type='email'], #first_name"
        )
        await self._page.wait_for_selector(
            form_anchor, state="attached", timeout=DEFAULT_TIMEOUT_MS
        )

        # Handle split First/Last name (Greenhouse standard) or unified Full Name
        first_filled = await self._fill_first_matching(
            self.FIELD_SELECTORS["first_name"], payload.first_name, timeout=3000
        )
        last_filled = await self._fill_first_matching(
            self.FIELD_SELECTORS["last_name"], payload.last_name, timeout=3000
        )
        if first_filled or last_filled:
            results["Full Name (Split)"] = first_filled and last_filled
        else:
            results["Full Name"] = await self._fill_first_matching(
                self.FIELD_SELECTORS["full_name"], payload.full_name, timeout=3000
            )

        # Mandatory and social profile fields
        field_map = [
            ("Email", "email", payload.email, DEFAULT_TIMEOUT_MS),
            ("Phone", "phone", payload.phone, 4000),
            ("LinkedIn URL", "linkedin", payload.linkedin_url, 3000),
            ("GitHub URL", "github", payload.github_url, 3000),
            ("Portfolio URL", "portfolio", payload.portfolio_url, 3000),
        ]
        for label, key, val, field_timeout in field_map:
            if val.strip():
                results[label] = await self._fill_first_matching(
                    self.FIELD_SELECTORS[key], val.strip(), timeout=field_timeout
                )

        # Populate Cover Letter / Tailored Notes if provided
        if payload.tailored_text.strip():
            # Greenhouse often hides the cover letter textarea behind a "Paste" button
            paste_toggles = [
                "#cover_letter_fieldset button[data-source='paste']",
                "#cover_letter_fieldset a[data-source='paste']",
                "button:has-text('Enter manually')",
            ]
            for toggle in paste_toggles:
                loc = self._page.locator(toggle).first
                with contextlib.suppress(PlaywrightError):
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=2000)
                        break

            results["Tailored Cover Letter"] = await self._fill_first_matching(
                self.FIELD_SELECTORS["cover_letter_text"],
                payload.tailored_text.strip(),
                timeout=3000,
            )

        return results

    async def _upload_resume(self, pdf_path: str) -> bool:
        """Uploads a PDF resume to standard or hidden file inputs using set_input_files."""
        if not self._page:
            raise RuntimeError("Browser page is not initialized.")
        if not pdf_path or not os.path.exists(pdf_path):
            raise FileNotFoundError(f"Resume PDF file not found at: {pdf_path}")

        # Some Greenhouse themes require clicking "Attach" to attach the <input type="file">
        attach_toggles = [
            "#resume_fieldset button[data-source='attach']",
            "#resume_fieldset a[data-source='attach']",
        ]
        for toggle in attach_toggles:
            loc = self._page.locator(toggle).first
            with contextlib.suppress(PlaywrightError):
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=2000)
                    break

        combined_file_sel = ", ".join(self.RESUME_FILE_SELECTORS)
        await self._page.wait_for_selector(
            combined_file_sel, state="attached", timeout=DEFAULT_TIMEOUT_MS
        )

        for sel in self.RESUME_FILE_SELECTORS:
            locator = self._page.locator(sel).first
            try:
                if await locator.count() > 0:
                    await locator.set_input_files(pdf_path, timeout=DEFAULT_TIMEOUT_MS)
                    return True
            except PlaywrightError:
                continue

        raise PlaywrightTimeoutError("Unable to bind PDF resume to any file upload input.")

    async def capture_preview_screenshot(self) -> Optional[bytes]:
        """Takes a snapshot of the current viewport so operators can verify even in Headless mode."""
        if not self._page:
            return None
        with contextlib.suppress(PlaywrightError):
            return await self._page.screenshot(full_page=False)
        return None

    async def execute_workflow(
        self,
        app_id: int,
        url: str,
        payload: CandidatePayload,
        headless: bool,
        review_before_submit: bool,
        phase_callback: Callable[[str, int, str], None],
    ) -> Tuple[bool, str, Dict[str, bool]]:
        """
        Executes phases: Navigating -> Populating Fields -> Uploading Resume -> Awaiting Review / Complete.
        Leaves browser session alive if review_before_submit is True.
        """
        self.active_app_id = app_id
        populated_summary: Dict[str, bool] = {}

        try:
            self.db.update_status(app_id, "Queued")

            # Phase 1: Navigating
            phase_callback("Navigating", 20, f"Launching browser and navigating to {url}...")
            await self.start_session(headless=headless)
            assert self._page is not None

            response = await self._page.goto(
                url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS
            )
            if response and response.status >= 400:
                raise PlaywrightError(
                    f"HTTP {response.status} returned when loading target URL: {url}"
                )

            # Handle embedded Greenhouse iframes automatically if present
            iframe_loc = self._page.locator("iframe#grnhse_iframe").first
            if await iframe_loc.count() > 0:
                src = await iframe_loc.get_attribute("src")
                if src:
                    await self._page.goto(
                        src, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS
                    )

            # Phase 2: Populating Fields
            phase_callback("Populating Fields", 50, "Populating candidate profile & URLs...")
            populated_summary = await self._populate_fields(payload)

            # Phase 3: Uploading Resume
            phase_callback("Uploading Resume", 75, "Attaching PDF resume via set_input_files...")
            await self._upload_resume(payload.resume_pdf_path)

            # Phase 4: Review Gate or Direct Submission
            if review_before_submit:
                phase_callback(
                    "Awaiting Review",
                    90,
                    "Paused before final click. Verify inputs or solve CAPTCHA, then click Confirm.",
                )
                return True, "AWAITING_REVIEW", populated_summary

            # Direct submission when human review gate is bypassed
            return await self.finalize_submission(phase_callback, populated_summary)

        except (PlaywrightTimeoutError, PlaywrightError, FileNotFoundError, Exception) as exc:
            err_msg = f"{type(exc).__name__}: {str(exc)}"
            logger.error("Automation failed for application %s: %s", app_id, err_msg)
            self.db.update_status(app_id, "Failed", error_log=err_msg)
            await self.close_session()
            return False, err_msg, populated_summary

    async def finalize_submission(
        self,
        phase_callback: Callable[[str, int, str], None],
        populated_summary: Optional[Dict[str, bool]] = None,
    ) -> Tuple[bool, str, Dict[str, bool]]:
        """Clicks the submit button, verifies submission, updates SQLite, and closes the browser."""
        summary = populated_summary or {}
        if not self._page or self.active_app_id is None:
            return False, "No active browser session found to submit.", summary

        try:
            combined_submit = ", ".join(self.SUBMIT_SELECTORS)
            await self._page.wait_for_selector(
                combined_submit, state="visible", timeout=DEFAULT_TIMEOUT_MS
            )

            clicked = False
            for sel in self.SUBMIT_SELECTORS:
                btn = self._page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.scroll_into_view_if_needed(timeout=DEFAULT_TIMEOUT_MS)
                    await btn.click(timeout=DEFAULT_TIMEOUT_MS)
                    clicked = True
                    break

            if not clicked:
                raise PlaywrightTimeoutError("Submit button could not be actuated.")

            # Wait for network activity to settle post-submission
            with contextlib.suppress(PlaywrightTimeoutError):
                await self._page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT_MS)

            self.db.update_status(self.active_app_id, "Submitted")
            phase_callback("Complete", 100, "Application successfully submitted!")
            return True, "Submitted", summary

        except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
            err_msg = f"Submission Error ({type(exc).__name__}): {str(exc)}"
            logger.error("Final submit failed for ID %s: %s", self.active_app_id, err_msg)
            self.db.update_status(self.active_app_id, "Failed", error_log=err_msg)
            return False, err_msg, summary
        finally:
            await self.close_session()


# =====================================================================
# Persistent Async Runtime Manager for Streamlit
# =====================================================================
class AsyncWorkerRuntime:
    """Hosts a persistent background asyncio loop so Playwright objects survive Streamlit reruns."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result()


@st.cache_resource
def get_runtime_and_services() -> Tuple[AsyncWorkerRuntime, ApplicationDB, ATSAutomator]:
    runtime = AsyncWorkerRuntime()
    db = ApplicationDB(DB_PATH)
    automator = ATSAutomator(db=db)
    return runtime, db, automator


# =====================================================================
# Streamlit User Interface
# =====================================================================
def render_app() -> None:
    st.set_page_config(
        page_title="ATS Automation Workbench",
        page_icon="⚡",
        layout="wide",
    )

    runtime, db, automator = get_runtime_and_services()

    # Initialize Streamlit session state keys
    st.session_state.setdefault("awaiting_review", False)
    st.session_state.setdefault("current_phase", "Idle")
    st.session_state.setdefault("last_field_report", {})
    st.session_state.setdefault("review_screenshot", None)

    st.title("⚡ ATS Job Application Workflow")
    st.caption("Playwright + SQLite Automation Pipeline for Greenhouse & Standard ATS Forms")

    # Sidebar: Execution Engine Controls
    with st.sidebar:
        st.header("⚙️ Engine Configuration")
        execution_mode = st.radio(
            "Browser Execution Mode",
            options=["Headed (Visible Browser)", "Headless (Background)"],
            index=0,
            help="Use Headed mode to watch live DOM population or solve CAPTCHAs manually.",
        )
        is_headless = execution_mode.startswith("Headless")

        review_before_submit = st.toggle(
            "Review Before Submit",
            value=True,
            help="Pause automation before clicking Submit so you can inspect inputs or solve CAPTCHAs.",
        )

        st.divider()
        st.subheader("👤 Default Candidate Profile")
        full_name = st.text_input("Full Name", value="Alex Rivera")
        email = st.text_input("Email Address", value="alex.rivera@example.com")
        phone = st.text_input("Phone Number", value="+1-555-019-2834")
        linkedin_url = st.text_input(
            "LinkedIn URL", value="https://www.linkedin.com/in/alex-rivera"
        )
        github_url = st.text_input("GitHub URL", value="https://github.com/alex-rivera")
        portfolio_url = st.text_input("Portfolio / Website", value="https://alexrivera.dev")

    col_form, col_monitor = st.columns([1.15, 1], gap="large")

    # Left Column: Target Job & Tailored Materials Form
    with col_form:
        st.subheader("1. Application Dispatch")
        with st.form("job_dispatch_form", clear_on_submit=False):
            c1, c2 = st.columns(2)
            with c1:
                company = st.text_input("Company Name *", placeholder="e.g., Acme Corp")
            with c2:
                role = st.text_input("Target Role *", placeholder="e.g., Principal Software Engineer")

            target_url = st.text_input(
                "Target ATS Job URL *",
                placeholder="https://boards.greenhouse.io/company/jobs/1234567",
            )

            uploaded_pdf = st.file_uploader(
                "Upload Tailored Resume (PDF)",
                type=["pdf"],
                help="Uploaded PDF is passed directly to Playwright's set_input_files.",
            )

            tailored_text = st.text_area(
                "Tailored Cover Letter / Resume Text",
                height=150,
                placeholder="Paste your tailored cover letter or summary text here...",
            )

            btn_col1, btn_col2 = st.columns(2)
            with btn_col1:
                launch_submitted = st.form_submit_button(
                    "🚀 Launch Automation Worker",
                    use_container_width=True,
                    type="primary",
                    disabled=st.session_state["awaiting_review"],
                )
            with btn_col2:
                save_draft_submitted = st.form_submit_button(
                    "💾 Save as Draft",
                    use_container_width=True,
                    disabled=st.session_state["awaiting_review"],
                )

    # Right Column: Live Execution Status & Human-in-the-Loop Gate
    with col_monitor:
        st.subheader("2. Live Execution Telemetry")
        progress_bar = st.progress(0, text="Phase: Idle")
        status_container = st.empty()

        # Save Draft Handler
        if save_draft_submitted:
            if not company.strip() or not role.strip() or not target_url.strip():
                st.error("Company, Role, and Target ATS Job URL are required.")
            else:
                draft_id = db.create_application(company, role, target_url, status="Draft")
                st.success(f"Saved application #{draft_id} ({company} - {role}) as Draft.")

        # Automation Worker Trigger Handler
        if launch_submitted:
            if not company.strip() or not role.strip() or not target_url.strip():
                st.error("Please provide Company Name, Target Role, and Target ATS Job URL.")
            elif not full_name.strip() or not email.strip():
                st.error("Full Name and Email Address are required in the sidebar profile.")
            else:
                # Persist uploaded PDF to a temporary path or generate fallback PDF from text
                if uploaded_pdf is not None:
                    tmp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix="_resume.pdf")
                    tmp_pdf.write(uploaded_pdf.getvalue())
                    tmp_pdf.close()
                    pdf_path = tmp_pdf.name
                else:
                    pdf_path = build_fallback_pdf(
                        tailored_text or f"Resume for {full_name} ({email})"
                    )

                payload = CandidatePayload(
                    full_name=full_name,
                    email=email,
                    phone=phone,
                    linkedin_url=linkedin_url,
                    github_url=github_url,
                    portfolio_url=portfolio_url,
                    resume_pdf_path=pdf_path,
                    tailored_text=tailored_text,
                )

                app_id = db.create_application(company, role, target_url, status="Queued")

                with status_container.status(
                    "Initializing ATS Automation Worker...", expanded=True
                ) as status_box:

                    def update_ui_phase(phase_name: str, pct: int, detail: str) -> None:
                        st.session_state["current_phase"] = phase_name
                        progress_bar.progress(
                            pct,
                            text=(
                                "Phase: Navigating -> Populating Fields -> Uploading Resume "
                                f"-> Awaiting Review -> Complete | Current: {phase_name}"
                            ),
                        )
                        status_box.update(label=f"[{phase_name}] {detail}", state="running")
                        status_box.write(f"✔️ **{phase_name}**: {detail}")

                    ok, result_msg, field_report = runtime.run(
                        automator.execute_workflow(
                            app_id=app_id,
                            url=target_url,
                            payload=payload,
                            headless=is_headless,
                            review_before_submit=review_before_submit,
                            phase_callback=update_ui_phase,
                        )
                    )
                    st.session_state["last_field_report"] = field_report

                    if ok and result_msg == "AWAITING_REVIEW":
                        st.session_state["awaiting_review"] = True
                        st.session_state["review_screenshot"] = runtime.run(
                            automator.capture_preview_screenshot()
                        )
                        status_box.update(
                            label="⏸️ Awaiting Operator Review Before Final Submit",
                            state="running",
                        )
                    elif ok:
                        st.session_state["awaiting_review"] = False
                        status_box.update(
                            label="✅ Application Workflow Complete!", state="complete"
                        )
                        st.toast(f"Application #{app_id} submitted!", icon="✅")
                    else:
                        st.session_state["awaiting_review"] = False
                        progress_bar.progress(100, text="Phase: Failed")
                        status_box.update(
                            label=f"❌ Automation Failed: {result_msg}", state="error"
                        )

        # Render Human-in-the-Loop Review Controls when paused at 'Awaiting Review'
        if st.session_state["awaiting_review"]:
            progress_bar.progress(90, text="Phase: Awaiting Review (Paused before final click)")
            st.warning(
                "**Review Before Submit Active:** Inspect the populated browser window (or preview below), "
                "solve any CAPTCHA challenges, and click **Confirm & Submit** to finalize."
            )

            if st.session_state["last_field_report"]:
                st.markdown("**Populated Field Telemetry:**")
                badges = [
                    f"{'🟢' if v else '⚪'} {k}"
                    for k, v in st.session_state["last_field_report"].items()
                ]
                st.caption(" | ".join(badges))

            if st.session_state["review_screenshot"]:
                with st.expander("🔍 Browser Viewport Snapshot", expanded=False):
                    st.image(
                        st.session_state["review_screenshot"],
                        caption="Current ATS Form State",
                        use_container_width=True,
                    )

            rev_col1, rev_col2 = st.columns(2)
            with rev_col1:
                if st.button(
                    "✅ Confirm & Click Submit", type="primary", use_container_width=True
                ):
                    with status_container.status(
                        "Finalizing submission...", expanded=True
                    ) as fin_status:

                        def fin_callback(phase_name: str, pct: int, detail: str) -> None:
                            progress_bar.progress(pct, text=f"Phase: {phase_name}")
                            fin_status.write(f"✔️ **{phase_name}**: {detail}")

                        ok, msg, _ = runtime.run(automator.finalize_submission(fin_callback))
                        st.session_state["awaiting_review"] = False
                        st.session_state["review_screenshot"] = None
                        if ok:
                            fin_status.update(label="✅ Complete — Submitted!", state="complete")
                            st.success("Application successfully submitted and logged!")
                        else:
                            fin_status.update(label=f"❌ Failed: {msg}", state="error")
                            st.error(msg)
                    st.rerun()

            with rev_col2:
                if st.button("🛑 Abort & Mark Failed", use_container_width=True):
                    if automator.active_app_id is not None:
                        db.update_status(
                            automator.active_app_id,
                            "Failed",
                            error_log="Aborted manually by operator during review gate.",
                        )
                    runtime.run(automator.close_session())
                    st.session_state["awaiting_review"] = False
                    st.session_state["review_screenshot"] = None
                    st.rerun()

    # Bottom Section: SQLite Applications Ledger
    st.divider()
    st.subheader("📊 SQLite Applications Ledger (`applications.db`)")
    records = db.fetch_all()
    if records:
        df = pd.DataFrame(records)
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "company": st.column_config.TextColumn("Company"),
                "role": st.column_config.TextColumn("Role"),
                "url": st.column_config.LinkColumn("Job URL"),
                "status": st.column_config.TextColumn("Status"),
                "timestamp": st.column_config.TextColumn("Last Updated"),
                "error_log": st.column_config.TextColumn("Error Log / Telemetry"),
            },
        )
    else:
        st.info("No applications recorded yet. Use the form above to dispatch or save a draft.")


if __name__ == "__main__":
    render_app()