from functools import wraps
import math
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    session,
    abort,
    flash,
    jsonify,
    current_app,
)
from werkzeug.security import (
    check_password_hash,
    generate_password_hash,
)
from psycopg2.extras import RealDictCursor
from app.db import get_connection

main = Blueprint("main", __name__)


# -----------------------------------------
# ACCESS CONTROL
# -----------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("main.login"))
        return view(*args, **kwargs)

    return wrapped_view


def super_admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("main.login"))

        if session.get("role") != "super_admin":
            abort(403)

        return view(*args, **kwargs)

    return wrapped_view


# -----------------------------------------
# HOME
# -----------------------------------------

@main.route("/")
def home():
    if session.get("user_id"):
        return redirect(url_for("main.dashboard"))

    return redirect(url_for("main.login"))


# -----------------------------------------
# LOGIN
# -----------------------------------------

@main.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("main.dashboard"))

    if request.method == "GET":
        return render_template("login.html")

    employee_code = request.form.get("employee_code", "").strip()
    password = request.form.get("password", "")

    if not employee_code or not password:
        return render_template(
            "login.html",
            error="Please enter your employee code and password."
        )

    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    name,
                    employee_code,
                    password_hash,
                    role,
                    department_id,
                    is_active
                FROM users
                WHERE employee_code = %s
                """,
                (employee_code,)
            )
            user = cur.fetchone()

        if (
            user is None
            or not user[6]
            or not check_password_hash(user[3], password)
        ):
            return render_template(
                "login.html",
                error="Invalid employee code or password."
            )

        session.clear()
        session["user_id"] = user[0]
        session["user_name"] = user[1]
        session["employee_code"] = user[2]
        session["role"] = user[4]
        session["department_id"] = user[5]

        return redirect(url_for("main.dashboard"))

    finally:
        conn.close()


# State pages and the corresponding database statuses.
STAGE_CONFIG = {
    "candidate": {
        "title": "Candidates",
        "statuses": ["applied"],
    },
    "interview-request": {
        "title": "Interview Request",
        "statuses": ["interview_requested"],
    },
    "first-interview": {
        "title": "First Interview",
        "statuses": ["first_interview"],
    },
    "second-interview": {
        "title": "Second Interview",
        "statuses": ["second_interview"],
    },
    "offer": {
        "title": "Offer",
        "statuses": ["offer"],
    },
    "joined": {
        "title": "Joined",
        "statuses": ["joined"],
    },
    "resign": {
        "title": "Resign Candidates",
        "statuses": ["resign"],
    },
}

STATUS_DISPLAY = {
    "applied": ("Candidate", "status-applied"),
    "interview_requested": (
        "Interview Request", "status-screening"
    ),
    "candidate_reject": (
        "Candidate Rejected", "status-rejected"
    ),
    "first_interview": (
        "First Interview", "status-interview"
    ),
    "second_interview": (
        "Second Interview", "status-interview"
    ),
    "interview_request_reject": (
        "Interview Request Rejected", "status-rejected"
    ),
    "interview_request_cancel": (
        "Interview Request Cancelled", "status-rejected"
    ),
    "first_interview_reject": (
        "First Interview Rejected", "status-rejected"
    ),
    "first_interview_cancel": (
        "First Interview Cancelled", "status-rejected"
    ),
    "second_interview_reject": (
        "Second Interview Rejected", "status-rejected"
    ),
    "second_interview_cancel": (
        "Second Interview Cancelled", "status-rejected"
    ),
    "offer": ("Offer", "status-offered"),
    "offer_reject": ("Offer Rejected", "status-rejected"),
    "offer_cancel": ("Offer Cancelled", "status-rejected"),
    "joined": ("Joined", "status-hired"),
}

ALLOWED_STATUSES = set(STATUS_DISPLAY.keys())


def get_candidate_scope():
    """
    Super Admin:
        Access all departments.

    Department Head:
        Access only assigned departments.
    """

    role = session.get("role")
    user_id = session.get("user_id")

    if role == "super_admin":
        return "", ()

    if role == "department_head" and user_id:

        conn = get_connection()

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ud.department_id
                    FROM user_departments ud
                    JOIN departments d
                        ON d.id = ud.department_id
                    WHERE ud.user_id = %s
                      AND d.is_active = TRUE
                    ORDER BY ud.department_id
                    """,
                    (user_id,)
                )

                department_ids = [
                    row[0] for row in cur.fetchall()
                ]

                # Backward compatibility:
                # If no mapping exists, use old department_id
                if not department_ids:
                    cur.execute(
                        """
                        SELECT department_id
                        FROM users
                        WHERE id = %s
                          AND role = 'department_head'
                        """,
                        (user_id,)
                    )

                    old_department = cur.fetchone()

                    if old_department and old_department[0]:
                        department_ids = [old_department[0]]

                if department_ids:
                    return (
                        "WHERE c.department_id = ANY(%s)",
                        (department_ids,)
                    )

        finally:
            conn.close()

    # Invalid role or no assigned department
    return "WHERE 1 = 0", ()

def add_candidate_status_display(candidates):
    for candidate in candidates:
        label, css_class = STATUS_DISPLAY.get(
            candidate["status"],
            (
                candidate["status"].replace("_", " ").title(),
                "status-rejected",
            ),
        )

        candidate["status_label"] = label
        candidate["status_class"] = css_class

    return candidates


