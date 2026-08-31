"""
Summary routes (FR-04) — executive summary + structured risk/deadline/action digest.

  GET  /api/summary/{doc}            cached summary, generated on first request
  POST /api/summary/{doc}/regenerate force a fresh generation
  GET  /api/summary/{doc}/export     download as Markdown or PDF (?format=md|pdf)

The model returns a single JSON object (executive ~200-300 words, key points, and
tabular risks/deadlines/actions). The result is cached on documents.summary so the
split-pane digest and exports are instant after the first build. Clipboard export
is a client-side action over the Markdown this endpoint produces.
"""
import io
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import User, Document, DocumentChunk
from ..schemas import SummaryOut, RiskItem, DeadlineItem, ActionItem
from .. import llm

log = logging.getLogger("docusense.summary")
router = APIRouter(prefix="/api/summary", tags=["summary"])

_SUMMARY_BUDGET = 16000     # chars of document text fed to the model


def _owned_ready_doc(db: Session, user: User, doc_id: str) -> Document:
    doc = db.get(Document, doc_id)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    if doc.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Document is not ready (status: {doc.status}).")
    return doc


def _gather_text(db: Session, doc_id: str, budget: int = _SUMMARY_BUDGET) -> str:
    """Concatenate chunk text in reading order, evenly sampling large documents."""
    rows = (
        db.query(DocumentChunk.content)
        .filter(DocumentChunk.doc_id == doc_id)
        .order_by(DocumentChunk.page_num, DocumentChunk.chunk_index)
        .all()
    )
    texts = [r[0] for r in rows if r[0]]
    if not texts:
        return ""
    total = sum(len(t) for t in texts)
    if total <= budget:
        return "\n".join(texts)

    # Evenly sample chunks across the whole document until the budget is filled.
    stride = max(1, total // budget)
    picked, acc = [], 0
    for i, t in enumerate(texts):
        if i % stride == 0:
            picked.append(t)
            acc += len(t)
            if acc >= budget:
                break
    return "\n".join(picked)[:budget]


def _summary_prompt(doc_name: str, text: str) -> str:
    return (
        "You are DocuSense. Read the document excerpt and produce a JSON object with "
        "exactly these keys:\n"
        '  "executive": a 200-300 word executive summary (plain prose),\n'
        '  "key_points": array of 3-7 short strings,\n'
        '  "risks": array of {"description": str, "severity": "low"|"medium"|"high"},\n'
        '  "deadlines": array of {"description": str, "date": str or null},\n'
        '  "actions": array of {"description": str, "priority": "low"|"medium"|"high"}.\n'
        "Base everything ONLY on the text provided. Use empty arrays when a category "
        "has no items. Return JSON only — no prose, no code fences.\n\n"
        f"DOCUMENT: {doc_name}\n\nTEXT:\n{text}"
    )


def _coerce_summary(data: dict) -> SummaryOut:
    def as_items(raw, model, desc_key="description"):
        items = []
        for it in (raw or []):
            if isinstance(it, str):
                items.append(model(description=it))
            elif isinstance(it, dict):
                payload = dict(it)
                if desc_key not in payload:
                    payload[desc_key] = payload.get("text") or payload.get("name") or ""
                try:
                    items.append(model(**{k: v for k, v in payload.items()
                                          if k in model.model_fields}))
                except Exception:
                    items.append(model(description=str(payload.get(desc_key, ""))))
        return items

    key_points = [str(k) for k in (data.get("key_points") or [])]
    return SummaryOut(
        executive=str(data.get("executive") or "").strip(),
        key_points=key_points,
        risks=as_items(data.get("risks"), RiskItem),
        deadlines=as_items(data.get("deadlines"), DeadlineItem),
        actions=as_items(data.get("actions"), ActionItem),
    )


def _generate_summary(db: Session, doc: Document) -> SummaryOut:
    text = _gather_text(db, doc.id)
    if not text.strip():
        raise HTTPException(422, "This document has no extractable text to summarize.")
    try:
        data = llm.generate_json(_summary_prompt(doc.name, text))
    except llm.LLMError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    summary = _coerce_summary(data)
    if not (summary.executive.strip() or summary.key_points or summary.risks
            or summary.deadlines or summary.actions):
        # The model produced nothing usable. Do NOT cache a blank (that traps the
        # doc into re-serving an empty summary forever) — surface a retryable error.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "The model returned an empty summary. Please try again.")
    doc.summary = summary.model_dump()
    db.commit()
    return summary


@router.get("/{doc_id}", response_model=SummaryOut)
def get_summary(doc_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = _owned_ready_doc(db, user, doc_id)
    if doc.summary:
        return SummaryOut(**doc.summary)
    return _generate_summary(db, doc)


@router.post("/{doc_id}/regenerate", response_model=SummaryOut)
def regenerate_summary(doc_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = _owned_ready_doc(db, user, doc_id)
    return _generate_summary(db, doc)


# ── Export ────────────────────────────────────────────────────────────────
def _summary_markdown(doc_name: str, s: SummaryOut) -> str:
    lines = [f"# Summary — {doc_name}", "", "## Executive Summary", "", s.executive or "_None_", ""]
    if s.key_points:
        lines += ["## Key Takeaways", ""]
        lines += [f"- {k}" for k in s.key_points]
        lines.append("")
    if s.risks:
        lines += ["## Risks", "", "| Severity | Description |", "| --- | --- |"]
        lines += [f"| {r.severity} | {r.description} |" for r in s.risks]
        lines.append("")
    if s.deadlines:
        lines += ["## Deadlines", "", "| Date | Description |", "| --- | --- |"]
        lines += [f"| {d.date or '—'} | {d.description} |" for d in s.deadlines]
        lines.append("")
    if s.actions:
        lines += ["## Action Items", "", "| Priority | Description |", "| --- | --- |"]
        lines += [f"| {a.priority} | {a.description} |" for a in s.actions]
        lines.append("")
    return "\n".join(lines)


def _summary_pdf(doc_name: str, s: SummaryOut) -> bytes:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, ListFlowable, ListItem)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, title=f"Summary — {doc_name}",
                            leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.9 * inch, bottomMargin=0.9 * inch)
    styles = getSampleStyleSheet()
    story = [Paragraph(f"Summary — {doc_name}", styles["Title"]), Spacer(1, 12),
             Paragraph("Executive Summary", styles["Heading2"]),
             Paragraph(s.executive or "None", styles["BodyText"]), Spacer(1, 12)]

    if s.key_points:
        story += [Paragraph("Key Takeaways", styles["Heading2"]),
                  ListFlowable([ListItem(Paragraph(k, styles["BodyText"])) for k in s.key_points],
                               bulletType="bullet"),
                  Spacer(1, 12)]

    def table(title, header, rows):
        if not rows:
            return []
        data = [header] + rows
        t = Table(data, colWidths=[1.4 * inch, 4.6 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1B140F")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5EFE3")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]))
        return [Paragraph(title, styles["Heading2"]), t, Spacer(1, 12)]

    body = styles["BodyText"]
    story += table("Risks", ["Severity", "Description"],
                   [[r.severity, Paragraph(r.description, body)] for r in s.risks])
    story += table("Deadlines", ["Date", "Description"],
                   [[d.date or "—", Paragraph(d.description, body)] for d in s.deadlines])
    story += table("Action Items", ["Priority", "Description"],
                   [[a.priority, Paragraph(a.description, body)] for a in s.actions])

    doc.build(story)
    return buf.getvalue()


@router.get("/{doc_id}/export")
def export_summary(doc_id: str,
                   format: str = Query("md", pattern="^(md|pdf)$"),
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    doc = _owned_ready_doc(db, user, doc_id)
    summary = SummaryOut(**doc.summary) if doc.summary else _generate_summary(db, doc)
    safe_name = (doc.name.rsplit(".", 1)[0] or "summary").replace('"', "")

    if format == "md":
        content = _summary_markdown(doc.name, summary).encode("utf-8")
        return Response(content, media_type="text/markdown",
                        headers={"Content-Disposition": f'attachment; filename="{safe_name}-summary.md"'})

    pdf = _summary_pdf(doc.name, summary)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{safe_name}-summary.pdf"'})