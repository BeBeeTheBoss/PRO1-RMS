
from app import create_app

from app.interview_reminders import (
    send_overdue_interview_reminders
)


app = create_app()

with app.app_context():

    send_overdue_interview_reminders()

print("Overdue Interview Reminder Test Completed.")
