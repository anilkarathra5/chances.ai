"""
Core resume-tailoring logic. UI-agnostic so it can be reused, tested, or driven
from a script as well as from the Streamlit app.

One Gemini call:
  tailor_resume -> analyzes the job description, selects + rewrites the most
                   relevant experience, organizes it into the PDF's sections, and
                   returns an honest fit assessment — all in a single request
                   (halves free-tier quota usage vs. a separate analysis call).

Guardrails:
  - The model may ONLY select and rephrase accomplishments that exist in the
    database. It must never invent experience, employers, metrics, or dates.
  - Real metrics are preserved exactly. Where an accomplishment's notes flag a
    figure as unrealized/projected, that figure must not be claimed.
  - "match_score" is a heuristic fit estimate (0-100), NOT an interview
    probability. We have no outcome data to calibrate a real probability.
"""

import json
import re
from datetime import datetime
from google import genai
from google.genai import types

# Switchable LLM backends. Gemini is the default (free tier); Claude is optional.
PROVIDERS = {
    "Google Gemini": {
        "models": ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash"],
        "key_env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "key_label": "Google Gemini API key",
        "key_help": "Free key from https://aistudio.google.com/apikey",
    },
    "Anthropic Claude": {
        "models": ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
        "key_env": ["ANTHROPIC_API_KEY"],
        "key_label": "Anthropic API key",
        "key_help": "Key from https://console.anthropic.com/settings/keys (paid usage).",
    },
}
DEFAULT_PROVIDER = "Google Gemini"
DEFAULT_MODEL = "gemini-3.8-flash"


def get_client(provider: str, api_key: str):
    """Create the backend client for the chosen provider."""
    if provider == "Anthropic Claude":
        from anthropic import Anthropic  # lazy import: only needed if Claude is used
        return Anthropic(api_key=api_key)
    return genai.Client(api_key=api_key)


def _call(provider: str, client, model: str, prompt: str, max_tokens: int = 6144) -> str:
    """Dispatch a single JSON-returning completion to the chosen provider."""
    if provider == "Anthropic Claude":
        return _call_claude(client, model, prompt, max_tokens)
    return _call_gemini(client, model, prompt, max_tokens)


def _call_gemini(client, model: str, prompt: str, max_tokens: int) -> str:
    # Ask Gemini for JSON directly; _extract_json still defends against fences/prose
    # if the model ignores the hint. Low temperature keeps rephrasing faithful.
    cfg = dict(
        max_output_tokens=max_tokens,
        temperature=0.3,
        response_mime_type="application/json",
    )
    # Gemini 2.5/3.x models "think" by default, drawing from max_output_tokens —
    # which can truncate the JSON. Gemini 3 replaced thinking_budget with
    # thinking_level; "minimal" isn't accepted on all gemini-3 models (some
    # reject it with INVALID_ARGUMENT), so "low" is the safe minimum across the
    # family. 2.5 still uses the older thinking_budget=0 to turn it off.
    if model.startswith("gemini-3"):
        cfg["thinking_config"] = types.ThinkingConfig(thinking_level="low")
    elif model.startswith("gemini-2.5"):
        cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

    resp = client.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(**cfg),
    )
    try:
        finish = getattr(resp.candidates[0].finish_reason, "name", None)
    except (AttributeError, IndexError, TypeError):
        finish = None
    if finish == "MAX_TOKENS":
        raise RuntimeError(
            "Gemini ran out of output tokens before completing the JSON. "
            "Try the gemini-2.5-flash model, or a smaller experience database."
        )
    text = resp.text
    if not text:
        raise RuntimeError(
            "Empty response from Gemini (the request may have been blocked, e.g. "
            "by a safety filter). Try a different model or rephrase the input."
        )
    return text