@main.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    conn = None

    role = session.get("role")
    department_id = session.get("department_id") or session.get("session_department_id")

    try:
        conn = get_connection()

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            # Department stats
            if role == "super_admin":
                cur.execute("""
                    SELECT COUNT(*) AS total_departments,
                           COUNT(*) FILTER (WHERE is_active = TRUE) AS active_departments
                    FROM departments
                """)
                department_stats = cur.fetchone()

                cur.execute("""
                    SELECT COUNT(*) AS total_heads,
                           COUNT(*) FILTER (WHERE is_active = TRUE) AS active_heads
                    FROM users
                    WHERE role = 'department_head'
                """)
                head_stats = cur.fetchone()

            elif role == "department_head":
                user_id = session.get("user_id")

                cur.execute(
                    """
                    SELECT department_id
                    FROM user_departments
                    WHERE user_id = %s
                    ORDER BY department_id
                    """,
                    (user_id,)
                )

                department_ids = [
                    row["department_id"] if isinstance(row, dict) else row[0]
                    for row in cur.fetchall()
                ]

                # Legacy fallback
                if not department_ids:
                    cur.execute(
                        """
                        SELECT department_id
                        FROM users
                        WHERE id = %s
                        AND role = 'department_head'
                        """,
                        (user_id,)
                    )
                    old_department = cur.fetchone()

                    if old_department:
                        old_id = (
                            old_department["department_id"]
                            if isinstance(old_department, dict)
                            else old_department[0]
                        )
                        if old_id:
                            department_ids = [old_id]

                if department_ids:
                    cur.execute(
                        """
                        SELECT COUNT(*) AS total_departments,
                            COUNT(*) FILTER (
                                WHERE is_active = TRUE
                            ) AS active_departments
                        FROM departments
                        WHERE id = ANY(%s)
                        """,
                        (department_ids,)
                    )
                    department_stats = cur.fetchone()

                    cur.execute(
                        """
                        SELECT COUNT(DISTINCT u.id) AS total_heads,
                            COUNT(DISTINCT u.id) FILTER (
                                WHERE u.is_active = TRUE
                            ) AS active_heads
                        FROM users u
                        WHERE u.role = 'department_head'
                        AND (
                            EXISTS (
                                SELECT 1
                                FROM user_departments ud
                                WHERE ud.user_id = u.id
                                    AND ud.department_id = ANY(%s)
                            )
                            OR (
                                u.department_id = ANY(%s)
                                AND NOT EXISTS (
                                    SELECT 1
                                    FROM user_departments ud2
                                    WHERE ud2.user_id = u.id
                                )
                            )
                        )
                        """,
                        (department_ids, department_ids)
                    )
                    head_stats = cur.fetchone()

                else:
                    department_stats = {
                        "total_departments": 0,
                        "active_departments": 0
                    }
                    head_stats = {
                        "total_heads": 0,
                        "active_heads": 0
                    }

            # Candidate scope
            candidate_scope, scope_params = get_candidate_scope()

            # Graph and summary counts
            cur.execute(f"""
                SELECT COUNT(*) AS total_candidates,
                    COUNT(*) FILTER (WHERE c.status = 'applied') AS candidate_count,
                    COUNT(*) FILTER (WHERE c.status = 'interview_requested') AS interview_request_count,
                    COUNT(*) FILTER (WHERE c.status = 'first_interview') AS first_interview_count,
                    COUNT(*) FILTER (WHERE c.status = 'second_interview') AS second_interview_count,
                    COUNT(*) FILTER (WHERE c.status = 'offer') AS offer_count,
                    COUNT(*) FILTER (WHERE c.status = 'joined') AS joined_count,
                    COUNT(*) FILTER (WHERE c.status = 'resign') AS resigned_count
                FROM candidates c
                {candidate_scope}
            """, scope_params)

            stats = cur.fetchone()

            # Dashboard table: latest 5 candidates
            cur.execute(f"""
                SELECT c.id,
                       c.name,
                       c.phone,
                       c.email,
                       c.status,
                       c.created_at,
                       d.name AS department_name
                FROM candidates c
                JOIN departments d ON d.id = c.department_id
                {candidate_scope}
                ORDER BY c.created_at DESC, c.id DESC
                LIMIT %s
            """, tuple(scope_params) + (5,))

            candidates = add_candidate_status_display(cur.fetchall())

        return render_template(
            "dashboard.html",
            total_departments=department_stats["total_departments"],
            active_departments=department_stats["active_departments"],
            total_department_heads=head_stats["total_heads"],
            active_department_heads=head_stats["active_heads"],
            total_candidates=stats["total_candidates"],
            candidate_count=stats["candidate_count"],
            interview_request_count=stats["interview_request_count"],
            first_interview_count=stats["first_interview_count"],
            second_interview_count=stats["second_interview_count"],
            offer_count=stats["offer_count"],
            joined_count=stats["joined_count"],
            resigned_count=stats["resigned_count"],
            candidates=candidates
        )

    except Exception:
        current_app.logger.exception("Dashboard loading failed")
        flash("Unable to load dashboard data. Please try again.", "error")
        return redirect(url_for("main.dashboard"))

    finally:
        if conn:
            conn.close()

@main.route("/dashboard/stage/<stage>", methods=["GET"])
@login_required
def dashboard_stage(stage):
    config = STAGE_CONFIG.get(stage)

    if config is None:
        return redirect(url_for("main.dashboard"))

    candidate_scope, scope_params = get_candidate_scope()
    conn = None

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    per_page = 10
    conn = get_connection()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            conditions = []

            if candidate_scope:
                conditions.append(
                    candidate_scope.replace("WHERE ", "", 1)
                )

            params = list(scope_params)
            stage_statuses = config["statuses"]

            conditions.append("c.status = ANY(%s)")
            params.append(stage_statuses)

            where_sql = "WHERE " + " AND ".join(conditions)

            cur.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM candidates c
                {where_sql}
                """,
                tuple(params),
            )
            total = cur.fetchone()["total"]

            total_pages = max(1, math.ceil(total / per_page))
            page = min(page, total_pages)
            offset = (page - 1) * per_page

            cur.execute(
                f"""
                SELECT
                    c.id,
                    c.name,
                    c.phone,
                    c.email,
                    c.status,
                    c.created_at,
                    d.name AS department_name
                FROM candidates c
                JOIN departments d
                    ON d.id = c.department_id
                {where_sql}
                ORDER BY c.created_at DESC, c.id DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params) + (per_page, offset),
            )
            candidates = add_candidate_status_display(cur.fetchall())

        return render_template(
            "candidate_stage.html",
            stage=stage,
            stage_title=config["title"],
            candidates=candidates,
            total=total,
            page=page,
            total_pages=total_pages,
        )

    except Exception:
        current_app.logger.exception("Candidate stage loading failed")
        flash("Unable to load candidate stage. Please try again.", "error")
        return redirect(url_for("main.dashboard"))

    finally:
        if conn:
            conn.close()

