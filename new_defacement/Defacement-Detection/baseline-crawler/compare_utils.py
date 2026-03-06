#!/usr/bin/env python3
import difflib
from pathlib import Path
from html import escape
import re
from bs4 import BeautifulSoup, NavigableString, Tag, Comment
from datetime import datetime

DEFAULT_CONTEXT = 5


# ============================================================
# HTML-AWARE NORMALIZATION
# ============================================================

# ============================================================
# IGNORED ATTRIBUTES & TAGS
# ============================================================

IGNORED_ATTR_PATTERNS = {
    # Exact matches
    "nonce", "value", "floatingButtonsClickTracking", 
    "aria-controls", "aria-labelledby", "data-smartmenus-id", 
    "id", "name", "cb",
     
    # security tokens 
      "data-csrf", "csrf-token", "authenticity_token",
    "__VIEWSTATE", "__EVENTVALIDATION", "_token",
    "ajax_nonce",
    
   #Versioning 
   "data-version", "ver",
   
   #WP / VC / slider data attribute
    "data-source",
    "data-vc-full-width",
    "data-vc-full-width-init",
    "data-vc-stretch-content",

    "data-zs-prev",
    "data-zs-next",
    "data-zs-overlay",
    "data-zs-initzoom",
    "data-zs-speed",
    "data-zs-interval",
    "data-zs-switchSpeed",
    "data-zs-arrows",
    "data-zs-bullets",
    "data-zs-src",
    "data-zs-src2",

}

SUFFIX_PATTERNS = {
    "nonce",        # *nonce
    "ver",          # ?ver=5.8.10
    "version",      # data-version
    "timestamp",    # cached@12345
    "cache",        # cachebuster
}


IGNORED_TAGS = {
    "base", "meta", "link",# Frequently changes environment-to-environment or is injected
}

PREFIX_PATTERNS = {
    "data-aos", "data-wow", "data-framer", "data-scroll", "aria-hidden"
    #slider and carousel 
     "slider-",
    "carousel-",
    "owl-",
    "rev_slider_",
    "like_sc_",
 # WP / page builders
    "vc_custom_",
    "js-view-dom-id-",
    "ltx-sr-id-",
    "zoom-",
}

IGNORED_STYLE_PROPERTIES = {
    "transition", "transform", "animation", "will-change", "opacity",
    "transition-duration", "transition-delay", "transition-timing-function",
    "animation-duration", "animation-delay", "animation-iteration-count"
}

# Tags that trigger a score boost if their content changes
CRITICAL_TAGS = {"title", "h1"}
CRITICAL_BOOST = 1.0  # Percentage points to add if critical content changes

def should_ignore_attr(attr_name: str) -> bool:
    """Check if an attribute should be ignored based on patterns."""
    if attr_name in IGNORED_ATTR_PATTERNS:
        return True
    
    for suffix in SUFFIX_PATTERNS:
        if attr_name.endswith(suffix):
            return True

    for prefix in PREFIX_PATTERNS:
        if attr_name.startswith(prefix):
            return True
            
    return False


VENDOR_PREFIXES = ("-webkit-", "-moz-", "-ms-", "-o-")

def _normalize_style(style_str: str) -> str:
    """
    Parse inline style, remove ignored properties, and return stable sorted string.
    """
    if not style_str:
        return ""

    declarations = [d.strip() for d in style_str.split(";") if d.strip()]
    valid_decls = {}

    for decl in declarations:
        if ":" not in decl:
            continue

        raw_key, val = decl.split(":", 1)
        key = raw_key.strip().lower()
        val = " ".join(val.strip().split())

        # Strip vendor prefix
        for prefix in VENDOR_PREFIXES:
            if key.startswith(prefix):
                key = key[len(prefix):]
                break

        # Ignore dynamic / layout / animation properties
        if key in IGNORED_STYLE_PROPERTIES:
            continue

        valid_decls[key] = val

    if not valid_decls:
        return ""

    return "; ".join(f"{k}: {v}" for k, v in sorted(valid_decls.items()))



# ============================================================
# HTML-AWARE NORMALIZATION
# ============================================================

