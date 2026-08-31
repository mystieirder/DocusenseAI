"""Email utility — sends transactional emails via Resend API (HTTPS, works on Render free tier)."""
import logging
import httpx
from .config import settings

log = logging.getLogger("docusense.email")


def send_verification_email(to_email: str, name: str, token: str) -> None:
    verify_url = f"{settings.FRONTEND_URL}/api/auth/verify-email?token={token}"

    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#1a1208;font-family:Georgia,serif;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td align="center" style="padding:40px 20px;">
            <table width="480" cellpadding="0" cellspacing="0"
                   style="background:#2a1f0e;border-radius:12px;overflow:hidden;">
              <tr>
                <td style="padding:32px 40px 24px;border-bottom:1px solid #3d2e14;">
                  <span style="font-size:22px;font-weight:bold;color:#c9933a;">DS</span>
                  <span style="font-size:18px;color:#e8d5b0;margin-left:10px;">DocuSense AI</span>
                </td>
              </tr>
              <tr>
                <td style="padding:32px 40px;">
                  <h2 style="margin:0 0 16px;color:#e8d5b0;font-size:20px;">Welcome, {name}!</h2>
                  <p style="margin:0 0 24px;color:#b0a080;font-size:15px;line-height:1.6;">
                    Please verify your email address to activate your DocuSense AI account.
                  </p>
                  <table cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="border-radius:8px;background:#c9933a;">
                        <a href="{verify_url}"
                           style="display:inline-block;padding:14px 32px;
                                  color:#1a1208;font-size:15px;font-weight:bold;
                                  text-decoration:none;border-radius:8px;">
                          Verify Email Address
                        </a>
                      </td>
                    </tr>
                  </table>
                  <p style="margin:24px 0 0;color:#7a6a4a;font-size:13px;">
                    This link expires in <strong style="color:#b0a080;">24 hours</strong>.
                    If you didn't create an account, ignore this email.
                  </p>
                </td>
              </tr>
              <tr>
                <td style="padding:20px 40px;border-top:1px solid #3d2e14;">
                  <p style="margin:0;color:#5a4a2a;font-size:12px;">
                    Or copy this link into your browser:<br/>
                    <span style="color:#8a7a5a;word-break:break-all;">{verify_url}</span>
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """

    plain = (
        f"Hi {name},\n\n"
        f"Verify your DocuSense AI account:\n{verify_url}\n\n"
        f"This link expires in 24 hours.\n\n"
        f"If you didn't sign up, ignore this email."
    )

    log.info("Sending verification email to %s via Resend", to_email)
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": f"DocuSense AI <{settings.SMTP_FROM}>",
                "to": [to_email],
                "subject": "Verify your DocuSense AI account",
                "html": html,
                "text": plain,
            },
            timeout=15,
        )
        resp.raise_for_status()
        log.info("Verification email sent to %s (id: %s)", to_email, resp.json().get("id"))
    except httpx.HTTPStatusError as e:
        log.error("Resend API error for %s: %s — %s", to_email, e.response.status_code, e.response.text)
        raise
    except Exception as e:
        log.error("Unexpected error sending email to %s: %s", to_email, e)
        raise