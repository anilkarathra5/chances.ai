# Build Prompt — Resume Tailor App

Paste this into Claude Code from inside the `resume-tailor/` project folder.

---

## Goal

Build a local Streamlit app that tailors my resume to a specific job description
using the Anthropic API and exports a polished, one-page **PDF** in a fixed
format. Given my structured experience database and a pasted job description, the
app should: parse the JD, select and rewrite the most relevant experience (using
only real content — never fabricated), produce an honest fit assessment, and
render a downloadable one-page PDF. A dashboard ranks every role I've tailored
for by fit.

## Important: reuse the existing files

This folder already contains working, tested starter files. **Do not rewrite
them from scratch** — read them first and build on them:

- `render_pdf.py` — DONE and dialed-in. The PDF format and auto-fit are final.
  Treat this as the source of truth for output formatting. Only touch it if I
  explicitly ask for a format change.
- `tailor.py` — Claude API logic (JD analysis + tailor/assess). Refine as needed.
- `app.py` — Streamlit UI. Refine as needed.
- `experience_database.json` — my real data. Source of truth for content.
- `requirements.txt`, `README.md` — keep updated.

Start by reading all of these so you understand the current state, then implement
the tasks below.

## Tech stack

- Python 3, Streamlit (UI), `anthropic` SDK (model calls), `reportlab` (PDF),
  `pypdf` (page-count check for auto-fit). All in `requirements.txt`.
- Model string: `claude-sonnet-4-6` by default; allow selecting
  `claude-opus-4-8` and `claude-haiku-4-5-20251001` in the sidebar.
- API key from `ANTHROPIC_API_KEY` env var or a sidebar password field.
- No browser storage; Streamlit session state only.

## Architecture (keep these separate)

1. `tailor.py` — all model calls and tailoring logic. UI-agnostic.
2. `render_pdf.py` — turns a structured resume dict into the one-page PDF.
3. `app.py` — Streamlit UI only; calls the two modules above.

## Data model — `experience_database.json`

The atomic unit is an **accomplishment**, stored in a flat list, each linked to a
role by `role_id` (or `null` for standalone projects/volunteering). Keep raw,
over-complete descriptions as source material; the tailoring step compresses and
re-angles them per job. Top-level keys: `profile`, `roles`, `accomplishments`,
`skills`, `education`, `certifications`, `publications`. Each accomplishment has:
`id`, `type` (work|project|volunteer), `role_id`, `title`, `raw`, `context`,
`skills`, `tools`, `categories`, `metrics` (structured: value/unit/metric/note),
`keywords` (ATS terms/synonyms), `impact_level`, `date`, `confidential`. (The
file already follows this — match it.)

## tailor.py — two model calls

1. `analyze_job_description(client, model, jd_text) -> dict`
   Returns JSON: `title`, `seniority`, `required_skills`, `preferred_skills`,
   `key_responsibilities`, `ats_keywords`.

2. `tailor_and_assess(client, model, database, jd_analysis) -> dict`
   Selects/rewrites and assesses fit in one call. Returns JSON with:
   `job_title`, `match_score` (int 0–100), `summary_assessment`,
   `requirements` (`met`/`partial`/`gaps` arrays), `missing_keywords`,
   `selected_accomplishment_ids`, and `resume` (see schema below).

   Output must organize the resume into the PDF's sections: map each selected
   accomplishment to its role for company/title/dates; convert dates `YYYY-MM`
   to `Mon YYYY` and `present` to `Present`; group `work` accomplishments by role
   under `work_experience`, `project` under `projects`, `volunteer` under
   `volunteering`; build categorized `technical_skills`
   (Software / Design Knowledge / Manufacturing and Prototyping /
   Programming and others / Certifications); include all `education`.

