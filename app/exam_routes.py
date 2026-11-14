import os
import json
import hashlib
import secrets
import random
from datetime import datetime, timedelta, date
from decimal import Decimal
from app.routes import login_required
from werkzeug.exceptions import HTTPException
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, abort, jsonify, current_app
)
from werkzeug.utils import secure_filename
from psycopg2.extras import RealDictCursor, Json

from functools import wraps
from app.db import get_connection

exam_bp = Blueprint("exam", __name__)

EXAM_DURATION_MINUTES = 30
LINK_LIFETIME_MINUTES = 60
ALLOWED_ROLES = {"super_admin", "department_head"}


def _db():
    return get_connection()


def _dict_cursor(conn):
    return conn.cursor(cursor_factory=RealDictCursor)


def _logged_in():
    return bool(session.get("user_id"))

def super_admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("main.login"))

        if session.get("role") != "super_admin":
            abort(403)

        return view(*args, **kwargs)

    return wrapped_view


def _require_login():
    if not _logged_in():
        return redirect(url_for("main.login"))
    return None


def _require_exam_role():
    if not _logged_in():
        return redirect(url_for("main.login"))
    if session.get("role") not in ALLOWED_ROLES:
        abort(403)
    return None


def _scope_department():
    """Department heads can only use their own department."""
    if session.get("role") == "department_head":
        return session.get("department_id")
    return None


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _get_assignment_by_token(cur, token):
    cur.execute("""
        SELECT ea.*, c.name AS candidate_name, c.email AS candidate_email,
               c.position, d.name AS department_name,
               es.exam_title, es.sub_department
        FROM exam_assignments ea
        JOIN candidates c ON c.id = ea.candidate_id
        JOIN departments d ON d.id = c.department_id
        JOIN exam_sets es ON es.id = ea.exam_set_id
        WHERE ea.token_hash = %s
        LIMIT 1
    """, (_hash_token(token),))
    return cur.fetchone()


def _get_assignment(cur, assignment_id):
    cur.execute("""
        SELECT ea.*, c.name AS candidate_name, c.email AS candidate_email,
               c.position, d.name AS department_name,
               es.exam_title, es.sub_department
        FROM exam_assignments ea
        JOIN candidates c ON c.id = ea.candidate_id
        JOIN departments d ON d.id = c.department_id
        JOIN exam_sets es ON es.id = ea.exam_set_id
        WHERE ea.id = %s
    """, (assignment_id,))
    return cur.fetchone()


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        # PostgreSQL TIMESTAMPTZ should be aware; tolerate legacy naive values as UTC.
        from datetime import timezone
        return value.replace(tzinfo=timezone.utc)
    return value


def _now():
    from datetime import timezone
    return datetime.now(timezone.utc)


def _assignment_expired(assignment):
    expiry = _as_utc(assignment.get("exam_expires_at"))
    return expiry is not None and _now() >= expiry


def _finalize_assignment(conn, cur, assignment):
    snapshot = _json_value(assignment["question_snapshot"]) or []
    answers = _json_value(assignment.get("answers")) or {}
    correct = 0
    wrong = 0
    total_points = Decimal("0")
    earned_points = Decimal("0")

    for index, question in enumerate(snapshot):
        total_points += Decimal(str(question.get("points", 1)))
        chosen = answers.get(str(index))
        if chosen is not None and str(chosen) == str(question.get("correct_option")):
            correct += 1
            earned_points += Decimal(str(question.get("points", 1)))
        else:
            wrong += 1

    score = (earned_points / total_points * Decimal("100")) if total_points else Decimal("0")
    cur.execute("""
        UPDATE exam_assignments
        SET status = 'submitted',
            submitted_at = NOW(),
            correct_answers = %s,
            wrong_answers = %s,
            score = %s,
            total_points = %s,
            updated_at = NOW()
        WHERE id = %s
        RETURNING *
    """, (correct, wrong, score.quantize(Decimal("0.01")),
          total_points, assignment["id"]))
    return cur.fetchone()


def _load_exam_sets(cur, department_id=None):
    if department_id:
        cur.execute("""
            SELECT es.*, d.name AS department_name,
                   COUNT(eq.id)::int AS question_count
            FROM exam_sets es
            JOIN departments d ON d.id = es.department_id
            LEFT JOIN exam_questions eq ON eq.exam_set_id = es.id
            WHERE es.deleted_at IS NULL
              AND es.department_id = %s
            GROUP BY es.id, d.name
            ORDER BY es.created_at DESC
        """, (department_id,))
    else:
        cur.execute("""
            SELECT es.*, d.name AS department_name,
                   COUNT(eq.id)::int AS question_count
            FROM exam_sets es
            JOIN departments d ON d.id = es.department_id
            LEFT JOIN exam_questions eq ON eq.exam_set_id = es.id
            WHERE es.deleted_at IS NULL
            GROUP BY es.id, d.name
            ORDER BY es.created_at DESC
        """)
    return cur.fetchall()

