"""
MD → PDF Converter  —  Self-contained Flask app
Zero server-side storage. All conversion in-memory.
PDF backend: ReportLab (pure-Python, no network calls, backgrounds work correctly).
"""

import io, os, re
import markdown
from flask import Flask, request, jsonify, send_file, render_template_string

# ── ReportLab imports ────────────────────────────────────────────────────────
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, letter, A3, legal, landscape as rl_landscape
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    HRFlowable,
    Table,
    TableStyle,
    Preformatted,
    ListFlowable,
    ListItem,
    PageBreak,
    KeepTogether,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_JUSTIFY

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# ── Page size map ────────────────────────────────────────────────────────────
PAGE_SIZES = {
    "A4": A4,
    "Letter": letter,
    "A3": A3,
    "Legal": legal,
}


# ── Colour helpers ───────────────────────────────────────────────────────────
def hx(h, fallback="#000000"):
    """Hex string → reportlab Color, safe."""
    try:
        h = (h or "").strip()
        if not h.startswith("#"):
            h = fallback
        return colors.HexColor(h)
    except Exception:
        return colors.HexColor(fallback)


def is_dark_color(hex_str):
    """Return True when perceived luminance of the hex colour is below 0.35."""
    try:
        h = (hex_str or "").lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        return lum < 0.35
    except Exception:
        return False


def derive_palette(bg_hex, fg_hex, accent_hex):
    """Return a full colour palette derived from the three scheme colours."""
    dark = is_dark_color(bg_hex)
    return {
        "bg": hx(bg_hex, "#ffffff"),
        "fg": hx(fg_hex, "#1a1a2e"),
        "accent": hx(accent_hex, "#d4a017"),
        "code_bg": hx("#2d2d3a" if dark else "#f3f4f6"),
        "code_fg": hx("#f8f8f2" if dark else "#c0392b"),
        "pre_bg": hx("#1a1a2a" if dark else "#272822"),
        "pre_fg": hx("#f8f8f2"),
        "bq_bg": hx("#2a2a3a" if dark else "#fdfaf3"),
        "bq_fg": hx("#aab0c8" if dark else "#555555"),
        "th_bg": hx("#2a2a40" if dark else "#f0f0f8"),
        "tr_even": hx("#252535" if dark else "#f9f9f9"),
        "border": hx("#444466" if dark else "#cccccc"),
        "hr": hx("#444466" if dark else "#dddddd"),
        "link": hx("#7eb8e8" if dark else "#2563eb"),
    }


# ── ReportLab style builder ──────────────────────────────────────────────────
def build_styles(p, fs, hfs=1.0, font_body="Times-Roman", font_head="Helvetica-Bold"):
    """Build all paragraph styles for a given palette, font size, and heading multiplier."""
    base = dict(fontName=font_body, textColor=p["fg"], leading=fs * 1.6)
    return {
        "body": ParagraphStyle(
            "body", fontSize=fs, spaceAfter=fs * 0.65, alignment=TA_JUSTIFY, **base
        ),
        "h1": ParagraphStyle(
            "h1",
            fontName=font_head,
            fontSize=fs * 1.85 * hfs,
            textColor=p["accent"],
            spaceAfter=fs * 0.3,
            spaceBefore=fs * 1.1,
            leading=fs * 2.3,
        ),
        "h2": ParagraphStyle(
            "h2",
            fontName=font_head,
            fontSize=fs * 1.45 * hfs,
            textColor=p["accent"],
            spaceAfter=fs * 0.25,
            spaceBefore=fs * 0.9,
            leading=fs * 1.85,
        ),
        "h3": ParagraphStyle(
            "h3",
            fontName=font_head,
            fontSize=fs * 1.15 * hfs,
            textColor=p["fg"],
            spaceAfter=fs * 0.2,
            spaceBefore=fs * 0.7,
            leading=fs * 1.55,
        ),
        "h4": ParagraphStyle(
            "h4",
            fontName=font_head + "-Oblique" if font_head == "Helvetica" else font_head,
            fontSize=fs * hfs,
            textColor=p["fg"],
            spaceAfter=fs * 0.2,
            spaceBefore=fs * 0.5,
            leading=fs * 1.4,
        ),
        "code": ParagraphStyle(
            "code",
            fontName="Courier",
            fontSize=fs * 0.84,
            textColor=p["code_fg"],
            backColor=p["code_bg"],
            leftIndent=5,
            rightIndent=5,
            spaceAfter=2,
            leading=fs * 1.2,
        ),
        "pre": ParagraphStyle(
            "pre",
            fontName="Courier",
            fontSize=fs * 0.77,
            textColor=p["pre_fg"],
            backColor=p["pre_bg"],
            leftIndent=10,
            rightIndent=10,
            spaceAfter=fs * 0.55,
            leading=fs * 1.25,
        ),
        "bq": ParagraphStyle(
            "bq",
            fontName="Times-Italic",
            fontSize=fs,
            textColor=p["bq_fg"],
            backColor=p["bq_bg"],
            leftIndent=18,
            rightIndent=8,
            spaceAfter=fs * 0.5,
            leading=fs * 1.55,
        ),
        "li": ParagraphStyle(
            "li",
            fontName=font_body,
            fontSize=fs,
            textColor=p["fg"],
            leading=fs * 1.5,
            leftIndent=0,
            spaceAfter=fs * 0.18,
        ),
    }


# ── Inline markdown → ReportLab XML ─────────────────────────────────────────
def inline_to_rl(text, p):
    """Convert inline markdown (bold, italic, code, links) to ReportLab XML."""
    # Escape XML special chars first (except where we insert tags)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Bold-italic
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    # Italic
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"_(.+?)_", r"<i>\1</i>", text)
    # Strikethrough
    text = re.sub(r"~~(.+?)~~", r"<strike>\1</strike>", text)
    # Inline code — use font tag with explicit colours
    code_bg_hex = p["code_bg"].hexval()
    code_fg_hex = p["code_fg"].hexval()
    text = re.sub(
        r"`([^`]+)`",
        lambda m: (
            f'<font name="Courier" color="{code_fg_hex}"'
            f' backColor="{code_bg_hex}"> {m.group(1)} </font>'
        ),
        text,
    )
    # Links
    link_hex = p["link"].hexval()
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<link href="{m.group(2)}" color="{link_hex}">{m.group(1)}</link>',
        text,
    )
    return text