def _call_claude(client, model: str, prompt: str, max_tokens: int) -> str:
    # Claude has no JSON-mode flag, but the prompts already demand JSON-only output
    # and _extract_json strips any stray fences/prose. No "temperature" kwarg:
    # anthropic SDK v1.0 dropped temperature/top_p/top_k from Messages.create()
    # entirely, and newer Claude models reject non-default values server-side —
    # fidelity comes from the prompt's own low-temperature-style instructions.
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(getattr(b, "text", "") for b in resp.content
                   if getattr(b, "type", None) == "text")
    if not text:
        raise RuntimeError(
            "Empty response from Claude (the request may have been refused or hit "
            "the token limit). Try a different model or shorten the input."
        )
    return text


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


# Strings the model sometimes emits for an unknown field; we blank them so
# placeholder text never reaches the PDF (e.g. an "N/A" in a location column).
_PLACEHOLDERS = {"n/a", "n.a.", "na", "none", "null", "tbd", "not applicable", "unknown"}


def _clean_placeholders(obj):
    if isinstance(obj, dict):
        return {k: _clean_placeholders(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_placeholders(v) for v in obj]
    if isinstance(obj, str):
        return "" if obj.strip().lower() in _PLACEHOLDERS else obj
    return obj


_MONEY_RE = re.compile(r"~?\$\d[\d.,]*[KkMmBb]?")
# If a metric note flags its figure as projected/unrealized, never surface it.
_PROJECTED_FLAGS = ("project", "not implemented", "unimplemented", "unrealized",
                    "do not claim", "never materialized", "do not use")


def _ensure_absolute_figures(result: dict, database: dict) -> dict:
    """Make sure a REALIZED absolute figure recorded in a metric "note" (e.g. the
    "~$3 per unit" behind a "15%" result) survives into the rewritten bullet.

    Deliberately conservative: it skips any note flagged projected/unrealized
    (so a projected/unrealized figure is never injected), only looks at relative "%"
    metrics, and edits a bullet only when it can match exactly one — otherwise it
    leaves the text untouched.
    """
    resume = result.get("resume") or {}
    bullet_lists = []
    for sec in ("work_experience", "projects", "volunteering"):
        for entry in resume.get(sec) or []:
            if isinstance(entry.get("bullets"), list):
                bullet_lists.append(entry["bullets"])

    accs = {a["id"]: a for a in database.get("accomplishments", [])}
    ids = result.get("selected_accomplishment_ids") or list(accs.keys())

    for aid in ids:
        acc = accs.get(aid)
        if not acc:
            continue
        for m in acc.get("metrics") or []:
            note = m.get("note") or ""
            if any(f in note.lower() for f in _PROJECTED_FLAGS):
                continue
            if str(m.get("unit", "")).strip() != "%":
                continue
            money_match = _MONEY_RE.search(note)
            if not money_match:
                continue
            money = money_match.group(0)
            rel = f"{m.get('value')}%"                       # e.g. "15%"
            tail = " ".join(note[money_match.end():].split()[:2]).lower()  # e.g. "per unit"

            candidates = [
                (blist, i, b)
                for blist in bullet_lists
                for i, b in enumerate(blist)
                if rel in b and (not tail or tail in b.lower())
            ]
            if len(candidates) != 1:
                continue                                     # ambiguous → leave alone
            blist, i, b = candidates[0]
            if money.lower() in b.lower():
                continue                                     # already present
            blist[i] = b.replace(rel, f"{rel} ({money})", 1)
    return result


_DASH_SPLIT = re.compile(r"\s*[–—-]\s*")  # en-dash, em-dash, or hyphen


def _parse_month_year(s: str):
    s = (s or "").strip()
    if s.lower() in ("present", "current", "now", "ongoing"):
        return datetime.max
    for fmt in ("%B %Y", "%b %Y", "%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _entry_sort_key(entry: dict):
    parts = _DASH_SPLIT.split(entry.get("dates") or "")
    start = _parse_month_year(parts[0]) if parts else None
    end = _parse_month_year(parts[-1]) if parts else None
    end = end or start or datetime.min
    start = start or end
    return (end, start)


def _sort_experience(result: dict) -> dict:
    """Order each experience section newest-first (by end date, then start date),
    so the layout never depends on the model getting chronology right."""
    resume = result.get("resume") or {}
    for sec in ("work_experience", "projects", "volunteering"):
        entries = resume.get(sec)
        if isinstance(entries, list) and len(entries) > 1:
            entries.sort(key=_entry_sort_key, reverse=True)
    return result


# Map each database skill "category" to one of the resume's fixed skill labels.
_SKILL_CATEGORY_TO_LABEL = {
    "CAD Software": "Software",
    "Analysis Software": "Software",
    "Rendering Software": "Software",
    "PDM/PLM": "Software",
    "Engineering Skill": "Design Knowledge",
    "Standards": "Design Knowledge",
    "Manufacturing": "Manufacturing and Prototyping",
    "Programming": "Programming and others",
}
_SKILL_LABEL_ORDER = ["Software", "Design Knowledge",
                      "Manufacturing and Prototyping", "Programming and others"]


def _build_skills_from_db(database: dict) -> dict:
    """Build the technical-skills section straight from the database — every skill,
    in database order, grouped by label. Deterministic: the model does NOT curate,
    prioritize, drop, or add anything here."""
    groups = {label: [] for label in _SKILL_LABEL_ORDER}
    for s in database.get("skills", []):
        name = s.get("name")
        if not name:
            continue
        label = _SKILL_CATEGORY_TO_LABEL.get(s.get("category"), "Programming and others")
        if name not in groups[label]:
            groups[label].append(name)
    out = {label: items for label, items in groups.items() if items}
    certs = [c.get("name") for c in database.get("certifications", []) if c.get("name")]
    if certs:
        out["Certifications"] = certs
    return out


# Interchangeable opening-verb synonyms, each entry (past, present-participle).
# A repeated opening verb is only ever swapped for an UNUSED verb in the SAME
# group, so the bullet's meaning and tense never change.
_VERB_GROUPS = [
    [("Led", "Leading"), ("Directed", "Directing"), ("Headed", "Heading"),
     ("Oversaw", "Overseeing"), ("Spearheaded", "Spearheading"), ("Drove", "Driving"),
     ("Guided", "Guiding"), ("Steered", "Steering")],
    [("Designed", "Designing"), ("Developed", "Developing"), ("Engineered", "Engineering"),
     ("Created", "Creating"), ("Built", "Building"), ("Produced", "Producing"),
     ("Modeled", "Modeling")],
    [("Performed", "Performing"), ("Conducted", "Conducting"), ("Executed", "Executing"),
     ("Ran", "Running"), ("Analyzed", "Analyzing"), ("Evaluated", "Evaluating"),
     ("Assessed", "Assessing")],
    [("Reduced", "Reducing"), ("Cut", "Cutting"), ("Lowered", "Lowering"),
     ("Decreased", "Decreasing"), ("Minimized", "Minimizing")],
    [("Improved", "Improving"), ("Optimized", "Optimizing"), ("Streamlined", "Streamlining"),
     ("Enhanced", "Enhancing"), ("Refined", "Refining")],
    [("Managed", "Managing"), ("Coordinated", "Coordinating"), ("Orchestrated", "Orchestrating"),
     ("Administered", "Administering")],
    [("Established", "Establishing"), ("Standardized", "Standardizing"), ("Defined", "Defining"),
     ("Implemented", "Implementing"), ("Instituted", "Instituting"), ("Formalized", "Formalizing")],
    [("Delivered", "Delivering"), ("Completed", "Completing"), ("Launched", "Launching"),
     ("Shipped", "Shipping")],
    [("Mentored", "Mentoring"), ("Supervised", "Supervising"), ("Trained", "Training"),
     ("Coached", "Coaching")],
    [("Automated", "Automating"), ("Prototyped", "Prototyping"), ("Fabricated", "Fabricating")],
]
_VERB_LOOKUP = {}
for _gi, _grp in enumerate(_VERB_GROUPS):
    for _ti, _pair in enumerate(_grp):
        for _w in _pair:
            _VERB_LOOKUP[_w.lower()] = (_gi, _ti)


def _limit_opening_verbs(result: dict) -> dict:
    """Constrain bullet opening verbs: use any verb at most TWICE across the whole
    resume, and never start two ADJACENT bullets (in reading order) with the same
    verb. A verb that breaks either rule is swapped for a synonym from the SAME
    meaning group and SAME tense that satisfies both; if none qualifies (or the
    verb isn't in any group), the bullet is left as-is so meaning never changes."""
    resume = result.get("resume") or {}
    seq = []  # bullets in document order, as (list, index)
    for sec in ("work_experience", "projects", "volunteering"):
        for entry in resume.get(sec) or []:
            bullets = entry.get("bullets")
            if isinstance(bullets, list):
                seq.extend((bullets, i) for i in range(len(bullets)))

    counts = {}
    prev = None

    def take(k):
        nonlocal prev
        counts[k] = counts.get(k, 0) + 1
        prev = k

    for bullets, i in seq:
        b = bullets[i]
        if not b:
            continue
        m = re.match(r"(\s*)([A-Za-z][A-Za-z'-]*)(.*)", b, re.DOTALL)
        if not m:
            continue
        lead, verb, rest = m.groups()
        key = verb.lower()
        if counts.get(key, 0) < 2 and key != prev:
            take(key)
            continue
        # Violates the cap or is adjacent-duplicate: try a safe in-group swap.
        info = _VERB_LOOKUP.get(key)
        if info is None:
            take(key)
            continue
        gi, ti = info
        for pair in _VERB_GROUPS[gi]:
            cand = pair[ti]
            ck = cand.lower()
            if counts.get(ck, 0) < 2 and ck != prev:
                bullets[i] = f"{lead}{cand}{rest}"
                take(ck)
                break
        else:
            take(key)  # nothing qualified; leave wording unchanged
    return result


# An em/en-dash (or hyphen used as one) surrounded by spaces is a clause-
# separating aside, e.g. "team of four — three engineers — to deliver...".
# That pattern is a common LLM writing tell, not something the candidate
# would write, so it's normalized away even if the prompt rule is missed.
# Dashes with no surrounding space (date ranges like "10,000–20,000") are
# untouched, as is the separate "dates" field (built independently).
_DASH_ASIDE_RE = re.compile(r"\s+[—–-]\s+")


def _strip_dash_asides(result: dict) -> dict:
    resume = result.get("resume") or {}
    for sec in ("work_experience", "projects", "volunteering"):
        for entry in resume.get(sec) or []:
            bullets = entry.get("bullets")
            if isinstance(bullets, list):
                entry["bullets"] = [_DASH_ASIDE_RE.sub(", ", b) if b else b for b in bullets]
    return result


def _build_education_from_db(database: dict) -> list:
    """Build the education section straight from the database so factual details
    (degree + field, dates) are never dropped or reworded by the model."""
    out = []
    for e in database.get("education", []):
        degree, field = e.get("degree", ""), e.get("field", "")
        full = f"{degree} in {field}" if degree and field else (degree or field)
        end = e.get("end_date") or ""
        date = end
        try:
            date = datetime.strptime(end, "%Y-%m").strftime("%b %Y")
        except ValueError:
            pass
        out.append({"institution": e.get("institution", ""), "location": e.get("location", ""),
                    "degree": full, "date": date})
    return out


# Shorthand keywords: typing just one of these (case-insensitive) into the job
# description box generates a general-purpose resume for that track instead of
# requiring a real job posting. Each maps to a canned JD (so the model still
# selects genuinely relevant accomplishments for that track) and pins the
# profile summary deterministically rather than leaving it to the model's guess.
GENERIC_REQUESTS = {
    "generic": {
        "job_title": "Mechanical Engineer (General)",
        "profile_summary_key": "summary_balanced",
        "jd_text": (
            "We are looking for a Mechanical Engineer with broad experience across "
            "both product design and manufacturing. Responsibilities span the full "
            "product lifecycle: CAD modeling, FEA and structural analysis, GD&T and "
            "tolerancing, DFM/DFA, prototyping and hands-on fabrication, as well as "
            "process improvement, tooling, and production support. Comfortable "
            "moving between R&D, design, and shop-floor environments and driving "
            "projects from concept through production."
        ),
    },
    "design": {
        "job_title": "Mechanical Design Engineer",
        "profile_summary_key": "summary",
        "jd_text": (
            "We are looking for a Mechanical Design Engineer to own product design "
            "from concept through release. Responsibilities include CAD modeling, "
            "FEA and structural analysis, GD&T and tolerancing, design for "
            "manufacturing, prototyping, and cross-functional collaboration with "
            "R&D and manufacturing partners to deliver precision-engineered "
            "products."
        ),
    },
    "manufacturing": {
        "job_title": "Manufacturing Engineer",
        "profile_summary_key": "summary_manufacturing",
        "jd_text": (
            "We are looking for a Manufacturing Engineer to drive process "
            "improvement, tooling design, and production optimization on the shop "
            "floor. Responsibilities include process automation, lean "
            "transformation, quality improvement, tooling and fixture design, and "
            "working closely with design engineering to bridge product design and "
            "production, grounded in solid CAD, FEA, and GD&T fundamentals."
        ),
    },
    "tech": {
        "job_title": "Mechanical Engineer (Design & Applied AI)",
        "profile_summary_key": "summary_tech",
        "jd_text": (
            "We are looking for a Mechanical Engineer who also codes and applies "
            "AI tooling to engineering work. Responsibilities include CAD "
            "modeling, FEA and structural analysis, GD&T and tolerancing, plus "
            "writing scripts and small tools (Python) and applying AI/LLM-based "
            "tools to speed up design, analysis, and documentation-heavy "
            "engineering work."
        ),
    },
}


def resolve_generic_request(jd_text: str):
    """If jd_text is just one of the shorthand keywords ("generic", "design",
    "manufacturing" — case-insensitive, whitespace-tolerant), return its canned
    JD spec; otherwise None."""
    return GENERIC_REQUESTS.get((jd_text or "").strip().lower())


def tailor_resume(provider: str, client, model: str, database: dict, jd_text: str) -> dict:
    generic = resolve_generic_request(jd_text)
    effective_jd = generic["jd_text"] if generic else jd_text
    prompt = f"""You are an expert resume writer and recruiter. First analyze the target job description below to understand its requirements and the exact terms an ATS/recruiter would search for, then tailor a candidate's resume to it using ONLY the candidate's real experience database, and honestly assess fit.

CRITICAL RULES:
- NEVER invent, exaggerate, or fabricate experience, skills, metrics, employers, titles, or dates. Use only what appears in the database.
- You may select, reorder, and rephrase existing accomplishments, and use the employer's terminology plus synonyms from each accomplishment's "keywords".
- Preserve real metrics exactly. If an accomplishment's "raw" text or a metric "note" says a figure was projected, not implemented, or unrealized, you MUST NOT claim that figure. Use only the realized outcome.
- When an accomplishment reports both a relative and an absolute figure for the SAME result (e.g. a percentage and a dollar amount, like "15% (~$3) per unit"), keep BOTH in the bullet — do not drop the absolute figure.
- Each bullet: strong action verb first, quantified with a REAL metric where one exists, concise (ideally one line). Use past tense for completed work; for a current role's ongoing responsibilities, present-tense verbs (e.g. "Leading", "Designing") are fine — follow the tense implied by the accomplishment's wording.
- Vary bullet opening verbs: use any given opening action verb at most TWICE across the whole resume, and NEVER start two adjacent bullets with the same verb. Choose precise verbs that reflect the actual action, e.g.: Designed, Developed, Defined, Automated, Standardized, Reduced, Executed, Delivered, Coordinated, Drove, Built, Owned, Performed, Conducted, Engineered, Implemented, Established, Optimized, Prototyped, Directed, Led, Spearheaded, Mentored, Managed, Streamlined, Improved, Created, Produced.
- Spell out a whole number ten or below as words ONLY when it stands alone with nothing attached to it (e.g. "led six engineers", "analyzed three candidate designs", "across four manufacturing cells", "delivered in nine months"). Numbers above ten stay as numerals. Keep a number as a numeral whenever it has an attached prefix or suffix — currency ($3, $200K), percentages (15%, 20%), a symbol/unit, or an identifier/code (MP4, ASME A17.1, IBC 2024) — and keep all dates and years as numerals. This only changes the word-vs-numeral form of standalone counts; never change a real metric's value.
- NEVER use an em-dash or a spaced hyphen/en-dash as a clause-separating aside inside a bullet (e.g. "team of four — three engineers and an industrial designer — to deliver..."). Write the bullet as one plain clause, or split the aside off with a comma or parentheses instead.

ORGANIZING THE RESUME:
- Map each selected accomplishment back to its role via "role_id" to get company, title, and location. Convert dates from "YYYY-MM" to "Mon YYYY" (e.g. "2025-05" -> "May 2025"); render an "end_date" of "present" as "Present".
- Put accomplishments with type "work" under "work_experience", grouped by role (one entry per role, multiple bullets). Order roles most-recent first.
- Put type "project" accomplishments under "projects" and type "volunteer" under "volunteering". For these, use the accomplishment title as the entry "title". For "company" and "location", use the accomplishment's own "company" and "location" fields when present; otherwise fall back to any org named in its text as "company" and leave "location" empty.
- If a field has no real value (e.g. a project or volunteer entry with no associated company, or an entry with no location), output an empty string "" for that field — never "N/A", "None", "-", or any other placeholder.
- For "technical_skills", include ALL of the candidate's skills from the database — do NOT optimize, prioritize, drop, or add any — grouped under these labels in this order: "Software", "Design Knowledge", "Manufacturing and Prototyping", "Programming and others", "Certifications". (This section is finalized in code from the database regardless, so just include everything.)
- Include all education from the database.

PROFILE SUMMARY:
- The candidate has four fixed profile summary options in "profile" — "summary" (design-focused), "summary_manufacturing" (manufacturing/production-focused), "summary_balanced" (neutral, spans both), and "summary_tech" (design/manufacturing background plus coding and applied-AI tooling). Do NOT rewrite or blend them.
- Pick whichever one the target job is actually asking for: use "summary_manufacturing" for manufacturing engineering, process/production, quality, tooling, or shop-floor-focused roles; use "summary" for product/mechanical design, R&D, or CAD/FEA-centric roles; use "summary_balanced" when the JD mixes design and manufacturing roughly evenly, is a generalist mechanical engineer posting, or doesn't clearly lean either way; use "summary_tech" when the JD explicitly calls for coding, scripting, automation, computational/digital engineering, or AI/ML familiarity alongside the mechanical engineering work.
- Return the chosen key verbatim as "profile_summary_key" (one of "summary", "summary_manufacturing", "summary_balanced", or "summary_tech").

SELECTION (FILL ONE FULL PAGE — do not overflow to a second page, but do not leave it sparse either):
- Include EVERY real work and internship role: each "work_experience" entry must appear with at least 1-2 bullets. NEVER drop an entire job. Only a clearly off-target PROJECT or VOLUNTEER entry may be dropped, and only if space is genuinely tight.
- Be generous with the candidate's strongest, most job-relevant accomplishments so the page is well filled. The most relevant current/recent roles should get 4-6 bullets; less-relevant roles get 1-3.
- ALWAYS keep standout, high-impact achievements (large quantified cost savings, key structural analysis, major project leadership) — never drop one just to save space, even on the most relevant role.
- Target roughly 18-22 bullets total across the whole resume: enough to fill a single page, not so many it spills onto a second.

The "match_score" (0-100) is a heuristic fit estimate, NOT a probability of getting an interview.

Return ONLY a JSON object (no prose, no code fences) with exactly these keys:
- "job_title": string
- "match_score": integer 0-100
- "summary_assessment": 2-3 sentence honest read on fit (string)
- "requirements": object with arrays "met", "partial", "gaps" (each an array of short strings)
- "missing_keywords": ATS keywords from the JD the candidate cannot truthfully claim (array of strings)
- "selected_accomplishment_ids": ids used, best-first (array of strings)
- "profile_summary_key": "summary", "summary_manufacturing", "summary_balanced", or "summary_tech" (string, per the PROFILE SUMMARY rule above)
- "resume": object with:
    - "education": array of objects {{"institution","location","degree","date"}}
    - "technical_skills": object mapping each label (string) to an array of strings
    - "work_experience": array of objects {{"company","location","title","dates","bullets":[...]}}
    - "projects": array of objects {{"company","location","title","dates","bullets":[...]}}
    - "volunteering": array of objects {{"company","location","title","dates","bullets":[...]}}

JOB DESCRIPTION:
\"\"\"
{effective_jd}
\"\"\"

CANDIDATE EXPERIENCE DATABASE:
{json.dumps(database, indent=2)}"""
    result = _clean_placeholders(_extract_json(_call(provider, client, model, prompt, max_tokens=16384)))
    result = _strip_dash_asides(result)
    result = _ensure_absolute_figures(result, database)
    result = _limit_opening_verbs(result)
    result = _sort_experience(result)
    if result.get("profile_summary_key") not in ("summary", "summary_manufacturing", "summary_balanced", "summary_tech"):
        result["profile_summary_key"] = "summary"
    if generic:
        # Pin deterministically for the shorthand keywords rather than trusting
        # the model's guess against a generic canned JD.
        result["profile_summary_key"] = generic["profile_summary_key"]
        result["job_title"] = generic["job_title"]
    # Skills are finalized deterministically from the database, not curated by the model.
    resume = result.setdefault("resume", {})
    resume["technical_skills"] = _build_skills_from_db(database)
    resume["education"] = _build_education_from_db(database)
    return result


def resolve_profile_summary(profile: dict, profile_summary_key: str) -> dict:
    """Return a copy of profile with 'summary' swapped to the variant the model
    picked for this job ("summary", "summary_manufacturing", "summary_balanced",
    or "summary_tech")."""
    chosen = profile.get(profile_summary_key) or profile.get("summary", "")
    return {**profile, "summary": chosen}


def render_resume_markdown(profile: dict, resume: dict) -> str:
    """Markdown preview of the same content the PDF renders (for in-app display)."""
    lines = [f"# {profile.get('name','')}"]
    bits = [profile.get("location", ""), profile.get("email", ""), profile.get("phone", "")]
    links = profile.get("links", {}) or {}
    bits += [v for v in (links.get("linkedin"), links.get("github"), links.get("portfolio")) if v]
    contact = "  |  ".join(b for b in bits if b)
    if contact:
        lines.append(contact)
    if profile.get("summary"):
        lines.append(f"\n{profile['summary']}")

    if resume.get("education"):
        lines.append("\n## Education")
        for e in resume["education"]:
            lines.append(f"**{e.get('institution','')}** — {e.get('location','')}")
            lines.append(f"*{e.get('degree','')}* — {e.get('date','')}")

    if resume.get("technical_skills"):
        lines.append("\n## Technical Skills")
        for label, items in resume["technical_skills"].items():
            if items:
                lines.append(f"**{label}:** " + ", ".join(items))

    for key, header in [("work_experience", "Work Experience"), ("projects", "Projects"), ("volunteering", "Volunteering")]:
        if resume.get(key):
            lines.append(f"\n## {header}")
            for item in resume[key]:
                hdr = f"**{item.get('company','')}**"
                if item.get("location"):
                    hdr += f" — {item['location']}"
                lines.append(f"\n{hdr}")
                sub = f"*{item.get('title','')}*"
                if item.get("dates"):
                    sub += f" — {item['dates']}"
                lines.append(sub)
                for b in item.get("bullets", []):
                    lines.append(f"- {b}")
    return "\n".join(lines)
