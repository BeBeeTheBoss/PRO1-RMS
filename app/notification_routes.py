
from flask import (
    Blueprint,
    jsonify,
    url_for,
    abort,
    session,
)
from werkzeug.exceptions import HTTPException

from app.db import get_connection

# Import the existing login decorator
from app.routes import login_required

# Import the existing helper
from app.candidate_routes import rows_as_dicts


notification_bp = Blueprint(
    "notification",
    __name__,
)


# =========================================================
# PERMISSION FILTER
# =========================================================

def notification_permission_sql():

    role = session.get("role")
    user_id = session.get("user_id")

    if role == "super_admin":
        return "1 = 1", []

    if role == "department_head":

        return """
            EXISTS (
                SELECT 1
                FROM public.candidate_interviews permission_ci

                INNER JOIN public.candidates permission_c
                    ON permission_c.id = permission_ci.candidate_id

                INNER JOIN public.user_department_permissions udp
                    ON udp.user_id = %s
                   AND udp.branch_id = permission_c.branch_id
                   AND udp.department_id = permission_c.department_id
                   AND udp.can_view = TRUE

                WHERE permission_ci.id = an.interview_id
            )
        """, [user_id]

    abort(403)


# =========================================================
# GENERATE INTERVIEW NOTIFICATIONS
# =========================================================

def sync_interview_notifications(cur):

    # -----------------------------------------------------
    # TODAY'S INTERVIEWS
    # -----------------------------------------------------

    cur.execute("""
        INSERT INTO public.admin_notifications (
            title,
            message,
            notification_type,
            interview_id
        )
        SELECT
            'Interview Today',

            CONCAT(
                c.name,
                ' has an interview scheduled today at ',
                TO_CHAR(ci.interview_time, 'HH12:MI AM'),
                ' (',
                REPLACE(ci.interview_stage, '_', ' '),
                ').'
            ),

            'today_interview',
            ci.id

        FROM public.candidate_interviews ci

        INNER JOIN public.candidates c
            ON c.id = ci.candidate_id

        WHERE ci.interview_date =
            (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Yangon')::date

          AND ci.status = 'scheduled'

        ON CONFLICT DO NOTHING
    """)

    # -----------------------------------------------------
    # DAILY OVERDUE NOTIFICATIONS
    # -----------------------------------------------------

    cur.execute("""
        INSERT INTO public.admin_notifications (
            title,
            message,
            notification_type,
            interview_id
        )
        SELECT
            'Overdue Interview',

            CONCAT(
                c.name,
                ' - interview date has passed, but the status is still Scheduled (',
                TO_CHAR(ci.interview_date, 'DD Mon YYYY'),
                ').'
            ),

            'overdue_interview',
            ci.id

        FROM public.candidate_interviews ci

        INNER JOIN public.candidates c
            ON c.id = ci.candidate_id

        WHERE ci.interview_date <
            (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Yangon')::date

          AND ci.status = 'scheduled'

        ON CONFLICT DO NOTHING
    """)

# =========================================================
# GET NOTIFICATIONS
# =========================================================

@notification_bp.route(
    "/api/notifications",
    methods=["GET"],
)
@login_required
def notifications_api():

    conn = get_connection()

    try:
        cur = conn.cursor()

        # Generate and store notifications
        sync_interview_notifications(cur)

        conn.commit()

        permission_sql, permission_params = (
            notification_permission_sql()
        )

        # -------------------------------------------------
        # UNREAD COUNT
        # -------------------------------------------------

        cur.execute(
            f"""
                SELECT COUNT(*)
                FROM public.admin_notifications an

                INNER JOIN public.candidate_interviews ci
                    ON ci.id = an.interview_id

                INNER JOIN public.candidates c
                    ON c.id = ci.candidate_id

                WHERE an.is_read = FALSE
                  AND {permission_sql}
            """,
            tuple(permission_params),
        )

        unread_count = cur.fetchone()[0]

        # -------------------------------------------------
        # NOTIFICATION LIST
        # -------------------------------------------------

        cur.execute(
            f"""
                SELECT
                    an.id,
                    an.title,
                    an.message,
                    an.notification_type,
                    an.interview_id,
                    an.is_read,

                    TO_CHAR(
                        an.created_at AT TIME ZONE 'Asia/Yangon',
                        'DD Mon YYYY, HH12:MI AM'
                    ) AS created_label,

                    TO_CHAR(
                        (an.created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Yangon',
                        'YYYY-MM-DD'
                    ) AS created_date,

                    c.name AS candidate_name,
                    ci.interview_stage

                FROM public.admin_notifications an

                INNER JOIN public.candidate_interviews ci
                    ON ci.id = an.interview_id

                INNER JOIN public.candidates c
                    ON c.id = ci.candidate_id

                WHERE {permission_sql}

                ORDER BY
                    an.is_read ASC,
                    an.created_at DESC

                LIMIT 20
            """,
            tuple(permission_params),
        )

        notifications = rows_as_dicts(cur)

        for item in notifications:

            item["url"] = url_for(
                "candidate.interview_schedules",
                search=item["candidate_name"],
            )

        cur.close()

        return jsonify({
            "success": True,
            "unread_count": unread_count,
            "notifications": notifications,
        })

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =========================================================
# MARK ONE NOTIFICATION AS READ
# =========================================================

@notification_bp.route(
    "/api/notifications/<int:notification_id>/read",
    methods=["POST"],
)
@login_required
def mark_notification_read(notification_id):

    conn = get_connection()

    try:
        cur = conn.cursor()

        permission_sql, permission_params = (
            notification_permission_sql()
        )

        cur.execute(
            f"""
                UPDATE public.admin_notifications an

                SET is_read = TRUE

                WHERE an.id = %s
                  AND an.is_read = FALSE
                  AND {permission_sql}

                RETURNING an.id
            """,
            tuple(
                [notification_id] + permission_params
            ),
        )

        updated = cur.fetchone()

        if updated is None:

            conn.rollback()
            abort(404)

        conn.commit()
        cur.close()

        return jsonify({
            "success": True,
            "message": "Notification marked as read.",
        })

    except HTTPException:
        conn.rollback()
        raise

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =========================================================
# MARK ALL NOTIFICATIONS AS READ
# =========================================================

@notification_bp.route(
    "/api/notifications/read-all",
    methods=["POST"],
)
@login_required
def mark_all_notifications_read():

    conn = get_connection()

    try:
        cur = conn.cursor()

        permission_sql, permission_params = (
            notification_permission_sql()
        )

        cur.execute(
            f"""
                UPDATE public.admin_notifications an

                SET is_read = TRUE

                WHERE an.is_read = FALSE
                  AND {permission_sql}
            """,
            tuple(permission_params),
        )

        updated_count = cur.rowcount

        conn.commit()
        cur.close()

        return jsonify({
            "success": True,
            "updated_count": updated_count,
            "message": "All notifications marked as read.",
        })

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()