def _html_to_semantic_lines(html: str, strip_noise: bool = True) -> list[str]:
    """
    Convert HTML into semantic, whitespace-stable lines.
    strip_noise=True: Removes ignored attrs/tags (for scoring).
    strip_noise=False: Keeps them (for visual diff).
    """
    soup = BeautifulSoup(html, "lxml")
    
    if strip_noise:
        # Pre-emptive strip of ignored tags to prevent them from breaking structure/lines
        for tag_name in IGNORED_TAGS:
            for match in soup.find_all(tag_name):
                match.decompose()

    lines = []

    def walk(node, depth=0):

        if isinstance(node, Comment):
            return

        if isinstance(node, NavigableString):
            text = str(node)
            # Aggressive normalization for code blocks to handle minification differences
            if node.parent and node.parent.name in ("script", "style"):
                # Collapse whitespace around operators
                text = re.sub(r'\s*([{}()\[\]:;,=+\-*/%&|<>!^?~"\'`])\s*', r'\1', text)
                
                # BREAK long lines for readability (Beautification)
                # We split at semicolons and braces to keep logic semantic
                if len(text) > 80:
                    parts = re.split(r'([;{}])', text)
                    current_line = ""
                    for p in parts:
                        current_line += p
                        if p in (";", "{", "}"):
                            l = current_line.strip()
                            if l: lines.append(l)
                            current_line = ""
                    rest = current_line.strip()
                    if rest: lines.append(rest)
                    return
            
            # Standard normalization for all text (collapses multiple spaces to one)
            text = " ".join(text.split())

            if text:
                lines.append(text)

        elif isinstance(node, Tag):
            # normalize attributes (sorted and filtered)
            valid_attrs = {}
            CASE_INSENSITIVE_ATTRS = {"charset", "lang", "type", "method", "rel", "media", "http-equiv"}
            
            for k, v in node.attrs.items():
                if strip_noise and should_ignore_attr(k):
                    continue
                
                # Handle list-type attributes (like class) vs string
                if isinstance(v, list):
                    v = " ".join(v)
                
                v = str(v)
                
                # Special handling for style attribute
                if k.lower() == "style":
                    v = _normalize_style(v)
                    if not v:
                        continue
                
                # Case-insensitive normalization for specific attributes
                if k.lower() in CASE_INSENSITIVE_ATTRS:
                    v = v.lower()
                
                # Collapse whitespace
                v = " ".join(v.split())
                
                valid_attrs[k] = v

            attrs = " ".join(
                f'{k}="{v}"'
                for k, v in sorted(valid_attrs.items())
            )

            open_tag = f"<{node.name}{(' ' + attrs) if attrs else ''}>"
            lines.append(open_tag)

            for child in node.children:
                walk(child, depth + 1)

            close_tag = f"</{node.name}>"
            lines.append(close_tag)

    for child in soup.contents:
        walk(child)

    return lines


# ============================================================
# CHANGE RANGE COLLECTION
# ============================================================

def _collect_change_ranges(a_lines, b_lines, context):
    sm = difflib.SequenceMatcher(None, a_lines, b_lines)
    ranges = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        start = max(0, min(i1, j1) - context)
        end = max(i2, j2) + context
        ranges.append((start, end))

    # merge overlapping ranges
    merged = []
    for r in sorted(ranges):
        if not merged or r[0] > merged[-1][1]:
            merged.append(list(r))
        else:
            merged[-1][1] = max(merged[-1][1], r[1])

    return merged


# ============================================================
# SIDE-BY-SIDE RENDERING
# ============================================================

def _row(cls, a_ln, a_txt, b_ln, b_txt, is_html=False):
    """Render a table row. If is_html=True, txt is already escaped/formatted."""
    a_display = a_txt if is_html else escape(a_txt)
    b_display = b_txt if is_html else escape(b_txt)
    
    # We wrap the code in a span so highlights work reliably in PDF export
    # (Browsers often ignore TD backgrounds but respect SPAN backgrounds)
    return (
        f"<tr class='{cls}'>"
        f"<td class='ln'>{a_ln}</td>"
        f"<td class='code'><span class='line-content'>{a_display}</span></td>"
        f"<td class='ln'>{b_ln}</td>"
        f"<td class='code'><span class='line-content'>{b_display}</span></td>"
        f"</tr>"
    )