# ── Markdown → ReportLab story ───────────────────────────────────────────────
def md_to_story(md_text, st, p, page_w_pt):
    """Parse markdown text and return a list of ReportLab Flowables."""
    story = []
    lines = md_text.split("\n")
    i = 0

    def flush_para(buf):
        text = " ".join(buf).strip()
        if text:
            story.append(Paragraph(inline_to_rl(text, p), st["body"]))

    para_buf = []

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        # ── Fenced code block ──────────────────────────────────────
        if stripped.startswith("```"):
            flush_para(para_buf)
            para_buf = []
            i += 1
            code_lines = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code_text = "\n".join(code_lines)
            # Preformatted keeps exact spacing; wrap in a KeepTogether
            story.append(KeepTogether([Preformatted(code_text, st["pre"])]))

        # ── Headings ───────────────────────────────────────────────
        elif stripped.startswith("#### "):
            flush_para(para_buf)
            para_buf = []
            story.append(Paragraph(inline_to_rl(stripped[5:], p), st["h4"]))
        elif stripped.startswith("### "):
            flush_para(para_buf)
            para_buf = []
            story.append(Paragraph(inline_to_rl(stripped[4:], p), st["h3"]))
        elif stripped.startswith("## "):
            flush_para(para_buf)
            para_buf = []
            story.append(Paragraph(inline_to_rl(stripped[3:], p), st["h2"]))
            story.append(
                HRFlowable(width="100%", thickness=0.6, color=p["accent"], spaceAfter=4)
            )
        elif stripped.startswith("# "):
            flush_para(para_buf)
            para_buf = []
            story.append(Paragraph(inline_to_rl(stripped[2:], p), st["h1"]))
            story.append(
                HRFlowable(width="100%", thickness=2, color=p["accent"], spaceAfter=6)
            )

        # ── HR ─────────────────────────────────────────────────────
        elif re.match(r"^[-*_]{3,}$", stripped):
            flush_para(para_buf)
            para_buf = []
            story.append(
                HRFlowable(
                    width="100%",
                    thickness=0.5,
                    color=p["hr"],
                    spaceAfter=8,
                    spaceBefore=8,
                )
            )

        # ── Blockquote ─────────────────────────────────────────────
        elif stripped.startswith("> "):
            flush_para(para_buf)
            para_buf = []
            bq_lines = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                bq_lines.append(lines[i].strip()[2:])
                i += 1
            story.append(Paragraph(inline_to_rl(" ".join(bq_lines), p), st["bq"]))
            continue

        # ── Table ──────────────────────────────────────────────────
        elif (
            "|" in stripped
            and i + 1 < len(lines)
            and re.match(r"^\|[\s\-:|]+\|", lines[i + 1].strip())
        ):
            flush_para(para_buf)
            para_buf = []
            headers = [c.strip() for c in stripped.split("|") if c.strip()]
            i += 1  # skip separator row
            rows = [headers]
            i += 1
            while i < len(lines) and "|" in lines[i]:
                row = [c.strip() for c in lines[i].split("|") if c.strip()]
                if row:
                    rows.append(row)
                i += 1
            # Build table with scheme-aware colours
            col_count = max(len(r) for r in rows)
            # Pad short rows
            rows = [r + [""] * (col_count - len(r)) for r in rows]
            # Convert cell text to Paragraph for inline formatting
            fs = st["body"].fontSize
            cell_style = ParagraphStyle(
                "cell",
                fontName="Helvetica",
                fontSize=fs * 0.88,
                textColor=p["fg"],
                leading=fs * 1.3,
            )
            hdr_style = ParagraphStyle(
                "hdr",
                fontName="Helvetica-Bold",
                fontSize=fs * 0.88,
                textColor=p["fg"],
                leading=fs * 1.3,
            )
            tdata = [
                [
                    Paragraph(inline_to_rl(c, p), hdr_style if r == 0 else cell_style)
                    for c in row
                ]
                for r, row in enumerate(rows)
            ]
            col_w = (page_w_pt - 40 * mm) / col_count
            tbl = Table(
                tdata, colWidths=[col_w] * col_count, hAlign="LEFT", repeatRows=1
            )
            tbl.setStyle(
                TableStyle(
                    [
                        # Header row
                        ("BACKGROUND", (0, 0), (-1, 0), p["th_bg"]),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        # Body rows — alternating
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [p["bg"], p["tr_even"]]),
                        # Grid
                        ("GRID", (0, 0), (-1, -1), 0.4, p["border"]),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ]
                )
            )
            story.append(tbl)
            story.append(Spacer(1, st["body"].spaceAfter))
            continue

        # ── Unordered list ─────────────────────────────────────────
        elif re.match(r"^[-*+] ", stripped):
            flush_para(para_buf)
            para_buf = []
            items = []
            while i < len(lines) and re.match(r"^[-*+] ", lines[i].strip()):
                text = re.sub(r"^[-*+] ", "", lines[i].strip())
                items.append(
                    ListItem(
                        Paragraph(inline_to_rl(text, p), st["li"]),
                        bulletColor=p["accent"],
                        leftIndent=12,
                    )
                )
                i += 1
            story.append(
                ListFlowable(
                    items,
                    bulletType="bullet",
                    leftIndent=16,
                    bulletFontSize=st["li"].fontSize * 0.7,
                )
            )
            continue

        # ── Ordered list ───────────────────────────────────────────
        elif re.match(r"^\d+\. ", stripped):
            flush_para(para_buf)
            para_buf = []
            items = []
            num = 1
            while i < len(lines) and re.match(r"^\d+\. ", lines[i].strip()):
                text = re.sub(r"^\d+\. ", "", lines[i].strip())
                items.append(
                    ListItem(
                        Paragraph(inline_to_rl(text, p), st["li"]),
                        bulletColor=p["fg"],
                        leftIndent=12,
                    )
                )
                i += 1
                num += 1
            story.append(
                ListFlowable(
                    items,
                    bulletType="1",
                    leftIndent=16,
                    bulletFontSize=st["li"].fontSize,
                )
            )
            continue

        # ── Page break ─────────────────────────────────────────────
        elif stripped.lower() in ("<!-- pagebreak -->", "<pagebreak/>"):
            flush_para(para_buf)
            para_buf = []
            story.append(PageBreak())

        # ── Blank line = end of paragraph ──────────────────────────
        elif stripped == "":
            flush_para(para_buf)
            para_buf = []

        # ── Normal text (accumulate into paragraph) ─────────────────
        else:
            para_buf.append(stripped)

        i += 1

    flush_para(para_buf)
    return story


