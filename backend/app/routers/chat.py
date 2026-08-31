"""
Chat routes (FR-03) — grounded Q&A over a document.

  POST /api/chat/stream    Server-Sent Events: token deltas then a final `done`
                           event carrying clickable page/bbox citations.
  POST /api/chat           Non-streaming equivalent (returns the full answer).
  GET  /api/chat/{doc}/history   Persisted conversation for the split-pane.

Answers are strictly grounded in retrieved passages; the model is instructed to
say it can't find something rather than hallucinate (FR-03 fallback). Citations
are derived from the retrieved chunks and, when the model references [Page N],
narrowed to the pages it actually used.
"""
import json
import logging
import re
import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..deps import get_current_user
from ..models import User, Document, ChatMessage
from ..schemas import ChatRequest, ChatMessageOut, Citation
from .. import rag, llm

log = logging.getLogger("docusense.chat")
router = APIRouter(prefix="/api/chat", tags=["chat"])

_PAGE_REF = re.compile(r"\[?\bpage\s+(\d+)\b\]?", re.IGNORECASE)

# How many times a throttled chat turn will auto-wait out the free-tier limit and
# resend before giving up. Each wait honors the server's retryDelay (capped ~60s in
# llm._parse_retry_delay), so worst case ≈ _MAX_QUOTA_RETRIES × 60s of patience.
_MAX_QUOTA_RETRIES = 2

_SYSTEM = (
    "You are DocuSense, a careful document-analysis assistant. Answer the user's "
    "question using ONLY the context passages provided below. Cite the pages you "
    "rely on inline using the form [Page N]. If the answer is not contained in the "
    "context, reply exactly: \"I could not find that in this document.\" Never use "
    "outside knowledge and never invent citations."
)


def _owned_ready_doc(db: Session, user: User, doc_id: str) -> Document:
    doc = db.get(Document, doc_id)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    if doc.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Document is not ready for chat (status: {doc.status}).")
    return doc


def _build_prompt(doc_name: str, chunks: list, history: list, query: str) -> str:
    context_blocks = []
    for c in chunks:
        context_blocks.append(f"[Page {c['page_num']}] {c['content']}")
    context = "\n\n".join(context_blocks) if context_blocks else "(no relevant passages found)"

    convo = ""
    if history:
        turns = []
        for m in history[-6:]:                      # keep the last few turns for continuity
            role = str(m.get("role", "user")).upper()
            turns.append(f"{role}: {m.get('content', '')}")
        convo = "PRIOR CONVERSATION:\n" + "\n".join(turns) + "\n\n"

    return (
        f"{_SYSTEM}\n\n"
        f"DOCUMENT: {doc_name}\n\n"
        f"CONTEXT PASSAGES:\n{context}\n\n"
        f"{convo}"
        f"QUESTION: {query}\n\n"
        f"ANSWER:"
    )


def _citations_for(chunks: list, answer: str) -> list:
    """Build citations from retrieved chunks, narrowed to pages the answer cites."""
    all_cites = rag.build_citations(chunks)
    referenced = {int(n) for n in _PAGE_REF.findall(answer or "")}
    if referenced:
        narrowed = [c for c in all_cites if c["page"] in referenced]
        if narrowed:
            return narrowed
    return all_cites


def _persist(db: Session, doc_id: str, user_id: int, role: str, content: str, citations=None):
    db.add(ChatMessage(
        doc_id=doc_id, user_id=user_id, role=role, content=content,
        citations_json=citations,
    ))
    db.commit()


@router.post("/stream")
def chat_stream(req: ChatRequest,
                db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    doc = _owned_ready_doc(db, user, req.doc_id)
    doc_name = doc.name
    user_id = user.id
    doc_id = doc.id

    # Retrieval + user-message persistence happen up front (fast, on the request session).
    chunks = rag.retrieve(db, doc_id, req.query)
    prompt = _build_prompt(doc_name, chunks, req.history or [], req.query)
    _persist(db, doc_id, user_id, "user", req.query)

    def event_stream():
        def sse(event, data):
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        answer_parts = []
        attempts = 0
        while True:
            try:
                for delta in llm.stream(prompt):
                    answer_parts.append(delta)
                    yield sse("token", {"text": delta})
                break                                # stream finished cleanly
            except llm.LLMError as e:
                # Auto-wait out the shared ~20 req/min free-tier limit and resend,
                # but ONLY if we were throttled before any token arrived (safe to
                # restart cleanly) and retries remain. A `notice` event flushes bytes
                # immediately — keeping the SSE connection alive through the wait and
                # letting the UI show a countdown. Mid-stream failures (partial answer)
                # surface as errors rather than risk a duplicated reply.
                retry_after = getattr(e, "retry_after", None)
                if retry_after and not answer_parts and attempts < _MAX_QUOTA_RETRIES:
                    attempts += 1
                    wait = max(1.0, min(float(retry_after), 60.0))
                    log.info("Chat throttled; auto-retrying in %.0fs (attempt %d/%d)",
                             wait, attempts, _MAX_QUOTA_RETRIES)
                    yield sse("notice", {
                        "detail": f"Free-tier limit reached — auto-retrying in {int(round(wait))}s…",
                        "retry_after": wait,
                        "attempt": attempts,
                    })
                    time.sleep(wait)
                    continue
                yield sse("error", {"detail": str(e)})
                return
            except Exception as e:                   # pragma: no cover - defensive
                log.exception("Streaming error")
                yield sse("error", {"detail": f"Unexpected error: {e}"})
                return

        answer = "".join(answer_parts).strip()
        citations = _citations_for(chunks, answer)
        # Persist assistant turn on a fresh session (request session may be closing).
        try:
            with SessionLocal() as s:
                _persist(s, doc_id, user_id, "assistant", answer, citations)
        except Exception:
            log.exception("Failed to persist assistant message")
        yield sse("done", {"content": answer, "citations": citations})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("", response_model=ChatMessageOut)
def chat(req: ChatRequest,
         db: Session = Depends(get_db),
         user: User = Depends(get_current_user)):
    doc = _owned_ready_doc(db, user, req.doc_id)
    chunks = rag.retrieve(db, doc.id, req.query)
    prompt = _build_prompt(doc.name, chunks, req.history or [], req.query)

    _persist(db, doc.id, user.id, "user", req.query)
    try:
        answer = llm.generate(prompt)
    except llm.LLMError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))

    citations = _citations_for(chunks, answer)
    _persist(db, doc.id, user.id, "assistant", answer, citations)
    return ChatMessageOut(
        role="assistant", content=answer,
        citations=[Citation(**c) for c in citations],
    )


@router.get("/{doc_id}/history", response_model=list[ChatMessageOut])
def chat_history(doc_id: str,
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    doc = db.get(Document, doc_id)
    if not doc or doc.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.doc_id == doc_id, ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    out = []
    for m in msgs:
        cites = [Citation(**c) for c in (m.citations_json or [])]
        out.append(ChatMessageOut(role=m.role, content=m.content, citations=cites))
    return out