@exam_bp.route("/exams")
def exam_list():
    denied = _require_exam_role()
    if denied:
        return denied

    conn = _db()

    try:
        cur = _dict_cursor(conn)

        dept_scope = _scope_department()
        search = (request.args.get("search") or "").strip()

        # --------------------------------------------------
        # PAGINATION SETTINGS
        # --------------------------------------------------
        PER_PAGE = 5

        try:
            page = int(request.args.get("page", 1))
        except (TypeError, ValueError):
            page = 1

        page = max(1, page)

        # --------------------------------------------------
        # SEARCH CONDITIONS
        # --------------------------------------------------
        where_conditions = """
            WHERE es.deleted_at IS NULL 
              AND (%s IS NULL OR es.department_id = %s)
              AND (
                  %s = ''
                  OR es.exam_title ILIKE %s
                  OR es.sub_department ILIKE %s
                  OR d.name ILIKE %s
              )
        """

        query_params = (
            dept_scope,
            dept_scope,
            search,
            f"%{search}%",
            f"%{search}%",
            f"%{search}%"
        )

        # --------------------------------------------------
        # 1. COUNT TOTAL RECORDS
        # --------------------------------------------------
        cur.execute(f"""
            SELECT COUNT(DISTINCT es.id)::int AS total
            FROM exam_sets es
            JOIN departments d ON d.id = es.department_id
            {where_conditions}
        """, query_params)

        count_row = cur.fetchone()

        if count_row:
            total_records = (
                count_row["total"]
                if isinstance(count_row, dict)
                else count_row[0]
            )
        else:
            total_records = 0

        # --------------------------------------------------
        # 2. CALCULATE TOTAL PAGES
        # --------------------------------------------------
        total_pages = max(
            1,
            (total_records + PER_PAGE - 1) // PER_PAGE
        )

        # Prevent invalid page number
        page = min(page, total_pages)

        offset = (page - 1) * PER_PAGE

        # --------------------------------------------------
        # 3. GET PAGINATED EXAM SETS
        # --------------------------------------------------
        cur.execute(f"""
            SELECT es.*, d.name AS department_name,
                   COUNT(eq.id)::int AS question_count
            FROM exam_sets es
            JOIN departments d ON d.id = es.department_id
            LEFT JOIN exam_questions eq ON eq.exam_set_id = es.id
            {where_conditions}
            GROUP BY es.id, d.name
            ORDER BY es.created_at DESC
            LIMIT %s OFFSET %s
        """, query_params + (PER_PAGE, offset))

        sets = cur.fetchall()

        # --------------------------------------------------
        # 4. PAGINATION DISPLAY INFORMATION
        # --------------------------------------------------
        start_record = (
            (page - 1) * PER_PAGE + 1
            if total_records > 0
            else 0
        )

        end_record = min(
            page * PER_PAGE,
            total_records
        )

        return render_template(
            "exams/manage_questions.html",
            exam_sets=sets,

            # Existing search
            search=search,

            # Pagination
            page=page,
            per_page=PER_PAGE,
            total_records=total_records,
            total_pages=total_pages,
            start_record=start_record,
            end_record=end_record,
            has_prev=page > 1,
            has_next=page < total_pages,
            prev_page=page - 1,
            next_page=page + 1
        )

    finally:
        conn.close()