# ── PDF builder ──────────────────────────────────────────────────────────────
def build_pdf(md_text, settings):
    """Render markdown → PDF bytes using ReportLab. Nothing written to disk."""

    # ── Settings ────────────────────────────────────────────────────────
    scheme_bg = settings.get("scheme_bg", "") or "#ffffff"
    scheme_text = settings.get("scheme_text", "") or "#1a1a2e"
    accent_hex = settings.get("accent", "#d4a017")
    font_size = float(settings.get("font_size", 13))
    heading_font_size = float(settings.get("heading_font_size", 1.0))
    page_key = settings.get("page_size", "A4")
    orientation = settings.get("orientation", "portrait")
    m_top = float(settings.get("m_top", 20))
    m_bot = float(settings.get("m_bot", 20))
    m_left = float(settings.get("m_left", 20))
    m_right = float(settings.get("m_right", 20))
    font_body_raw = settings.get("font_body", "Times-Roman")
    font_head_raw = settings.get("font_head", "Helvetica-Bold")
    custom_note = settings.get(
        "custom_css", ""
    )  # Not applicable in ReportLab but kept for future

    # Map CSS font names → ReportLab standard fonts
    FONT_MAP = {
        "Georgia, serif": "Times-Roman",
        "'Times New Roman', serif": "Times-Roman",
        "Times New Roman, serif": "Times-Roman",
        "Arial, sans-serif": "Helvetica",
        "'Helvetica Neue', sans-serif": "Helvetica",
        "Helvetica Neue, sans-serif": "Helvetica",
        "Verdana, sans-serif": "Helvetica",
        "'Courier New', monospace": "Courier",
        "Courier New, monospace": "Courier",
    }
    font_body = FONT_MAP.get(font_body_raw, "Times-Roman")
    font_head = FONT_MAP.get(font_head_raw, "Helvetica-Bold")
    if not font_head.endswith("-Bold"):
        font_head = font_head.split("-")[0] + "-Bold"

    # ── Page size & orientation ─────────────────────────────────────
    base_size = PAGE_SIZES.get(page_key, A4)
    pagesize = rl_landscape(base_size) if orientation == "landscape" else base_size
    page_w, page_h = pagesize

    # ── Colour palette ──────────────────────────────────────────────
    p = derive_palette(scheme_bg, scheme_text, accent_hex)

    # ── Styles ──────────────────────────────────────────────────────
    st = build_styles(p, font_size, heading_font_size, font_body, font_head)

    # ── Background painter (called every page) ───────────────────────
    def paint_bg(canvas_obj, doc):
        canvas_obj.saveState()
        canvas_obj.setFillColor(p["bg"])
        canvas_obj.rect(0, 0, page_w, page_h, fill=1, stroke=0)
        canvas_obj.restoreState()

    # ── Build story ─────────────────────────────────────────────────
    story = md_to_story(md_text, st, p, page_w)

    # ── Render ──────────────────────────────────────────────────────
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=pagesize,
        leftMargin=m_left * mm,
        rightMargin=m_right * mm,
        topMargin=m_top * mm,
        bottomMargin=m_bot * mm,
    )
    doc.build(story, onFirstPage=paint_bg, onLaterPages=paint_bg)
    buf.seek(0)
    return buf.read()


# ── Inline HTML template ─────────────────────────────────────────────────────
TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>MD → PDF</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700&family=JetBrains+Mono:wght@400;600&family=Lora:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/theme/dracula.min.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/display/fullscreen.min.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/matchesonscrollbar.min.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/dialog/dialog.min.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/matchesonscrollbar.min.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/dialog/dialog.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/markdown/markdown.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/edit/continuelist.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/display/fullscreen.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/dialog/dialog.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/search.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/searchcursor.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/matchesonscrollbar.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/dialog/dialog.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/search.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/searchcursor.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/addon/search/matchesonscrollbar.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.6/marked.min.js"></script>
<style>
:root{
  --bg:#0d0f12;--surface:#151820;--panel:#1c2030;--border:#2a3045;
  --accent:#e8c97e;--accent2:#7eb8e8;--accent3:#e87ea8;
  --text:#d4d8e8;--muted:#5a6280;--radius:10px;
  --font-ui:'JetBrains Mono',monospace;
  --shadow:0 8px 32px rgba(0,0,0,.45);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--font-ui);
     min-height:100vh;display:flex;flex-direction:column;overflow-x:hidden}
header{background:var(--surface);border-bottom:1px solid var(--border);
       padding:11px 22px;display:flex;align-items:center;gap:14px;
       position:sticky;top:0;z-index:100}
.logo{font-family:'Playfair Display',serif;font-size:1.3rem;color:var(--accent);white-space:nowrap}
.logo span{color:var(--accent2)}
.tagline{font-size:.62rem;color:var(--muted);border-left:1px solid var(--border);padding-left:11px}
.hdr-acts{margin-left:auto;display:flex;gap:7px;flex-wrap:wrap}
.btn{font-family:var(--font-ui);font-size:.66rem;font-weight:600;padding:7px 13px;
     border-radius:var(--radius);border:1px solid var(--border);cursor:pointer;
     transition:all .15s;background:var(--panel);color:var(--text);
     text-transform:uppercase;letter-spacing:.07em;white-space:nowrap}