def _intra_diff(left: str, right: str):
    """Perform character-level highlighting within a line."""
    sm = difflib.SequenceMatcher(None, left, right)
    left_out = []
    right_out = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        l_part = escape(left[i1:i2])
        r_part = escape(right[j1:j2])

        if tag == "equal":
            left_out.append(l_part)
            right_out.append(r_part)
        elif tag == "replace":
            left_out.append(f"<span class='char-mod'>{l_part}</span>")
            right_out.append(f"<span class='char-mod'>{r_part}</span>")
        elif tag == "delete":
            left_out.append(f"<span class='char-mod'>{l_part}</span>")
        elif tag == "insert":
            right_out.append(f"<span class='char-mod'>{r_part}</span>")

    return "".join(left_out), "".join(right_out)


def _render_side_by_side_block(a_lines, b_lines, start, end):
    sm = difflib.SequenceMatcher(
        None,
        a_lines[start:end],
        b_lines[start:end],
    )

    rows = []
    a_ln = start + 1
    b_ln = start + 1

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(
                    _row(
                        "ctx",
                        a_ln,
                        a_lines[start + i1 + k],
                        b_ln,
                        b_lines[start + j1 + k],
                    )
                )
                a_ln += 1
                b_ln += 1

        elif tag == "delete":
            for line in a_lines[start + i1:start + i2]:
                rows.append(_row("del", a_ln, line, "", ""))
                a_ln += 1

        elif tag == "insert":
            for line in b_lines[start + j1:start + j2]:
                rows.append(_row("add", "", "", b_ln, line))
                b_ln += 1

        elif tag == "replace":
            # For "replace" (mod), we do intra-line diffing for each corresponding line
            max_len = max(i2 - i1, j2 - j1)
            for k in range(max_len):
                left_idx = start + i1 + k
                right_idx = start + j1 + k
                
                left = a_lines[left_idx] if left_idx < start + i2 else ""
                right = b_lines[right_idx] if right_idx < start + j2 else ""

                if left and right:
                    # Both present -> highlight changes INSIDE the line
                    l_html, r_html = _intra_diff(left, right)
                    rows.append(_row("mod", a_ln, l_html, b_ln, r_html, is_html=True))
                    a_ln += 1
                    b_ln += 1
                elif left:
                    # Only left remains (unbalanced replace)
                    rows.append(_row("del", a_ln, left, "", ""))
                    a_ln += 1
                elif right:
                    # Only right remains (unbalanced replace)
                    rows.append(_row("add", "", "", b_ln, right))
                    b_ln += 1

    return rows


# ============================================================
# PUBLIC ENTRY POINT
# ============================================================