# -----------------------------------------
# LOGOUT
# -----------------------------------------

@main.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


# -----------------------------------------
# MANAGE DEPARTMENTS
# SUPER ADMIN ONLY
# -----------------------------------------

@main.route("/departments", methods=["GET", "POST"])
@super_admin_required
def manage_departments():

    conn = get_connection()

    try:
        if request.method == "POST":

            action = request.form.get("action")

            try:
                with conn.cursor() as cur:

                    if action == "add":

                        name = request.form.get("name", "").strip()
                        short_name = request.form.get(
                            "short_name", ""
                        ).strip()
                        branch_id = request.form.get(
                            "branch_id", ""
                        ).strip()

                        if not name or not short_name or not branch_id:
                            flash(
                                "Department name, short name နှင့် Branch ရွေးချယ်ပါ။",
                                "error"
                            )

                        else:
                            try:
                                branch_id = int(branch_id)
                            except (ValueError, TypeError):
                                branch_id = None

                            if not branch_id:
                                flash(
                                    "ရွေးချယ်ထားသော Branch မမှန်ကန်ပါ။",
                                    "error"
                                )
                            else:
                                cur.execute(
                                    """
                                    SELECT id
                                    FROM public.branches
                                    WHERE id = %s
                                      AND is_active = TRUE
                                    """,
                                    (branch_id,)
                                )

                                valid_branch = cur.fetchone()

                                if not valid_branch:
                                    flash(
                                        "ရွေးချယ်ထားသော Branch ကို အသုံးပြု၍မရပါ။",
                                        "error"
                                    )
                                else:
                                    # Check duplicate within the same branch
                                    cur.execute(
                                        """
                                        SELECT id
                                        FROM public.departments
                                        WHERE branch_id = %s
                                          AND (
                                              LOWER(TRIM(name)) = LOWER(TRIM(%s))
                                              OR LOWER(TRIM(short_name)) = LOWER(TRIM(%s))
                                          )
                                        """,
                                        (branch_id, name, short_name)
                                    )

                                    duplicate = cur.fetchone()

                                    if duplicate:
                                        flash(
                                            "ဒီ Branch မှာ Department name သို့မဟုတ် short name ရှိပြီးသားပါ။",
                                            "error"
                                        )
                                    else:
                                        cur.execute(
                                            """
                                            INSERT INTO public.departments (
                                                name,
                                                short_name,
                                                branch_id
                                            )
                                            VALUES (%s, %s, %s)
                                            """,
                                            (name, short_name, branch_id)
                                        )

                                        conn.commit()

                                        flash(
                                            "Department အသစ်ထည့်ပြီးပါပြီ။",
                                            "success"
                                        )

                    elif action == "edit":

                        department_id = request.form.get(
                            "department_id", ""
                        ).strip()
                        name = request.form.get("name", "").strip()
                        short_name = request.form.get(
                            "short_name", ""
                        ).strip()
                        branch_id = request.form.get(
                            "branch_id", ""
                        ).strip()

                        if not all([
                            department_id,
                            name,
                            short_name,
                            branch_id
                        ]):
                            flash(
                                "လိုအပ်သော အချက်အလက်အားလုံး ဖြည့်ပါ။",
                                "error"
                            )

                        else:
                            try:
                                department_id = int(department_id)
                                branch_id = int(branch_id)
                            except (ValueError, TypeError):
                                department_id = None
                                branch_id = None

                            if not department_id or not branch_id:
                                flash(
                                    "Department သို့မဟုတ် Branch ID မမှန်ကန်ပါ။",
                                    "error"
                                )
                            else:
                                cur.execute(
                                    """
                                    SELECT id
                                    FROM public.branches
                                    WHERE id = %s
                                      AND is_active = TRUE
                                    """,
                                    (branch_id,)
                                )

                                valid_branch = cur.fetchone()

                                if not valid_branch:
                                    flash(
                                        "ရွေးချယ်ထားသော Branch ကို အသုံးပြု၍မရပါ။",
                                        "error"
                                    )
                                else:
                                    # Check duplicate in the selected branch,
                                    # excluding the current department
                                    cur.execute(
                                        """
                                        SELECT id
                                        FROM public.departments
                                        WHERE branch_id = %s
                                          AND id != %s
                                          AND (
                                              LOWER(TRIM(name)) = LOWER(TRIM(%s))
                                              OR LOWER(TRIM(short_name)) = LOWER(TRIM(%s))
                                          )
                                        """,
                                        (
                                            branch_id,
                                            department_id,
                                            name,
                                            short_name
                                        )
                                    )

                                    duplicate = cur.fetchone()

                                    if duplicate:
                                        flash(
                                            "ဒီ Branch မှာ Department name သို့မဟုတ် short name ရှိပြီးသားပါ။",
                                            "error"
                                        )
                                    else:
                                        cur.execute(
                                            """
                                            UPDATE public.departments
                                            SET name = %s,
                                                short_name = %s,
                                                branch_id = %s,
                                                updated_at = CURRENT_TIMESTAMP
                                            WHERE id = %s
                                            """,
                                            (
                                                name,
                                                short_name,
                                                branch_id,
                                                department_id
                                            )
                                        )

                                        conn.commit()

                                        if cur.rowcount:
                                            flash(
                                                "Department ပြင်ဆင်ပြီးပါပြီ။",
                                                "success"
                                            )
                                        else:
                                            flash(
                                                "Department ကို မတွေ့ပါ။",
                                                "error"
                                            )

                    elif action == "toggle":

                        department_id = request.form.get(
                            "department_id", ""
                        )

                        try:
                            department_id = int(department_id)
                        except (ValueError, TypeError):
                            department_id = None

                        if not department_id:
                            flash(
                                "Department ID မမှန်ကန်ပါ။",
                                "error"
                            )
                        else:
                            cur.execute(
                                """
                                UPDATE public.departments
                                SET is_active = NOT is_active,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE id = %s
                                """,
                                (department_id,)
                            )

                            conn.commit()

                            if cur.rowcount:
                                flash(
                                    "Department status ပြောင်းပြီးပါပြီ။",
                                    "success"
                                )
                            else:
                                flash(
                                    "Department ကို မတွေ့ပါ။",
                                    "error"
                                )

                    else:
                        flash(
                            "လုပ်ဆောင်ချက် မမှန်ကန်ပါ။",
                            "error"
                        )

            except Exception as exc:

                conn.rollback()

                if getattr(exc, "sqlstate", None) == "23505":
                    flash(
                        "ဒီ Branch မှာ Department name သို့မဟုတ် short name ထပ်နေပါတယ်။",
                        "error"
                    )
                else:
                    print("Manage Department Error:", exc)
                    flash(
                        "Department သိမ်းဆည်းရာတွင် error ဖြစ်နေပါတယ်။",
                        "error"
                    )

            return redirect(url_for("main.manage_departments"))

        # GET: Load branches for the edit dropdown
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, name, is_active
                FROM public.branches
                ORDER BY name
                """
            )

            branches = [
                {
                    "id": row[0],
                    "name": row[1],
                    "is_active": row[2]
                }
                for row in cur.fetchall()
            ]

            # Department တစ်ခုချင်းစီအတွက် Head Count
            cur.execute(
                """
                WITH department_head_assignments AS (

                    -- Multi-department assignments
                    SELECT DISTINCT
                        ud.department_id,
                        u.id AS user_id
                    FROM public.user_departments ud
                    JOIN public.users u
                        ON u.id = ud.user_id
                    JOIN public.departments assigned_dept
                        ON assigned_dept.id = ud.department_id
                       AND assigned_dept.is_active = TRUE
                    WHERE u.role = 'department_head'
                      AND u.is_active = TRUE

                    UNION

                    -- Legacy department fallback
                    SELECT
                        u.department_id,
                        u.id AS user_id
                    FROM public.users u
                    JOIN public.departments legacy_dept
                        ON legacy_dept.id = u.department_id
                       AND legacy_dept.is_active = TRUE
                    WHERE u.role = 'department_head'
                      AND u.is_active = TRUE
                      AND u.department_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.user_departments ud
                          JOIN public.departments assigned_dept
                              ON assigned_dept.id = ud.department_id
                             AND assigned_dept.is_active = TRUE
                          WHERE ud.user_id = u.id
                      )
                ),

                department_head_counts AS (
                    SELECT
                        department_id,
                        COUNT(DISTINCT user_id) AS head_count
                    FROM department_head_assignments
                    GROUP BY department_id
                )

                SELECT
                    d.id,
                    d.name,
                    d.short_name,
                    d.is_active,
                    d.created_at,
                    d.updated_at,
                    COALESCE(dhc.head_count, 0) AS department_head_count,
                    d.branch_id,
                    b.name AS branch_name
                FROM public.departments d
                LEFT JOIN department_head_counts dhc
                    ON dhc.department_id = d.id
                LEFT JOIN public.branches b
                    ON b.id = d.branch_id
                ORDER BY d.id ASC
                """
            )

            departments = cur.fetchall()

            # Original unique Department Head count for Stats Card
            cur.execute(
                """
                SELECT COUNT(DISTINCT u.id)
                FROM public.users u
                WHERE u.role = 'department_head'
                  AND u.is_active = TRUE
                """
            )

            original_head_count = cur.fetchone()[0]

        return render_template(
            "department.html",
            departments=departments,
            original_head_count=original_head_count,
            branches=branches
        )

    finally:
        conn.close()



@main.route("/departments/add", methods=["GET", "POST"])
@super_admin_required
def add_department():

    conn = get_connection()

    try:
        # Get active branches for dropdown
        def get_active_branches():
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, name
                    FROM public.branches
                    WHERE is_active = TRUE
                    ORDER BY name
                """)

                return [
                    {
                        "id": row[0],
                        "name": row[1]
                    }
                    for row in cur.fetchall()
                ]

        branches = get_active_branches()

        if request.method == "POST":

            name = request.form.get("name", "").strip()
            short_name = request.form.get("short_name", "").strip()
            branch_id = request.form.get("branch_id", "").strip()

            # Validate required fields
            if not name or not short_name or not branch_id:

                flash(
                    "Department name, short name နှင့် branch ရွေးချယ်ပါ။",
                    "error"
                )

                return render_template(
                    "add_department.html",
                    branches=branches
                )

            # Validate branch ID
            try:
                branch_id = int(branch_id)
            except (ValueError, TypeError):

                flash(
                    "ရွေးချယ်ထားသော Branch မမှန်ကန်ပါ။",
                    "error"
                )

                return render_template(
                    "add_department.html",
                    branches=branches
                )

            if branch_id <= 0:
                flash(
                    "ရွေးချယ်ထားသော Branch မမှန်ကန်ပါ။",
                    "error"
                )

                return render_template(
                    "add_department.html",
                    branches=branches
                )

            try:
                with conn.cursor() as cur:

                    # Check selected branch is active
                    cur.execute("""
                        SELECT id
                        FROM public.branches
                        WHERE id = %s
                          AND is_active = TRUE
                    """, (branch_id,))

                    valid_branch = cur.fetchone()

                    if not valid_branch:

                        flash(
                            "ရွေးချယ်ထားသော Branch ကို အသုံးပြု၍မရပါ။",
                            "error"
                        )

                        return render_template(
                            "add_department.html",
                            branches=branches
                        )

                    # Check duplicate within the same branch
                    cur.execute(
                        """
                        SELECT id
                        FROM public.departments
                        WHERE branch_id = %s
                          AND (
                              LOWER(TRIM(name)) = LOWER(TRIM(%s))
                              OR LOWER(TRIM(short_name)) = LOWER(TRIM(%s))
                          )
                        """,
                        (branch_id, name, short_name)
                    )

                    duplicate = cur.fetchone()

                    if duplicate:

                        flash(
                            "ဒီ Branch မှာ Department name သို့မဟုတ် short name ရှိပြီးသားပါ။",
                            "error"
                        )

                        return render_template(
                            "add_department.html",
                            branches=branches
                        )

                    # Insert department with branch_id
                    cur.execute("""
                        INSERT INTO public.departments (
                            name,
                            short_name,
                            branch_id
                        )
                        VALUES (%s, %s, %s)
                    """, (
                        name,
                        short_name,
                        branch_id
                    ))

                    conn.commit()

                flash(
                    "Department အသစ်ထည့်ပြီးပါပြီ။",
                    "success"
                )

                return redirect(
                    url_for("main.manage_departments")
                )

            except Exception as exc:

                conn.rollback()

                if getattr(exc, "sqlstate", None) == "23505":

                    flash(
                        "ဒီ Branch မှာ Department name သို့မဟုတ် short name ထပ်နေပါတယ်။",
                        "error"
                    )

                else:

                    print("Add Department Error:", exc)

                    flash(
                        "Department သိမ်းဆည်းရာတွင် error ဖြစ်နေပါတယ်။",
                        "error"
                    )

                return render_template(
                    "add_department.html",
                    branches=branches
                )

        return render_template(
            "add_department.html",
            branches=branches
        )

    finally:
        conn.close()