.btn:hover{border-color:var(--accent);color:var(--accent);transform:translateY(-1px)}
.btn-p{background:var(--accent);color:#0d0f12;border-color:var(--accent)}
.btn-p:hover{background:#f0d898;color:#0d0f12}
.workspace{flex:1;display:grid;grid-template-columns:295px 1fr 1fr;
           height:calc(100vh - 51px);overflow:hidden}
.sidebar{background:var(--surface);border-right:1px solid var(--border);
         display:flex;flex-direction:column;overflow:hidden}
.stabs{display:flex;border-bottom:1px solid var(--border);flex-shrink:0}
.stab{flex:1;padding:9px 2px;font-size:.58rem;text-align:center;text-transform:uppercase;
      letter-spacing:.07em;cursor:pointer;color:var(--muted);
      border-bottom:2px solid transparent;transition:all .15s;user-select:none}
.stab.active{color:var(--accent);border-bottom-color:var(--accent)}
.spanel{display:none;flex-direction:column;overflow-y:auto;flex:1;padding:13px;gap:9px}
.spanel.active{display:flex}
.sl{font-size:.57rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:1px}
.if{width:100%;background:var(--panel);border:1px solid var(--border);color:var(--text);
    font-family:var(--font-ui);font-size:.68rem;padding:7px 9px;
    border-radius:var(--radius);outline:none;transition:border-color .15s}
.if:focus{border-color:var(--accent2)}
select.if{appearance:none;cursor:pointer}
input[type=range].if{padding:5px 0;cursor:pointer}
input[type=color]{width:32px;height:26px;border:none;background:none;cursor:pointer;border-radius:4px;padding:0}
.dz{border:2px dashed var(--border);border-radius:var(--radius);padding:16px 10px;
    text-align:center;cursor:pointer;transition:all .2s;color:var(--muted);
    font-size:.67rem;line-height:1.6}
.dz:hover,.dz.over{border-color:var(--accent);color:var(--accent);background:rgba(232,201,126,.04)}
.dz input{display:none}
.css-area{width:100%;min-height:130px;resize:vertical;background:var(--panel);
          border:1px solid var(--border);border-radius:var(--radius);color:var(--text);
          font-family:var(--font-ui);font-size:.65rem;padding:9px;outline:none;
          tab-size:2;transition:border-color .15s;line-height:1.5}
.css-area:focus{border-color:var(--accent2)}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.pc{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);
    padding:9px 7px;font-size:.6rem;cursor:pointer;transition:all .15s;text-align:center;color:var(--muted)}
.pc:hover{border-color:var(--accent);color:var(--accent)}
.pc strong{display:block;color:var(--text);margin-bottom:2px;font-size:.63rem}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.epane{display:flex;flex-direction:column;border-right:1px solid var(--border);overflow:hidden}
.phdr{background:var(--surface);border-bottom:1px solid var(--border);padding:7px 11px;
      font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);
      display:flex;align-items:center;gap:7px;flex-shrink:0}
.pt{color:var(--accent2);font-weight:600}
.pa{margin-left:auto;display:flex;gap:5px}
.tbar{display:flex;gap:3px;padding:5px 7px;background:var(--surface);
      border-bottom:1px solid var(--border);flex-wrap:wrap;flex-shrink:0}
.tb{background:none;border:none;color:var(--muted);font-family:var(--font-ui);
    font-size:.63rem;padding:3px 7px;border-radius:4px;cursor:pointer;transition:all .12s}
.tb:hover{background:var(--panel);color:var(--accent)}
.CodeMirror-dialog{background:var(--panel);border:1px solid var(--border);color:var(--text);font-family:var(--font-ui)}
.CodeMirror-search-hint{color:var(--muted)}
.CodeMirror-searchfield{background:var(--surface);color:var(--text);border:1px solid var(--border);font-family:var(--font-ui)}
.ts{width:1px;background:var(--border);margin:0 3px}
.ewrap{flex:1;overflow:hidden}
.CodeMirror{height:100%!important;font-size:.77rem!important;line-height:1.65!important;
            font-family:var(--font-ui)!important;background:var(--bg)!important}
.CodeMirror-scroll{padding-bottom:60px}
.CodeMirror-fullscreen{position:fixed!important;top:51px!important;left:0!important;right:0!important;bottom:0!important;height:auto!important;z-index:100}
.CodeMirror-fullscreen ~ .phdr,.CodeMirror-fullscreen ~ .tbar{display:none}
body.fs-active #exitFullscreenBtn{display:inline-block!important}
.ppane{display:flex;flex-direction:column;overflow:hidden;background:var(--bg)}
.pscroll{flex:1;overflow-y:auto;padding:22px}

/* Preview paper */
#preview{
  max-width:710px;margin:0 auto;border-radius:3px;
  box-shadow:0 8px 32px rgba(0,0,0,.45);min-height:880px;
  font-family:Georgia,serif;font-size:15px;line-height:1.75;padding:48px;
  background:#fff;color:#1a1a2e;
  transition:background .2s,color .2s,padding .2s,font-size .2s;
}
/* Default preview markdown styles (overridden by dyn-style) */
#preview h1,#preview h2,#preview h3,#preview h4{font-family:'Playfair Display',serif;margin-top:1.4em;margin-bottom:.4em;line-height:1.2}
#preview h1{font-size:2rem;border-bottom:2px solid currentAccent;padding-bottom:.2em}
#preview h2{font-size:1.45rem;border-bottom:1px solid #e0d8c8;padding-bottom:.15em}
#preview h3{font-size:1.15rem}
#preview p{margin-bottom:.9em}
#preview a{color:#2563eb}
#preview code{background:#f3f4f6;padding:2px 5px;border-radius:3px;
              font-family:'Courier New',monospace;font-size:.85em;color:#c0392b}
#preview pre{background:#272822;color:#f8f8f2;padding:13px 17px;border-radius:6px;
             overflow-x:auto;margin-bottom:.9em;font-size:.82em}
#preview pre code{background:none;color:inherit;padding:0}
#preview blockquote{border-left:4px solid #d4a017;padding:5px 14px;margin:.9em 0;
                    color:#555;background:#fdfaf3;border-radius:0 4px 4px 0}
#preview table{width:100%;border-collapse:collapse;margin-bottom:.9em}
#preview th{background:#f8f8ff;font-weight:700;border:1px solid #dde;padding:6px 10px}
#preview td{border:1px solid #dde;padding:6px 10px}
#preview tr:nth-child(even) td{background:#fafafa}
#preview ul,#preview ol{padding-left:1.4em;margin-bottom:.9em}
#preview li{margin-bottom:.25em}
#preview hr{border:none;border-top:1px solid #dde;margin:1.8em 0}
#preview del{text-decoration:line-through;color:#888}
/* Note box */
.note-box{background:rgba(232,201,126,.08);border:1px solid rgba(232,201,126,.25);
          border-radius:8px;padding:10px 14px;font-size:.65rem;color:var(--muted);
          line-height:1.6;margin-top:4px}
.note-box b{color:var(--accent)}
.sbar{background:var(--surface);border-top:1px solid var(--border);padding:5px 16px;
      font-size:.58rem;color:var(--muted);display:flex;gap:16px;align-items:center;flex-shrink:0}
.dot{width:6px;height:6px;border-radius:50%;background:#3ddc84;display:inline-block;margin-right:4px}
.dot.busy{background:var(--accent);animation:pulse 1s infinite}
#overlay{display:none;position:fixed;inset:0;background:rgba(13,15,18,.82);
         z-index:999;align-items:center;justify-content:center;backdrop-filter:blur(4px)}
#overlay.on{display:flex}
.lbox{background:var(--panel);border:1px solid var(--border);border-radius:14px;
      padding:28px 36px;text-align:center;box-shadow:var(--shadow)}
.spin{width:36px;height:36px;border:3px solid var(--border);border-top-color:var(--accent);
      border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 13px}
