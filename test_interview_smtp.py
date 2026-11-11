
from app.interview_mailer import send_interview_reminder_email


success = send_interview_reminder_email(
    subject="Interview SMTP Test",
    text_body="""
Hello,

This is a test email from the Recruitment Candidate Tracking System.

Interview reminder SMTP configuration is working.

Thank you.
"""
)

if success:
    print("Email sent successfully!")
else:
    print("Email sending failed. Please check the terminal log.")
