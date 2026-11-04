import os
import sys
import json
import sqlite3
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List
import streamlit as st
from ats_scraper import execute_application

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

DB_PATH = "ats_queue.db"
PROFILE_PATH = "candidate_profile.json"

st.set_page_config(page_title="Zero-Touch ATS Auto-Applier", page_icon="⚡", layout="wide")

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                company TEXT DEFAULT '',
                role TEXT DEFAULT '',
                status TEXT DEFAULT 'QUEUED',
                schema_json TEXT DEFAULT '[]',
                custom_answers_json TEXT DEFAULT '{}',
                applied_answers_json TEXT DEFAULT '{}',
                needs_human_json TEXT DEFAULT '[]',
                screenshot_path TEXT DEFAULT '',
                error_msg TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
            """
        )
        conn.commit()

def add_job_to_queue(url: str, company: str = "", role: str = "") -> bool:
    url = url.strip()
    if not url:
        return False
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO job_queue (url, company, role, status, updated_at) VALUES (?, ?, ?, 'QUEUED', ?)",
                (url, company.strip(), role.strip(), now)
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def delete_job_from_queue(job_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM job_queue WHERE id = ?", (job_id,))
        conn.commit()

def get_all_jobs() -> List[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM job_queue ORDER BY id DESC").fetchall()]

def get_job_by_id(job_id: int) -> Dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM job_queue WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else {}

def update_job_execution_state(job_id: int, result: Dict[str, Any], custom_answers: Dict[str, Any]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE job_queue
            SET status = ?, schema_json = ?, custom_answers_json = ?, applied_answers_json = ?,
                needs_human_json = ?, screenshot_path = ?, error_msg = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                result.get("status", "FAILED"),
                json.dumps(result.get("schema", [])),
                json.dumps(custom_answers),
                json.dumps(result.get("applied_answers", {})),
                json.dumps(result.get("needs_human", [])),
                result.get("screenshot_path") or "",
                result.get("error") or "",
                now,
                job_id
            )
        )
        conn.commit()

def load_candidate_profile() -> Dict[str, Any]:
    path = Path(PROFILE_PATH)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

def run_async_task(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()

init_db()
profile = load_candidate_profile()

st.title("⚡ Zero-Touch ATS Auto-Applier")
st.caption("Playwright Async + Groq Llama 3.3 Semantic Mapping | Raleigh (Current) vs. Greensboro (Permanent)")

with st.sidebar:
    st.header("⚙️ Controls")
    groq_api_key = st.text_input("Groq API Key", value=os.environ.get("GROQ_API_KEY", ""), type="password")
    headless_mode = st.toggle("Run Browser Headless", value=False)
    if profile:
        st.divider()
        st.markdown(f"**Default / School:** `{profile['addresses']['current_address']['location_string']}`")
        st.markdown(f"**Permanent:** `{profile['addresses']['permanent_address']['location_string']}`")

tab_queue, tab_bridge = st.tabs(["📥 Tab 1: Job Queue", "🌉 Tab 2: The Manual Bridge & Verifier"])

with tab_queue:
    st.subheader("Add Application URL")
    with st.form("add_job_form", clear_on_submit=True):
        c1, c2, c3 = st.columns([3, 1.5, 1.5])
        with c1: new_url = st.text_input("ATS Posting URL")
        with c2: new_company = st.text_input("Company Name")
        with c3: new_role = st.text_input("Role Title")
        if st.form_submit_button("➕ Add to Queue"):
            if add_job_to_queue(new_url, new_company, new_role):
                st.success(f"Added to queue.")
                st.rerun()

    st.divider()
    jobs = get_all_jobs()
    if not jobs:
        st.info("No applications queued.")
    for j in jobs:
        col_id, col_info, col_st, col_act = st.columns([0.6, 4.2, 1.5, 1.0])
        with col_id: st.markdown(f"**#{j['id']}**")
        with col_info: st.markdown(f"**{j['company']} - {j['role']}** | [{j['url']}]({j['url']})")
        with col_st: st.markdown(f"`{j['status']}`")
        with col_act:
            if st.button("🗑️", key=f"d_{j['id']}"):
                delete_job_from_queue(j['id'])
                st.rerun()

with tab_bridge:
    jobs = get_all_jobs()
    if not jobs:
        st.info("Queue an application URL in Tab 1 first.")
    else:
        job_map = {f"#{j['id']} [{j['status']}] {j['company']} ({j['url']})": j["id"] for j in jobs}
        active_label = st.selectbox("Select Active Application", options=list(job_map.keys()))
        selected_id = job_map[active_label]
        active_job = get_job_by_id(selected_id)

        custom_ans = json.loads(active_job["custom_answers_json"] or "{}")
        needs_human = json.loads(active_job["needs_human_json"] or "[]")
        applied_ans = json.loads(active_job["applied_answers_json"] or "{}")

        b1, b2 = st.columns(2)
        with b1:
            preview_btn = st.button("🔍 Preview & Verify (auto_submit=False)", use_container_width=True)
        with b2:
            submit_disabled = len(needs_human) > 0 or active_job["status"] == "QUEUED"
            final_btn = st.button("🚀 FINAL: Submit (auto_submit=True)", type="primary", use_container_width=True, disabled=submit_disabled)

        if preview_btn:
            with st.spinner("Executing Playwright & Llama 3.3 mapping..."):
                res = run_async_task(execute_application(active_job["url"], profile, custom_ans, False, headless_mode, groq_api_key))
                update_job_execution_state(selected_id, res, custom_ans)
                st.rerun()

        if final_btn:
            with st.spinner("Submitting application..."):
                res = run_async_task(execute_application(active_job["url"], profile, custom_ans, True, headless_mode, groq_api_key))
                update_job_execution_state(selected_id, res, custom_ans)
                st.rerun()

        st.divider()
        left, right = st.columns([1.2, 1.0])
        with left:
            st.subheader("Manual Bridge")
            if needs_human:
                with st.form(key=f"nh_form_{selected_id}"):
                    updated_inputs = dict(custom_ans)
                    for idx, q in enumerate(needs_human):
                        qid, q_type, label, opts = q["qid"], q.get("type", "text"), q["label"], q.get("options", [])
                        k = f"nh_{selected_id}_{qid}_{idx}"
                        if (q_type in ("multiselect", "checkbox") or "select all" in label.lower()) and opts:
                            updated_inputs[label] = st.multiselect(label=f"📌 {label}", options=opts, default=[v for v in custom_ans.get(label, []) if v in opts], key=k)
                        elif q_type in ("select", "radio") and opts:
                            s_opts = ["-- Select --"] + opts
                            def_idx = s_opts.index(custom_ans[label]) if custom_ans.get(label) in s_opts else 0
                            val = st.selectbox(label=f"📌 {label}", options=s_opts, index=def_idx, key=k)
                            if val != "-- Select --": updated_inputs[label] = val
                        else:
                            updated_inputs[label] = st.text_area(label=f"📌 {label}", value=str(custom_ans.get(label, "")), key=k)

                    if st.form_submit_button("💾 Save & Preview Again", use_container_width=True):
                        with st.spinner("Re-verifying with your answers..."):
                            res = run_async_task(execute_application(active_job["url"], profile, updated_inputs, False, headless_mode, groq_api_key))
                            update_job_execution_state(selected_id, res, updated_inputs)
                            st.rerun()
            elif active_job["status"] == "READY_TO_SUBMIT":
                st.success("All fields mapped. Ready to submit!")

            if applied_ans:
                with st.expander(f"Auto-Mapped Answers ({len(applied_ans)})"):
                    st.json(applied_ans)

        with right:
            st.subheader("Verification Screenshot")
            if active_job.get("screenshot_path") and Path(active_job["screenshot_path"]).exists():
                st.image(active_job["screenshot_path"], use_container_width=True)
            else:
                st.info("Run Preview to see the captured DOM screenshot.")