Robust JSON parsing: strip ```json fences and fall back to the outermost
`{...}` span. Wrap calls so API/parse errors surface cleanly in the UI.

### Guardrails (non-negotiable — put these in the tailoring prompt)

- NEVER invent or exaggerate experience, skills, metrics, employers, titles, or
  dates. Only select and rephrase what exists in the database.
- Preserve real metrics exactly. If an accomplishment's `raw` text or a metric
  `note` flags a figure as projected / not implemented / unrealized, DO NOT claim
  it — use only the realized outcome. (The database is the source of truth for
  what's claimable.)
- ATS-friendly bullets: strong past-tense action verb first, quantified with a
  real metric where one exists, concise (ideally one line).
- `match_score` is a **heuristic fit estimate, not an interview probability.**
  Label it that way in the UI. Never present it as a calibrated probability.

## resume schema consumed by render_pdf.py

```
{
  "education":        [ {institution, location, degree, date} ],
  "technical_skills": { "<label>": [ "<skill>", ... ], ... },   # ordered
  "work_experience":  [ {company, location, title, dates, bullets:[...]} ],
  "projects":         [ {company, location, title, dates, bullets:[...]} ],
  "volunteering":     [ {company, location, title, dates, bullets:[...]} ]
}
```

## Output format (FINAL — implemented in render_pdf.py)

Classic single-column serif resume. Don't change unless I ask.

- **Page:** A4. Margins: top 0.45", bottom 0.4", left/right 0.5" (equal). Frame
  has zero internal padding so section rules and the two-column rows align on
  BOTH edges.
- **Fonts:** Times (serif). Centered bold name; centered contact line with
  `Location | phone | email | LinkedIn`, where email and LinkedIn are clickable
  links.
- **Sections in order:** Education, Technical Skills, Work Experience, Projects,
  Volunteering. Each header is bold ALL-CAPS with a full-width horizontal rule.
- **Entry rows:** company bold (left) / location bold (right); title italic
  (left) / dates italic (right); bullets below with a hanging indent (bullet
  glyph indented ~0.15", text at ~0.3"). Use en-dash in date ranges.
- A small gap separates the two education entries.

### Auto-fit (FINAL — the key feature)

`build_resume_pdf(path, profile, resume, max_pages=1)` scales the entire document
(font sizes, leading, spacing, indents — all via one factor) DOWN only as much as
needed to fit one page. It binary-searches for the LARGEST scale that fits, so
short tailored resumes render at the comfortable base size and long ones shrink
gracefully. A `MIN_SCALE` floor prevents unreadably small text; if content can't
fit even at the floor, allow a second page rather than become illegible. Tunable
constants live at the top of `render_pdf.py` (`BASE`, `MIN_SCALE`, margins).

## app.py — Streamlit UI

- Sidebar: API key field (defaults to `ANTHROPIC_API_KEY`), model selector,
  uploader for `experience_database.json` (parse + show accomplishment count).
- Tab 1 "Tailor a resume": JD textarea + "Tailor resume" button. On submit:
  run `analyze_job_description` then `tailor_and_assess`, render the PDF to a
  temp file, read bytes, and offer a PDF download button. Show the fit assessment
  (match score with the "heuristic, not a probability" caption; met/partial/gaps
  in three columns; missing keywords). Show a text preview of the resume content.
- Tab 2 "Dashboard": table of all tailored roles sorted by match score (role,
  score, timestamp), with a selector to reopen any tailored resume's PDF.
- Store history in `st.session_state`. No browser storage.

## Setup / run

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
streamlit run app.py
```

## Enhancements to add (in priority order)

1. **.docx export** alongside the PDF, matching the same format/sections.
2. **Persistence**: save tailored resumes + dashboard history to disk (JSON on
   disk) so the dashboard survives app restarts (session state clears otherwise).
3. **JD-analysis caching**: cache by a hash of the JD text so re-runs don't
   re-call the API.
4. **Screening-question drafting**: optional step that drafts answers to common
   application screening questions from the database (for me to review — the app
   does NOT auto-submit applications or create accounts).

## Constraints / non-goals

- No auto-applying to jobs, no account creation, no auto-submitting forms. The
  app prepares materials for me to review and submit myself.
- Keep `render_pdf.py`'s format and auto-fit intact unless I explicitly request a
  change.
- Add a `CLAUDE.md` summarizing: "logic in tailor.py, formatting in
  render_pdf.py (final — don't alter format without being asked), UI in app.py,
  database is the source of truth, never fabricate experience, match score is a
  heuristic not a probability."

Start by reading the existing files, then confirm your plan before making large
changes.
