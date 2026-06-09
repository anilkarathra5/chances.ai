"""
Streamlit interface for the resume tailoring assistant.

Run with:  streamlit run app.py
Set your key first:  export GEMINI_API_KEY=...
(or paste it into the sidebar at runtime).
"""

import io
import json
import os
import tempfile
from datetime import datetime

import streamlit as st

import tailor
import render_pdf

st.set_page_config(page_title="Resume Tailor", page_icon="📄", layout="wide")

if "database" not in st.session_state:
    st.session_state.database = None
if "history" not in st.session_state:
    st.session_state.history = []  # each: {job_title, match_score, result, pdf_bytes, ts}


def _secret(name: str):
    """Read a value from Streamlit secrets, falling back to env var; tolerant of
    no secrets file existing (local use)."""
    try:
        return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


def _require_password():
    """Gate the app behind a password when APP_PASSWORD is set (e.g. on a public
    cloud deployment). If it's not set, the app is open (local use)."""
    expected = _secret("APP_PASSWORD")
    if not expected or st.session_state.get("_authed"):
        return
    st.title("🔒 Resume Tailor")
    pw = st.text_input("Password", type="password")
    if pw == expected:
        st.session_state._authed = True
        st.rerun()
    elif pw:
        st.error("Incorrect password.")
    st.stop()


_require_password()

# Load the experience database: prefer a local file (used when running on your own
# machine); fall back to Streamlit Secrets (EXPERIENCE_DB) when hosted, so personal
# data lives only in encrypted secrets and never in the public repo. The sidebar
# uploader can still override either.
_DB_PATH = os.path.join(os.path.dirname(__file__), "experience_database.json")
if st.session_state.database is None:
    if os.path.exists(_DB_PATH):
        try:
            with open(_DB_PATH, encoding="utf-8") as f:
                st.session_state.database = json.load(f)
        except Exception:
            pass
    else:
        _raw_db = _secret("EXPERIENCE_DB")
        if _raw_db:
            try:
                st.session_state.database = json.loads(_raw_db)
            except Exception:
                pass