def generate_html_diff(
    *,
    url: str,
    html_a: str,
    html_b: str,
    out_dir: Path,
    file_prefix: str,
    context_lines: int = DEFAULT_CONTEXT,
    severity: str = "NONE",
    score: float = 0.0,
    checked_at: str = "",
):
    """
    Generates ONE premium HTML-aware, side-by-side diff file.
    """

    out_dir.mkdir(parents=True, exist_ok=True)

    # HTML-aware normalization but KEEP noise for display
    a_lines = _html_to_semantic_lines(html_a, strip_noise=False)
    b_lines = _html_to_semantic_lines(html_b, strip_noise=False)

    ranges = _collect_change_ranges(a_lines, b_lines, context_lines)

    all_rows = []
    for start, end in ranges:
        rows = _render_side_by_side_block(a_lines, b_lines, start, end)
        if rows:
            all_rows.extend(rows)
            all_rows.append("<tr class='sep'><td colspan='4'></td></tr>")

    if not all_rows:
        body = "<div class='no-changes'>No visible changes detected in the semantic structure.</div>"
    else:
        body = (
            "<table class='diff'>"
            "<colgroup>"
            "<col style='width: 40px;'>"
            "<col>"
            "<col style='width: 40px;'>"
            "<col>"
            "</colgroup>"
            "<tr class='col-header'>"
            "<th colspan='2'>Baseline Content (Stored)</th>"
            "<th colspan='2'>Observed Content (Live)</th>"
            "</tr>"
            + "".join(all_rows)
            + "</table>"
        )

    # Styling and Layout
    html_page = f"""
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <meta charset="utf-8">
    <title>Diff: {escape(url)}</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {{
            --accent: #38bdf8;
            --legend-red: #ef4444;
            --legend-green: #22c55e;
            --legend-yellow: #facc15;
            
            /* High visibility backgrounds for Print/PDF */
            --print-add: #d1fae5;
            --print-del: #fee2e2;
            --print-mod: #fef3c7;
            --print-char: #facc15;
        }}

        [data-theme='dark'] {{
            --bg: #0f172a;
            --header-bg: #1e293b;
            --text: #f8fafc;
            --text-dim: #94a3b8;
            --del: #450a0a;
            --add: #064e3b;
            --mod: #facc15;
            --mod-char: #fef08a;
            --ln-bg: #141b2d;
            --diff-bg: #000000;
            --border: rgba(255,255,255,0.1);
            --border-dim: rgba(255,255,255,0.05);
            --legend-bg: rgba(0,0,0,0.2);
            --separator: #050505;
        }}

        [data-theme='light'] {{
            --bg: #f8fafc;
            --header-bg: #ffffff;
            --text: #0f172a;
            --text-dim: #64748b;
            --del: #fee2e2;
            --add: #d1fae5;
            --mod: #fef3c7;
            --mod-char: #facc15;
            --ln-bg: #f1f5f9;
            --diff-bg: #ffffff;
            --border: rgba(0,0,0,0.1);
            --border-dim: rgba(0,0,0,0.05);
            --legend-bg: rgba(0,0,0,0.05);
            --separator: #f1f5f9;
        }}

        * {{ box-sizing: border-box; }}
        body {{
            font-family: 'Inter', sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 0;
            width: 100%;
            overflow-x: hidden;
            -webkit-print-color-adjust: exact;
            print-color-adjust: exact;
            transition: background 0.3s, color 0.3s;
        }}

        /* Header & Navigation */
        .top-nav {{
            background: var(--header-bg);
            padding: 12px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            position: sticky;
            top: 0;
            z-index: 100;
            transition: background 0.3s, border 0.3s;
        }}
        .nav-links {{ display: flex; gap: 15px; align-items: center; }}
        .btn {{
            padding: 8px 16px;
            border-radius: 8px;
            text-decoration: none;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            border: none;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .btn-back {{ background: var(--border); color: var(--text); }}
        .btn-back:hover {{ background: var(--border-dim); }}
        .btn-theme {{ 
            background: var(--border); 
            color: var(--text); 
            padding: 8px; 
            border-radius: 50%;
            width: 36px;
            height: 36px;
            justify-content: center;
        }}
        .btn-pdf {{ background: var(--accent); color: #000; }}
        .btn-pdf:hover {{ transform: translateY(-1px); box-shadow: 0 4px 12px rgba(56, 189, 248, 0.3); }}

        /* Metadata Header & Cards */
        .report-header {{
            padding: 30px 20px;
            background: linear-gradient(180deg, var(--header-bg) 0%, var(--bg) 100%);
            border-bottom: 1px solid var(--border-dim);
        }}
        .report-title {{
            font-size: 24px;
            font-weight: 700;
            margin-bottom: 25px;
            color: var(--text);
        }}
        .cards-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .card {{
            background: var(--header-bg);
            border-radius: 12px;
            padding: 20px;
            border: 1px solid var(--border);
            transition: transform 0.2s, box-shadow 0.2s;
        }}
        .card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        .card-blue {{ border-left: 4px solid var(--accent); }}
        .card-green {{ border-left: 4px solid var(--legend-green); }}
        .card-yellow {{ border-left: 4px solid var(--legend-yellow); }}
        
        .card-label {{
            font-size: 14px;
            color: var(--text-dim);
            margin-bottom: 10px;
            font-weight: 500;
        }}
        .card-value {{
            font-size: 18px;
            font-weight: 700;
            color: var(--accent);
            word-break: break-all;
        }}
        .card-green .card-value {{ color: var(--legend-green); }}
        .card-yellow .card-value {{ color: var(--legend-yellow); }}
        
        .score-row {{ display: flex; align-items: center; gap: 12px; }}
        .risk-badge {{
            padding: 2px 10px;
            border-radius: 6px;
            background: #ef4444;
            color: #fff;
            font-size: 11px;
            font-weight: 800;
            text-transform: uppercase;
        }}

        /* Section Containers */
        .section-container {{
            margin: 20px;
            padding: 20px;
            background: var(--header-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
        }}
        .section-header {{
            font-size: 13px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-dim);
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--border-dim);
        }}

        /* Color Legend */
        .legend {{
            display: flex;
            gap: 20px;
            padding: 10px 15px;
            background: var(--legend-bg);
            border-radius: 8px;
            border: 1px solid var(--border-dim);
        }}
        .legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 11px; font-weight: 600; color: var(--text-dim); }}
        .legend-box {{ width: 12px; height: 12px; border-radius: 2px; }}
        .bg-add {{ background: var(--legend-green); }}
        .bg-mod {{ background: var(--legend-yellow); }}
        .bg-del {{ background: var(--legend-red); }}
        
        .severity-badge {{
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
        }}
        .sev-HIGH {{ background: #ef4444; color: #fff; }}
        .sev-MEDIUM {{ background: #f97316; color: #fff; }}
        .sev-LOW {{ background: #eab308; color: #000; }}
        .sev-CRITICAL {{ background: #991b1b; color: #fff; }}

        /* Diff Table */
        .diff-container {{ 
            width: 100%; 
            margin: 0;
            padding: 0;
        }}
        .diff {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
            background: var(--diff-bg);
        }}
        th {{
            background: var(--border-dim);
            padding: 10px;
            text-align: left !important;
            border-bottom: 2px solid var(--border);
            color: var(--text-dim);
            font-size: 11px;
            text-transform: uppercase;
        }}
        td {{ padding: 2px; vertical-align: top; border-bottom: none; text-align: left !important; }}
        
        .ln {{
            background: var(--ln-bg);
            color: var(--text-dim);
            text-align: right;
            user-select: none;
            font-size: 11px;
            padding-right: 6px !important;
            padding-left: 4px !important;
            border-right: 1px solid var(--border-dim);
        }}
        .code {{ 
            white-space: pre-wrap; 
            word-break: break-all;
            padding-left: 8px !important;
            padding-right: 8px !important;
            text-align: left !important;
        }}

        tr.ctx {{ background: transparent; }}
        tr.add .line-content {{ background: var(--add); display: block; width: 100%; }}
        tr.del .line-content {{ background: var(--del); display: block; width: 100%; }}
        tr.mod .line-content {{ background: var(--mod); color: #000 !important; font-weight: 500; display: block; width: 100%; }}
        
        .char-mod {{
            background: var(--mod-char);
            color: #000;
            border: 1px solid rgba(0,0,0,0.1);
            font-weight: 900;
            padding: 0 1px;
            border-radius: 2px;
        }}

        tr.sep td {{ height: 20px; background: var(--separator); border-top: 1px solid var(--border-dim); border-bottom: 1px solid var(--border-dim); }}
        .no-changes {{ padding: 100px; text-align: center; color: var(--text-dim); font-style: italic; }}

        /* Theme Toggle Icon Shapes */
        .sun-icon {{ display: none; }}
        [data-theme='light'] .moon-icon {{ display: none; }}
        [data-theme='light'] .sun-icon {{ display: block; }}

        /* Print Override */
        @media print {{
            .top-nav, .btn {{ display: none !important; }}
            html, body {{ 
                background: #fff !important; 
                color: #000 !important; 
                padding: 0 !important; 
                overflow: visible !important;
                height: auto !important;
            }}
            
            .report-header, .card, .section-container, .diff, .legend, .legend-item {{
                background: #fff !important;
                color: #000 !important;
                border-color: #ccc !important;
                box-shadow: none !important;
                -webkit-print-color-adjust: exact !important;
                print-color-adjust: exact !important;
            }}

            /* Page break control */
            .card {{ page-break-inside: avoid; break-inside: avoid; }}
            .section-container {{ 
                page-break-inside: auto !important; 
                break-inside: auto !important; 
                display: contents !important;
            }}
            
            .section-header, .diff-container, .diff {{
                display: block !important;
                width: 100% !important;
                page-break-inside: auto !important;
                break-inside: auto !important;
            }}
            
            .report-header {{ page-break-after: avoid; break-after: avoid; }}
            
            .report-header {{ padding: 5px 10px !important; border-bottom: 2px solid #000; margin-bottom: 10px !important; }}
            .report-title {{ font-size: 18px !important; margin-bottom: 10px !important; }}
            .cards-grid {{ 
                display: flex !important;
                flex-wrap: wrap !important;
                gap: 10px !important; 
                margin-bottom: 10px !important; 
            }}
            .card {{ flex: 1 1 30% !important; min-width: 0 !important; }}
            .card {{ padding: 10px !important; border: 1px solid #ccc !important; }}
            .card-label {{ font-size: 10px !important; margin-bottom: 2px !important; color: #666 !important; }}
            .card-value {{ font-size: 12px !important; color: #000 !important; }}
            
            .section-container {{ margin: 5px 0 !important; padding: 5px !important; border: 1px solid #eee !important; }}
            .section-header {{ margin-bottom: 2px !important; padding-bottom: 2px !important; font-size: 10px !important; border-bottom: 1px solid #ddd !important; }}
            
            .ln {{ background: #f3f4f6 !important; color: #9ca3af !important; border-right: 1px solid #ddd !important; padding: 2px 4px !important; }}
            .diff-container {{ padding: 0 !important; }}
            .diff {{ font-size: 9px !important; line-height: 1.2 !important; border: 1px solid #ddd !important; width: 100% !important; border-collapse: collapse; }}
            
            /* High-Contrast Highlights: Using span-based backgrounds for PDF reliability */
            tr.add .line-content {{ background-color: #bbf7d0 !important; color: #064e3b !important; display: block; width: 100%; }}
            tr.del .line-content {{ background-color: #fecaca !important; color: #7f1d1d !important; display: block; width: 100%; }}
            tr.mod .line-content {{ background-color: #fef08a !important; color: #854d0e !important; display: block; width: 100%; }}
            
            /* Intra-line symmetry and visibility */
            .char-mod {{ 
                background: #facc15 !important; 
                border: 1px solid #000 !important; 
                color: #000 !important;
                font-weight: 800 !important;
                text-decoration: underline;
                display: inline-block;
            }}
            
            /* Ensure text is sharp black in the diff */
            .code {{ color: #000 !important; text-align: left !important; white-space: pre-wrap !important; font-weight: 500; }}
        }}
    </style>
    <script>
        function toggleTheme() {{
            const html = document.documentElement;
            const currentTheme = html.getAttribute('data-theme');
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
            html.setAttribute('data-theme', newTheme);
            localStorage.setItem('crawler-theme', newTheme);
        }}

        // Initialize theme on load
        (function() {{
            const savedTheme = localStorage.getItem('crawler-theme') || 'dark';
            document.documentElement.setAttribute('data-theme', savedTheme);
        }})();
    </script>
</head>
<body>
    <nav class="top-nav">
        <div class="logo" style="font-weight:700">Audit <span style="color:var(--accent)">Evidence</span></div>
        <div class="nav-links">
            <button onclick="toggleTheme()" class="btn btn-theme" title="Toggle Light/Dark Mode">
                <span class="moon-icon">🌙</span>
                <span class="sun-icon">☀️</span>
            </button>
            <a href="../../../reports/latest_report.html" class="btn btn-back">← Back to Dashboard</a>
            <button onclick="window.print()" class="btn btn-pdf">Download as PDF</button>
        </div>
    </nav>

    <header class="report-header">
        <h1 class="report-title">Defacement Analysis Report</h1>
        <div class="cards-grid">
            <div class="card card-blue">
                <div class="card-label">Defacement Report For</div>
                <div class="card-value">{escape(url)}</div>
            </div>
            <div class="card card-green">
                <div class="card-label">Date & Time (IST)</div>
                <div class="card-value">{checked_at or datetime.now().strftime("%d-%m-%Y %H:%M:%S")}</div>
            </div>
            <div class="card card-yellow">
                <div class="card-label">Change Percentage</div>
                <div class="score-row">
                    <div class="card-value">{score}%</div>
                    <div class="risk-badge">Risk</div>
                </div>
            </div>
        </div>
    </header>

    <div class="section-container">
        <div class="section-header">Legend</div>
        <div class="legend">
            <div class="legend-item"><div class="legend-box bg-add"></div> Inserted</div>
            <div class="legend-item"><div class="legend-box bg-mod"></div> Modified</div>
            <div class="legend-item"><div class="legend-box bg-del"></div> Removed</div>
        </div>
    </div>

    <div class="section-container">
        <div class="section-header">Side-by-Side Comparison</div>
        <div class="diff-container">
{body}
        </div>
    </div>
</body>
</html>
"""

    out_path = out_dir / f"{file_prefix}.html"
    out_path.write_text(html_page, encoding="utf-8")

    return str(out_path)
