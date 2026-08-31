"""Pydantic request/response schemas (API contract)."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field


# ── Auth ─────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=256)


class UserOut(BaseModel):
    id: int
    email: EmailStr
    name: str
    role: str

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=256)

# ── Documents ────────────────────────────────────────────────────────
class DocumentOut(BaseModel):
    id: str
    name: str
    mime_type: Optional[str] = None
    page_count: int = 0
    status: str
    error: Optional[str] = None
    uploaded_at: datetime

    class Config:
        from_attributes = True


class PresignedUrlOut(BaseModel):
    url: str
    expires_in: int


# ── Chat ─────────────────────────────────────────────────────────────
class Citation(BaseModel):
    page: int
    boxes: List[List[float]] = []          # each box = [x0,y0,x1,y1] normalized 0..1


class ChatRequest(BaseModel):
    doc_id: str
    query: str = Field(min_length=1)
    history: Optional[List[dict]] = []


class ChatMessageOut(BaseModel):
    role: str
    content: str
    citations: List[Citation] = []


# ── Summary (FR-04) ──────────────────────────────────────────────────
class RiskItem(BaseModel):
    description: str
    severity: str = "medium"               # low|medium|high


class DeadlineItem(BaseModel):
    description: str
    date: Optional[str] = None


class ActionItem(BaseModel):
    description: str
    priority: str = "medium"


class SummaryOut(BaseModel):
    executive: str = ""
    key_points: List[str] = []
    risks: List[RiskItem] = []
    deadlines: List[DeadlineItem] = []
    actions: List[ActionItem] = []


# ── Annotations + selection actions (FR-05) ──────────────────────────
class AnnotationCreate(BaseModel):
    doc_id: str
    page_num: int
    rect_coords: Optional[list] = None     # normalized rect(s)
    selected_text: Optional[str] = None
    ai_notes: Optional[str] = None
    tags: Optional[List[str]] = None
    action: str = "manual"                 # explain|summarize|risks|custom|manual


class AnnotationUpdate(BaseModel):
    ai_notes: Optional[str] = None
    tags: Optional[List[str]] = None
    selected_text: Optional[str] = None


class AnnotationOut(BaseModel):
    id: int
    doc_id: str
    page_num: int
    rect_coords: Optional[list] = None
    selected_text: Optional[str] = None
    ai_notes: Optional[str] = None
    tags: Optional[List[str]] = None
    action: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class SelectionActionRequest(BaseModel):
    doc_id: Optional[str] = None
    text: str = Field(min_length=1, max_length=6000)
    action: str = "explain"                # explain|summarize|risks|custom
    question: Optional[str] = None         # used when action == "custom"
    page_num: Optional[int] = None
    rect_coords: Optional[list] = None
    save: bool = False                     # persist as an annotation?


class SelectionActionResponse(BaseModel):
    action: str
    result: str
    annotation_id: Optional[int] = None