def make_pdf_bytes(profile: dict, resume: dict) -> bytes:
    """Render the resume PDF to a temp file and return its bytes."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        path = tmp.name
    render_pdf.build_resume_pdf(path, profile, resume)
    with open(path, "rb") as f:
        data = f.read()
    os.unlink(path)
    return data


def _pdf_filename(title: str) -> str:
    """Build the download filename from the loaded profile name (kept out of the
    repo — it comes from the database at runtime), falling back to 'Resume'."""
    name = ""
    if st.session_state.database:
        name = st.session_state.database.get("profile", {}).get("name", "")
    prefix = (name or "Resume").replace(" ", "_")
    return f"{prefix}_{title.replace(' ', '_')}.pdf"


# --- Sidebar -----------------------------------------------------------------
with st.sidebar:
    st.header("Setup")
    api_key = st.text_input(
        "Google Gemini API key",
        value=os.environ.get("GEMINI_API_KEY", os.environ.get("GOOGLE_API_KEY", "")),
        type="password",
        help="Free key from https://aistudio.google.com/apikey — stored only for this session.",
    )
    model = st.selectbox(
        "Model",
        ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"],
        index=0,
    )
    st.divider()
    st.subheader("Experience database")
    db_file = st.file_uploader("Override experience_database.json (optional)", type=["json"])
    if db_file is not None:
        try:
            st.session_state.database = json.load(db_file)
            n = len(st.session_state.database.get("accomplishments", []))
            st.success(f"Loaded upload — {n} accomplishments.")
        except json.JSONDecodeError as e:
            st.error(f"Couldn't parse JSON: {e}")
    elif st.session_state.database:
        n = len(st.session_state.database.get("accomplishments", []))
        st.caption(f"Using bundled database — {n} accomplishments.")
    else:
        try:
            _seen = list(st.secrets.keys())
        except Exception as _e:
            _seen = [f"(secrets error: {_e})"]
        st.warning("No database found — upload one to begin.")
        st.caption(f"debug · secrets keys visible: {_seen}")

# --- Main --------------------------------------------------------------------
st.title("📄 Resume Tailor")
st.caption("Upload your experience database, paste a job description, and get a tailored, ATS-friendly PDF resume plus an honest fit assessment.")

tab_tailor, tab_dashboard = st.tabs(["Tailor a resume", "Dashboard"])

with tab_tailor:
    jd_text = st.text_area("Job description", height=260, placeholder="Paste the full job posting here…")
    go = st.button("Tailor resume", type="primary", disabled=not jd_text.strip())

    if go:
        if not api_key:
            st.error("Add your Google Gemini API key in the sidebar first.")
            st.stop()
        if not st.session_state.database:
            st.error("Upload your experience database in the sidebar first.")
            st.stop()

        client = tailor.get_client(api_key)
        try:
            with st.spinner("Analyzing the job description, tailoring, and assessing fit…"):
                result = tailor.tailor_resume(client, model, st.session_state.database, jd_text)
        except Exception as e:
            st.error(f"Something went wrong: {e}")
            st.stop()

        profile = st.session_state.database.get("profile", {})
        resume = result.get("resume", {})
        try:
            pdf_bytes = make_pdf_bytes(profile, resume)
        except Exception as e:
            st.error(f"Resume tailored, but PDF rendering failed: {e}")
            pdf_bytes = None

        title = result.get("job_title", "Untitled role")
        st.session_state.history.insert(0, {
            "job_title": title,
            "match_score": result.get("match_score", 0),
            "result": result,
            "pdf_bytes": pdf_bytes,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

        # --- Fit assessment ---
        score = result.get("match_score", 0)
        st.subheader(f"Fit assessment — {title}")
        c1, c2 = st.columns([1, 3])
        with c1:
            st.metric("Match score", f"{score}/100")
            st.caption("Heuristic fit estimate, not an interview probability.")
        with c2:
            st.write(result.get("summary_assessment", ""))

        reqs = result.get("requirements", {})
        rc1, rc2, rc3 = st.columns(3)
        with rc1:
            st.markdown("**✅ Met**")
            for r in reqs.get("met", []):
                st.markdown(f"- {r}")
        with rc2:
            st.markdown("**🟡 Partial**")
            for r in reqs.get("partial", []):
                st.markdown(f"- {r}")
        with rc3:
            st.markdown("**❌ Gaps**")
            for r in reqs.get("gaps", []):
                st.markdown(f"- {r}")

        missing = result.get("missing_keywords", [])
        if missing:
            st.info("Keywords in the JD you can't currently claim: " + ", ".join(missing))

        # --- Resume output ---
        st.subheader("Tailored resume")
        if pdf_bytes:
            st.download_button(
                "⬇ Download resume (PDF)",
                data=pdf_bytes,
                file_name=_pdf_filename(title),
                mime="application/pdf",
                type="primary",
            )
        with st.expander("Preview content (text)", expanded=True):
            st.markdown(tailor.render_resume_markdown(profile, resume))

with tab_dashboard:
    st.subheader("Roles you've tailored for, ranked by fit")
    if not st.session_state.history:
        st.caption("Nothing yet — tailor a resume to see it here.")
    else:
        ranked = sorted(st.session_state.history, key=lambda h: h["match_score"], reverse=True)
        st.dataframe(
            [{"Role": h["job_title"], "Match score": h["match_score"], "When": h["ts"]} for h in ranked],
            use_container_width=True, hide_index=True,
        )
        pick = st.selectbox("Open a tailored resume", [h["job_title"] for h in ranked])
        chosen = next(h for h in ranked if h["job_title"] == pick)
        if chosen.get("pdf_bytes"):
            st.download_button(
                "⬇ Download this resume (PDF)",
                data=chosen["pdf_bytes"],
                file_name=_pdf_filename(pick),
                mime="application/pdf",
            )
        profile = st.session_state.database.get("profile", {}) if st.session_state.database else {}
        st.markdown(tailor.render_resume_markdown(profile, chosen["result"].get("resume", {})))