# -----------------------------------------
# MANAGE DEPARTMENT HEADS  (View / Edit / Toggle)
# SUPER ADMIN ONLY
# -----------------------------------------

@main.route("/department-heads", methods=["GET", "POST"])
@super_admin_required
def manage_department_heads():
    conn = get_connection()

    try:
        if request.method == "POST":
            action = request.form.get("action")

            if action == "toggle":
                user_id = request.form.get("user_id")

                try:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE users
                            SET is_active = NOT is_active,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                              AND role = 'department_head'
                        """, (user_id,))

                        if cur.rowcount:
                            conn.commit()
                            flash(
                                "Department Head account status updated.",
                                "success"
                            )
                        else:
                            conn.rollback()
                            flash(
                                "Department Head account not found.",
                                "error"
                            )

                except Exception:
                    conn.rollback()
                    current_app.logger.exception(
                        "Failed to update Department Head status"
                    )
                    flash(
                        "Unable to update account status.",
                        "error"
                    )

            else:
                flash("Invalid action.", "error")

            return redirect(url_for("main.manage_department_heads"))

        with conn.cursor() as cur:

            # Active departments
            cur.execute("""
                SELECT id, name, short_name
                FROM departments
                WHERE is_active = TRUE
                ORDER BY name
            """)
            active_departments = cur.fetchall()

            # Department Head accounts
            cur.execute("""
                SELECT
                    u.id,
                    u.name,
                    u.employee_code,
                    u.department_id,
                    d.name AS department_name,
                    u.is_active,
                    u.created_at,
                    u.updated_at,
                    COALESCE(
                        (
                            SELECT ARRAY_AGG(
                                ud.department_id
                                ORDER BY ud.department_id
                            )
                            FROM user_departments ud
                            WHERE ud.user_id = u.id
                        ),
                        ARRAY[u.department_id]
                    ) AS assigned_department_ids,
                    (
                        SELECT ARRAY_AGG(
                            DISTINCT b.name
                            ORDER BY b.name
                        )
                        FROM user_department_permissions udp
                        INNER JOIN branches b
                            ON b.id = udp.branch_id
                        WHERE udp.user_id = u.id
                    ) AS assigned_branch_names
                FROM users u
                LEFT JOIN departments d
                    ON d.id = u.department_id
                WHERE u.role = 'department_head'
                ORDER BY u.id ASC
            """)
            department_heads = cur.fetchall()

            # Department Head permissions
            cur.execute("""
                SELECT
                    p.user_id,
                    p.branch_id,
                    b.name AS branch_name,
                    p.department_id,
                    d.name AS department_name,
                    p.can_view,
                    p.can_add,
                    p.can_edit,
                    p.can_delete,
                    p.can_export
                FROM user_department_permissions p
                INNER JOIN branches b
                    ON b.id = p.branch_id
                INNER JOIN departments d
                    ON d.id = p.department_id
                   AND d.branch_id = p.branch_id
                WHERE p.user_id IN (
                    SELECT id
                    FROM users
                    WHERE role = 'department_head'
                )
                ORDER BY p.user_id, p.branch_id, p.department_id
            """)

            permission_rows = cur.fetchall()

        # Build permission dictionary
        permission_map = {}

        for p in permission_rows:
            user_id = p[0]
            department_id = p[3]

            if user_id not in permission_map:
                permission_map[user_id] = {}

            permission_map[user_id][department_id] = {
                "branch_id": p[1],
                "branch_name": p[2],
                "department_name": p[4],
                "can_view": p[5],
                "can_add": p[6],
                "can_edit": p[7],
                "can_delete": p[8],
                "can_export": p[9]
            }

        return render_template(
            "department_head.html",
            department_heads=department_heads,
            departments=active_departments,
            permission_map=permission_map
        )

    finally:
        conn.close()



@main.route("/department-heads/<int:user_id>/edit", methods=["GET", "POST"])
@super_admin_required
def edit_department_head(user_id):

    conn = get_connection()

    try:
        if request.method == "POST":

            name = request.form.get("name", "").strip()
            employee_code = request.form.get("employee_code", "").strip()
            raw_department_ids = request.form.getlist("department_ids")
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            try:
                department_ids = list(dict.fromkeys(
                    int(dept_id)
                    for dept_id in raw_department_ids
                    if dept_id.strip()
                ))
            except (ValueError, AttributeError):
                department_ids = []

            if not name or not employee_code or not department_ids:
                flash(
                    "Name၊ Employee Code နှင့် Department အနည်းဆုံးတစ်ခု ရွေးပါ။",
                    "error"
                )
                return redirect(
                    url_for("main.edit_department_head", user_id=user_id)
                )

            if password and len(password) < 8:
                flash(
                    "The password must be at least 8 characters long.",
                    "error"
                )
                return redirect(
                    url_for("main.edit_department_head", user_id=user_id)
                )

            if password != confirm_password:
                flash(
                    "Password and Confirm Password do not match.",
                    "error"
                )
                return redirect(
                    url_for("main.edit_department_head", user_id=user_id)
                )

            try:
                with conn.cursor() as cur:

                    # Check Department Head exists
                    cur.execute("""
                        SELECT id
                        FROM users
                        WHERE id = %s
                          AND role = 'department_head'
                        FOR UPDATE
                    """, (user_id,))

                    if not cur.fetchone():
                        conn.rollback()
                        flash(
                            "The Department Head account could not be found.",
                            "error"
                        )
                        return redirect(
                            url_for("main.manage_department_heads")
                        )

                    # Check duplicate employee code
                    cur.execute("""
                        SELECT id
                        FROM users
                        WHERE employee_code = %s
                          AND id <> %s
                    """, (employee_code, user_id))

                    if cur.fetchone():
                        conn.rollback()
                        flash("Employee Code ထပ်နေပါတယ်။", "error")
                        return redirect(
                            url_for(
                                "main.edit_department_head",
                                user_id=user_id
                            )
                        )

                    # Validate selected departments and their branches
                    cur.execute("""
                        SELECT
                            d.id,
                            d.branch_id
                        FROM departments d
                        INNER JOIN branches b
                            ON b.id = d.branch_id
                        WHERE d.id = ANY(%s)
                          AND d.is_active = TRUE
                    """, (department_ids,))

                    department_rows = cur.fetchall()

                    department_branch_map = {
                        row[0]: row[1]
                        for row in department_rows
                    }

                    if set(department_branch_map.keys()) != set(department_ids):
                        conn.rollback()
                        flash(
                            "ရွေးချယ်ထားသော Department သို့မဟုတ် Branch ကို စစ်ဆေးပါ။",
                            "error"
                        )
                        return redirect(
                            url_for(
                                "main.edit_department_head",
                                user_id=user_id
                            )
                        )

                    # Branch IDs are derived from selected departments
                    selected_branch_ids = list(dict.fromkeys(
                        department_branch_map[dept_id]
                        for dept_id in department_ids
                    ))

                    # Update account information
                    if password:
                        cur.execute("""
                            UPDATE users
                            SET
                                name = %s,
                                employee_code = %s,
                                password_hash = %s,
                                department_id = %s,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                              AND role = 'department_head'
                        """, (
                            name,
                            employee_code,
                            generate_password_hash(password),
                            department_ids[0],
                            user_id
                        ))
                    else:
                        cur.execute("""
                            UPDATE users
                            SET
                                name = %s,
                                employee_code = %s,
                                department_id = %s,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                              AND role = 'department_head'
                        """, (
                            name,
                            employee_code,
                            department_ids[0],
                            user_id
                        ))

                    if cur.rowcount == 0:
                        conn.rollback()
                        flash(
                            "Department Head account ကို ရှာမတွေ့ပါ။",
                            "error"
                        )
                        return redirect(
                            url_for("main.manage_department_heads")
                        )

                    # Replace department assignments
                    cur.execute("""
                        DELETE FROM user_departments
                        WHERE user_id = %s
                    """, (user_id,))

                    cur.executemany("""
                        INSERT INTO user_departments (
                            user_id,
                            department_id
                        )
                        VALUES (%s, %s)
                        ON CONFLICT (user_id, department_id)
                        DO NOTHING
                    """, [
                        (user_id, dept_id)
                        for dept_id in department_ids
                    ])

                    # Replace old department permissions
                    cur.execute("""
                        DELETE FROM user_department_permissions
                        WHERE user_id = %s
                    """, (user_id,))

                    # Save updated permissions
                    for department_id in department_ids:

                        branch_id = department_branch_map[department_id]

                        can_view = (
                            request.form.get(
                                f"permission_{department_id}_view"
                            ) == "on"
                        )

                        can_add = (
                            request.form.get(
                                f"permission_{department_id}_add"
                            ) == "on"
                        )

                        can_edit = (
                            request.form.get(
                                f"permission_{department_id}_edit"
                            ) == "on"
                        )

                        can_delete = (
                            request.form.get(
                                f"permission_{department_id}_delete"
                            ) == "on"
                        )

                        can_export = (
                            request.form.get(
                                f"permission_{department_id}_export"
                            ) == "on"
                        )

                        cur.execute("""
                            INSERT INTO user_department_permissions (
                                user_id,
                                branch_id,
                                department_id,
                                can_view,
                                can_add,
                                can_edit,
                                can_delete,
                                can_export
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (
                                user_id,
                                branch_id,
                                department_id
                            )
                            DO UPDATE SET
                                can_view = EXCLUDED.can_view,
                                can_add = EXCLUDED.can_add,
                                can_edit = EXCLUDED.can_edit,
                                can_delete = EXCLUDED.can_delete,
                                can_export = EXCLUDED.can_export,
                                updated_at = CURRENT_TIMESTAMP
                        """, (
                            user_id,
                            branch_id,
                            department_id,
                            can_view,
                            can_add,
                            can_edit,
                            can_delete,
                            can_export
                        ))

                conn.commit()

                flash(
                    "Department Head information has been updated.",
                    "success"
                )

                return redirect(
                    url_for("main.manage_department_heads")
                )

            except Exception as exc:
                conn.rollback()

                if getattr(exc, "sqlstate", None) == "23505":
                    flash("Employee Code ထပ်နေပါတယ်။", "error")
                else:
                    current_app.logger.exception(
                        "Edit Department Head Error"
                    )
                    flash(
                        "Department Head ပြင်ဆင်ရာတွင် error ဖြစ်နေပါတယ်။",
                        "error"
                    )

                return redirect(
                    url_for("main.edit_department_head", user_id=user_id)
                )

        # GET: Load account details
        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    u.id,
                    u.name,
                    u.employee_code,
                    COALESCE(
                        (
                            SELECT ARRAY_AGG(
                                ud.department_id
                                ORDER BY ud.department_id
                            )
                            FROM user_departments ud
                            WHERE ud.user_id = u.id
                        ),
                        ARRAY[u.department_id]
                    ) AS assigned_department_ids
                FROM users u
                WHERE u.id = %s
                  AND u.role = 'department_head'
            """, (user_id,))

            department_head = cur.fetchone()

            if not department_head:
                flash(
                    "Department Head account ကို ရှာမတွေ့ပါ။",
                    "error"
                )
                return redirect(
                    url_for("main.manage_department_heads")
                )

            # Load active branches
            cur.execute("""
                SELECT
                    id,
                    name
                FROM branches
                WHERE is_active = TRUE
                ORDER BY name
            """)

            branches = cur.fetchall()

            # Load active departments with branch information
            cur.execute("""
                SELECT
                    d.id,
                    d.name,
                    d.short_name,
                    d.branch_id,
                    b.name AS branch_name
                FROM departments d
                INNER JOIN branches b
                    ON b.id = d.branch_id
                WHERE d.is_active = TRUE
                  AND b.is_active = TRUE
                ORDER BY b.name, d.name
            """)

            departments = cur.fetchall()

            # Load existing permissions
            cur.execute("""
                SELECT
                    department_id,
                    branch_id,
                    can_view,
                    can_add,
                    can_edit,
                    can_delete,
                    can_export
                FROM user_department_permissions
                WHERE user_id = %s
            """, (user_id,))

            permission_rows = cur.fetchall()

            permissions = {}

            for row in permission_rows:
                permissions[row[0]] = {
                    "branch_id": row[1],
                    "view": row[2],
                    "add": row[3],
                    "edit": row[4],
                    "delete": row[5],
                    "export": row[6]
                }

            # Get assigned branch IDs from assigned departments
            assigned_department_ids = department_head[3] or []

            assigned_branch_ids = list(dict.fromkeys(
                department[3]
                for department in departments
                if department[0] in assigned_department_ids
            ))

        return render_template(
            "edit_department_head.html",
            department_head=department_head,
            branches=branches,
            departments=departments,
            permissions=permissions,
            assigned_branch_ids=assigned_branch_ids
        )

    finally:
        conn.close()

