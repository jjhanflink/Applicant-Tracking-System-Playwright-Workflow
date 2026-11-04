import os
import re
import json
import time
import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional
from playwright.async_api import async_playwright, Page
from llama_engine import map_questions_with_llama

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

async def dismiss_cookie_banners(page: Page) -> None:
    banner_selectors = [
        "button:has-text('Accept All')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Accept')",
        "button:has-text('I Agree')",
        "button:has-text('Got it')",
        "#onetrust-accept-btn-handler",
        ".cc-btn.cc-dismiss"
    ]
    for sel in banner_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=500):
                await loc.click(timeout=1000)
                await asyncio.sleep(0.3)
        except Exception:
            continue

async def scroll_entire_page(page: Page) -> None:
    await page.evaluate(
        """async () => {
            const delay = ms => new Promise(res => setTimeout(res, ms));
            const height = document.body.scrollHeight;
            for (let y = 0; y <= height; y += 450) {
                window.scrollTo(0, y);
                await delay(80);
            }
            window.scrollTo(0, 0);
        }"""
    )
    await asyncio.sleep(0.4)

async def upload_resume_and_wait_for_lock(page: Page, profile: Dict[str, Any]) -> bool:
    """LESSON #1 FIX: Enforces a hard 6.0s sleep for Lever's resume parser lock."""
    resume_path = Path(profile.get("identity", {}).get("resume_path", "resume.pdf"))
    if not resume_path.exists():
        logger.warning(f"Resume not found at '{resume_path.resolve()}'. Skipping upload.")
        return False

    file_selectors = [
        "input#resume-upload-input",
        "input[name='resume']",
        "input[data-qa='input-resume']",
        "input[type='file']"
    ]
    for sel in file_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                logger.info(f"Uploading resume via '{sel}'...")
                await loc.set_input_files(str(resume_path.resolve()))
                logger.info("Enforcing hard 6.0s sleep for Lever parser DOM lock...")
                await asyncio.sleep(6.0)
                return True
        except Exception as exc:
            logger.warning(f"Upload attempt failed on '{sel}': {exc}")
    return False

async def fill_core_identity_fields(page: Page, profile: Dict[str, Any]) -> None:
    ident = profile.get("identity", {})
    curr_addr = profile.get("addresses", {}).get("current_address", {})
    edu = profile.get("education", {})
    links = profile.get("links", {})

    core_payload = {
        "full_name": ident.get("full_name", "Joshua Jason Hanflink"),
        "first_name": ident.get("first_name", "Joshua"),
        "last_name": ident.get("last_name", "Hanflink"),
        "email": ident.get("email", "jjhanflink@gmail.com"),
        "phone": ident.get("phone", "3369296685"),
        "location": curr_addr.get("location_string", "Raleigh, NC, USA"),
        "org": edu.get("school", "North Carolina State University"),
        "linkedin": links.get("linkedin", ""),
        "github": links.get("github", ""),
        "portfolio": links.get("portfolio", "https://jjhanflink.github.io")
    }

    await page.evaluate(
        """(data) => {
            function setNativeValue(el, val) {
                if (!el || !val) return;
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                if (setter) setter.call(el, val);
                else el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            }
            const rules = [
                { sel: "input[name='name']", val: data.full_name },
                { sel: "input[name='first_name'], input[id*='first_name' i]", val: data.first_name },
                { sel: "input[name='last_name'], input[id*='last_name' i]", val: data.last_name },
                { sel: "input[name='email'], input[type='email']", val: data.email },
                { sel: "input[name='phone'], input[type='tel']", val: data.phone },
                { sel: "input[name='location'], input.location-input", val: data.location },
                { sel: "input[name='org'], input[name='company']", val: data.org },
                { sel: "input[name*='linkedin' i]", val: data.linkedin },
                { sel: "input[name*='github' i]", val: data.github },
                { sel: "input[name*='portfolio' i], input[name*='website' i]", val: data.portfolio }
            ];
            for (const rule of rules) {
                document.querySelectorAll(rule.sel).forEach(node => {
                    if (node.offsetParent !== null && !node.disabled && node.type !== 'hidden') {
                        setNativeValue(node, rule.val);
                    }
                });
            }
        }""",
        core_payload
    )

    try:
        await asyncio.sleep(0.5)
        dropdown_item = page.locator(".ac-results li, .autocomplete-suggestion, .ui-menu-item").first
        if await dropdown_item.is_visible(timeout=500):
            await dropdown_item.click(timeout=600)
    except Exception:
        pass

