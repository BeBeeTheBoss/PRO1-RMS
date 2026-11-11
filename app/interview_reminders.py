
import os
import logging

from datetime import datetime, time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db import get_connection
from app.interview_mailer import send_interview_reminder_email


logger = logging.getLogger(__name__)

TIMEZONE = ZoneInfo(
    os.getenv("TIMEZONE", "Asia/Yangon")
)

scheduler = None


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def rows_as_dicts(cur):

    columns = [
        column[0] for column in cur.description
    ]

    return [
        dict(zip(columns, row))
        for row in cur.fetchall()
    ]


def format_interview_time(value):

    if value is None:
        return "Not specified"

    if isinstance(value, time):
        return value.strftime("%I:%M %p")

    return str(value)


def create_notification(
    cur,
    title,
    message,
    notification_type,
    interview_id=None
):

    cur.execute(
        """
        INSERT INTO public.admin_notifications
        (
            title,
            message,
            notification_type,
            interview_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (
            title,
            message,
            notification_type,
            interview_id
        )
    )


def get_interview_message(interview):

    return (
        f"Candidate: {interview['candidate_name']}\n"
        f"Position: {interview['position'] or 'Not specified'}\n"
        f"Interview Stage: {interview['interview_stage']}\n"
        f"Interview Date: {interview['interview_date']}\n"
        f"Interview Time: "
        f"{format_interview_time(interview['interview_time'])}\n"
        f"Interviewer: "
        f"{interview['interviewer'] or 'Not specified'}"
    )


# =========================================================
# DAILY INTERVIEW REMINDER
# Every day at 9:00 AM Myanmar Time
# =========================================================

def send_daily_interview_reminder():

    today = datetime.now(TIMEZONE).date()

    conn = None

    try:

        conn = get_connection()
        cur = conn.cursor()

        # Check whether today's reminder was already sent.
        cur.execute(
            """
            SELECT id
            FROM public.interview_daily_reminder_logs
            WHERE reminder_date = %s
            """,
            (today,)
        )

        if cur.fetchone():

            logger.info(
                "Daily reminder already sent for %s",
                today
            )

            return

        # Get all scheduled interviews for today.
        cur.execute(
            """
            SELECT
                ci.id,
                ci.candidate_id,
                ci.interview_stage,
                ci.interview_date,
                ci.interview_time,
                ci.interviewer,
                c.name AS candidate_name,
                c.position

            FROM public.candidate_interviews ci

            INNER JOIN public.candidates c
                ON c.id = ci.candidate_id

            WHERE ci.interview_date = %s
              AND ci.status = 'scheduled'

            ORDER BY
                ci.interview_time ASC,
                ci.id ASC
            """,
            (today,)
        )

        interviews = rows_as_dicts(cur)

        # Do not send an email if there are no interviews.
        if not interviews:

            logger.info(
                "No interviews scheduled today: %s",
                today
            )

            return

        email_lines = [
            "DAILY INTERVIEW SCHEDULE",
            "=" * 45,
            f"Date: {today}",
            f"Total Interviews: {len(interviews)}",
            ""
        ]

        for index, interview in enumerate(
            interviews,
            start=1
        ):

            email_lines.extend([
                f"INTERVIEW {index}",
                "-" * 45,
                get_interview_message(interview),
                ""
            ])

        email_body = "\n".join(email_lines)

        # Send one email containing all interviews.
        sent = send_interview_reminder_email(
            subject=f"Daily Interview Reminder - {today}",
            text_body=email_body
        )

        if not sent:

            logger.error(
                "Daily reminder email failed for %s",
                today
            )

            return

        # Record successful email delivery.
        cur.execute(
            """
            INSERT INTO public.interview_daily_reminder_logs
            (
                reminder_date,
                recipient_email,
                sent_at
            )
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (reminder_date) DO NOTHING
            """,
            (
                today,
                os.getenv("ADMIN_NOTIFICATION_EMAIL")
            )
        )

        # Create one notification for today's schedule.
        create_notification(
            cur,
            "Today's Interview Reminder",
            (
                f"{len(interviews)} interview(s) "
                f"are scheduled for {today}."
            ),
            "daily_interview_reminder"
        )

        conn.commit()

        logger.info(
            "Daily interview reminder completed: %s",
            today
        )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "Daily interview reminder job failed."
        )

    finally:

        if conn:
            conn.close()


# =========================================================
# OVERDUE INTERVIEW REMINDER
#
# First email:
# Next day at 9:00 AM after interview date.
#
# Repeat:
# Every 7 calendar days at 9:00 AM.
# =========================================================

def send_overdue_interview_reminders():

    now = datetime.now(TIMEZONE)

    today = now.date()

    conn = None

    try:

        conn = get_connection()
        cur = conn.cursor()

        # Get overdue interviews that are still scheduled.
        cur.execute(
            """
            SELECT
                ci.id,
                ci.candidate_id,
                ci.interview_stage,
                ci.interview_date,
                ci.interview_time,
                ci.interviewer,
                ci.overdue_reminder_sent_at,
                c.name AS candidate_name,
                c.position

            FROM public.candidate_interviews ci

            INNER JOIN public.candidates c
                ON c.id = ci.candidate_id

            WHERE ci.interview_date < %s

              AND ci.status = 'scheduled'

              AND (
                    ci.overdue_reminder_sent_at IS NULL

                    OR ci.overdue_reminder_sent_at
                       <= %s - INTERVAL '7 days'
              )

            ORDER BY
                ci.interview_date ASC,
                ci.id ASC

            FOR UPDATE OF ci
            """,
            (
                today,
                now
            )
        )

        interviews = rows_as_dicts(cur)

        for interview in interviews:

            interview_id = interview["id"]

            details = get_interview_message(
                interview
            )

            # Send overdue email.
            sent = send_interview_reminder_email(
                subject=(
                    "Overdue Interview Reminder - "
                    f"{interview['candidate_name']}"
                ),

                text_body=(
                    "OVERDUE INTERVIEW REMINDER\n"
                    + "=" * 45
                    + "\n\n"
                    + "The following interview is overdue "
                    + "and its status is still scheduled.\n\n"
                    + details
                    + "\n\nPlease update the interview status."
                )
            )

            if not sent:

                logger.error(
                    "Overdue email failed. Interview ID: %s",
                    interview_id
                )

                continue

            # Update only after successful email delivery.
            cur.execute(
                """
                UPDATE public.candidate_interviews

                SET overdue_reminder_sent_at =
                        %s,

                    updated_at = CURRENT_TIMESTAMP

                WHERE id = %s
                  AND status = 'scheduled'
                """,
                (
                    now,
                    interview_id
                )
            )

            if cur.rowcount == 1:

                create_notification(
                    cur,

                    "Overdue Interview",

                    (
                        f"{interview['candidate_name']} - "
                        f"{interview['interview_stage']} interview "
                        f"scheduled for {interview['interview_date']} "
                        "is overdue. Please update the interview status."
                    ),

                    "overdue_interview",

                    interview_id
                )

            conn.commit()

            logger.info(
                "Overdue reminder sent. Interview ID: %s",
                interview_id
            )

    except Exception:

        if conn:
            conn.rollback()

        logger.exception(
            "Overdue interview reminder job failed."
        )

    finally:

        if conn:
            conn.close()


# =========================================================
# APSCHEDULER
# =========================================================

def start_interview_scheduler(app):

    global scheduler

    if scheduler is not None and scheduler.running:

        logger.info(
            "Interview scheduler is already running."
        )

        return

    timezone = TIMEZONE

    scheduler = BackgroundScheduler(
        timezone=timezone
    )

    # -----------------------------------------------------
    # DAILY INTERVIEW REMINDER - 9:00 AM
    # -----------------------------------------------------

    scheduler.add_job(
        func=lambda: run_daily_job(app),

        trigger=CronTrigger(
            hour=9,
            minute=0,
            timezone=timezone
        ),

        id="daily_interview_reminder",

        replace_existing=True,

        max_instances=1,

        coalesce=True
    )

    # -----------------------------------------------------
    # OVERDUE INTERVIEW REMINDER - 9:00 AM
    # -----------------------------------------------------

    scheduler.add_job(
        func=lambda: run_overdue_job(app),

        trigger=CronTrigger(
            hour=9,
            minute=0,
            timezone=timezone
        ),

        id="overdue_interview_reminder",

        replace_existing=True,

        max_instances=1,

        coalesce=True
    )

    scheduler.start()

    logger.info(
        "Interview Reminder Scheduler started."
    )

    logger.info(
        "Daily reminder: 09:00 AM Asia/Yangon"
    )

    logger.info(
        "Overdue reminder: 09:00 AM Asia/Yangon"
    )


# =========================================================
# SCHEDULER JOB WRAPPERS
# =========================================================

def run_daily_job(app):

    with app.app_context():

        send_daily_interview_reminder()


def run_overdue_job(app):

    with app.app_context():

        send_overdue_interview_reminders()
