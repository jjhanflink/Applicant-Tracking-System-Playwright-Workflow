import os
import json
import logging
from typing import Dict, List, Any
from groq import Groq

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"

def build_system_prompt(profile: Dict[str, Any]) -> str:
    return f"""You are a deterministic ATS Semantic Mapping Engine.
Your job is to map scraped job application questions to a candidate's verified profile.

CANDIDATE PROFILE JSON:{json.dumps(profile, indent=2)}

================================================================================
CRITICAL ROUTING RULES (UNIVERSAL VS. FALLBACK STRATEGY)
================================================================================

1. UNIVERSAL FACTS YOU ARE ALLOWED TO ANSWER:
  - Identity & Contact: Full Name ("Joshua Jason Hanflink"), Signature ("Joshua Hanflink"), Email ("jjhanflink@gmail.com"), Phone ("3369296685").
  - Dual-Address Routing Rule:
    - DEFAULT (Current / School Address): For any general "Location", "City", "Current Address", "Mailing Address", "State", or "Zip/Postal Code" question, ALWAYS use the Raleigh address -> City: "Raleigh", State: "NC" / "North Carolina", Zip: "27606", Street: "NC State University".
    - PERMANENT ADDRESS EXCEPTION: ONLY if the question label explicitly asks for "Permanent Address", "Home Address", or "Permanent Street/City/Zip", use -> Street: "701 Blanton Place", City: "Greensboro", State: "NC" / "North Carolina", Zip: "27408".
  - Education:
    - School: "North Carolina State University" (or closest exact option match like "NC State University").
    - Degree Pursuing: "B.S. Electrical & Computer Engineering" (or closest exact match in options like "Bachelor's" / "B.S.").
    - Academic Standing: "Senior" / "4th Year" / "Undergraduate".
    - Past Degrees Completed: If asked about already-completed degrees, select "I have not yet completed a degree" or "None / Currently Pursuing".
    - GPA: Exact is "3.45". If only bracketed or rounded options exist (e.g., "3.5", "3.0 - 3.5"), round to 3.5.
    - Start Date: MM/YYYY as "08/2023", MM/DD/YYYY as "08/15/2023".
    - Graduation Date: MM/YYYY as "12/2027", MM/DD/YYYY as "12/31/2027".
  - Links: LinkedIn, GitHub, Portfolio ("https://jjhanflink.github.io").
  - Work Authorization: US Citizen ("Yes"), Authorized to work in US ("Yes"), Sponsorship needed ("No"), Over 18/21 ("Yes").
  - Terms / Privacy Notice / Accuracy acknowledgment -> "Yes" or select the acknowledgment checkbox.
  - EEO: Gender ("Male"), Hispanic/Latino ("Yes"), Race/Ethnicity ("Hispanic or Latino"), Veteran ("Not a protected veteran"), Disability ("Yes, I have a disability, or have had one in the past").

2. STRICT FALLBACK ("NEEDS_HUMAN") - DO NOT GUESS:
   You MUST return the exact string "NEEDS_HUMAN" for ANY question that is company-specific, role-specific, or not explicitly covered in Rule 1, including:
- Job / Office Location Preferences (e.g., "Which office location are you applying for?").
- University clubs or student organizations.
- Referral sources (e.g., "How did you hear about this job?").
- Start dates for the job itself.
- Compensation / Salary / Hourly rate expectations.
- Essay questions, cover letters, or technical project descriptions.

3. OUTPUT CONSTRAINTS:
- For "select" or "radio": value MUST match an exact string from that question's options list. If unsure, return "NEEDS_HUMAN".
  - For "multiselect" or "checkbox": return a JSON array of matching option strings (e.g., ["Yes"]).
  - For "text": return the exact string, or "NEEDS_HUMAN".

Return ONLY a valid JSON object mapping each qid to its answer or "NEEDS_HUMAN".
"""

def validate_and_sanitize_llm_output(
    questions: List[Dict[str, Any]],
    raw_mapping: Dict[str, Any]) -> Dict[str, Any]:
    validated: Dict[str, Any] = {}
    q_lookup = {q["qid"]: q for q in questions}

    for qid, q_meta in q_lookup.items():
        if qid not in raw_mapping:
            validated[qid] = "NEEDS_HUMAN"
            continue

        ans = raw_mapping[qid]
        q_type = q_meta.get("type", "text")
        options = q_meta.get("options", [])

        if ans == "NEEDS_HUMAN" or ans is None or ans == "":
            validated[qid] = "NEEDS_HUMAN"
            continue

        if q_type in ("select", "radio") and options:
            if isinstance(ans, list):
                ans = ans[0] if ans else "NEEDS_HUMAN"
            ans_str = str(ans).strip()
            exact = next((opt for opt in options if opt.strip() == ans_str), None)
            ci = next((opt for opt in options if opt.strip().lower() == ans_str.lower()), None)

            if exact:
                validated[qid] = exact
            elif ci:
                validated[qid] = ci
            else:
                logger.warning(f"[{qid}] LLM answer '{ans_str}' not in options. Downgrading to NEEDS_HUMAN.")
                validated[qid] = "NEEDS_HUMAN"

        elif q_type in ("multiselect", "checkbox") and options:
            if isinstance(ans, str):
                ans = [ans]
            clean_list = []
            for item in ans:
                match = next((opt for opt in options if opt.strip().lower() == str(item).strip().lower()), None)
                if match:
                    clean_list.append(match)
            validated[qid] = clean_list if clean_list else "NEEDS_HUMAN"
        else:
            validated[qid] = ", ".join(str(x) for x in ans) if isinstance(ans, list) else str(ans).strip()

    return validated

def map_questions_with_llama(
    questions: List[Dict[str, Any]],
    profile: Dict[str, Any],
    api_key: str = None) -> Dict[str, Any]:
    if not questions:
        return {}

    resolved_key = api_key or os.environ.get("GROQ_API_KEY")
    if not resolved_key:
        logger.error("GROQ_API_KEY is missing. Marking all questions as NEEDS_HUMAN.")
        return {q["qid"]: "NEEDS_HUMAN" for q in questions}

    client = Groq(api_key=resolved_key)
    compact_questions = [
        {"qid": q["qid"], "type": q["type"], "label": q["label"], "options": q.get("options", [])}
        for q in questions
    ]

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": build_system_prompt(profile)},
                {"role": "user", "content": f"Map these ATS questions using the profile:\n\n{json.dumps(compact_questions, indent=2)}"}
            ],
            temperature=0.0,
            response_format={"type": "json_object"}
        )
        parsed = json.loads(response.choices[0].message.content)
        return validate_and_sanitize_llm_output(questions, parsed)
    except Exception as exc:
        logger.error(f"Groq API call failed: {exc}")
        return {q["qid"]: "NEEDS_HUMAN" for q in questions}