# ============================================================
# DEFACE­MENT SCORING
# ============================================================

from bs4 import BeautifulSoup, NavigableString, Tag
import difflib






def _extract_critical_content(html: str) -> dict[str, str]:
    """
    Extract text content from critical tags.
    Returns: {'title': 'Page Title', 'h1': 'Header Text'}
    """
    soup = BeautifulSoup(html or "", "lxml")
    content = {}
    
    for tag_name in CRITICAL_TAGS:
        # Find first occurrence (usually the most important one)
        tag = soup.find(tag_name)
        if tag:
            # Normalize whitespace
            text = " ".join(tag.get_text().split())
            content[tag_name] = text
            
    return content


def calculate_defacement_percentage(
    baseline_html: str,
    observed_html: str,
    threshold: float = 1.0,
) -> float:
    """
    Returns defacement percentage (0.0 – 100.0)
    Includes a boost for critical tag changes (title, h1) IF score < threshold.
    """
    base_lines = _html_to_semantic_lines(baseline_html, strip_noise=True)
    obs_lines = _html_to_semantic_lines(observed_html, strip_noise=True)

    if not base_lines:
        return 100.0 if obs_lines else 0.0

    # 1. Standard Diff Score
    sm = difflib.SequenceMatcher(None, base_lines, obs_lines)

    changed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            changed += (i2 - i1)
        if tag == "insert":
            changed += (j2 - j1)

    pct = (changed / len(base_lines)) * 100 
    
    # Optimization: If we already exceeded the threshold, don't pretend to inspect critical tags
    # The user is already alerted.
    if pct >= threshold:
        return round(min(100.0, pct), 2)

    # 2. Critical Content Boost
    base_critical = _extract_critical_content(baseline_html)
    obs_critical = _extract_critical_content(observed_html)
    
    boost = 0.0
    for tag in CRITICAL_TAGS:
        b_text = base_critical.get(tag, "")
        o_text = obs_critical.get(tag, "")
        
        # If content exists in both but differs -> BOOST
        if b_text and o_text and b_text != o_text:
            boost += CRITICAL_BOOST
            
    final_score = min(100.0, pct + boost)
    return round(final_score, 2)


def semantic_hash(html: str) -> str:
    """
    Generate a hash of the semantic content of the HTML.
    Stable against whitespace and attribute ordering changes.
    """
    import hashlib
    lines = _html_to_semantic_lines(html, strip_noise=True)
    content = "\n".join(lines)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def defacement_severity(score: float) -> str:
    if score < 5:
        return "LOW"
    if score < 20:
        return "MEDIUM"
    if score < 50:
        return "HIGH"
    return "CRITICAL"