async def extract_clean_schema(page: Page) -> List[Dict[str, Any]]:
    """LESSON #2 FIX: Full-DOM sweep targeting human-readable labels and ignoring hidden IDs."""
    schema = await page.evaluate(
        """() => {
            function isVisible(el) {
                if (!el || el.getAttribute('aria-hidden') === 'true') return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' && el.offsetParent !== null;
            }
            function isBackendIdNoise(str) {
                if (!str) return true;
                const s = str.trim();
                return /^cards\\[[a-z0-9-]+\\]/i.test(s) || /^[0-9a-f]{8}-[0-9a-f]{4}/i.test(s) || /^field[_\\d]+$/i.test(s);
            }
            function cleanText(raw) {
                return (raw || "").replace(/\\s+/g, " ").replace(/\\*+$/g, "").replace(/\\(required\\)/ig, "").trim();
            }

            const CORE_SKIP = new Set(['name', 'first_name', 'last_name', 'email', 'phone', 'org', 'location', 'resume']);
            document.querySelectorAll('[data-ats-qid]').forEach(el => el.removeAttribute('data-ats-qid'));

            const results = [];
            let qCounter = 1;
            const processed = new Set();
            const containerSels = ['li.application-question', '.application-question', '.application-additional', 'fieldset', '.field', '.form-group'];
            const containers = Array.from(document.querySelectorAll(containerSels.join(', ')));

            for (const container of containers) {
                if (!isVisible(container)) continue;
                const nested = container.querySelector('li.application-question, .application-question, fieldset');
                if (nested && nested !== container) continue;

                const inputs = Array.from(container.querySelectorAll('input:not([type="hidden"]):not([type="file"]):not([type="submit"]), select, textarea'))
                    .filter(inp => isVisible(inp) && !processed.has(inp));
                if (inputs.length === 0) continue;

                const firstInputName = inputs[0].getAttribute('name') || '';
                if (CORE_SKIP.has(firstInputName)) {
                    inputs.forEach(i => processed.add(i));
                    continue;
                }

                let labelText = "";
                const labelCandidates = container.querySelectorAll('.application-label .text, .application-label, legend, .section-headline, label:not(.Option-label)');
                for (const lbl of labelCandidates) {
                    if (lbl.querySelector('input[type="radio"], input[type="checkbox"]')) continue;
                    const txt = cleanText(lbl.innerText || lbl.textContent);
                    if (txt && !isBackendIdNoise(txt) && txt.length > 1) {
                        labelText = txt;
                        break;
                    }
                }
                if (!labelText) {
                    const aria = cleanText(inputs[0].getAttribute('aria-label') || inputs[0].getAttribute('placeholder') || '');
                    if (!isBackendIdNoise(aria)) labelText = aria;
                }
                if (!labelText) continue;

                let qType = 'text';
                let options = [];
                let filled = false;

                const selectEl = inputs.find(i => i.tagName.toLowerCase() === 'select');
                const radioEls = inputs.filter(i => i.type === 'radio');
                const checkEls = inputs.filter(i => i.type === 'checkbox');
                const textEl = inputs.find(i => ['text', 'email', 'tel', 'number', 'url', 'textarea'].includes(i.type || i.tagName.toLowerCase()));

                if (selectEl) {
                    qType = selectEl.multiple ? 'multiselect' : 'select';
                    options = Array.from(selectEl.options)
                        .filter(opt => opt.value !== '' && !/^(select|choose|--)/i.test(opt.text.trim()))
                        .map(opt => cleanText(opt.text));
                    filled = selectEl.value !== '';
                } else if (radioEls.length > 0) {
                    qType = 'radio';
                    for (const r of radioEls) {
                        const parentLbl = r.closest('label') || container.querySelector(`label[for="${r.id}"]`);
                        const optTxt = cleanText(parentLbl ? parentLbl.innerText : r.value);
                        if (optTxt && !isBackendIdNoise(optTxt)) options.push(optTxt);
                        if (r.checked) filled = true;
                    }
                } else if (checkEls.length > 0) {
                    qType = (checkEls.length > 1 || /select all that apply/i.test(labelText)) ? 'multiselect' : 'checkbox';
                    for (const c of checkEls) {
                        const parentLbl = c.closest('label') || container.querySelector(`label[for="${c.id}"]`);
                        let optTxt = cleanText(parentLbl ? parentLbl.innerText : c.value);
                        if (!optTxt || isBackendIdNoise(optTxt)) optTxt = "Yes";
                        options.push(optTxt);
                        if (c.checked) filled = true;
                    }
                } else if (textEl) {
                    qType = 'text';
                    filled = Boolean(textEl.value && textEl.value.trim().length > 0);
                }

                const qid = `q_${qCounter++}`;
                container.setAttribute('data-ats-qid', qid);
                inputs.forEach(i => processed.add(i));

                results.push({
                    qid: qid,
                    type: qType,
                    label: labelText,
                    options: Array.from(new Set(options)),
                    filled: filled
                });
            }
            return results;
        }"""
    )
    return schema