.lbox p{font-size:.7rem;color:var(--muted)}
#toasts{position:fixed;bottom:18px;right:18px;z-index:1000;display:flex;flex-direction:column;gap:6px}
.toast{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);
       padding:9px 15px;font-size:.68rem;color:var(--text);box-shadow:var(--shadow);
       animation:slideIn .2s ease;max-width:270px}
.toast.ok{border-color:#3ddc84;color:#3ddc84}
.toast.err{border-color:var(--accent3);color:var(--accent3)}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:none;opacity:1}}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
</style>
</head>
<body>
<header>
  <div class="logo">MD<span>→</span>PDF</div>
  <div class="tagline">Self-hosted · Zero storage </div>
  <div class="hdr-acts">
    <button class="btn" id="exitFullscreenBtn" style="display:none" onclick="toggleFS()">⊟ Exit FS</button>
    <button class="btn" onclick="clearEditor()">✕ Clear</button>
    <button class="btn" onclick="copyMD()">⎘ Copy MD</button>
    <button class="btn btn-p" onclick="exportPDF()">⬇ Export PDF</button>
  </div>
</header>

<div class="workspace">
  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="stabs">
      <div class="stab active" onclick="stab('import')">Import</div>
      <div class="stab" onclick="stab('style')">Style</div>
      <div class="stab" onclick="stab('page')">Page</div>
      <div class="stab" onclick="stab('help')">Help</div>
    </div>

    <!-- Import -->
    <div class="spanel active" id="sp-import">
      <div class="sl">Drop or browse file</div>
      <div class="dz" id="dz" onclick="document.getElementById('fi').click()">
        <input type="file" id="fi" accept=".md,.markdown,.txt" onchange="handleFile(event)"/>
        <div style="font-size:1.4rem;margin-bottom:5px">📄</div>
        <strong style="color:var(--text);font-size:.7rem">Drop .md / .txt here</strong><br/>or click to browse
      </div>
      <div class="sl">Or paste raw markdown</div>
      <textarea class="if" id="pasteArea" rows="5"
        style="resize:vertical;font-size:.67rem;line-height:1.5"
        placeholder="Paste markdown here…"></textarea>
      <button class="btn" onclick="loadPasted()">↓ Load Pasted Text</button>
      <div class="sl">Templates</div>
      <div class="pgrid">
        <div class="pc" onclick="loadSample('report')"><strong>📊 Report</strong>Business report</div>
        <div class="pc" onclick="loadSample('readme')"><strong>📖 README</strong>Project docs</div>
        <div class="pc" onclick="loadSample('letter')"><strong>✉ Letter</strong>Formal letter</div>
        <div class="pc" onclick="loadSample('resume')"><strong>👤 Resume</strong>CV template</div>
      </div>
    </div>

    <!-- Style -->
    <div class="spanel" id="sp-style">
      <div class="sl">Heading font size: <span id="hfsv">1.0x</span></div>
      <input class="if" type="range" id="headingFontSize" min="0.8" max="1.5" step="0.05" value="1.0"
        oninput="document.getElementById('hfsv').textContent=(+this.value).toFixed(2)+'x'; refreshPreview()"/>
      <div class="sl">Body font</div>
      <select class="if" id="fontBody" onchange="refreshPreview()">
        <option value="Georgia, serif">Georgia (Serif)</option>
        <option value="'Times New Roman', serif">Times New Roman</option>
        <option value="Arial, sans-serif">Arial (Sans)</option>
        <option value="'Helvetica Neue', sans-serif">Helvetica Neue</option>
        <option value="Verdana, sans-serif">Verdana</option>
        <option value="'Courier New', monospace">Courier New (Mono)</option>
      </select>
      <div class="sl">Heading font</div>
      <select class="if" id="fontHead" onchange="refreshPreview()">
        <option value="Georgia, serif">Georgia</option>
        <option value="'Times New Roman', serif">Times New Roman</option>
        <option value="Arial, sans-serif">Arial</option>
        <option value="Verdana, sans-serif">Verdana</option>
      </select>
      <div class="sl">Font size: <span id="fsv">13px</span></div>
      <input class="if" type="range" id="fontSize" min="9" max="20" value="13"
        oninput="document.getElementById('fsv').textContent=this.value+'pt'; refreshPreview()"/>
      <div class="sl">Line height: <span id="lhv">1.6</span></div>
      <input class="if" type="range" id="lineHeight" min="1.2" max="2.4" step="0.05" value="1.6"
        oninput="document.getElementById('lhv').textContent=(+this.value).toFixed(2); refreshPreview()"/>
      <div class="sl">Accent color</div>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="color" id="accentColor" value="#d4a017" oninput="refreshPreview()"/>
        <span style="font-size:.66rem;color:var(--muted)">Headings &amp; borders</span>
      </div>
      <div class="sl">Page padding: <span id="ppv">48px</span></div>
      <input class="if" type="range" id="pagePad" min="16" max="96" value="48"
        oninput="document.getElementById('ppv').textContent=this.value+'px'; refreshPreview()"/>
      <div class="sl">Color scheme</div>
      <div class="pgrid">
        <div class="pc" onclick="applyScheme('classic')"><strong>Classic</strong>White / dark</div>
        <div class="pc" onclick="applyScheme('warm')"><strong>Warm</strong>Sepia tones</div>
        <div class="pc" onclick="applyScheme('night')"><strong>Night</strong>Dark paper</div>
        <div class="pc" onclick="applyScheme('ocean')"><strong>Ocean</strong>Blue accent</div>
      </div>
    </div>

    <!-- Page -->
    <div class="spanel" id="sp-page">
      <div class="sl">Paper size</div>
      <select class="if" id="pageSize">
        <option value="A4">A4 (210×297 mm)</option>
        <option value="Letter">Letter (8.5×11 in)</option>
        <option value="A3">A3 (297×420 mm)</option>
        <option value="Legal">Legal (8.5×14 in)</option>
      </select>
      <div class="sl">Orientation</div>
      <select class="if" id="pageOrient">
        <option value="portrait">Portrait</option>
        <option value="landscape">Landscape</option>
      </select>
      <div class="sl">Margins (mm)</div>
      <div class="row2">
        <div><div class="sl">Top</div><input class="if" type="number" id="mTop" value="20" min="0" max="80"/></div>
        <div><div class="sl">Bottom</div><input class="if" type="number" id="mBot" value="20" min="0" max="80"/></div>
        <div><div class="sl">Left</div><input class="if" type="number" id="mLeft" value="20" min="0" max="80"/></div>
        <div><div class="sl">Right</div><input class="if" type="number" id="mRight" value="20" min="0" max="80"/></div>
      </div>
      <div class="sl">Output filename</div>
      <input class="if" type="text" id="filename" value="document" placeholder="document"/>
      <div class="sl">PDF Title</div>
      <input class="if" type="text" id="metaTitle" placeholder="Document title"/>
      <div class="sl">Author</div>
      <input class="if" type="text" id="metaAuthor" placeholder="Author name"/>
    </div>

    <!-- Help -->
    <div class="spanel" id="sp-help">
      <div class="sl">Renderer</div>
      <div class="note-box">
        <b>ReportLab</b> is used for PDF generation — pure Python, no network calls.
        Backgrounds, fonts, and colour schemes are fully supported.
      </div>
      <div class="sl">Supported markdown</div>
      <div class="note-box" style="font-size:.62rem;line-height:1.8">
        <b># H1</b> &nbsp; <b>## H2</b> &nbsp; <b>### H3</b><br/>
        <b>**bold**</b> &nbsp; <i>*italic*</i> &nbsp; <strike>~~strike~~</strike><br/>
        <b>`code`</b> &nbsp; <b>```code block```</b><br/>
        <b>- bullet</b> &nbsp; <b>1. ordered</b><br/>
        <b>&gt; blockquote</b> &nbsp; <b>---</b> (HR)<br/>
        <b>| table | cells |</b><br/>
        <b>[link](url)</b>
      </div>
      <div class="sl">Tips</div>
      <div class="note-box">
        The preview uses browser rendering — the exported PDF uses ReportLab, so
        minor layout differences are normal. Colour schemes (Night, Warm, Ocean)
        fully affect the PDF background and all element colours.
      </div>
    </div>
  </aside>

  <!-- Editor -->
  <section class="epane">
    <div class="phdr">
      <span class="pt">✏ Editor</span>
      <span id="wc" style="color:var(--muted)">0 words</span>
      <div class="pa">
        <button class="btn" style="font-size:.56rem;padding:3px 9px" onclick="toggleFS()">⛶ FS</button>
      </div>
    </div>
    <div class="tbar">
      <button class="tb" onclick="fmt('**','**')"><b>B</b></button>
      <button class="tb" onclick="fmt('*','*')"><i>I</i></button>
      <button class="tb" onclick="fmt('~~','~~')">~~</button>
      <button class="tb" onclick="fmt('`','`')">`</button>
      <div class="ts"></div>
      <button class="tb" onclick="ins('# ')">H1</button>
      <button class="tb" onclick="ins('## ')">H2</button>
      <button class="tb" onclick="ins('### ')">H3</button>
      <div class="ts"></div>
      <button class="tb" onclick="ins('- ')">• List</button>
      <button class="tb" onclick="ins('1. ')">1. List</button>
      <button class="tb" onclick="ins('> ')">❝</button>
      <button class="tb" onclick="ins('---')">—</button>
      <div class="ts"></div>
      <button class="tb" onclick="insBlock()">{ }</button>
      <button class="tb" onclick="insLink()">🔗</button>
      <button class="tb" onclick="insTable()">⊞</button>
      <div class="ts"></div>
      <button class="tb" onclick="openFind()">🔍 Find</button><button class="tb" onclick="openReplace()">↻ Replace</button>
    </div>
    <div class="ewrap"><textarea id="mdEditor"></textarea></div>
  </section>

  <!-- Preview -->
  <section class="ppane">
    <div class="phdr">
      <span class="pt">Preview</span>
      <span style="color:var(--muted);font-size:.57rem">Live · browser render</span>
    </div>
    <div class="pscroll"><div id="preview"></div></div>
  </section>
</div>

<div class="sbar">
  <span><span class="dot" id="sdot"></span><span id="stxt">Ready</span></span>
  <span>Lines: <span id="lc">0</span></span>
  <span>Chars: <span id="cc">0</span></span>
  <span style="margin-left:auto;font-size:.57rem">🔒 Zero server storage · ReportLab PDF</span>
</div>
<div id="overlay"><div class="lbox"><div class="spin"></div><p>Generating PDF…</p></div></div>
<div id="toasts"></div>

<script>
'use strict';
marked.setOptions({ breaks: true, gfm: true });

const cm = CodeMirror.fromTextArea(document.getElementById('mdEditor'), {
  mode: 'markdown', theme: 'dracula', lineNumbers: true, lineWrapping: true,
  extraKeys: {
    'Enter': 'newlineAndIndentContinueMarkdownList',
    'F11': c => c.setOption('fullScreen', !c.getOption('fullScreen')),
    'Esc': c => { if (c.getOption('fullScreen')) c.setOption('fullScreen', false); }
  }, autofocus: true
});

// Track fullscreen state changes
cm.on('optionChange', (inst, option) => {
  if (option === 'fullScreen') {
    document.body.classList.toggle('fs-active', inst.getOption('fullScreen'));
  }
});

let schemeBg = '', schemeText = '';

// ── refreshPreview ──────────────────────────────────────────────────────────
function refreshPreview() {
  const page = document.getElementById('preview');
  page.innerHTML = marked.parse(cm.getValue());

  const fs  = document.getElementById('fontSize').value;
  const lh  = document.getElementById('lineHeight').value;
  const fb  = document.getElementById('fontBody').value;
  const fh  = document.getElementById('fontHead').value;
  const acc = document.getElementById('accentColor').value;
  const pad = document.getElementById('pagePad').value;
  const hfs = parseFloat(document.getElementById('headingFontSize').value) || 1.0;

  const bg = schemeBg || '#ffffff';
  const fg = schemeText || '#1a1a2e';

  // Determine dark/light
  const hex = bg.replace('#','');
  const r = parseInt(hex.slice(0,2),16), g = parseInt(hex.slice(2,4),16), b2 = parseInt(hex.slice(4,6),16);
  const lum = (0.299*r + 0.587*g + 0.114*b2) / 255;
  const isDark = lum < 0.35;

  const codeBg  = isDark ? '#2d2d3a' : '#f3f4f6';
  const codeFg  = isDark ? '#f8f8f2' : '#c0392b';
  const preBg   = isDark ? '#1a1a2a' : '#272822';
  const bqBg    = isDark ? '#2a2a3a' : '#fdfaf3';
  const bqFg    = isDark ? '#aab0c8' : '#555555';
  const thBg    = isDark ? '#2a2a40' : '#f8f8ff';
  const trEven  = isDark ? '#252535' : '#fafafa';
  const border  = isDark ? '#444466' : '#dde';
  const link    = isDark ? '#7eb8e8' : '#2563eb';

  // Paper style
  page.style.cssText = [
    'font-family:'+fb, 'font-size:'+fs+'px', 'line-height:'+lh,
    'padding:'+pad+'px', 'background-color:'+bg, 'color:'+fg,
    'max-width:710px', 'margin:0 auto', 'border-radius:3px',
    'box-shadow:0 8px 32px rgba(0,0,0,.45)', 'min-height:880px'
  ].join(';');

  // Scoped dynamic styles
  let ds = document.getElementById('dyn-style');
  if (!ds) { ds = document.createElement('style'); ds.id='dyn-style'; document.head.appendChild(ds); }
  ds.textContent =
    '#preview h1,#preview h2,#preview h3,#preview h4{font-family:'+fh+';color:'+acc+'}' +
    '#preview h1{font-size:'+((fs*1.85)*hfs)+'px;border-bottom:2px solid '+acc+'}' +
    '#preview h2{font-size:'+((fs*1.45)*hfs)+'px;border-bottom:1px solid '+acc+'88}' +
    '#preview h3{font-size:'+((fs*1.15)*hfs)+'px}' +
    '#preview h4{font-size:'+((fs)*hfs)+'px}' +
    '#preview p,#preview li,#preview td{color:'+fg+'}' +
    '#preview a{color:'+link+'}' +
    '#preview code{background-color:'+codeBg+'!important;color:'+codeFg+'!important;border-radius:3px}' +
    '#preview pre{background-color:'+preBg+'!important;color:#f8f8f2!important;border-radius:6px}' +
    '#preview pre code{background-color:transparent!important;color:#f8f8f2!important}' +
    '#preview blockquote{border-left:4px solid '+acc+';background-color:'+bqBg+';color:'+bqFg+';border-radius:0 4px 4px 0}' +
    '#preview table{width:100%;border-collapse:collapse}' +
    '#preview th{background-color:'+thBg+';color:'+fg+';border:1px solid '+border+';padding:6px 10px}' +
    '#preview td{background-color:'+bg+';color:'+fg+';border:1px solid '+border+';padding:6px 10px}' +
    '#preview tr:nth-child(even) td{background-color:'+trEven+'}' +
    '#preview hr{border:none;border-top:1px solid '+border+'}' +
    '#preview del{color:'+bqFg+'}' +
    '#preview ul,#preview ol{padding-left:1.4em;margin-bottom:.9em}';

  updateStats();
}

const debouncedRefresh = (fn => { let t; return (...a) => { clearTimeout(t); t=setTimeout(()=>fn(...a),220); }; })(refreshPreview);
cm.on('change', debouncedRefresh);

cm.setValue(`# MD to PDF Converter