# -----------------------------------------
# ADD DEPARTMENT HEAD
# SUPER ADMIN ONLY
# -----------------------------------------

# -----------------------------------------
# ADD DEPARTMENT HEAD
# SUPER ADMIN ONLY
# MULTI-DEPARTMENT ASSIGNMENT
# -----------------------------------------


@main.route("/department-heads/add", methods=["GET", "POST"])
@super_admin_required
def add_department_head():

    conn = get_connection()

    try:
        if request.method == "POST":

            name = request.form.get("name", "").strip()
            employee_code = request.form.get("employee_code", "").strip()

            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            branch_id = request.form.get("branch_id", "").strip()

            raw_department_ids = request.form.getlist("department_ids")

            # Convert branch ID
            try:
                branch_id = int(branch_id)
            except (TypeError, ValueError):
                flash("Branch ကို ရွေးချယ်ပေးပါ။", "error")
                return redirect(url_for("main.add_department_head"))

            # Convert department IDs
            department_ids = []

            try:
                for value in raw_department_ids:
                    department_id = int(value)

                    if department_id not in department_ids:
                        department_ids.append(department_id)

            except (TypeError, ValueError):
                flash("Department ID မမှန်ကန်ပါ။", "error")
                return redirect(url_for("main.add_department_head"))

            if not name or not employee_code or not password or not confirm_password:

                flash("လိုအပ်သော အချက်အလက်အားလုံး ဖြည့်ပါ။", "error")

            elif not department_ids:

                flash("Department အနည်းဆုံးတစ်ခု ရွေးပါ။", "error")

            elif len(password) < 8:

                flash("Password အနည်းဆုံး 8 လုံး ရှိရပါမယ်။", "error")

            elif password != confirm_password:

                flash("Password နှင့် Confirm Password မတူပါ။", "error")

            else:

                try:
                    with conn.cursor() as cur:

                        # Check branch
                        cur.execute("""
                            SELECT id
                            FROM branches
                            WHERE id = %s
                              AND is_active = TRUE
                            FOR SHARE
                        """, (branch_id,))

                        if not cur.fetchone():
                            flash("Branch မရှိပါ သို့မဟုတ် inactive ဖြစ်နေပါတယ်။", "error")
                            return redirect(url_for("main.add_department_head"))

                        # Check employee code
                        cur.execute("""
                            SELECT id
                            FROM users
                            WHERE employee_code = %s
                        """, (employee_code,))

                        if cur.fetchone():
                            flash("Employee Code အသုံးပြုပြီးသား ဖြစ်ပါတယ်။", "error")
                            return redirect(url_for("main.add_department_head"))

                        # Validate departments belong to branch
                        cur.execute("""
                            SELECT id
                            FROM departments
                            WHERE id = ANY(%s)
                              AND branch_id = %s
                              AND is_active = TRUE
                        """, (department_ids, branch_id))

                        valid_department_ids = {
                            row[0] for row in cur.fetchall()
                        }

                        if valid_department_ids != set(department_ids):
                            flash(
                                "ရွေးထားသော Department အချို့သည် "
                                "Branch နှင့် မသက်ဆိုင်ပါ။",
                                "error"
                            )
                            return redirect(url_for("main.add_department_head"))

                        # Create Department Head
                        primary_department_id = department_ids[0]

                        cur.execute("""
                            INSERT INTO users (
                                name,
                                employee_code,
                                password_hash,
                                role,
                                department_id
                            )
                            VALUES (%s, %s, %s, 'department_head', %s)
                            RETURNING id
                        """, (
                            name,
                            employee_code,
                            generate_password_hash(password),
                            primary_department_id
                        ))

                        user_id = cur.fetchone()[0]

                        # Keep existing department assignment
                        cur.executemany("""
                            INSERT INTO user_departments (
                                user_id,
                                department_id
                            )
                            VALUES (%s, %s)
                            ON CONFLICT (user_id, department_id)
                            DO NOTHING
                        """, [
                            (user_id, department_id)
                            for department_id in department_ids
                        ])

                        # Save individual department permissions
                        for department_id in department_ids:

                            can_view = (
                                request.form.get(
                                    f"permission_{department_id}_view"
                                ) == "on"
                            )

                            can_add = (
                                request.form.get(
                                    f"permission_{department_id}_add"
                                ) == "on"
                            )

                            can_edit = (
                                request.form.get(
                                    f"permission_{department_id}_edit"
                                ) == "on"
                            )

                            can_delete = (
                                request.form.get(
                                    f"permission_{department_id}_delete"
                                ) == "on"
                            )

                            can_export = (
                                request.form.get(
                                    f"permission_{department_id}_export"
                                ) == "on"
                            )

                            cur.execute("""
                                INSERT INTO user_department_permissions (
                                    user_id,
                                    branch_id,
                                    department_id,
                                    can_view,
                                    can_add,
                                    can_edit,
                                    can_delete,
                                    can_export
                                )
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (
                                    user_id,
                                    branch_id,
                                    department_id
                                )
                                DO UPDATE SET
                                    can_view = EXCLUDED.can_view,
                                    can_add = EXCLUDED.can_add,
                                    can_edit = EXCLUDED.can_edit,
                                    can_delete = EXCLUDED.can_delete,
                                    can_export = EXCLUDED.can_export,
                                    updated_at = CURRENT_TIMESTAMP
                            """, (
                                user_id,
                                branch_id,
                                department_id,
                                can_view,
                                can_add,
                                can_edit,
                                can_delete,
                                can_export
                            ))

                    conn.commit()

                    flash(
                        "Department Head အသစ်ထည့်ပြီး Permission များ "
                        "သိမ်းဆည်းပြီးပါပြီ။",
                        "success"
                    )

                    return redirect(
                        url_for("main.manage_department_heads")
                    )

                except Exception:

                    conn.rollback()

                    current_app.logger.exception(
                        "Failed to add department head"
                    )

                    flash(
                        "Department Head ထည့်ရာတွင် Error ဖြစ်နေပါတယ်။",
                        "error"
                    )

        with conn.cursor() as cur:

            cur.execute("""
                SELECT id, name
                FROM branches
                WHERE is_active = TRUE
                ORDER BY id DESC
            """)

            branches = cur.fetchall()

            cur.execute("""
                SELECT id, name, short_name, branch_id
                FROM departments
                WHERE is_active = TRUE
                ORDER BY id DESC
            """)

            active_departments = cur.fetchall()

        return render_template(
            "add_department_head.html",
            branches=branches,
            departments=active_departments
        )

    finally:
        conn.close()