@exam_bp.route("/exams/create", methods=["GET", "POST"])
@super_admin_required
def exam_create():
    denied = _require_exam_role()
    if denied:
        return denied

    conn = _db()

    try:
        cur = _dict_cursor(conn)
        dept_scope = _scope_department()

        # -------------------------------------------------
        # LOAD UNIQUE DEPARTMENT NAMES
        # Same department name across different branches
        # appears once.
        # -------------------------------------------------

        if dept_scope:
            cur.execute("""
                SELECT
                    MIN(d.id) AS id,
                    MIN(d.name) AS name
                FROM departments d
                JOIN departments own
                    ON LOWER(TRIM(d.name)) = LOWER(TRIM(own.name))
                WHERE d.is_active = TRUE
                  AND own.id = %s
                  AND own.is_active = TRUE
                GROUP BY LOWER(TRIM(d.name))
                ORDER BY MIN(d.name)
            """, (dept_scope,))
        else:
            cur.execute("""
                SELECT
                    MIN(id) AS id,
                    MIN(name) AS name
                FROM departments
                WHERE is_active = TRUE
                GROUP BY LOWER(TRIM(name))
                ORDER BY MIN(name)
            """)

        departments = cur.fetchall()

        if request.method == "POST":

            selected_department_id = request.form.get(
                "department_id", type=int
            )

            sub_department = (
                request.form.get("sub_department") or ""
            ).strip()

            title = (
                request.form.get("exam_title") or ""
            ).strip()

            description = (
                request.form.get("description") or ""
            ).strip()

            questions_raw = request.form.get(
                "questions_json", "[]"
            )

            try:
                questions = json.loads(questions_raw)
            except (ValueError, TypeError):
                questions = None

            # -------------------------------------------------
            # GET SELECTED DEPARTMENT
            # -------------------------------------------------

            selected_department = None

            if selected_department_id:
                cur.execute("""
                    SELECT id, name
                    FROM departments
                    WHERE id = %s
                      AND is_active = TRUE
                """, (selected_department_id,))

                selected_department = cur.fetchone()

            # -------------------------------------------------
            # BASIC VALIDATION
            # -------------------------------------------------

            if not selected_department_id or not sub_department or not title:
                flash(
                    "Department, sub department and exam title are required.",
                    "error"
                )

            elif not selected_department:
                flash(
                    "Invalid department selected.",
                    "error"
                )

            elif len(sub_department) > 150:
                flash(
                    "Sub Department must not exceed 150 characters.",
                    "error"
                )

            elif not any(char.isalnum() for char in sub_department):
                flash(
                    "Sub Department must contain at least one letter or number.",
                    "error"
                )

            elif len(title) > 150:
                flash(
                    "Exam title must not exceed 150 characters.",
                    "error"
                )

            elif not isinstance(questions, list) or not questions:
                flash(
                    "Please add at least one valid question.",
                    "error"
                )

            else:

                # -------------------------------------------------
                # RESOLVE CANONICAL DEPARTMENT
                # Same department name across branches is one
                # shared Question Bank department.
                # -------------------------------------------------

                cur.execute("""
                    SELECT
                        MIN(d.id) AS id,
                        MIN(d.name) AS name
                    FROM departments d
                    WHERE d.is_active = TRUE
                      AND LOWER(TRIM(d.name)) = (
                          SELECT LOWER(TRIM(name))
                          FROM departments
                          WHERE id = %s
                            AND is_active = TRUE
                      )
                    GROUP BY LOWER(TRIM(d.name))
                """, (selected_department_id,))

                department = cur.fetchone()

                if not department:
                    flash(
                        "Invalid department selected.",
                        "error"
                    )

                else:

                    # -------------------------------------------------
                    # DEPARTMENT HEAD PERMISSION VALIDATION
                    # -------------------------------------------------

                    if dept_scope:
                        cur.execute("""
                            SELECT name
                            FROM departments
                            WHERE id = %s
                              AND is_active = TRUE
                        """, (dept_scope,))

                        own_department = cur.fetchone()

                        if (
                            not own_department
                            or own_department["name"].strip().casefold()
                            != department["name"].strip().casefold()
                        ):
                            abort(403)

                    # -------------------------------------------------
                    # DUPLICATE QUESTION BANK VALIDATION
                    #
                    # Department + Sub Department combination
                    # must be unique.
                    #
                    # Exam Title is NOT considered.
                    #
                    # Example:
                    # IT + Software = one Question Bank only.
                    #
                    # Same Department name across branches
                    # is treated as one department.
                    # -------------------------------------------------

                    cur.execute("""
                        SELECT EXISTS (
                            SELECT 1
                            FROM exam_sets es
                            JOIN departments d
                                ON d.id = es.department_id
                            WHERE es.deleted_at IS NULL
                            AND LOWER(TRIM(d.name)) =
                                LOWER(TRIM(%s))
                            AND LOWER(TRIM(es.sub_department)) =
                                LOWER(TRIM(%s))
                        ) AS already_exists
                    """, (
                        department["name"],
                        sub_department
                    ))

                    duplicate = cur.fetchone()

                    if duplicate and duplicate["already_exists"]:

                        flash(
                            f'Question Bank already exists for '
                            f'Department "{department["name"]}" '
                            f'and Sub Department "{sub_department}".',
                            "error"
                        )

                    else:

                        # -------------------------------------------------
                        # QUESTION VALIDATION
                        # -------------------------------------------------

                        cleaned = []
                        invalid = False

                        for q in questions:

                            if not isinstance(q, dict):
                                invalid = True
                                break

                            question_text = (
                                q.get("question") or ""
                            ).strip()

                            options = q.get("options") or []
                            correct = q.get("correct_option")

                            if (
                                not question_text
                                or not isinstance(options, list)
                                or len(options) != 4
                                or any(
                                    not str(x).strip()
                                    for x in options
                                )
                                or not isinstance(correct, int)
                                or isinstance(correct, bool)
                                or correct not in range(4)
                            ):
                                invalid = True
                                break

                            cleaned.append({
                                "question": question_text,
                                "options": [
                                    {
                                        "label": "ABCD"[i],
                                        "text": str(options[i]).strip()
                                    }
                                    for i in range(4)
                                ],
                                "correct_option": correct,
                                "points": 1
                            })

                        if invalid:

                            flash(
                                "Each question needs text, four options and one correct answer.",
                                "error"
                            )

                        else:

                            # -------------------------------------------------
                            # CREATE EXAM SET
                            # -------------------------------------------------

                            cur.execute("""
                                INSERT INTO exam_sets
                                    (
                                        department_id,
                                        sub_department,
                                        exam_title,
                                        description,
                                        created_by
                                    )
                                VALUES (%s, %s, %s, %s, %s)
                                RETURNING id
                            """, (
                                department["id"],
                                sub_department,
                                title,
                                description,
                                session.get("user_id")
                            ))

                            exam_set_id = cur.fetchone()["id"]

                            # -------------------------------------------------
                            # INSERT QUESTIONS
                            # -------------------------------------------------

                            for q in cleaned:
                                cur.execute("""
                                    INSERT INTO exam_questions
                                        (
                                            exam_set_id,
                                            question_text,
                                            options,
                                            correct_option,
                                            points
                                        )
                                    VALUES (%s, %s, %s, %s, %s)
                                """, (
                                    exam_set_id,
                                    q["question"],
                                    Json(q["options"]),
                                    q["correct_option"],
                                    q["points"]
                                ))

                            # -------------------------------------------------
                            # COMMIT
                            # -------------------------------------------------

                            conn.commit()

                            flash(
                                "Exam and questions created successfully.",
                                "success"
                            )

                            return redirect(
                                url_for("exam.exam_list")
                            )

        return render_template(
            "exams/create_questions.html",
            departments=departments
        )

    except Exception:

        conn.rollback()

        current_app.logger.exception(
            "Exam creation failed"
        )

        flash(
            "Unable to create exam. Please check the entered information.",
            "error"
        )

        return redirect(
            url_for("exam.exam_create")
        )

    finally:
        conn.close()