Convert **Markdown documents** to beautifully formatted PDFs with full control over styling.

## Features

- Write in plain Markdown syntax
- Real-time preview with live rendering
- Customizable fonts, colours, and page layouts
- No server storage — everything stays on your device
- Support for tables, code blocks, and rich formatting

## Example Content

This converter supports standard Markdown elements:

### Text Formatting

- **Bold text** using double asterisks
- *Italic text* using single asterisks
- ~~Strikethrough~~ text
- \`Inline code\` formatting

### Code & Blocks

\`\`\`
// Fenced code blocks preserve formatting
const message = "Hello, PDF!";
\`\`\`

### Styling Options

| Feature       | Support |
|---------------|---------|
| Colour Schemes | Night, Ocean, Warm, Classic |
| Page Sizes     | A4, Letter, A3, Legal |
| Margins        | Fully customizable |

> Use the Style tab to customize fonts, colours, and backgrounds.

---

Start by pasting or uploading your Markdown file, then customize the PDF styling to your liking!
`);
refreshPreview();

function updateStats() {
  const v = cm.getValue();
  document.getElementById('wc').textContent = (v.trim()?v.trim().split(/\s+/).length:0)+' words';
  document.getElementById('lc').textContent = cm.lineCount();
  document.getElementById('cc').textContent = v.length;
}

