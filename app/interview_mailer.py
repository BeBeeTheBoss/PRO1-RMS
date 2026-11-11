
import os
import smtplib
import logging

from email.message import EmailMessage
from dotenv import load_dotenv


load_dotenv()

logger = logging.getLogger(__name__)


def send_interview_reminder_email(
    subject,
    text_body,
    html_body=None
):
    smtp_host = os.getenv("MAIL_SMTP_HOST")
    smtp_port = int(os.getenv("MAIL_SMTP_PORT", "587"))

    smtp_username = os.getenv("MAIL_SMTP_USERNAME")
    smtp_password = os.getenv("MAIL_SMTP_PASSWORD")
    smtp_sender = os.getenv("MAIL_SMTP_SENDER")

    recipient = os.getenv("ADMIN_NOTIFICATION_EMAIL")

    use_tls = (
        os.getenv("MAIL_SMTP_USE_TLS", "True").lower()
        == "true"
    )

    required_settings = {
        "MAIL_SMTP_HOST": smtp_host,
        "MAIL_SMTP_USERNAME": smtp_username,
        "MAIL_SMTP_PASSWORD": smtp_password,
        "MAIL_SMTP_SENDER": smtp_sender,
        "ADMIN_NOTIFICATION_EMAIL": recipient
    }

    missing = [
        key for key, value in required_settings.items()
        if not value
    ]

    if missing:
        logger.error(
            "Interview SMTP configuration missing: %s",
            ", ".join(missing)
        )
        return False

    message = EmailMessage()

    message["Subject"] = subject
    message["From"] = smtp_sender
    message["To"] = recipient

    message.set_content(text_body)

    if html_body:
        message.add_alternative(
            html_body,
            subtype="html"
        )

    try:
        with smtplib.SMTP(
            smtp_host,
            smtp_port,
            timeout=30
        ) as server:

            server.ehlo()

            if use_tls:
                server.starttls()
                server.ehlo()

            server.login(
                smtp_username,
                smtp_password
            )

            server.send_message(message)

        logger.info(
            "Interview reminder email sent successfully to %s",
            recipient
        )

        return True

    except Exception:
        logger.exception(
            "Interview reminder SMTP delivery failed."
        )
        return False
