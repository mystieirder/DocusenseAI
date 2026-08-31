"""
Annotation routes (FR-05) — persistent highlights + AI selection actions.

  POST   /api/annotations             create a highlight/note
  GET    /api/annotations/{doc}       list a document's annotations (Highlight Inspector)
  PATCH  /api/annotations/{id}        edit notes / tags
  DELETE /api/annotations/{id}        remove one
  POST   /api/annotations/selection   run one of the 4 floating-pill actions on
                                      selected text (explain | summarize | risks |
                                      custom question), optionally saving the AI
                                      response as an annotation.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import User, Document, Annotation
from ..schemas import (AnnotationCreate, AnnotationOut, AnnotationUpdate,
                       SelectionActionRequest, SelectionActionResponse)
from .. import llm

log = logging.getLogger("docusense.annotations")
router = APIRouter(prefix="/api/annotations", tags=["annotations"])

_ACTION_PROMPTS = {
    "explain":   "Explain the following passage in clear, plain language for a non-expert. "
                 "Be concise and faithful to the text.",
    "summarize": "Summarize the following passage in 2-4 sentences, preserving the key facts.",
    "risks":     "Identify any risks, obligations, liabilities, or concerns in the following "
                 "passage. Return a short bulleted list; if there are none, say so.",
}


def _owned_doc(db: Session, user: User, doc_id: str) -> Document:
    doc = db.get(Document, doc_id)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return doc


def _owned_annotation(db: Session, user: User, ann_id: int) -> Annotation:
    ann = db.get(Annotation, ann_id)
    if not ann or ann.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Annotation not found")
    return ann


@router.post("", response_model=AnnotationOut, status_code=201)
def create_annotation(req: AnnotationCreate,
                      db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    _owned_doc(db, user, req.doc_id)
    ann = Annotation(
        doc_id=req.doc_id,
        user_id=user.id,
        page_num=req.page_num,
        rect_coords=req.rect_coords,
        selected_text=req.selected_text,
        ai_notes=req.ai_notes,
        tags=req.tags,
        action=req.action,
    )
    db.add(ann)
    db.commit()
    db.refresh(ann)
    return ann


@router.get("/{doc_id}", response_model=list[AnnotationOut])
def list_annotations(doc_id: str,
                     db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    _owned_doc(db, user, doc_id)
    return (
        db.query(Annotation)
        .filter(Annotation.doc_id == doc_id, Annotation.user_id == user.id)
        .order_by(Annotation.page_num, Annotation.created_at)
        .all()
    )


@router.patch("/{ann_id}", response_model=AnnotationOut)
def update_annotation(ann_id: int,
                      req: AnnotationUpdate,
                      db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    ann = _owned_annotation(db, user, ann_id)
    if req.ai_notes is not None:
        ann.ai_notes = req.ai_notes
    if req.tags is not None:
        ann.tags = req.tags
    if req.selected_text is not None:
        ann.selected_text = req.selected_text
    db.commit()
    db.refresh(ann)
    return ann


@router.delete("/{ann_id}", status_code=204)
def delete_annotation(ann_id: int,
                      db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    ann = _owned_annotation(db, user, ann_id)
    db.delete(ann)
    db.commit()
    return None


@router.post("/selection", response_model=SelectionActionResponse)
def selection_action(req: SelectionActionRequest,
                     db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Run a floating-pill action on a text selection; optionally persist it."""
    action = req.action if req.action in {"explain", "summarize", "risks", "custom"} else "explain"

    if action == "custom":
        question = (req.question or "").strip()
        if not question:
            raise HTTPException(400, "A question is required for the 'custom' action.")
        instruction = (f"Answer the following question using ONLY the passage below. "
                       f"If the passage does not contain the answer, say so.\n\n"
                       f"QUESTION: {question}")
    else:
        instruction = _ACTION_PROMPTS[action]

    prompt = f"{instruction}\n\nPASSAGE:\n\"\"\"\n{req.text}\n\"\"\""
    try:
        result = llm.generate(prompt)
    except llm.LLMError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))

    annotation_id = None
    if req.save:
        if not req.doc_id:
            raise HTTPException(400, "doc_id is required to save an annotation.")
        _owned_doc(db, user, req.doc_id)
        ann = Annotation(
            doc_id=req.doc_id,
            user_id=user.id,
            page_num=req.page_num or 1,
            rect_coords=req.rect_coords,
            selected_text=req.text,
            ai_notes=result,
            tags=[action],
            action=action,
        )
        db.add(ann)
        db.commit()
        db.refresh(ann)
        annotation_id = ann.id

    return SelectionActionResponse(action=action, result=result, annotation_id=annotation_id)
