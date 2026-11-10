import asyncio
import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import TypedDict

from dotenv import load_dotenv

load_dotenv()


class RefundEmailResult(TypedDict):
    sent: bool
    recipient: str
    detail: str


def _render_email(
    recipient: str, results: list[dict[str, object]]
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Your course refund request update"
    message["From"] = os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_USER", "")
    message["To"] = recipient

    text_rows: list[str] = []
    html_rows: list[str] = []
    for result in results:
        title = str(result["course_title"])
        status = str(result["refund_status"])
        refund_id = result.get("refund_id")
        amount = str(result["refund_amount"])
        reason = str(result["reason"])
        request_reason = result.get("request_reason")
        text_rows.append(
            "\n".join(
                [
                    f"Course: {title}",
                    f"Status: {status}",
                    f"Refund ID: {refund_id or 'Not created'}",
                    f"Amount: {amount}",
                    f"Reason: {reason}",
                    f"Request reason: {request_reason or 'Not provided'}",
                ]
            )
        )
        html_rows.append(
            "<tr>"
            f'<td style="padding:12px;border-bottom:1px solid #e5e7eb">{html.escape(title)}</td>'
            f'<td style="padding:12px;border-bottom:1px solid #e5e7eb">{html.escape(status.title())}</td>'
            f'<td style="padding:12px;border-bottom:1px solid #e5e7eb">{html.escape(str(refund_id or "Not created"))}</td>'
            f'<td style="padding:12px;border-bottom:1px solid #e5e7eb">{html.escape(amount)}</td>'
            f'<td style="padding:12px;border-bottom:1px solid #e5e7eb">{html.escape(reason)}</td>'
            f'<td style="padding:12px;border-bottom:1px solid #e5e7eb">{html.escape(str(request_reason or "Not provided"))}</td>'
            "</tr>"
        )

    disclaimer = (
        "Disclaimer: This is an AI-generated response. For any concerns, "
        "please contact s.avinashpandey@gmail.com."
    )
    plain_body = (
        "Hello,\n\n"
        "Here is the latest update on your course refund request:\n\n"
        + "\n\n".join(text_rows)
        + f"\n\n{disclaimer}\n\nCourse Support"
    )
    html_body = (
        '<div style="margin:0;padding:32px 12px;background:#f3f4f6;'
        'font-family:Arial,Helvetica,sans-serif;color:#1f2937">'
        '<div style="max-width:760px;margin:0 auto;background:#ffffff;'
        'border:1px solid #e5e7eb;border-radius:12px;overflow:hidden">'
        '<div style="padding:24px 28px;background:#123047;color:#ffffff">'
        '<div style="font-size:13px;letter-spacing:1px;text-transform:uppercase;'
        'color:#a7d8d2">Course Support</div>'
        '<h1 style="margin:10px 0 0;font-size:24px">Refund request update</h1>'
        "</div>"
        '<div style="padding:26px 28px">'
        "<p>Hello,</p>"
        "<p>Here is the latest update on your course refund request.</p>"
        '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;'
        'font-size:14px;text-align:left">'
        '<thead><tr style="background:#f8fafc">'
        '<th style="padding:12px">Course</th><th style="padding:12px">Status</th>'
        '<th style="padding:12px">Refund ID</th><th style="padding:12px">Amount</th>'
        '<th style="padding:12px">Decision reason</th>'
        '<th style="padding:12px">Request reason</th></tr></thead><tbody>'
        + "".join(html_rows)
        + "</tbody></table></div>"
        '<div style="margin-top:24px;padding:16px;background:#f8fafc;'
        'border-left:4px solid #2b7a78;font-size:13px;line-height:1.6">'
        f"{html.escape(disclaimer)}"
        "</div>"
        '<p style="margin-top:22px">Regards,<br><strong>Course Support</strong></p>'
        "</div></div></div>"
    )
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")
    return message


def _send_smtp_message(message: EmailMessage) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USER") or os.getenv("IMAP_USER", "")
    password = os.getenv("SMTP_PASSWORD") or os.getenv("IMAP_PASSWORD", "")
    if not username or not password or "replace_with" in password.lower():
        raise RuntimeError(
            "SMTP credentials are not configured. Set SMTP_USER and "
            "SMTP_PASSWORD (or IMAP_USER and IMAP_PASSWORD) in .env."
        )

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(username, password)
        smtp.send_message(message)


async def send_refund_email(
    recipient: str, results: list[dict[str, object]]
) -> RefundEmailResult:
    """Send one formatted email with the outcome of a refund request."""
    max_chars = int(os.getenv("MAX_CHARS_PER_EMAIL", "2000"))
    if max_chars < 1:
        raise ValueError("MAX_CHARS_PER_EMAIL must be positive")
    if not results:
        return {
            "sent": False,
            "recipient": recipient,
            "detail": "No refund results to include in the notification.",
        }

    bounded_results = [
        {
            **item,
            "reason": str(item["reason"])[:max_chars],
        }
        for item in results
    ]
    message = _render_email(recipient, bounded_results)
    try:
        await asyncio.to_thread(_send_smtp_message, message)
    except (OSError, RuntimeError, smtplib.SMTPException, ValueError) as exc:
        return {
            "sent": False,
            "recipient": recipient,
            "detail": str(exc),
        }
    return {
        "sent": True,
        "recipient": recipient,
        "detail": "Refund update email sent.",
    }
