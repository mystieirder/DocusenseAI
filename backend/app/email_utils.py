"""
Email sending utility using SMTP (Brevo / any SMTP provider).
"""
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from .config import settings

logger = logging.getLogger(__name__)


def _send(to: str, subject: str, html: str, text: str = "") -> bool:
    """Send an email. Returns True on success, False on failure (never raises)."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"DocuSense AI <{settings.SMTP_FROM}>"
        msg["To"]      = to
        if text:
            msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            srv.sendmail(settings.SMTP_FROM, to, msg.as_string())
        logger.info("Email sent to %s — %s", to, subject)
        return True
    except Exception as e:
        logger.error("Failed to send email to %s: %s", to, e)
        return False


def _base(title: str, body: str) -> str:
    """Wrap content in a clean branded HTML shell."""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  body{{margin:0;padding:0;background:#0e0e0e;font-family:'Segoe UI',Arial,sans-serif;color:#e8e0d0}}
  .wrap{{max-width:520px;margin:40px auto;background:#1a1a14;border:1px solid #2e2e22;border-radius:12px;overflow:hidden}}
  .hdr{{background:#2d4a1e;padding:28px 32px;text-align:center}}
  .hdr h1{{margin:0;font-size:22px;color:#a8c97a;letter-spacing:.5px}}
  .hdr p{{margin:4px 0 0;font-size:13px;color:#8aaa5a}}
  .body{{padding:32px}}
  .body p{{font-size:15px;line-height:1.7;color:#c8bfa8;margin:0 0 16px}}
  .btn{{display:inline-block;margin:8px 0 20px;padding:13px 28px;background:#4a7c2e;color:#fff!important;text-decoration:none;border-radius:8px;font-size:15px;font-weight:600}}
  .note{{font-size:12px;color:#6b6455;line-height:1.6;border-top:1px solid #2e2e22;padding-top:16px;margin-top:8px}}
  .footer{{padding:18px 32px;text-align:center;font-size:11px;color:#4a4438;border-top:1px solid #2e2e22}}
</style></head><body>
<div class="wrap">
  <div class="hdr"><h1>DocuSense AI</h1><p>document intelligence · grounded in the page</p></div>
  <div class="body">
    <h2 style="margin:0 0 16px;font-size:18px;color:#e8e0d0">{title}</h2>
    {body}
  </div>
  <div class="footer">© DocuSense AI · You received this because you signed up at our platform.</div>
</div>
</body></html>"""


def send_verification_email(to: str, name: str, token: str, base_url: str) -> bool:
    link = f"{base_url}/api/auth/verify-email?token={token}"
    html = _base("Verify your email address", f"""
        <p>Hi {name},</p>
        <p>Thanks for signing up! Click the button below to verify your email address and activate your account.</p>
        <a class="btn" href="{link}">Verify Email Address</a>
        <p class="note">This link expires in <strong>24 hours</strong>. If you didn't create an account, you can safely ignore this email.</p>
    """)
    text = f"Hi {name},\n\nVerify your DocuSense AI account:\n{link}\n\nExpires in 24 hours."
    return _send(to, "Verify your DocuSense AI account", html, text)


def send_welcome_email(to: str, name: str) -> bool:
    html = _base("Welcome to DocuSense AI 🎉", f"""
        <p>Hi {name},</p>
        <p>Your account is verified and ready to go. You can now upload documents and start asking questions.</p>
        <p><strong>What you can do:</strong></p>
        <ul style="color:#c8bfa8;font-size:15px;line-height:1.8;padding-left:20px">
          <li>Upload PDFs, DOCX, images, and text files</li>
          <li>Chat with your documents using AI</li>
          <li>Get instant summaries and key insights</li>
          <li>Highlight text for instant AI explanations</li>
        </ul>
        <p>Happy reading!</p>
    """)
    text = f"Hi {name},\n\nWelcome to DocuSense AI! Your account is active and ready to use."
    return _send(to, "Welcome to DocuSense AI!", html, text)


def send_login_confirmation_email(to: str, name: str) -> bool:
    from datetime import datetime
    now = datetime.utcnow().strftime("%d %b %Y at %H:%M UTC")
    html = _base("New sign-in to your account", f"""
        <p>Hi {name},</p>
        <p>We noticed a new sign-in to your DocuSense AI account on <strong>{now}</strong>.</p>
        <p>If this was you, no action is needed.</p>
        <p class="note">If you did not sign in, please reset your password immediately using the link on the login page.</p>
    """)
    text = f"Hi {name},\n\nNew sign-in to your DocuSense AI account on {now}.\nIf this wasn't you, reset your password immediately."
    return _send(to, "New sign-in to your DocuSense AI account", html, text)


def send_password_reset_email(to: str, name: str, token: str, base_url: str) -> bool:
    link = f"{base_url}/reset-password?token={token}"
    html = _base("Reset your password", f"""
        <p>Hi {name},</p>
        <p>We received a request to reset your DocuSense AI password. Click the button below to choose a new one.</p>
        <a class="btn" href="{link}">Reset Password</a>
        <p class="note">This link expires in <strong>1 hour</strong>. If you didn't request a password reset, you can safely ignore this email — your password will not change.</p>
    """)
    text = f"Hi {name},\n\nReset your DocuSense AI password:\n{link}\n\nExpires in 1 hour."
    return _send(to, "Reset your DocuSense AI password", html, text)