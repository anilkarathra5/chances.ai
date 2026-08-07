"""
Renders a tailored resume to a one-page PDF in a fixed classic serif format:
centered name, contact line with a linked "LinkedIn", ruled section headers,
and company-left / location-right + title-left / dates-right entry rows.

AUTO-FIT: the layout has a comfortable base font size. build_resume_pdf scales
the whole document (fonts, leading, spacing, indents) down only as much as
needed so the content fits on a single page. Short (heavily tailored) resumes
render at the full base size; long ones shrink gracefully. A floor prevents
unreadably small text; if content can't fit one page even at the floor, it is
allowed to flow to a second page rather than become illegible.

Public API is unchanged:  build_resume_pdf(path, profile, resume) -> path

resume schema:
{
  "education": [ {institution, location, degree, date} ],
  "technical_skills": { "Software": [..], "Design Knowledge": [..], ... },
  "work_experience": [ {company, location, title, dates, bullets:[..]} ],
  "projects":        [ {company, location, title, dates, bullets:[..]} ],
  "volunteering":    [ {company, location, title, dates, bullets:[..]} ],
}
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from reportlab.lib.colors import black, HexColor
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate,
    Paragraph, Spacer, Table, TableStyle, HRFlowable,
)
from reportlab.lib.styles import ParagraphStyle
from xml.sax.saxutils import escape
from pypdf import PdfReader

SERIF = "Times-Roman"
SERIF_BOLD = "Times-Bold"
SERIF_ITALIC = "Times-Italic"
LINK = "#0563C1"

# Page geometry (fixed). A4 with tight, equal side margins.
PAGE = A4
TOP_MARGIN = 0.45 * inch
BOTTOM_MARGIN = 0.4 * inch
SIDE_MARGIN = 0.5 * inch

# Base (scale = 1.0) sizes — comfortable for a short, heavily tailored resume.
# Auto-fit scales these down for longer content. Tuple convention below:
#   font sizes in points, spacing in points.
BASE = {
    "name": 17, "name_lead": 19,
    "contact": 10, "contact_lead": 11.5,
    "section": 11, "section_lead": 11, "section_before": 6,
    "entry": 10, "entry_lead": 11.5,
    "bullet": 10, "bullet_lead": 11.4,
    "skill": 10, "skill_lead": 11.8,
    "left_indent": 24, "bullet_indent": 11,
    # inter-element spacing
    "sp_after_name": 1, "sp_after_contact": 4,
    "sp_hr_after": 2,
    "sp_entry": 7,        # gap after each work/project/volunteer entry
    "sp_edu_entry": 6,    # gap after each education entry
    "sp_after_skills": 3,
}

MIN_SCALE = 0.74   # don't shrink below this; below ~7.4pt body is too small
MAX_SCALE = 1.0    # never grow beyond the comfortable base


def _esc(text) -> str:
    return escape(str(text or ""))


def _styles(s: float) -> dict:
    """Build a set of ParagraphStyles scaled by factor s."""
    b = BASE
    return {
        "NAME": ParagraphStyle("name", fontName=SERIF_BOLD, fontSize=b["name"] * s,
                               alignment=TA_CENTER, spaceAfter=b["sp_after_name"] * s,
                               leading=b["name_lead"] * s),
        "CONTACT": ParagraphStyle("contact", fontName=SERIF, fontSize=b["contact"] * s,
                                  alignment=TA_CENTER, spaceAfter=b["sp_after_contact"] * s,
                                  leading=b["contact_lead"] * s),
        "SECTION": ParagraphStyle("section", fontName=SERIF_BOLD, fontSize=b["section"] * s,
                                  alignment=TA_LEFT, spaceBefore=b["section_before"] * s,
                                  spaceAfter=1 * s, leading=b["section_lead"] * s),
        "LEFT_BOLD": ParagraphStyle("lb", fontName=SERIF_BOLD, fontSize=b["entry"] * s,
                                    alignment=TA_LEFT, leading=b["entry_lead"] * s),
        "RIGHT_BOLD": ParagraphStyle("rb", fontName=SERIF_BOLD, fontSize=b["entry"] * s,
                                     alignment=TA_RIGHT, leading=b["entry_lead"] * s),
        "LEFT_ITALIC": ParagraphStyle("li", fontName=SERIF_ITALIC, fontSize=b["entry"] * s,
                                      alignment=TA_LEFT, leading=b["entry_lead"] * s),
        "RIGHT_ITALIC": ParagraphStyle("ri", fontName=SERIF_ITALIC, fontSize=b["entry"] * s,
                                       alignment=TA_RIGHT, leading=b["entry_lead"] * s),
        "BULLET": ParagraphStyle("bullet", fontName=SERIF, fontSize=b["bullet"] * s,
                                 alignment=TA_JUSTIFY, leading=b["bullet_lead"] * s,
                                 leftIndent=b["left_indent"] * s, bulletIndent=b["bullet_indent"] * s,
                                 spaceAfter=0),
        "SKILL": ParagraphStyle("skill", fontName=SERIF, fontSize=b["skill"] * s,
                                alignment=TA_LEFT, leading=b["skill_lead"] * s, spaceAfter=0),
        "SUMMARY_TEXT": ParagraphStyle("summary_text", fontName=SERIF, fontSize=b["skill"] * s,
                                       alignment=TA_JUSTIFY, leading=b["skill_lead"] * s,
                                       spaceAfter=b["sp_after_skills"] * s),
        "EDU_LEFT": ParagraphStyle("edu", fontName=SERIF_BOLD, fontSize=b["entry"] * s,
                                   alignment=TA_LEFT, leading=b["entry_lead"] * s),
    }


def _two_col(left_para, right_para, width):
    t = Table([[left_para, right_para]], colWidths=[width * 0.62, width * 0.38])
    t.hAlign = "LEFT"
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _build_story(profile, resume, st, s, width):
    """Assemble the flowables for the given styles `st` and scale `s`."""
    b = BASE
    story = []
    story.append(Paragraph(_esc(profile.get("name", "")), st["NAME"]))

    bits = []
    if profile.get("location"):
        bits.append(_esc(profile["location"]))
    if profile.get("phone"):
        bits.append(_esc(profile["phone"]))
    if profile.get("email"):
        em = _esc(profile["email"])
        bits.append(f'<a href="mailto:{em}" color="{LINK}">{em}</a>')
    links = profile.get("links", {}) or {}
    if links.get("linkedin"):
        bits.append(f'<a href="{_esc(links["linkedin"])}" color="{LINK}">LinkedIn</a>')
    if links.get("github"):
        bits.append(f'<a href="{_esc(links["github"])}" color="{LINK}">GitHub</a>')
    if links.get("portfolio"):
        bits.append(f'<a href="{_esc(links["portfolio"])}" color="{LINK}">Portfolio</a>')
    story.append(Paragraph(" | ".join(bits), st["CONTACT"]))

    def section_header(title):
        story.append(Paragraph(title.upper(), st["SECTION"]))
        story.append(HRFlowable(width="100%", thickness=0.8, color=black,
                                spaceBefore=1 * s, spaceAfter=b["sp_hr_after"] * s))

    if profile.get("summary"):
        section_header("Profile Summary")
        story.append(Paragraph(_esc(profile["summary"]), st["SUMMARY_TEXT"]))

    def entry(item):
        company, location = _esc(item.get("company", "")), _esc(item.get("location", ""))
        title, dates = _esc(item.get("title", "")), _esc(item.get("dates", ""))
        if company or location:
            story.append(_two_col(Paragraph(company, st["LEFT_BOLD"]),
                                  Paragraph(location, st["RIGHT_BOLD"]), width))
        if title or dates:
            story.append(_two_col(Paragraph(title, st["LEFT_ITALIC"]),
                                  Paragraph(dates, st["RIGHT_ITALIC"]), width))
        for bl in item.get("bullets", []):
            story.append(Paragraph(_esc(bl), st["BULLET"], bulletText="\u2022"))
        story.append(Spacer(1, b["sp_entry"] * s))

    # Education
    if resume.get("education"):
        section_header("Education")
        for e in resume["education"]:
            story.append(_two_col(Paragraph(_esc(e.get("institution", "")), st["EDU_LEFT"]),
                                  Paragraph(_esc(e.get("location", "")), st["RIGHT_BOLD"]), width))
            story.append(_two_col(Paragraph(_esc(e.get("degree", "")), st["LEFT_ITALIC"]),
                                  Paragraph(_esc(e.get("date", "")), st["RIGHT_ITALIC"]), width))
            story.append(Spacer(1, b["sp_edu_entry"] * s))

    # Technical skills
    if resume.get("technical_skills"):
        section_header("Technical Skills")
        for label, items in resume["technical_skills"].items():
            if items:
                story.append(Paragraph(f"<b>{_esc(label)}:</b> " + ", ".join(_esc(i) for i in items), st["SKILL"]))
        story.append(Spacer(1, b["sp_after_skills"] * s))

    # Experience / Projects / Volunteering
    for key, header in [("work_experience", "Work Experience"),
                        ("projects", "Projects"),
                        ("volunteering", "Volunteering")]:
        if resume.get(key):
            section_header(header)
            for item in resume[key]:
                entry(item)

    return story


def _render(path, profile, resume, s):
    doc = BaseDocTemplate(
        path, pagesize=PAGE,
        topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
        leftMargin=SIDE_MARGIN, rightMargin=SIDE_MARGIN,
        title=f"{profile.get('name','')} Resume",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="body")
    doc.addPageTemplates([PageTemplate(id="body", frames=[frame])])
    st = _styles(s)
    doc.build(_build_story(profile, resume, st, s, doc.width))


def _page_count(path) -> int:
    return len(PdfReader(path).pages)


def build_resume_pdf(path: str, profile: dict, resume: dict, max_pages: int = 1) -> str:
    """Render the resume, auto-scaling the font down until it fits `max_pages`.

    Returns the path. Picks the LARGEST scale (most readable) that fits; if even
    the minimum scale can't fit, renders at the minimum and allows overflow.
    """
    # Coarse-to-fine search over scale factors.
    hi, lo = MAX_SCALE, MIN_SCALE
    best = None
    # Try the full base size first — short resumes pass immediately.
    _render(path, profile, resume, hi)
    if _page_count(path) <= max_pages:
        return path
    # Binary search for the largest scale that fits.
    for _ in range(12):
        mid = round((hi + lo) / 2, 4)
        _render(path, profile, resume, mid)
        if _page_count(path) <= max_pages:
            best = mid
            lo = mid          # try larger (more readable)
        else:
            hi = mid          # need smaller
    if best is None:
        best = MIN_SCALE      # nothing fit; use the floor and allow overflow
    _render(path, profile, resume, best)
    return path