@main.route("/api/profile", methods=["GET"])
@login_required
def profile_api():

    conn = None
    cur = None

    try:
        user_id = session.get("user_id")

        conn = get_connection()
        cur = conn.cursor()

        # Get logged-in user information
        cur.execute("""
            SELECT
                name,
                employee_code,
                role,
                is_active,
                created_at
            FROM users
            WHERE id = %s
            LIMIT 1
        """, (user_id,))

        user = cur.fetchone()

        if not user:
            return jsonify({
                "success": False,
                "message": "User account not found."
            }), 404

        name = user[0]
        employee_code = user[1]
        role = user[2]
        is_active = user[3]
        created_at = user[4]

        # Super Admin has access to all departments
        if role == "super_admin":

            departments = ["All Access"]

        else:

            # Get every department assigned to this user
            cur.execute("""
                SELECT DISTINCT
                    d.name
                FROM user_department_permissions udp
                INNER JOIN departments d
                    ON d.id = udp.department_id
                WHERE udp.user_id = %s
                ORDER BY d.name
            """, (user_id,))

            departments = [
                row[0]
                for row in cur.fetchall()
                if row[0]
            ]

        department_text = (
            "All Access"
            if role == "super_admin"
            else ", ".join(departments) if departments
            else "Not assigned"
        )

        return jsonify({
            "success": True,
            "profile": {
                "name": name,
                "employee_code": employee_code,
                "role": role,
                "departments": departments,
                "department": department_text,
                "status": "Active" if is_active else "Inactive",
                "created_at": (
                    created_at.strftime("%d %b %Y")
                    if created_at else "-"
                )
            }
        })

    except Exception:
        current_app.logger.exception("Profile API error")

        return jsonify({
            "success": False,
            "message": "Unable to load profile information."
        }), 500

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()
            
