# CLAUDE.md — Resume Tailor

Project context and standing rules for Claude Code. Read this before making changes.

## What this project is

A local Streamlit app that tailors my resume to a specific job description using
the Anthropic API and exports a one-page PDF in a fixed format. Flow: parse the
job description → select and rewrite my most relevant experience → produce an
honest fit assessment → render a downloadable one-page PDF. A dashboard ranks
roles I've tailored for by fit.

## File responsibilities (keep these separate)

- `tailor.py` — all Anthropic API calls and tailoring logic. UI-agnostic.
- `render_pdf.py` — turns a structured resume dict into the one-page PDF.
- `app.py` — Streamlit UI only; calls the two modules above.
- `experience_database.json` — my real experience data. **Source of truth for content.**
- `BUILD_PROMPT.md` — the full build spec, if you need detail beyond this file.

## Locked decisions — do not change without being asked

- **The PDF format in `render_pdf.py` is FINAL.** A4; margins top 0.45" / bottom
  0.4" / sides 0.5"; Times serif; centered name; contact line with clickable
  email + LinkedIn; ALL-CAPS section headers with full-width rules; company/
  location and title/dates two-column rows; hanging-indent bullets; en-dash date
  ranges; a gap between education entries. Don't restyle it on your own.
- **Auto-fit is FINAL.** `build_resume_pdf(path, profile, resume, max_pages=1)`
  scales the whole document down only as much as needed to fit one page, picking
  the largest readable scale. A `MIN_SCALE` floor prevents unreadable text.
  Tunable constants (`BASE`, `MIN_SCALE`, margins) live at the top of the file.

## Guardrails — non-negotiable

- **Never fabricate.** Only select and rephrase experience that exists in
  `experience_database.json`. Never invent or exaggerate skills, metrics,
  employers, titles, or dates.
- **Preserve real metrics exactly.** If an accomplishment's `raw` text or a
  metric `note` flags a figure as projected / unimplemented / unrealized, do NOT
  claim it — use only the realized outcome.
- **The match score is a heuristic fit estimate, not an interview probability.**
  Always label it that way in the UI. Never present it as a calibrated chance.
- **No auto-applying.** The app prepares materials for me to review and submit
  myself. No submitting forms, no creating accounts, no auto-replies.

## Conventions

- Backend is the **Google Gemini API** (free tier). Default model
  `gemini-2.5-flash`; also allow `gemini-2.0-flash` and `gemini-1.5-flash`.
- API key from `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) env var or the sidebar
  field. Get a free key at https://aistudio.google.com/apikey.
- Streamlit session state only — no browser storage (localStorage/sessionStorage
  fail in this context).
- Parse model JSON robustly: strip ```json fences, fall back to the outermost
  `{...}` span; surface API/parse errors cleanly in the UI.

## Run

```
pip install -r requirements.txt
export GEMINI_API_KEY=...
streamlit run app.py
```
