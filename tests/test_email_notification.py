import asyncio
from email.message import EmailMessage

from app.services import email as email_service


REFUND_RESULTS: list[dict[str, object]] = [
    {
        "course_title": "Python <Basics>",
        "refund_status": "rejected",
        "refund_id": None,
        "refund_amount": "49.00",
        "reason": "The 7-day refund window has expired",
        "request_reason": "I no longer want this course",
    }
]


def test_refund_email_is_sent_with_html_and_disclaimer(monkeypatch) -> None:
    captured: list[EmailMessage] = []
    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "test-app-password")
    monkeypatch.setattr(
        email_service,
        "_send_smtp_message",
        lambda message: captured.append(message),
    )

    result = asyncio.run(
        email_service.send_refund_email(
            "student@example.com", REFUND_RESULTS
        )
    )

    assert result["sent"]
    assert len(captured) == 1
    message = captured[0]
    assert message["To"] == "student@example.com"
    assert "rejected" in message.get_body(preferencelist=("plain",)).get_content()
    html_part = message.get_body(preferencelist=("html",))
    assert html_part is not None
    assert "Python &lt;Basics&gt;" in html_part.get_content()
    assert "The 7-day refund window has expired" in html_part.get_content()
    assert "I no longer want this course" in html_part.get_content()
    assert "AI-generated response" in html_part.get_content()
    assert "s.avinashpandey@gmail.com" in html_part.get_content()


def test_refund_email_failure_is_explicit(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "test-app-password")

    def fail_sending(_message: EmailMessage) -> None:
        raise OSError("SMTP server unavailable")

    monkeypatch.setattr(email_service, "_send_smtp_message", fail_sending)
    result = asyncio.run(
        email_service.send_refund_email(
            "student@example.com", REFUND_RESULTS
        )
    )

    assert not result["sent"]
    assert result["detail"] == "SMTP server unavailable"


def test_refund_email_reports_missing_credentials(monkeypatch) -> None:
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("IMAP_USER", raising=False)
    monkeypatch.delenv("IMAP_PASSWORD", raising=False)

    result = asyncio.run(
        email_service.send_refund_email(
            "student@example.com", REFUND_RESULTS
        )
    )

    assert not result["sent"]
    assert "SMTP credentials are not configured" in result["detail"]