async def apply_answer_to_dom(page: Page, qid: str, q_type: str, answer: Any) -> bool:
    """LESSON #3 FIX: Evaluates JS to trigger click and bubbling change/input events."""
    if answer == "NEEDS_HUMAN" or answer is None or answer == "":
        return False

    return await page.evaluate(
        """({ qid, qType, answer }) => {
            const container = document.querySelector(`[data-ats-qid="${qid}"]`);
            if (!container) return false;
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

            const selectEl = container.querySelector('select');
            if (selectEl && (qType === 'select' || qType === 'multiselect')) {
                const targets = Array.isArray(answer) ? answer.map(norm) : [norm(String(answer))];
                let matched = false;
                Array.from(selectEl.options).forEach(opt => {
                    const optText = norm(opt.text);
                    const optVal = norm(opt.value);
                    if (targets.includes(optText) || targets.includes(optVal)) {
                        opt.selected = true;
                        if (!selectEl.multiple) selectEl.value = opt.value;
                        matched = true;
                    }
                });
                if (matched) {
                    selectEl.dispatchEvent(new Event('input', { bubbles: true }));
                    selectEl.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
            }

            if (qType === 'radio') {
                const target = norm(Array.isArray(answer) ? answer[0] : String(answer));
                for (const r of Array.from(container.querySelectorAll('input[type="radio"]'))) {
                    const lbl = r.closest('label') || container.querySelector(`label[for="${r.id}"]`);
                    const lblText = norm(lbl ? lbl.innerText : r.value);
                    if (lblText === target || norm(r.value) === target || lblText.includes(target)) {
                        r.click();
                        r.checked = true;
                        r.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                }
            }

            if (qType === 'checkbox' || qType === 'multiselect') {
                const targets = (Array.isArray(answer) ? answer : [String(answer)]).map(norm);
                let anyChecked = false;
                for (const c of Array.from(container.querySelectorAll('input[type="checkbox"]'))) {
                    const lbl = c.closest('label') || container.querySelector(`label[for="${c.id}"]`);
                    const lblText = norm(lbl ? lbl.innerText : c.value);
                    const shouldCheck = targets.some(t => lblText === t || norm(c.value) === t || lblText.includes(t) || t === 'yes');
                    if (shouldCheck && !c.checked) {
                        c.click();
                        c.checked = true;
                        c.dispatchEvent(new Event('change', { bubbles: true }));
                        anyChecked = true;
                    }
                }
                return anyChecked;
            }

            const textEl = container.querySelector('textarea, input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]):not([type="file"])');
            if (textEl) {
                const valStr = Array.isArray(answer) ? answer.join(', ') : String(answer);
                const proto = textEl.tagName.toLowerCase() === 'textarea' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(textEl, valStr);
                else textEl.value = valStr;
                textEl.dispatchEvent(new Event('input', { bubbles: true }));
                textEl.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
            return false;
        }""",
        {"qid": qid, "qType": q_type, "answer": answer}
    )

