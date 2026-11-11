
from app import create_app

from app.interview_reminders import (
    send_daily_interview_reminder
)


app = create_app()

with app.app_context():

    send_daily_interview_reminder()

print("Daily Interview Reminder Test Completed.")