function stab(name) {
  const names=['import','style','page','help'];
  document.querySelectorAll('.stab').forEach((el,i)=>el.classList.toggle('active',names[i]===name));
  document.querySelectorAll('.spanel').forEach(p=>p.classList.toggle('active',p.id==='sp-'+name));
}

const SCHEMES = {
  classic:{bg:'#ffffff', text:'#1a1a2e', acc:'#d4a017'},
  warm:   {bg:'#fdf6e8', text:'#3c2f1a', acc:'#b07d3a'},
  night:  {bg:'#1e1e2e', text:'#cdd6f4', acc:'#89dceb'},
  ocean:  {bg:'#eef6ff', text:'#0a2540', acc:'#0066cc'},
};
function applyScheme(n) {
  const s=SCHEMES[n];
  schemeBg=s.bg; schemeText=s.text;
  document.getElementById('accentColor').value=s.acc;
  refreshPreview(); toast('Scheme: '+n,'ok');
}

// File handling
const dzEl=document.getElementById('dz');
dzEl.addEventListener('dragover',e=>{e.preventDefault();dzEl.classList.add('over')});
dzEl.addEventListener('dragleave',()=>dzEl.classList.remove('over'));
dzEl.addEventListener('drop',e=>{e.preventDefault();dzEl.classList.remove('over');if(e.dataTransfer.files[0])readFile(e.dataTransfer.files[0])});
function handleFile(e){if(e.target.files[0])readFile(e.target.files[0])}
function readFile(f){const r=new FileReader();r.onload=e=>{cm.setValue(e.target.result);toast('Loaded: '+f.name,'ok')};r.readAsText(f)}
function loadPasted(){const v=document.getElementById('pasteArea').value;if(!v.trim()){toast('Nothing to load','err');return}cm.setValue(v);document.getElementById('pasteArea').value='';toast('Loaded','ok')}