@exam_bp.route("/exams/<int:exam_set_id>/edit", methods=["GET", "POST"])
@super_admin_required
def exam_edit(exam_set_id):
    user_id = session.get("user_id")
    denied = _require_exam_role()
    if denied:
        return denied

    conn = _db()

    try:
        cur = _dict_cursor(conn)
        dept_scope = _scope_department()

        cur.execute("""
            SELECT *
            FROM exam_sets
            WHERE id = %s
                AND deleted_at IS NULL
                AND (%s IS NULL OR department_id = %s)
        """, (exam_set_id, dept_scope, dept_scope))

        exam_set = cur.fetchone()

        if not exam_set:
            abort(404)

        if request.method == "POST":
            title = (request.form.get("exam_title") or "").strip()
            sub_department = (
                request.form.get("sub_department") or ""
            ).strip()
            description = (
                request.form.get("description") or ""
            ).strip()

            try:
                questions = json.loads(
                    request.form.get("questions_json", "[]")
                )
            except (ValueError, TypeError):
                questions = None

            if not title or not sub_department:
                flash("Exam title and category are required.", "error")
                return redirect(
                    url_for("exam.exam_edit", exam_set_id=exam_set_id)
                )

            if not isinstance(questions, list) or not questions:
                flash("Please add at least one question.", "error")
                return redirect(
                    url_for("exam.exam_edit", exam_set_id=exam_set_id)
                )

            cleaned = []

            for q in questions:
                if not isinstance(q, dict):
                    abort(400)

                question_text = (q.get("question") or "").strip()
                options = q.get("options") or []
                correct = q.get("correct_option")

                if (
                    not question_text
                    or len(options) != 4
                    or any(not str(x).strip() for x in options)
                    or not isinstance(correct, int)
                    or correct not in range(4)
                ):
                    flash(
                        "Each question needs four options and one correct answer.",
                        "error"
                    )
                    return redirect(
                        url_for("exam.exam_edit", exam_set_id=exam_set_id)
                    )

                cleaned.append({
                    "question": question_text,
                    "options": [
                        {
                            "label": "ABCD"[i],
                            "text": str(options[i]).strip()
                        }
                        for i in range(4)
                    ],
                    "correct_option": correct,
                    "points": 1
                })

            cur.execute("""
                UPDATE exam_sets
                SET exam_title = %s,
                    sub_department = %s,
                    description = %s,
                    updated_by = %s,
                    updated_at = NOW()
                WHERE id = %s
                AND deleted_at IS NULL
            """, (
                title,
                sub_department,
                description,
                user_id,
                exam_set_id
            ))

            # Replace question bank questions.
            # Existing assignments keep their question_snapshot.
            cur.execute("""
                DELETE FROM exam_questions
                WHERE exam_set_id = %s
            """, (exam_set_id,))

            for q in cleaned:
                cur.execute("""
                    INSERT INTO exam_questions
                        (exam_set_id, question_text, options,
                         correct_option, points)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    exam_set_id,
                    q["question"],
                    Json(q["options"]),
                    q["correct_option"],
                    q["points"]
                ))

            conn.commit()

            flash("Question bank updated successfully.", "success")
            return redirect(url_for("exam.exam_list"))

        cur.execute("""
            SELECT id, question_text, options, correct_option, points
            FROM exam_questions
            WHERE exam_set_id = %s
            ORDER BY id
        """, (exam_set_id,))

        questions = cur.fetchall()

        for q in questions:
            q["options"] = _json_value(q["options"])

        if dept_scope:
            cur.execute("""
                SELECT id, name
                FROM departments
                WHERE id = %s AND is_active = TRUE
            """, (dept_scope,))
        else:
            cur.execute("""
                SELECT id, name
                FROM departments
                WHERE is_active = TRUE
                ORDER BY name
            """)

        departments = cur.fetchall()

        return render_template(
            "exams/edit_questions.html",
            exam_set=exam_set,
            questions=questions,
            departments=departments
        )

    except Exception:
        conn.rollback()
        current_app.logger.exception("Question bank update failed")
        flash("Unable to update question bank.", "error")
        return redirect(url_for("exam.exam_list"))

    finally:
        conn.close()


@exam_bp.route("/exams/<int:exam_set_id>/delete", methods=["POST"])
@super_admin_required
def exam_delete(exam_set_id):
    denied = _require_exam_role()
    if denied:
        return denied

    conn = _db()

    try:
        cur = _dict_cursor(conn)
        dept_scope = _scope_department()

        cur.execute("""
            SELECT id
            FROM public.exam_sets
            WHERE id = %s
              AND deleted_at IS NULL
              AND (%s IS NULL OR department_id = %s)
            FOR UPDATE
        """, (exam_set_id, dept_scope, dept_scope))

        if not cur.fetchone():
            abort(404)

        cur.execute("""
            SELECT COUNT(*) AS total
            FROM public.exam_assignments
            WHERE exam_set_id = %s
        """, (exam_set_id,))

        if cur.fetchone()["total"] > 0:
            conn.rollback()
            flash(
                "This question bank cannot be deleted because it has exam assignments.",
                "error"
            )
            return redirect(url_for("exam.exam_list"))

        # Soft delete exam set and record deleted user.
        # Keep exam_questions in the database.
        cur.execute("""
            UPDATE public.exam_sets
            SET
                deleted_at = NOW(),
                deleted_by = %s,
                updated_at = NOW()
            WHERE id = %s
              AND deleted_at IS NULL
              AND (%s IS NULL OR department_id = %s)
        """, (
            session.get("user_id"),
            exam_set_id,
            dept_scope,
            dept_scope
        ))

        if cur.rowcount == 0:
            conn.rollback()
            abort(404)

        conn.commit()

        flash("Question bank deleted successfully.", "success")

    except HTTPException:
        conn.rollback()
        raise

    except Exception:
        conn.rollback()
        current_app.logger.exception(
            "Question bank soft deletion failed"
        )
        flash("Unable to delete question bank.", "error")

    finally:
        conn.close()

    return redirect(url_for("exam.exam_list"))


@exam_bp.route("/exams/<int:exam_set_id>/toggle", methods=["POST"])
def exam_toggle(exam_set_id):
    denied = _require_exam_role()
    if denied:
        return denied

    conn = _db()

    try:
        cur = _dict_cursor(conn)
        dept_scope = _scope_department()

        cur.execute("""
            UPDATE exam_sets
            SET is_active = NOT is_active,
                updated_at = NOW()
            WHERE id = %s
              AND deleted_at IS NULL
              AND (%s IS NULL OR department_id = %s)
        """, (exam_set_id, dept_scope, dept_scope))

        if cur.rowcount == 0:
            abort(404)

        conn.commit()
        flash("Exam status updated.", "success")

    except Exception:
        conn.rollback()
        current_app.logger.exception("Exam status update failed")
        flash("Unable to update exam status.", "error")

    finally:
        conn.close()

    return redirect(url_for("exam.exam_list"))

@exam_bp.route("/exams/assign", methods=["GET", "POST"])
def exam_assign():
    denied = _require_exam_role()
    if denied:
        return denied

    conn = _db()

    try:
        cur = _dict_cursor(conn)
        dept_scope = _scope_department()

        # -----------------------------------------
        # LOAD TODAY'S SCHEDULED INTERVIEW CANDIDATES
        # -----------------------------------------

        cur.execute("""
            SELECT
                ci.id AS interview_id,
                ci.candidate_id,
                ci.interview_date,
                ci.interview_time,
                ci.interview_stage,

                c.name,
                c.email,
                c.phone,
                c.position,
                c.department_id,
                c.branch_id,

                d.name AS department_name

            FROM candidate_interviews ci

            JOIN candidates c
                ON c.id = ci.candidate_id

            JOIN departments d
                ON d.id = c.department_id

            WHERE c.deleted_at IS NULL

              AND ci.interview_date =
                  (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Yangon')::date

              AND ci.status = 'scheduled'

              AND (%s IS NULL OR c.department_id = %s)

              AND NOT EXISTS (
                  SELECT 1
                  FROM exam_assignments ea
                  WHERE ea.interview_id = ci.id
                    AND ea.status IN (
                        'ready',
                        'in_progress',
                        'submitted'
                    )
              )

            ORDER BY
                ci.interview_time ASC,
                c.name ASC
        """, (dept_scope, dept_scope))

        candidates = cur.fetchall()

        # =========================================
        # POST REQUEST
        # =========================================

        if request.method == "POST":

            interview_id = request.form.get(
                "interview_id",
                type=int
            )

            exam_set_id = request.form.get(
                "exam_set_id",
                type=int
            )

            count = request.form.get(
                "question_count",
                type=int
            )

            selection = request.form.get("selection_method")
            method = request.form.get("exam_method")

            selected_ids = request.form.getlist(
                "question_ids",
                type=int
            )

            # -----------------------------------------
            # BASIC VALIDATION
            # -----------------------------------------

            if (
                not interview_id
                or not exam_set_id
                or not count
                or count < 1
            ):
                flash(
                    "Please complete all exam assignment fields.",
                    "error"
                )

                return redirect(
                    url_for("exam.exam_assign")
                )

            if (
                selection not in {"random", "manual"}
                or method not in {"direct", "qr"}
            ):
                flash(
                    "Invalid exam selection method.",
                    "error"
                )

                return redirect(
                    url_for("exam.exam_assign")
                )

            # -----------------------------------------
            # VALIDATE INTERVIEW
            # -----------------------------------------

            cur.execute("""
                SELECT
                    ci.id,
                    ci.candidate_id,
                    c.department_id

                FROM candidate_interviews ci

                JOIN candidates c
                    ON c.id = ci.candidate_id

                WHERE ci.id = %s

                  AND c.deleted_at IS NULL

                  AND ci.interview_date =
                      (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Yangon')::date

                  AND ci.status = 'scheduled'

                  AND (%s IS NULL OR c.department_id = %s)

                FOR UPDATE OF ci
            """, (
                interview_id,
                dept_scope,
                dept_scope
            ))

            interview = cur.fetchone()

            if not interview:
                flash(
                    "Selected interview is unavailable or no longer valid.",
                    "error"
                )

                return redirect(
                    url_for("exam.exam_assign")
                )

            # -----------------------------------------
            # PREVENT DUPLICATE ACTIVE ASSIGNMENT
            # -----------------------------------------

            cur.execute("""
                SELECT id
                FROM exam_assignments
                WHERE interview_id = %s
                  AND status IN (
                      'ready',
                      'in_progress',
                      'submitted'
                  )
                LIMIT 1
            """, (interview_id,))

            existing_assignment = cur.fetchone()

            if existing_assignment:
                flash(
                    "An exam has already been assigned to this interview.",
                    "error"
                )

                return redirect(
                    url_for("exam.exam_assign")
                )

            # -----------------------------------------
            # VALIDATE EXAM DEPARTMENT
            # -----------------------------------------

            cur.execute("""
                SELECT es.*

                FROM exam_sets es

                JOIN departments exam_dept
                    ON exam_dept.id = es.department_id

                JOIN departments candidate_dept
                    ON candidate_dept.id = %s

                WHERE es.id = %s

                  AND es.deleted_at IS NULL

                  AND es.is_active = TRUE

                  AND LOWER(TRIM(exam_dept.name))
                      = LOWER(TRIM(candidate_dept.name))

                  AND (
                      %s IS NULL
                      OR es.department_id = %s
                  )

            """, (
                interview["department_id"],
                exam_set_id,
                dept_scope,
                dept_scope
            ))

            exam_set = cur.fetchone()

            if not exam_set:
                flash(
                    "Selected exam does not belong to this candidate's department or is no longer available.",
                    "error"
                )

                return redirect(
                    url_for("exam.exam_assign")
                )

            # -----------------------------------------
            # LOAD QUESTION BANK
            # -----------------------------------------

            cur.execute("""
                SELECT
                    id,
                    question_text,
                    options,
                    correct_option,
                    points

                FROM exam_questions

                WHERE exam_set_id = %s

                ORDER BY id
            """, (exam_set_id,))

            bank = cur.fetchall()

            if count > len(bank):
                flash(
                    "Question count exceeds the available question bank.",
                    "error"
                )

                return redirect(
                    url_for("exam.exam_assign")
                )

            # -----------------------------------------
            # VALIDATE PREVIEW QUESTION IDS
            # -----------------------------------------

            if (
                len(selected_ids) != count
                or len(set(selected_ids)) != count
            ):
                flash(
                    "Please select exactly the requested number of questions.",
                    "error"
                )

                return redirect(
                    url_for("exam.exam_assign")
                )

            by_id = {
                q["id"]: q
                for q in bank
            }

            if any(qid not in by_id for qid in selected_ids):
                flash(
                    "Invalid question selection. Please try again.",
                    "error"
                )

                return redirect(
                    url_for("exam.exam_assign")
                )

            # -----------------------------------------
            # USE EXACT PREVIEWED QUESTIONS
            # -----------------------------------------

            chosen = [
                by_id[qid]
                for qid in selected_ids
            ]

            # -----------------------------------------
            # CREATE QUESTION SNAPSHOT
            # -----------------------------------------

            snapshot = []

            for q in chosen:
                snapshot.append({
                    "question_id": q["id"],
                    "question": q["question_text"],
                    "options": _json_value(q["options"]),
                    "correct_option": q["correct_option"],
                    "points": float(q["points"])
                })

            # -----------------------------------------
            # QR TOKEN
            # -----------------------------------------

            raw_token = (
                secrets.token_urlsafe(32)
                if method == "qr"
                else None
            )

            token_hash = (
                _hash_token(raw_token)
                if raw_token
                else None
            )

            link_expiry = (
                _now() + timedelta(
                    minutes=LINK_LIFETIME_MINUTES
                )
                if raw_token
                else None
            )

            # -----------------------------------------
            # SAVE EXAM ASSIGNMENT
            # -----------------------------------------

            cur.execute("""
                INSERT INTO exam_assignments
                (
                    interview_id,
                    candidate_id,
                    exam_set_id,
                    selection_method,
                    exam_method,
                    question_snapshot,
                    token_hash,
                    link_expires_at,
                    total_questions,
                    created_by
                )

                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )

                RETURNING id

            """, (
                interview_id,
                interview["candidate_id"],
                exam_set_id,
                selection,
                method,
                Json(snapshot),
                token_hash,
                link_expiry,
                len(snapshot),
                session.get("user_id")
            ))

            assignment_id = cur.fetchone()["id"]

            conn.commit()

            # -----------------------------------------
            # REDIRECT
            # -----------------------------------------

            if method == "qr":
                return redirect(
                    url_for(
                        "exam.exam_qr",
                        assignment_id=assignment_id,
                        token=raw_token
                    )
                )

            return redirect(
                url_for(
                    "exam.exam_preview",
                    assignment_id=assignment_id
                )
            )

        # =========================================
        # GET REQUEST
        # =========================================

        exam_sets = _load_exam_sets(
            cur,
            dept_scope
        )

        return render_template(
            "exams/assign_exam.html",
            candidates=candidates,
            exam_sets=exam_sets
        )

    except HTTPException:
        conn.rollback()
        raise

    except Exception:
        conn.rollback()

        current_app.logger.exception(
            "Exam assignment failed"
        )

        flash(
            "Unable to assign exam. Please try again.",
            "error"
        )

        return redirect(
            url_for("exam.exam_assign")
        )

    finally:
        conn.close()

@exam_bp.route("/exams/questions")
def exam_questions_api():
    denied = _require_exam_role()
    if denied:
        return jsonify({"error": "Unauthorized"}), 401
    exam_set_id = request.args.get("exam_set_id", type=int)
    if not exam_set_id:
        return jsonify({"error": "Missing exam_set_id"}), 400
    conn = _db()
    try:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT es.department_id FROM exam_sets es
            WHERE es.id=%s
                AND es.deleted_at IS NULL
                AND (%s IS NULL OR es.department_id=%s)
        """, (exam_set_id, _scope_department(), _scope_department()))
        if not cur.fetchone():
            return jsonify({"error": "Exam not found"}), 404
        cur.execute("""
            SELECT id, question_text FROM exam_questions
            WHERE exam_set_id=%s ORDER BY id
        """, (exam_set_id,))
        return jsonify({"questions": cur.fetchall()})
    finally:
        conn.close()


@exam_bp.route("/exams/assignments/<int:assignment_id>/preview")
def exam_preview(assignment_id):
    denied = _require_exam_role()
    if denied:
        return denied
    conn = _db()
    try:
        cur = _dict_cursor(conn)
        assignment = _get_assignment(cur, assignment_id)
        if not assignment:
            abort(404)
        if _scope_department():
            cur.execute("SELECT department_id FROM candidates WHERE id=%s",
                        (assignment["candidate_id"],))
            row = cur.fetchone()
            if not row or row["department_id"] != _scope_department():
                abort(403)
        snapshot = _json_value(assignment["question_snapshot"])
        return render_template("exams/exam_preview.html",
                               assignment=assignment, questions=snapshot)
    finally:
        conn.close()


@exam_bp.route("/exams/assignments/<int:assignment_id>/qr")
def exam_qr(assignment_id):
    denied = _require_exam_role()
    if denied:
        return denied
    token = request.args.get("token", "")
    conn = _db()
    try:
        cur = _dict_cursor(conn)
        assignment = _get_assignment(cur, assignment_id)
        if not assignment or assignment["exam_method"] != "qr":
            abort(404)
        
        # ✅ Department Head Permission Check
        if _scope_department():
            cur.execute("SELECT department_id FROM candidates WHERE id=%s",
                        (assignment["candidate_id"],))
            row = cur.fetchone()
            if not row or row["department_id"] != _scope_department():
                abort(403)

        if not token or not secrets.compare_digest(
            _hash_token(token), assignment["token_hash"] or ""
        ):
            abort(403)

        public_url = url_for("exam.public_exam", token=token, _external=True)
        expires_at = _as_utc(assignment.get("link_expires_at"))
        qr_filename = f"exam_{assignment_id}.png"
        qr_path = os.path.join(current_app.root_path, "static", "exam_qr", qr_filename)
        os.makedirs(os.path.dirname(qr_path), exist_ok=True)
        try:
            import qrcode
            qrcode.make(public_url).save(qr_path)
            qr_url = url_for("static", filename=f"exam_qr/{qr_filename}")
        except ImportError:
            qr_url = None
            flash("Install qrcode[pil] to generate QR images. The direct link is available.", "warning")

        return render_template(
            "exams/assign_exam.html",
            assignment=assignment,
            qr_url=qr_url,
            public_url=public_url,
            expires_at=expires_at,
            page_mode="qr"
        )
    finally:
        conn.close()


@exam_bp.route("/exam/<token>", methods=["GET", "POST"])
def public_exam(token):
    conn = _db()
    try:
        cur = _dict_cursor(conn)
        assignment = _get_assignment_by_token(cur, token)
        if not assignment or assignment["exam_method"] != "qr":
            abort(404)
        if assignment["status"] in ("submitted", "expired"):
            return render_template("public_exam/exam_finished.html",
                                   assignment=assignment)
        if assignment["status"] == "in_progress":
            return redirect(url_for("exam.public_exam_take", token=token))
        if _now() >= _as_utc(assignment["link_expires_at"]) and assignment["status"] == "ready":
            cur.execute("""
                UPDATE exam_assignments SET status='expired', updated_at=NOW()
                WHERE id=%s AND status='ready'
            """, (assignment["id"],))
            conn.commit()
            return render_template("public_exam/exam_expired.html", assignment=assignment)

        if request.method == "POST":
            if assignment["status"] != "ready":
                return redirect(url_for("exam.public_exam", token=token))
            cur.execute("""
                UPDATE exam_assignments
                SET status='in_progress', started_at=NOW(),
                    exam_expires_at=NOW() + INTERVAL '30 minutes',
                    updated_at=NOW()
                WHERE id=%s AND status='ready'
                RETURNING id
            """, (assignment["id"],))
            if not cur.fetchone():
                conn.rollback()
                return redirect(url_for("exam.public_exam", token=token))
            conn.commit()
            return redirect(url_for("exam.public_exam_take", token=token))
        return render_template("public_exam/exam_entry.html", assignment=assignment)
    finally:
        conn.close()


@exam_bp.route("/exam/<token>/take")
def public_exam_take(token):
    conn = _db()
    try:
        cur = _dict_cursor(conn)
        assignment = _get_assignment_by_token(cur, token)
        if not assignment:
            abort(404)
        if assignment["status"] == "ready":
            return redirect(url_for("exam.public_exam", token=token))
        if assignment["status"] in ("submitted", "expired"):
            return render_template("public_exam/exam_finished.html", assignment=assignment)
        if _assignment_expired(assignment):
            _finalize_assignment(conn, cur, assignment)
            conn.commit()
            return render_template("public_exam/exam_finished.html", assignment=assignment)
        questions = _json_value(assignment["question_snapshot"])
        # Never send correct_option to candidate browser.
        safe_questions = [
            {"question": q["question"],
             "options": q["options"],
             "points": q.get("points", 1)}
            for q in questions
        ]
        answers = _json_value(assignment["answers"]) or {}
        return render_template("public_exam/exam_take.html",
                               assignment=assignment,
                               questions=safe_questions,
                               answers=answers,
                               token=token,
                               expires_at=_as_utc(assignment["exam_expires_at"]).isoformat())
    finally:
        conn.close()


@exam_bp.route("/exam/<token>/answer", methods=["POST"])
def public_exam_save_answer(token):
    conn = _db()
    try:
        cur = _dict_cursor(conn)
        assignment = _get_assignment_by_token(cur, token)
        if not assignment or assignment["status"] != "in_progress":
            return jsonify({"ok": False, "error": "Exam is not active."}), 403
        if _assignment_expired(assignment):
            _finalize_assignment(conn, cur, assignment)
            conn.commit()
            return jsonify({"ok": False, "error": "Time is over."}), 410

        payload = request.get_json(silent=True) or {}
        index = payload.get("question_index")
        selected = payload.get("selected_option")
        snapshot = _json_value(assignment["question_snapshot"]) or []
        if not isinstance(index, int) or index < 0 or index >= len(snapshot):
            return jsonify({"ok": False, "error": "Invalid question."}), 400
        if not isinstance(selected, int) or selected not in range(4):
            return jsonify({"ok": False, "error": "Invalid option."}), 400
        answers = _json_value(assignment["answers"]) or {}
        answers[str(index)] = selected
        cur.execute("""
            UPDATE exam_assignments
            SET answers=%s, updated_at=NOW()
            WHERE id=%s AND status='in_progress'
        """, (Json(answers), assignment["id"]))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@exam_bp.route("/exam/<token>/submit", methods=["POST"])
def public_exam_submit(token):
    conn = _db()
    try:
        cur = _dict_cursor(conn)
        assignment = _get_assignment_by_token(cur, token)
        if not assignment:
            abort(404)
        if assignment["status"] == "in_progress":
            _finalize_assignment(conn, cur, assignment)
            conn.commit()
        return render_template("public_exam/exam_finished.html",
                               assignment=_get_assignment_by_token(cur, token))
    finally:
        conn.close()


@exam_bp.route("/exams/direct/<int:assignment_id>", methods=["GET", "POST"])
def direct_exam(assignment_id):
    denied = _require_exam_role()
    if denied:
        return denied
    conn = _db()
    try:
        cur = _dict_cursor(conn)
        assignment = _get_assignment(cur, assignment_id)
        if not assignment or assignment["exam_method"] != "direct":
            abort(404)
        
        # ✅ Department Head Permission Check
        if _scope_department():
            cur.execute("SELECT department_id FROM candidates WHERE id=%s",
                        (assignment["candidate_id"],))
            row = cur.fetchone()
            if not row or row["department_id"] != _scope_department():
                abort(403)

        if request.method == "POST" and assignment["status"] == "ready":
            cur.execute("""
                UPDATE exam_assignments
                SET status='in_progress', started_at=NOW(),
                    exam_expires_at=NOW() + INTERVAL '30 minutes',
                    updated_at=NOW()
                WHERE id=%s AND status='ready'
            """, (assignment_id,))
            conn.commit()
            return redirect(url_for("exam.direct_exam", assignment_id=assignment_id))
        if assignment["status"] == "ready":
            return render_template("public_exam/exam_entry.html",
                                   assignment=assignment,
                                   direct_url=url_for("exam.direct_exam", assignment_id=assignment_id))
        if assignment["status"] in ("submitted", "expired"):
            return render_template("public_exam/exam_finished.html", assignment=assignment)
        if _assignment_expired(assignment):
            _finalize_assignment(conn, cur, assignment)
            conn.commit()
            return render_template("public_exam/exam_finished.html", assignment=assignment)

        snapshot = _json_value(assignment["question_snapshot"])
        safe_questions = [{"question": q["question"], "options": q["options"],
                           "points": q.get("points", 1)} for q in snapshot]
        return render_template("public_exam/exam_take.html",
                               assignment=assignment, questions=safe_questions,
                               answers=_json_value(assignment["answers"]) or {},
                               token=None, direct_assignment_id=assignment_id,
                               expires_at=_as_utc(assignment["exam_expires_at"]).isoformat())
    finally:
        conn.close()


@exam_bp.route("/exams/direct/<int:assignment_id>/answer", methods=["POST"])
def direct_exam_save_answer(assignment_id):
    denied = _require_exam_role()
    if denied:
        return denied
    return _save_direct_answer(assignment_id)


def _save_direct_answer(assignment_id):
    conn = _db()
    try:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM exam_assignments WHERE id=%s", (assignment_id,))
        assignment = cur.fetchone()
        if not assignment or assignment["status"] != "in_progress":
            return jsonify({"ok": False, "error": "Exam is not active."}), 403
        if _assignment_expired(assignment):
            _finalize_assignment(conn, cur, assignment)
            conn.commit()
            return jsonify({"ok": False, "error": "Time is over."}), 410

        # ✅ Department Head Permission Check
        if _scope_department():
            cur.execute("SELECT department_id FROM candidates WHERE id=%s",
                        (assignment["candidate_id"],))
            row = cur.fetchone()
            if not row or row["department_id"] != _scope_department():
                return jsonify({"ok": False, "error": "Unauthorized."}), 403

        payload = request.get_json(silent=True) or {}
        index, selected = payload.get("question_index"), payload.get("selected_option")
        snapshot = _json_value(assignment["question_snapshot"]) or []
        if not isinstance(index, int) or not 0 <= index < len(snapshot):
            return jsonify({"ok": False, "error": "Invalid question."}), 400
        if not isinstance(selected, int) or selected not in range(4):
            return jsonify({"ok": False, "error": "Invalid option."}), 400
        answers = _json_value(assignment["answers"]) or {}
        answers[str(index)] = selected
        cur.execute("UPDATE exam_assignments SET answers=%s, updated_at=NOW() WHERE id=%s",
                    (Json(answers), assignment_id))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@exam_bp.route("/exams/direct/<int:assignment_id>/submit", methods=["POST"])
def direct_exam_submit(assignment_id):
    denied = _require_exam_role()
    if denied:
        return denied
    conn = _db()
    try:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM exam_assignments WHERE id=%s", (assignment_id,))
        assignment = cur.fetchone()
        if not assignment:
            abort(404)

        # ✅ Department Head Permission Check
        if _scope_department():
            cur.execute("SELECT department_id FROM candidates WHERE id=%s",
                        (assignment["candidate_id"],))
            row = cur.fetchone()
            if not row or row["department_id"] != _scope_department():
                abort(403)

        if assignment["status"] == "in_progress":
            _finalize_assignment(conn, cur, assignment)
            conn.commit()
        return redirect(url_for("exam.exam_results"))
    finally:
        conn.close()



@exam_bp.route("/exams/results")
def exam_results():

    denied = _require_exam_role()
    if denied:
        return denied

    search = request.args.get("search", "").strip()
    selected_status = request.args.get("status", "").strip()

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    per_page = 10

    conn = _db()

    try:
        cur = _dict_cursor(conn)

        dept_scope = _scope_department()

        # -------------------------------------------------
        # COMMON FILTER
        # -------------------------------------------------

        where_sql = """
            WHERE (%s IS NULL OR c.department_id = %s)

              AND (
                    %s = ''
                    OR CONCAT_WS(
                        ' ',
                        ea.id::text,
                        c.name,
                        COALESCE(c.position, ''),
                        COALESCE(b.name, ''),
                        COALESCE(d.name, ''),
                        COALESCE(es.exam_title, ''),
                        COALESCE(es.sub_department, ''),
                        COALESCE(ea.exam_method, ''),
                        COALESCE(ea.status, '')
                    ) ILIKE %s
              )

              AND (
                    %s = ''
                    OR ea.status = %s
              )
        """

        filter_params = (
            dept_scope,
            dept_scope,
            search,
            f"%{search}%",
            selected_status,
            selected_status
        )

        # -------------------------------------------------
        # TOTAL RECORDS + STATISTICS
        # -------------------------------------------------

        cur.execute(
            f"""
            SELECT
                COUNT(*) AS total_records,

                COUNT(*) FILTER (
                    WHERE ea.status = 'submitted'
                ) AS completed,

                COUNT(*) FILTER (
                    WHERE ea.status = 'in_progress'
                ) AS in_progress,

                COUNT(*) FILTER (
                    WHERE ea.status = 'ready'
                ) AS ready,

                COUNT(*) FILTER (
                    WHERE ea.status = 'expired'
                ) AS expired

            FROM public.exam_assignments ea

            INNER JOIN public.candidates c
                ON c.id = ea.candidate_id

            INNER JOIN public.departments d
                ON d.id = c.department_id

            LEFT JOIN public.branches b
                ON b.id = c.branch_id

            INNER JOIN public.exam_sets es
                ON es.id = ea.exam_set_id

            {where_sql}
            """,
            filter_params
        )

        stats = cur.fetchone()

        total_records = stats["total_records"] or 0

        total_pages = max(
            1,
            (total_records + per_page - 1) // per_page
        )

        page = min(page, total_pages)

        offset = (page - 1) * per_page

        # -------------------------------------------------
        # FETCH PAGINATED RESULTS
        # -------------------------------------------------

        cur.execute(
            f"""
            SELECT
                ea.*,

                c.name AS candidate_name,
                c.position,

                d.name AS department_name,

                b.name AS branch_name,

                es.exam_title,
                es.sub_department

            FROM public.exam_assignments ea

            INNER JOIN public.candidates c
                ON c.id = ea.candidate_id

            INNER JOIN public.departments d
                ON d.id = c.department_id

            LEFT JOIN public.branches b
                ON b.id = c.branch_id

            INNER JOIN public.exam_sets es
                ON es.id = ea.exam_set_id

            {where_sql}

            ORDER BY ea.created_at DESC, ea.id DESC

            LIMIT %s OFFSET %s
            """,
            filter_params + (per_page, offset)
        )

        results = cur.fetchall()

        return render_template(
            "exams/exam_results.html",
            results=results,
            search=search,
            selected_status=selected_status,
            page=page,
            per_page=per_page,
            total_pages=total_pages,
            total_records=total_records,
            stats=stats
        )

    finally:
        conn.close()

        