async def execute_application(
    url: str,
    profile: Dict[str, Any],
    custom_answers: Optional[Dict[str, Any]] = None,
    auto_submit: bool = False,
    headless: bool = True,
    groq_api_key: Optional[str] = None) -> Dict[str, Any]:
    custom_answers = custom_answers or {}
    timestamp = int(time.time())
    screenshot_path = str(SCREENSHOT_DIR / f"ats_preview_{timestamp}.png")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1280, "height": 1000})
        page = await context.new_page()

        try:
            logger.info(f"Navigating to {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(1.5)

            await dismiss_cookie_banners(page)
            await scroll_entire_page(page)

            # 1. Upload Resume + 6.0s Lock Wait
            await upload_resume_and_wait_for_lock(page, profile)

            # 2. Fill Deterministic Core Fields
            await fill_core_identity_fields(page, profile)
            await asyncio.sleep(0.5)

            # 3. Extract Clean Schema
            schema = await extract_clean_schema(page)

            # 4. Separate human custom_answers vs questions for Llama
            questions_for_llama = []
            resolved_by_qid = {}
            for q in schema:
                qid, label = q["qid"], q["label"]
                if label in custom_answers and custom_answers[label] not in ("", "NEEDS_HUMAN", None, []):
                    resolved_by_qid[qid] = custom_answers[label]
                elif qid in custom_answers and custom_answers[qid] not in ("", "NEEDS_HUMAN", None, []):
                    resolved_by_qid[qid] = custom_answers[qid]
                else:
                    questions_for_llama.append(q)

            # 5. Semantic Mapping via Groq Llama 3.3
            if questions_for_llama:
                llama_mapped = map_questions_with_llama(questions_for_llama, profile, groq_api_key)
                resolved_by_qid.update(llama_mapped)

            # 6. Apply answers to live DOM
            needs_human_list = []
            applied_summary = {}
            for q in schema:
                qid, q_type, label = q["qid"], q["type"], q["label"]
                ans = resolved_by_qid.get(qid, "NEEDS_HUMAN")
                if ans == "NEEDS_HUMAN" or ans is None or ans == "" or ans == []:
                    needs_human_list.append(q)
                else:
                    applied_ok = await apply_answer_to_dom(page, qid, q_type, ans)
                    if applied_ok:
                        applied_summary[label] = ans
                    else:
                        needs_human_list.append(q)

            await asyncio.sleep(0.8)

            # 7. Submit ONLY if auto_submit=True and no NEEDS_HUMAN remain
            submitted = False
            if auto_submit and len(needs_human_list) == 0:
                submit_selectors = ["button#btn-submit", "button.postings-btn-submit", "button[type='submit']", "button:has-text('Submit Application')"]
                for sel in submit_selectors:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click()
                        await asyncio.sleep(4.0)
                        submitted = True
                        break

            # 8. Screenshot
            await page.screenshot(path=screenshot_path, full_page=True)

            status = "SUBMITTED" if submitted else ("NEEDS_HUMAN" if needs_human_list else "READY_TO_SUBMIT")
            return {
                "status": status,
                "submitted": submitted,
                "screenshot_path": screenshot_path,
                "schema": schema,
                "needs_human": needs_human_list,
                "applied_answers": applied_summary,
                "error": None
            }
        except Exception as exc:
            logger.exception(f"Execution error: {exc}")
            return {
                "status": "FAILED",
                "submitted": False,
                "screenshot_path": screenshot_path if Path(screenshot_path).exists() else None,
                "schema": [],
                "needs_human": [],
                "applied_answers": {},
                "error": str(exc)
            }
        finally:
            await browser.close()
