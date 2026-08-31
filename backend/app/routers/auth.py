"""Auth routes — register, login, verify email, forgot/reset password, me, refresh."""
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import (ForgotPasswordRequest, RegisterRequest, ResetPasswordRequest,
                       Token, UserOut, RefreshRequest)
from ..security import (hash_password, verify_password, validate_password_strength,
                        create_access_token, create_refresh_token, decode_token)
from ..email_utils import (send_verification_email, send_welcome_email,
                           send_login_confirmation_email, send_password_reset_email)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ── helpers ───────────────────────────────────────────────────────────────────

def _role_for(email: str) -> str:
    return "admin" if email.lower().strip() in settings.admin_email_set else "user"


def _issue(user: User) -> Token:
    return Token(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
        user=UserOut.model_validate(user),
    )


def _base_url(request: Request) -> str:
    """Best-effort public base URL for links in emails."""
    fwd = request.headers.get("x-forwarded-proto")
    scheme = fwd if fwd else request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.hostname
    return f"{scheme}://{host}"


# ── register ──────────────────────────────────────────────────────────────────

@router.post("/register", response_model=UserOut, status_code=201)
def register(req: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    try:
        validate_password_strength(req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))

    email = req.email.lower().strip()

    # NOTE: email verification is currently disabled — SMTP isn't configured on the
    # deployed backend (SMTP_USER/PASSWORD/FROM aren't set in render.yaml), so
    # verification emails were silently failing to send and every new account was
    # permanently stuck unverified, unable to log in. Accounts are auto-verified for
    # now. To re-enable verification: set is_verified=False below, restore the
    # verification_token/expiry fields, call send_verification_email() again, and
    # add real SMTP_HOST/PORT/USER/PASSWORD/FROM values as env vars in Render.
    user = User(
        email=email,
        name=req.name.strip(),
        password_hash=hash_password(req.password),
        role=_role_for(email),
        is_verified=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "An account with that email already exists")
    db.refresh(user)

    return user


# ── verify email ──────────────────────────────────────────────────────────────

@router.get("/verify-email", response_class=HTMLResponse)
def verify_email(token: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.verification_token == token).first()

    def _page(title: str, msg: str, color: str) -> str:
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width,initial-scale=1"/>
        <title>{title}</title>
        <style>body{{margin:0;background:#0e0e0e;display:flex;align-items:center;justify-content:center;min-height:100vh;font-family:'Segoe UI',sans-serif}}
        .box{{background:#1a1a14;border:1px solid #2e2e22;border-radius:12px;padding:40px 48px;text-align:center;max-width:420px}}
        h2{{color:{color};margin:0 0 12px}}p{{color:#c8bfa8;line-height:1.7}}
        a{{display:inline-block;margin-top:20px;padding:11px 24px;background:#4a7c2e;color:#fff;text-decoration:none;border-radius:8px;font-size:14px}}</style>
        </head><body><div class="box"><h2>{title}</h2><p>{msg}</p><a href="/">Go to DocuSense AI</a></div></body></html>"""

    if not user:
        return HTMLResponse(_page("Invalid Link", "This verification link is invalid or has already been used.", "#e05555"), status_code=400)
    if user.verification_token_expiry and user.verification_token_expiry < datetime.utcnow():
        return HTMLResponse(_page("Link Expired", "This verification link has expired. Please register again or contact support.", "#e0a055"), status_code=400)
    if user.is_verified:
        return HTMLResponse(_page("Already Verified ✓", "Your email is already verified. You can sign in.", "#a8c97a"))

    user.is_verified = True
    user.verification_token = None
    user.verification_token_expiry = None
    db.commit()

    # Send welcome email now that they're verified
    send_welcome_email(user.email, user.name)

    return HTMLResponse(_page("Email Verified ✓", "Your email has been verified! You can now sign in to DocuSense AI.", "#a8c97a"))


# ── login ─────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form.username.lower().strip()).first()
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if not user.is_verified:
        raise HTTPException(403, "Please verify your email address before signing in. Check your inbox.")
    desired = _role_for(user.email)
    if user.role != desired:
        user.role = desired
        db.commit()
        db.refresh(user)
    # Send login confirmation (non-blocking)
    send_login_confirmation_email(user.email, user.name)
    return _issue(user)


# ── forgot password ───────────────────────────────────────────────────────────

@router.post("/forgot-password", status_code=200)
def forgot_password(req: ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower().strip()).first()
    # Always return 200 to avoid user enumeration
    if user and user.is_verified:
        token = secrets.token_urlsafe(32)
        user.reset_token = token
        user.reset_token_expiry = datetime.utcnow() + timedelta(hours=1)
        db.commit()
        send_password_reset_email(user.email, user.name, token, _base_url(request))
    return {"message": "If that email exists, a reset link has been sent."}


# ── reset password ────────────────────────────────────────────────────────────

@router.post("/reset-password", status_code=200)
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.reset_token == req.token).first()
    if not user:
        raise HTTPException(400, "Invalid or expired reset link.")
    if user.reset_token_expiry and user.reset_token_expiry < datetime.utcnow():
        raise HTTPException(400, "This reset link has expired. Please request a new one.")
    try:
        validate_password_strength(req.new_password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    user.password_hash = hash_password(req.new_password)
    user.reset_token = None
    user.reset_token_expiry = None
    db.commit()
    return {"message": "Password updated successfully. You can now sign in."}


# ── me / refresh ──────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/refresh", response_model=Token)
def refresh(req: RefreshRequest, db: Session = Depends(get_db)):
    try:
        payload = decode_token(req.refresh_token)
        if payload.get("type") != "refresh":
            raise HTTPException(401, "Invalid refresh token")
        user = db.get(User, int(payload.get("sub")))
    except (JWTError, TypeError, ValueError):
        raise HTTPException(401, "Invalid refresh token")
    if not user:
        raise HTTPException(401, "User not found")
    return _issue(user)