@main.route("/api/change-password", methods=["POST"])
@login_required
def change_password_api():

    data = request.get_json(silent=True) or {}

    new_password = data.get("new_password") or ""
    confirm_password = data.get("confirm_password") or ""

    if len(new_password) < 8:
        return jsonify({
            "success": False,
            "message": "Password must contain at least 8 characters."
        }), 400

    if len(new_password) > 128:
        return jsonify({
            "success": False,
            "message": "Password is too long."
        }), 400

    if new_password != confirm_password:
        return jsonify({
            "success": False,
            "message": "The passwords do not match."
        }), 400

    user_id = session.get("user_id")

    conn = get_connection()
    cur = conn.cursor()

    try:
        password_hash = generate_password_hash(
            new_password,
            method="scrypt"
        )

        cur.execute("""
            UPDATE public.users
            SET
                password_hash = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND is_active = TRUE
            RETURNING id
        """, (password_hash, user_id))

        updated = cur.fetchone()

        if updated is None:
            conn.rollback()

            return jsonify({
                "success": False,
                "message": "Active user account not found."
            }), 404

        conn.commit()

        return jsonify({
            "success": True,
            "message": "Your password has been updated successfully."
        })

    except Exception:
        conn.rollback()

        current_app.logger.exception(
            "Change password API failed."
        )

        return jsonify({
            "success": False,
            "message": "Unable to update password. Please try again."
        }), 500

    finally:
        cur.close()
        conn.close()