// Toolbar
function fmt(pre,post){const sel=cm.getSelection();if(sel){cm.replaceSelection(pre+sel+post)}else{const c=cm.getCursor();cm.replaceRange(pre+'text'+post,c);cm.setSelection({line:c.line,ch:c.ch+pre.length},{line:c.line,ch:c.ch+pre.length+4})}cm.focus()}
function ins(prefix){const c=cm.getCursor();cm.replaceRange('\n'+prefix,{line:c.line,ch:cm.getLine(c.line).length});cm.setCursor({line:c.line+1,ch:prefix.length});cm.focus()}
function insBlock(){const c=cm.getCursor();cm.replaceRange('\n```\n\n```',{line:c.line,ch:cm.getLine(c.line).length});cm.setCursor({line:c.line+2,ch:0});cm.focus()}
function insLink(){cm.replaceSelection('['+(cm.getSelection()||'link text'+'](https://)'));cm.focus()}
function insTable(){cm.replaceRange('\n| Col 1 | Col 2 | Col 3 |\n|-------|-------|-------|\n| Cell  | Cell  | Cell  |\n',{line:cm.getCursor().line,ch:cm.getLine(cm.getCursor().line).length});cm.focus()}
function clearEditor(){if(!cm.getValue().trim()||confirm('Clear?')){cm.setValue('');toast('Cleared')}}
function copyMD(){navigator.clipboard.writeText(cm.getValue()).then(()=>toast('Copied','ok'))}
function toggleFS(){cm.setOption('fullScreen',!cm.getOption('fullScreen'))}

// Templates
const SAMPLES={
  report:`# Quarterly Report\n**Q4 2024** · Finance Team\n\n---\n\n## Executive Summary\n\nRevenue grew **12%** YoY, exceeding projections by 3 points.\n\n## Revenue Table\n\n| Quarter | Revenue | Growth |\n|---------|---------|--------|\n| Q1 2024 | $2.1M   | +8%    |\n| Q2 2024 | $2.4M   | +10%   |\n| Q3 2024 | $2.7M   | +11%   |\n| Q4 2024 | $3.0M   | +12%   |\n\n## Highlights\n\n- Launched 3 new product lines\n- Expanded into 2 new markets\n- Reduced costs by 7%\n\n> We target 15% annual growth in 2025.\n\n---\n*Confidential*`,
  readme:`# Project Name\n\nA short description of what this project does.\n\n## Installation\n\n\`\`\`bash\nnpm install my-package\n\`\`\`\n\n## Quick Start\n\n\`\`\`js\nconst c = new Client();\nawait c.run();\n\`\`\`\n\n## Features\n\n- **Fast** — optimised for performance\n- **Flexible** — any data format\n- **Secure** — encrypted\n\n## License\n\nMIT © 2024`,
  letter:`# Formal Letter\n\n**Date:** January 1, 2025\n\n**From:** Jane Smith · 123 Main St\n\n**To:** Mr. John Doe · Acme Corp\n\n---\n\nDear Mr. Doe,\n\nI am writing to express my interest in the Senior Project Manager position.\n\nWith eight years of experience, I am confident I can contribute to your team.\n\nYours sincerely,\n\n**Jane Smith**`,
  resume:`# Jane Smith\n\njane@email.com · (555) 123-4567 · New York, NY\n\n---\n\n## Summary\n\nSoftware engineer with 6+ years building scalable web applications.\n\n## Experience\n\n### Senior Engineer — TechCorp (2021–Present)\n\n- Led microservices migration; latency reduced 40%\n- Mentored 5 junior engineers\n\n### Engineer — StartupXYZ (2018–2020)\n\n- Core API serving 1M+ daily requests\n- CI/CD deploy time reduced 60%\n\n## Education\n\nB.S. Computer Science — State University, 2018\n\n## Skills\n\nPython · JavaScript · React · PostgreSQL · Docker · AWS`,
};
function loadSample(n){cm.setValue(SAMPLES[n]);toast('Template: '+n,'ok')}

// Export
async function exportPDF() {
  const md = cm.getValue().trim();
  if (!md) { toast('Editor is empty','err'); return; }
  setLoading(true); setStatus('Generating PDF…', true);
  const payload = {
    markdown:    md,
    font_body:   document.getElementById('fontBody').value,
    font_head:   document.getElementById('fontHead').value,
    font_size:   document.getElementById('fontSize').value,
    line_height: document.getElementById('lineHeight').value,
    accent:      document.getElementById('accentColor').value,
    page_size:   document.getElementById('pageSize').value,
    orientation: document.getElementById('pageOrient').value,
    m_top:       document.getElementById('mTop').value,
    m_bot:       document.getElementById('mBot').value,
    m_left:      document.getElementById('mLeft').value,
    m_right:     document.getElementById('mRight').value,
    filename:    document.getElementById('filename').value || 'document',
    meta_title:  document.getElementById('metaTitle').value,
    meta_author: document.getElementById('metaAuthor').value,
    scheme_bg:   schemeBg,
    scheme_text: schemeText,
    heading_font_size: document.getElementById('headingFontSize').value,
  };
  try {
    const res = await fetch('/export', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      let msg='Server error ('+res.status+')';
      try{msg=(await res.json()).error||msg}catch(_){}
      throw new Error(msg);
    }
    const blob = await res.blob();
    if (blob.size===0) throw new Error('Empty PDF received');
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url; a.download=(payload.filename||'document')+'.pdf';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(()=>URL.revokeObjectURL(url),5000);
    toast('PDF downloaded!','ok'); setStatus('Ready');
  } catch(e) {
    toast('Export failed: '+e.message,'err'); setStatus('Error'); console.error(e);
  } finally { setLoading(false); }
}

function openFind(){cm.execCommand('find')}
function openReplace(){cm.execCommand('replace')}

function setLoading(v){document.getElementById('overlay').classList.toggle('on',v)}
function setStatus(t,busy=false){document.getElementById('stxt').textContent=t;document.getElementById('sdot').classList.toggle('busy',busy)}
function toast(msg,type=''){const el=document.createElement('div');el.className='toast '+type;el.textContent=msg;document.getElementById('toasts').appendChild(el);setTimeout(()=>el.remove(),3200)}
</script>
</body>
</html>"""


# ── Flask routes ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(TEMPLATE)


@app.route("/health")
def health():
    import reportlab

    return jsonify(
        {"status": "ok", "pdf_backend": "reportlab", "version": reportlab.Version}
    )


@app.route("/export", methods=["POST"])
def export_pdf():
    """Convert Markdown → PDF in-memory with ReportLab. Zero server storage."""
    data = request.get_json(force=True, silent=True)
    if not data or not data.get("markdown"):
        return jsonify({"error": "No markdown provided"}), 400

    filename = (
        re.sub(r"[^\w\-]", "_", (data.get("filename") or "document").strip())
        or "document"
    )

    try:
        pdf_bytes = build_pdf(data["markdown"], data)
    except Exception as exc:
        app.logger.exception("PDF build failed")
        return jsonify({"error": str(exc)}), 500

    buf = io.BytesIO(pdf_bytes)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{filename}.pdf",
    )


if __name__ == "__main__":
    import reportlab

    print(f"✓  PDF backend: ReportLab {reportlab.Version}")
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
