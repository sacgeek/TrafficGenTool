"""
Export engine.

Produces downloadable reports from a completed (or running) TestSession.

Supported formats:
  CSV  — one row per snapshot, all numeric fields
  PDF  — summary report with aggregate stats per stream type

PDF uses only the Python standard library + reportlab (if available),
falling back to a plain-text report if reportlab is not installed.
"""

from __future__ import annotations
import csv
import io
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller.models import TestSession, StreamSnapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "session_id", "node_id", "stream_type", "timestamp",
    "loss_pct", "latency_ms", "jitter_ms",
    "mos_mos", "mos_label", "mos_color", "mos_r_factor",
    "packets_recv", "packets_expected",
    "page_load_ms", "bytes_downloaded",
]


def export_csv(session: "TestSession") -> bytes:
    """Return CSV bytes for all snapshots in the session."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for snap in session.snapshots:
        row = snap.to_dict()
        row["session_id"] = session.session_id
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def _compute_stats(snapshots: list) -> dict:
    """Aggregate statistics over a list of StreamSnapshot objects."""
    if not snapshots:
        return {}

    loss_vals    = [s.loss_pct   for s in snapshots]
    lat_vals     = [s.latency_ms for s in snapshots if s.latency_ms > 0]
    jitter_vals  = [s.jitter_ms  for s in snapshots]
    mos_vals     = [s.mos_mos    for s in snapshots if s.mos_mos > 0]

    def safe_avg(lst):
        return sum(lst) / len(lst) if lst else 0.0

    def safe_max(lst):
        return max(lst) if lst else 0.0

    def safe_min(lst):
        return min(lst) if lst else 0.0

    return {
        "count":        len(snapshots),
        "loss_avg":     round(safe_avg(loss_vals), 2),
        "loss_max":     round(safe_max(loss_vals), 2),
        "latency_avg":  round(safe_avg(lat_vals), 1),
        "latency_max":  round(safe_max(lat_vals), 1),
        "jitter_avg":   round(safe_avg(jitter_vals), 1),
        "jitter_max":   round(safe_max(jitter_vals), 1),
        "mos_avg":      round(safe_avg(mos_vals), 2),
        "mos_min":      round(safe_min(mos_vals), 2),
    }


def _mos_label(mos: float) -> str:
    if mos >= 4.3: return "Excellent"
    if mos >= 4.0: return "Good"
    if mos >= 3.6: return "Fair"
    if mos >= 3.1: return "Poor"
    return "Bad"


# ---------------------------------------------------------------------------
# PDF export (reportlab)
# ---------------------------------------------------------------------------

def export_pdf(session: "TestSession") -> bytes:
    """
    Generate a PDF report.  Falls back to a plain-text PDF lookalike
    (as bytes) if reportlab is not installed.
    """
    try:
        return _pdf_reportlab(session)
    except ImportError:
        logger.warning("reportlab not installed — generating text report as PDF fallback")
        return _pdf_text_fallback(session)


def _pdf_reportlab(session: "TestSession") -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            leftMargin=0.75*inch, rightMargin=0.75*inch,
                            topMargin=0.75*inch, bottomMargin=0.75*inch)

    styles = getSampleStyleSheet()
    style_h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18, spaceAfter=6)
    style_h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, spaceAfter=4)
    style_body = styles["Normal"]

    # Color helpers
    GREEN  = colors.HexColor("#22c55e")
    YELLOW = colors.HexColor("#eab308")
    RED    = colors.HexColor("#ef4444")
    GRAY   = colors.HexColor("#6b7280")

    def mos_color(mos: float):
        if mos >= 4.0: return GREEN
        if mos >= 3.6: return YELLOW
        return RED

    story = []

    # --- Title ---
    story.append(Paragraph("NetLab Traffic Generator — Test Report", style_h1))
    story.append(HRFlowable(width="100%", thickness=1, color=GRAY))
    story.append(Spacer(1, 8))

    # --- Session metadata ---
    ts_fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "—"
    meta = [
        ["Session ID",  session.session_id],
        ["Plan name",   session.plan.name],
        ["Status",      session.status.value],
        ["Started",     ts_fmt(session.start_time)],
        ["Ended",       ts_fmt(session.end_time)],
        ["Duration",    f"{session.duration_s:.0f} s" if session.duration_s else "—"],
        ["Nodes",       ", ".join(session.node_ids) or "—"],
        ["Snapshots",   str(len(session.snapshots))],
    ]
    t = Table(meta, colWidths=[1.8*inch, 4.5*inch])
    t.setStyle(TableStyle([
        ("FONTNAME",    (0,0), (-1,-1), "Helvetica"),
        ("FONTSIZE",    (0,0), (-1,-1), 9),
        ("FONTNAME",    (0,0), (0,-1), "Helvetica-Bold"),
        ("BACKGROUND",  (0,0), (-1,0), colors.HexColor("#f3f4f6")),
        ("ROWBACKGROUNDS", (0,0), (-1,-1), [colors.white, colors.HexColor("#f9fafb")]),
        ("BOX",         (0,0), (-1,-1), 0.5, GRAY),
        ("INNERGRID",   (0,0), (-1,-1), 0.25, GRAY),
        ("TOPPADDING",  (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
    ]))
    story.append(t)
    story.append(Spacer(1, 16))

    # --- Per-stream-type aggregate stats ---
    by_type: dict[str, list] = defaultdict(list)
    for s in session.snapshots:
        by_type[s.stream_type].append(s)

    if by_type:
        story.append(Paragraph("Aggregate statistics by stream type", style_h2))
        header = ["Stream type", "Samples", "Avg loss %", "Max loss %",
                  "Avg latency ms", "Max latency ms", "Avg jitter ms", "MOS avg", "MOS min", "Quality"]
        rows = [header]
        for stype, snaps in sorted(by_type.items()):
            st = _compute_stats(snaps)
            rows.append([
                stype,
                st["count"],
                f"{st['loss_avg']:.2f}",
                f"{st['loss_max']:.2f}",
                f"{st['latency_avg']:.1f}",
                f"{st['latency_max']:.1f}",
                f"{st['jitter_avg']:.1f}",
                f"{st['mos_avg']:.2f}",
                f"{st['mos_min']:.2f}",
                _mos_label(st["mos_avg"]),
            ])

        col_w = [1.1*inch, 0.6*inch, 0.75*inch, 0.75*inch,
                 0.9*inch, 0.9*inch, 0.85*inch, 0.65*inch, 0.65*inch, 0.75*inch]
        tbl = Table(rows, colWidths=col_w)
        style_cmds = [
            ("FONTNAME",     (0,0), (-1,0),  "Helvetica-Bold"),
            ("FONTNAME",     (0,1), (-1,-1), "Helvetica"),
            ("FONTSIZE",     (0,0), (-1,-1), 8),
            ("BACKGROUND",   (0,0), (-1,0),  colors.HexColor("#1e40af")),
            ("TEXTCOLOR",    (0,0), (-1,0),  colors.white),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [colors.white, colors.HexColor("#f0f9ff")]),
            ("BOX",          (0,0), (-1,-1), 0.5, GRAY),
            ("INNERGRID",    (0,0), (-1,-1), 0.25, GRAY),
            ("TOPPADDING",   (0,0), (-1,-1), 3),
            ("BOTTOMPADDING",(0,0), (-1,-1), 3),
        ]
        # Color the MOS avg column
        for i, row in enumerate(rows[1:], start=1):
            try:
                mos = float(row[7])
                c = mos_color(mos)
                style_cmds.append(("TEXTCOLOR", (7,i), (8,i), c))
                style_cmds.append(("FONTNAME",  (9,i), (9,i), "Helvetica-Bold"))
                style_cmds.append(("TEXTCOLOR", (9,i), (9,i), c))
            except (ValueError, IndexError):
                pass
        tbl.setStyle(TableStyle(style_cmds))
        story.append(tbl)
        story.append(Spacer(1, 16))

    # --- Plan configuration ---
    story.append(Paragraph("Test plan configuration", style_h2))
    plan = session.plan
    plan_rows = [
        ["Voice calls",      str(plan.voice_calls)],
        ["Video calls",      str(plan.video_calls)],
        ["Screen shares",    str(plan.screen_shares)],
        ["Web users",        str(plan.web_users)],
        ["YouTube users",    str(plan.youtube_users)],
        ["Duration (s)",     str(plan.duration_s) if plan.duration_s else "Manual stop"],
        ["URLs",             "\n".join(plan.web_urls) if plan.web_urls else "—"],
    ]
    pt = Table(plan_rows, colWidths=[1.8*inch, 4.5*inch])
    pt.setStyle(TableStyle([
        ("FONTNAME",     (0,0), (-1,-1), "Helvetica"),
        ("FONTSIZE",     (0,0), (-1,-1), 9),
        ("FONTNAME",     (0,0), (0,-1), "Helvetica-Bold"),
        ("ROWBACKGROUNDS",(0,0),(-1,-1),[colors.white, colors.HexColor("#f9fafb")]),
        ("BOX",          (0,0), (-1,-1), 0.5, GRAY),
        ("INNERGRID",    (0,0), (-1,-1), 0.25, GRAY),
        ("TOPPADDING",   (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
    ]))
    story.append(pt)

    # --- Footer ---
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=GRAY))
    story.append(Paragraph(
        f"Generated by NetLab Traffic Generator  ·  {ts_fmt(time.time())}",
        ParagraphStyle("footer", parent=style_body, fontSize=8,
                       textColor=GRAY, alignment=1)
    ))

    doc.build(story)
    return buf.getvalue()


def _pdf_text_fallback(session: "TestSession") -> bytes:
    """Plain-text fallback when reportlab is absent."""
    lines = [
        "NetLab Traffic Generator — Test Report",
        "=" * 50,
        f"Session:   {session.session_id}",
        f"Plan:      {session.plan.name}",
        f"Status:    {session.status.value}",
        f"Duration:  {session.duration_s:.0f}s" if session.duration_s else "Duration:  —",
        f"Snapshots: {len(session.snapshots)}",
        "",
        "Aggregate statistics by stream type",
        "-" * 50,
    ]
    by_type: dict[str, list] = defaultdict(list)
    for s in session.snapshots:
        by_type[s.stream_type].append(s)

    for stype, snaps in sorted(by_type.items()):
        st = _compute_stats(snaps)
        lines += [
            f"  {stype.upper()}",
            f"    Samples  : {st['count']}",
            f"    Loss avg : {st['loss_avg']:.2f}%  max: {st['loss_max']:.2f}%",
            f"    Latency  : avg {st['latency_avg']:.1f}ms  max {st['latency_max']:.1f}ms",
            f"    Jitter   : avg {st['jitter_avg']:.1f}ms  max {st['jitter_max']:.1f}ms",
            f"    MOS      : avg {st['mos_avg']:.2f}  min {st['mos_min']:.2f}  ({_mos_label(st['mos_avg'])})",
            "",
        ]

    return "\n".join(lines).encode("utf-8")
