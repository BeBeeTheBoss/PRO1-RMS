import re
import uuid
from pathlib import Path
import os
import smtplib
import ssl
from email.message import EmailMessage
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    abort,
    send_from_directory,
    current_app,
    session,
    jsonify,
)
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException

from app.db import get_connection
from app.routes import login_required
from psycopg2.extras import RealDictCursor
from datetime import datetime
import json, requests
# import fitz  # pymupdf
import pymupdf
from PIL import Image
import pytesseract
from pdf2image import convert_from_bytes

candidate_bp = Blueprint("candidate", __name__)


# --------------------------------------------------
# REFRESH USER PERMISSIONS
# --------------------------------------------------
@candidate_bp.before_request
def refresh_candidate_user_permissions():
    """Refresh role and department from the database."""
    user_id = session.get("user_id")

    if not user_id:
        return None

    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, department_id, is_active
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )
            user = cur.fetchone()
    finally:
        conn.close()

    if user is None or not user[2]:
        session.clear()
        return redirect(url_for("main.login"))

    if user[0] not in ("super_admin", "department_head"):
        abort(403)

    session["role"] = user[0]
    session["department_id"] = user[1]

    return None

# --------------------------------------------------
# CANDIDATE STATUSES
# Must match the PostgreSQL CHECK constraints.
# --------------------------------------------------
CANDIDATE_STATUSES = [
    "applied",
    "interview_requested",
    "candidate_reject",
    "first_interview",
    "second_interview",
    "interview_request_reject",
    "interview_request_cancel",
    "first_interview_reject",
    "first_interview_cancel",
    "second_interview_reject",
    "second_interview_cancel",
    "offer",
    "offer_reject",
    "offer_cancel",
    "joined",
    "resign",
]


# --------------------------------------------------
# RECRUITMENT PIPELINE
# --------------------------------------------------
PIPELINE_TABS = {
    "candidates": {
        "title": "Candidates",
        "subtitle": "New applications waiting for review.",
        "statuses": ("applied",),
        "next_status": "interview_requested",
        "reject_status": "candidate_reject",
        "cancel_status": None,
        "skip_status": None,
    },
    "interview_request": {
        "title": "Interview Request",
        "subtitle": "Candidates accepted for an interview request.",
        "statuses": ("interview_requested",),
        "next_status": "first_interview",
        "reject_status": "interview_request_reject",
        "cancel_status": "interview_request_cancel",
        # Skip → Second Interview (First Interview ကို ကျော်)
        "skip_status": "second_interview",
    },
    "first_interview": {
        "title": "First Interview",
        "subtitle": "Candidates accepted for the first interview.",
        "statuses": ("first_interview",),
        "next_status": "second_interview",
        "reject_status": "first_interview_reject",
        "cancel_status": "first_interview_cancel",
        "skip_status": None,
    },
    "second_interview": {
        "title": "Second Interview",
        "subtitle": "Candidates accepted for the second interview.",
        "statuses": ("second_interview",),
        "next_status": "offer",
        "reject_status": "second_interview_reject",
        "cancel_status": "second_interview_cancel",
        # Skip → Joined (Offer ကို ကျော်)
        "skip_status": "joined",
    },
    "offer": {
        "title": "Offer",
        "subtitle": "Candidates who have received an offer.",
        "statuses": ("offer",),
        "next_status": "joined",
        "reject_status": "offer_reject",
        "cancel_status": "offer_cancel",
        "skip_status": None,
    },
    "joined": {
        "title": "Joined",
        "subtitle": "Candidates who have successfully joined.",
        "statuses": ("joined",),
        "next_status": None,
        "reject_status": None,
        "cancel_status": None,
        "skip_status": None,
    },
    "resign": {
        "title": "Resign",
        "subtitle": "Candidates who joined and later resign.",
        "statuses": ("resign",),
        "next_status": None,
        "reject_status": None,
        "cancel_status": None,
        "skip_status": None,
    },
}

# --------------------------------------------------
# FILE UPLOAD SETTINGS
# --------------------------------------------------
ALLOWED_EXTENSIONS = {
    "pdf",
    "doc",
    "docx",
    "jpg",
    "jpeg",
    "png",
}

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per file


def get_upload_root():
    """Use the existing upload location: app/uploads/."""
    root = Path(current_app.root_path) / "uploads"

    (root / "cv").mkdir(parents=True, exist_ok=True)
    (root / "attachments").mkdir(parents=True, exist_ok=True)

    return root


def allowed_file(filename):
    return (
        bool(filename)
        and "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def save_upload(file_storage, file_type):
    if not file_storage or not file_storage.filename:
        return None

    original_name = secure_filename(file_storage.filename)

    if not original_name or not allowed_file(original_name):
        raise ValueError(
            "PDF, DOC, DOCX, JPG, JPEG, PNG files only."
        )

    file_storage.stream.seek(0, 2)
    file_size = file_storage.stream.tell()
    file_storage.stream.seek(0)

    if file_size > MAX_FILE_SIZE:
        raise ValueError("File size must be 10 MB or less.")

    extension = original_name.rsplit(".", 1)[1].lower()
    stored_name = f"{uuid.uuid4().hex}.{extension}"

    folder = "cv" if file_type == "cv" else "attachments"
    relative_path = f"{folder}/{stored_name}"

    file_storage.save(get_upload_root() / relative_path)

    return relative_path


def remove_uploaded_file(relative_path):
    if not relative_path:
        return

    root = get_upload_root().resolve()
    file_path = (root / relative_path).resolve()

    if file_path.is_relative_to(root) and file_path.is_file():
        file_path.unlink()


# --------------------------------------------------
# DATABASE HELPERS
# --------------------------------------------------
def rows_as_dicts(cursor):
    columns = [column[0] for column in cursor.description]

    return [
        dict(zip(columns, row))
        for row in cursor.fetchall()
    ]


def row_as_dict(cursor):
    row = cursor.fetchone()

    if row is None:
        return None

    columns = [column[0] for column in cursor.description]

    return dict(zip(columns, row))


def validate_phone(phone):
    """Allow common phone formatting and require 7-15 digits."""
    if not phone or not re.fullmatch(r"[+0-9()\-\s]{7,30}", phone):
        return False

    digit_count = len(re.sub(r"\D", "", phone))

    return 7 <= digit_count <= 15


def validate_email(email):
    """Practical email format validation."""
    if not email or len(email) > 255:
        return False

    pattern = (
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
        r"@"
        r"[A-Za-z0-9]"
        r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9]"
        r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
    )

    return re.fullmatch(pattern, email) is not None


def _candidate_list_redirect(tab="candidates"):
    if tab not in PIPELINE_TABS:
        tab = "candidates"

    return redirect(
        url_for("candidate.candidates", tab=tab)
    )


# --------------------------------------------------
# ROLE / DEPARTMENT ACCESS HELPERS
# --------------------------------------------------
def current_role():
    role = session.get("role")

    if role not in ("super_admin", "department_head"):
        abort(403)

    return role

def current_department_id():
    """Return the assigned department ID for department heads."""
    role = current_role()

    if role == "super_admin":
        return None

    try:
        department_id = int(session.get("department_id"))
    except (TypeError, ValueError):
        abort(403)

    if department_id <= 0:
        abort(403)

    return department_id



def candidate_is_accessible(cur, candidate_id, lock=False, permission="can_view"):
    """
    Check whether the current user has permission to access a candidate.

    Super Admin:
        Can access all candidates.

    Department Head:
        Can access candidates only when the exact
        branch_id + department_id pair has the requested permission.
    """

    allowed_permissions = {
        "can_view",
        "can_add",
        "can_edit",
        "can_delete",
        "can_export",
    }

    if permission not in allowed_permissions:
        raise ValueError("Invalid candidate permission.")

    role = (current_role() or "").strip().lower()
    user_id = session.get("user_id")

    sql = """
        SELECT
            c.id,
            c.branch_id,
            c.department_id,
            c.status
        FROM public.candidates c
        WHERE c.id = %s
    """

    params = [candidate_id]

    if role == "department_head":
        sql += f"""
            AND EXISTS (
                SELECT 1
                FROM public.user_department_permissions udp
                WHERE udp.user_id = %s
                  AND udp.branch_id = c.branch_id
                  AND udp.department_id = c.department_id
                  AND udp.{permission} = TRUE
            )
        """

        params.append(user_id)

    if lock:
        sql += " FOR UPDATE OF c"

    cur.execute(sql, tuple(params))

    row = cur.fetchone()

    if row is None:
        abort(404)

    return {
        "id": row[0],
        "branch_id": row[1],
        "department_id": row[2],
        "status": row[3],
    }

def record_status_history(
    cur,
    candidate_id,
    from_status,
    to_status,
    action,
    remarks=None,
):
    """Record a status transition in the current transaction."""
    cur.execute(
        """
        INSERT INTO candidate_status_history (
            candidate_id,
            from_status,
            to_status,
            action,
            performed_by_user_id,
            performed_by_name,
            performed_role,
            remarks
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            candidate_id,
            from_status,
            to_status,
            action,
            session.get("user_id"),
            session.get("user_name", "Unknown user"),
            session.get("role", "unknown"),
            remarks,
        ),
    )

def _truncate_value(value, limit=200):
    """Trim long values for JSON diff storage."""
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= limit else value[:limit] + "…"


def record_edit_history(cur, candidate_id, changed_fields):
    """Record candidate field edits in the current transaction."""
    if not changed_fields:
        return

    import json

    # Truncate long values before storing.
    safe_fields = {}

    for field, change in changed_fields.items():
        safe_fields[field] = {
            "old": _truncate_value(change.get("old")),
            "new": _truncate_value(change.get("new")),
        }

    cur.execute(
        """
        INSERT INTO candidate_edit_history (
            candidate_id,
            changed_fields,
            performed_by_user_id,
            performed_by_name,
            performed_role
        )
        VALUES (%s, %s::jsonb, %s, %s, %s)
        """,
        (
            candidate_id,
            json.dumps(safe_fields),
            session.get("user_id"),
            session.get("user_name", "Unknown user"),
            session.get("role", "unknown"),
        ),
    )


# --------------------------------------------------
# GLOBAL SEARCH AUTOCOMPLETE
# URL: /candidates/search-suggestions?q=...
# --------------------------------------------------
@candidate_bp.route(
    "/candidates/search-suggestions",
    methods=["GET"],
)
@login_required
def candidate_search_suggestions():
    role = current_role()
    department_id = current_department_id()

    query = (request.args.get("q") or "").strip()

    if not query:
        return jsonify(results=[])

    like = f"%{query}%"
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            sql = """
                SELECT
                    c.id,
                    c.name,
                    c.phone,
                    c.email,
                    c.status,
                    d.name AS department_name
                FROM candidates c
                JOIN departments d
                    ON d.id = c.department_id
                WHERE (
                    c.name ILIKE %s
                    OR c.phone ILIKE %s
                    OR c.email ILIKE %s
                    OR COALESCE(c.nrc, '') ILIKE %s
                    OR d.name ILIKE %s
                    OR CAST(c.id AS TEXT) ILIKE %s
                )
            """

            params = [like] * 6

            if role == "department_head":
                sql += " AND c.department_id = %s"
                params.append(department_id)

            sql += """
                ORDER BY c.created_at DESC NULLS LAST, c.id DESC
                LIMIT 10
            """

            cur.execute(sql, tuple(params))
            results = rows_as_dicts(cur)

        return jsonify(
            results=[
                {
                    "id": row["id"],
                    "name": row["name"],
                    "phone": row["phone"],
                    "email": row["email"],
                    "status": row["status"],
                    "department": row["department_name"],
                }
                for row in results
            ]
        )

    finally:
        conn.close()


# --------------------------------------------------
# CHECK CANDIDATE DUPLICATE
# URL: /candidates/check-duplicate (POST)
# --------------------------------------------------
@candidate_bp.route("/candidates/check-duplicate", methods=["POST"])
@login_required
def check_candidate_duplicate():

    from flask import request, jsonify, session, current_app

    phone = (request.form.get("phone") or "").strip()
    email = (request.form.get("email") or "").strip().lower()

    phone_digits = "".join(
        ch for ch in phone if ch.isdigit()
    )

    if not phone_digits and not email:
        return jsonify({
            "ok": True,
            "duplicates": []
        })

    role = current_role()
    department_id = current_department_id()
    user_id = session.get("user_id")

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            departments = get_available_candidate_departments(
                role,
                department_id,
                user_id
            )

            allowed_department_ids = [
                int(dept["id"])
                for dept in departments
            ]

            if role != "super_admin" and not allowed_department_ids:
                return jsonify({
                    "ok": True,
                    "duplicates": []
                })

            sql = """
                SELECT
                    c.id,
                    c.name,
                    c.phone,
                    c.email,
                    c.position,
                    c.status,
                    b.name AS branch_name,
                    d.name AS department_name
                FROM public.candidates c
                LEFT JOIN public.branches b
                    ON b.id = c.branch_id
                LEFT JOIN public.departments d
                    ON d.id = c.department_id
                WHERE
                    (
                        %s <> ''
                        AND regexp_replace(
                            COALESCE(c.phone, ''),
                            '[^0-9]',
                            '',
                            'g'
                        ) = %s
                    )
                    OR
                    (
                        %s <> ''
                        AND LOWER(TRIM(COALESCE(c.email, ''))) = %s
                    )
            """

            params = [
                phone_digits,
                phone_digits,
                email,
                email
            ]

            if role != "super_admin":
                sql += """
                    AND c.department_id = ANY(%s)
                """
                params.append(allowed_department_ids)

            sql += """
                ORDER BY c.id DESC
                LIMIT 5
            """

            cur.execute(sql, params)

            rows = rows_as_dicts(cur)

            duplicates = []

            for row in rows:

                existing_phone = "".join(
                    ch for ch in (row.get("phone") or "")
                    if ch.isdigit()
                )

                existing_email = (
                    row.get("email") or ""
                ).strip().lower()

                duplicates.append({
                    "id": row["id"],
                    "name": row["name"],
                    "phone": row["phone"],
                    "email": row["email"],
                    "position": row["position"],
                    "status": row["status"],
                    "branch": row["branch_name"],
                    "department": row["department_name"],
                    "phone_match": (
                        bool(phone_digits)
                        and existing_phone == phone_digits
                    ),
                    "email_match": (
                        bool(email)
                        and existing_email == email
                    )
                })

        return jsonify({
            "ok": True,
            "duplicates": duplicates
        })

    except Exception:

        current_app.logger.exception(
            "Candidate duplicate check failed"
        )

        return jsonify({
            "ok": False,
            "error": "Unable to check duplicate candidate."
        }), 500

    finally:
        conn.close()


# --------------------------------------------------
# RENDER ADD CANDIDATE FORM
# --------------------------------------------------

def render_add_candidate_form(
    form_mode,
    form_data=None,
    form_errors=None,
    duplicate_candidates=None,
    departments=None,
):

    if form_mode not in ("auto", "manual"):
        form_mode = "auto"

    return render_template(
        "add_candidate.html",
        departments=departments or [],
        branches=get_available_branches(),

        auto_form_data=(
            form_data if form_mode == "auto" else {}
        ),

        manual_form_data=(
            form_data if form_mode == "manual" else {}
        ),

        form_mode=form_mode,
        form_errors=form_errors or [],
        duplicate_candidates=duplicate_candidates or [],
    )


# --------------------------------------------------
# ADD CANDIDATE (POST)
# URL: /candidates/add (POST)
# --------------------------------------------------

@candidate_bp.route("/candidates/add", methods=["POST"])
@login_required
def add_candidate():

    from flask import request, session, current_app, jsonify

    # --------------------------------------------------
    # 1. Identify submitted form
    # --------------------------------------------------

    form_mode = (
        request.form.get("form_mode") or "auto"
    ).strip().lower()

    if form_mode not in ("auto", "manual"):
        form_mode = "auto"

    is_ajax = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )

    submitted_data = request.form.to_dict()

    auto_form_data = submitted_data if form_mode == "auto" else {}
    manual_form_data = submitted_data if form_mode == "manual" else {}

    role = current_role()
    session_department_id = current_department_id()
    user_id = session.get("user_id")

    # --------------------------------------------------
    # Helper: JSON response
    # --------------------------------------------------

    def json_error(
        message,
        status=400,
        errors=None,
        duplicate=False,
        duplicates=None,
        permission_denied=False
    ):
        return jsonify({
            "ok": False,
            "message": message,
            "errors": errors or [],
            "duplicate": duplicate,
            "duplicate_candidates": duplicates or [],
            "permission_denied": permission_denied,
        }), status

    # --------------------------------------------------
    # Helper: Re-render form (normal non-AJAX request)
    # --------------------------------------------------

    def render_form(
        errors=None,
        duplicates=None,
        permission_denied=False,
        permission_message=""
    ):

        conn = get_connection()

        try:
            with conn.cursor() as cur:

                departments = get_available_candidate_departments(
                    role,
                    session_department_id,
                    user_id
                )

                department_ids = [
                    int(department["id"])
                    for department in departments
                ]

                if department_ids:

                    cur.execute(
                        """
                        SELECT id, branch_id
                        FROM public.departments
                        WHERE id = ANY(%s)
                        """,
                        (department_ids,)
                    )

                    department_branch_map = {
                        row[0]: row[1]
                        for row in cur.fetchall()
                    }

                    for department in departments:
                        department["branch_id"] = (
                            department_branch_map.get(
                                int(department["id"])
                            )
                        )

                if role == "super_admin":

                    cur.execute(
                        """
                        SELECT id, name
                        FROM public.branches
                        WHERE is_active = TRUE
                        ORDER BY name
                        """
                    )

                    branches = rows_as_dicts(cur)

                else:

                    allowed_branch_ids = sorted({
                        int(department["branch_id"])
                        for department in departments
                        if department.get("branch_id") is not None
                    })

                    if allowed_branch_ids:

                        cur.execute(
                            """
                            SELECT id, name
                            FROM public.branches
                            WHERE is_active = TRUE
                              AND id = ANY(%s)
                            ORDER BY name
                            """,
                            (allowed_branch_ids,)
                        )

                        branches = rows_as_dicts(cur)

                    else:
                        branches = []

            return render_template(
                "add_candidate.html",
                departments=departments,
                branches=branches,
                role=role,
                form_mode=form_mode,
                auto_form_data=auto_form_data,
                manual_form_data=manual_form_data,
                form_errors=errors or [],
                duplicate_candidates=duplicates or [],
                permission_denied=permission_denied,
                permission_message=permission_message,
            )

        finally:
            conn.close()

    # --------------------------------------------------
    # 2. Read submitted candidate information
    # --------------------------------------------------

    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    email = (request.form.get("email") or "").strip()

    address = (
        request.form.get("address") or ""
    ).strip() or None

    nrc = (
        request.form.get("nrc") or ""
    ).strip() or None

    position = (request.form.get("position") or "").strip()
    channel = (request.form.get("channel") or "").strip()

    additional_data = (
        request.form.get("additional_data") or ""
    ).strip() or None

    department_id_raw = (
        request.form.get("department_id") or ""
    ).strip()

    branch_id_raw = (
        request.form.get("branch_id") or ""
    ).strip()

    cv_file = request.files.get("cv_file")
    attachment_file = request.files.get("other_attachment")

    errors = []
    duplicate_candidates = []
    duplicate_detected = False

    # --------------------------------------------------
    # 3. Validate department and branch IDs
    # --------------------------------------------------

    department_id = None
    branch_id = None

    if not department_id_raw.isdigit() or int(department_id_raw) <= 0:
        errors.append("Please select a valid department.")
    else:
        department_id = int(department_id_raw)

    if not branch_id_raw.isdigit() or int(branch_id_raw) <= 0:
        errors.append("Please select a valid branch.")
    else:
        branch_id = int(branch_id_raw)

    # --------------------------------------------------
    # 4. EARLY ADD PERMISSION CHECK
    # --------------------------------------------------

    if role != "super_admin":

        permission_conn = None

        try:
            permission_conn = get_connection()

            with permission_conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM public.user_department_permissions
                        WHERE user_id = %s
                          AND can_add = TRUE
                    )
                    """,
                    (user_id,)
                )

                has_any_add_permission = cur.fetchone()[0]

                if not has_any_add_permission:

                    message = (
                        "You don't have permission to add candidates. "
                        "Please contact your administrator."
                    )

                    current_app.logger.warning(
                        "Add candidate denied: user has no add permission. "
                        "user_id=%s",
                        user_id
                    )

                    if is_ajax:
                        return json_error(
                            message,
                            status=403,
                            permission_denied=True
                        )

                    return render_form(
                        permission_denied=True,
                        permission_message=message
                    ), 403

                if department_id is not None and branch_id is not None:

                    cur.execute(
                        """
                        SELECT 1
                        FROM public.user_department_permissions
                        WHERE user_id = %s
                          AND branch_id = %s
                          AND department_id = %s
                          AND can_add = TRUE
                        LIMIT 1
                        """,
                        (
                            user_id,
                            branch_id,
                            department_id,
                        )
                    )

                    if cur.fetchone() is None:

                        message = (
                            "You don't have permission to add candidates "
                            "in this department."
                        )

                        current_app.logger.warning(
                            "Add candidate denied: "
                            "user_id=%s department_id=%s branch_id=%s",
                            user_id,
                            department_id,
                            branch_id
                        )

                        if is_ajax:
                            return json_error(
                                message,
                                status=403,
                                permission_denied=True
                            )

                        return render_form(
                            permission_denied=True,
                            permission_message=message
                        ), 403

        finally:
            if permission_conn:
                permission_conn.close()

    # --------------------------------------------------
    # 5. Validate candidate information
    # --------------------------------------------------

    if not name or len(name) > 150:
        errors.append(
            "Full name is required and must be 150 characters or fewer."
        )

    if not validate_phone(phone):
        errors.append(
            "Enter a valid phone number containing 7-15 digits."
        )

    if not validate_email(email):
        errors.append("Enter a valid email address.")

    if nrc and len(nrc) > 50:
        errors.append("NRC must be 50 characters or fewer.")

    if address and len(address) > 5000:
        errors.append("Address must be 5,000 characters or fewer.")

    if additional_data and len(additional_data) > 10000:
        errors.append(
            "Additional information must be 10,000 characters or fewer."
        )

    if not position:
        errors.append("Position is required.")
    elif len(position) > 150:
        errors.append("Position must be 150 characters or fewer.")

    if not channel:
        errors.append("Channel is required.")
    elif len(channel) > 100:
        errors.append("Channel must be 100 characters or fewer.")

    # --------------------------------------------------
    # 6. Validate assigned department
    # --------------------------------------------------

    available_departments = get_available_candidate_departments(
        role,
        session_department_id,
        user_id
    )

    available_department_ids = {
        int(department["id"])
        for department in available_departments
    }

    if department_id is not None:

        if department_id not in available_department_ids:

            errors.append(
                "You can only add candidates to departments assigned to you."
                if role == "department_head"
                else "You do not have permission to select this department."
            )

    # --------------------------------------------------
    # 7. Validate CV file
    # --------------------------------------------------

    if not cv_file or not cv_file.filename:
        errors.append("Please select a CV / resume file.")

    # --------------------------------------------------
    # 8. Return validation errors
    # --------------------------------------------------

    if errors:

        if is_ajax:
            return json_error(
                errors[0],
                status=400,
                errors=errors
            )

        return render_form(errors=errors)

    # --------------------------------------------------
    # 9. Prepare upload and database variables
    # --------------------------------------------------

    uploaded_paths = []
    conn = None
    committed = False

    cv_path = None
    attachment_path = None

    phone_digits = "".join(
        character
        for character in phone
        if character.isdigit()
    )

    normalized_email = email.strip().lower()

    # --------------------------------------------------
    # Helper: Fetch duplicate candidate previews
    # --------------------------------------------------

    def fetch_duplicate_previews(cur):

        preview_sql = """
            SELECT
                c.id,
                c.name,
                c.phone,
                c.email,
                c.position,
                c.status,
                c.branch_id,
                c.department_id,

                b.name AS branch_name,
                d.name AS department_name,

                CASE
                    WHEN %s = 'super_admin' THEN TRUE

                    WHEN EXISTS (
                        SELECT 1
                        FROM public.user_department_permissions udp
                        WHERE udp.user_id = %s
                          AND udp.branch_id = c.branch_id
                          AND udp.department_id = c.department_id
                          AND udp.can_view = TRUE
                    ) THEN TRUE

                    ELSE FALSE
                END AS can_view

            FROM public.candidates c

            LEFT JOIN public.branches b
                ON b.id = c.branch_id

            LEFT JOIN public.departments d
                ON d.id = c.department_id

            WHERE
                (
                    regexp_replace(c.phone, '\\D', '', 'g') = %s
                    OR LOWER(TRIM(c.email)) = %s
                )

            ORDER BY c.id DESC
            LIMIT 5
        """

        cur.execute(
            preview_sql,
            (
                role,
                user_id,
                phone_digits,
                normalized_email,
            )
        )

        duplicate_rows = rows_as_dicts(cur)

        previews = []

        for row in duplicate_rows:

            existing_phone = "".join(
                character
                for character in (row.get("phone") or "")
                if character.isdigit()
            )

            existing_email = (
                row.get("email") or ""
            ).strip().lower()

            previews.append({
                "id": row["id"],
                "name": row["name"],
                "phone": row["phone"],
                "email": row["email"],
                "position": row["position"],
                "status": row["status"],
                "branch": row["branch_name"],
                "department": row["department_name"],

                "phone_match": (
                    bool(phone_digits)
                    and existing_phone == phone_digits
                ),

                "email_match": (
                    bool(normalized_email)
                    and existing_email == normalized_email
                ),

                "can_view": bool(row["can_view"]),
            })

        return previews

    # --------------------------------------------------
    # Helper: Check duplicate phone and email
    # --------------------------------------------------

    def check_duplicate_candidates(cur):

        cur.execute(
            """
            SELECT
                BOOL_OR(
                    regexp_replace(phone, '\\D', '', 'g') = %s
                ) AS phone_exists,

                BOOL_OR(
                    LOWER(TRIM(email)) = %s
                ) AS email_exists

            FROM public.candidates

            WHERE
                regexp_replace(phone, '\\D', '', 'g') = %s
                OR LOWER(TRIM(email)) = %s
            """,
            (
                phone_digits,
                normalized_email,
                phone_digits,
                normalized_email,
            ),
        )

        duplicate_row = cur.fetchone()

        phone_exists = bool(duplicate_row[0])
        email_exists = bool(duplicate_row[1])

        return phone_exists, email_exists

    # --------------------------------------------------
    # 10. Validate and insert candidate
    # --------------------------------------------------

    try:

        conn = get_connection()

        with conn.cursor() as cur:

            # ------------------------------------------
            # 10.1 Confirm selected department and branch
            # ------------------------------------------

            cur.execute(
                """
                SELECT id
                FROM public.departments
                WHERE id = %s
                  AND branch_id = %s
                  AND is_active = TRUE
                """,
                (
                    department_id,
                    branch_id,
                ),
            )

            if cur.fetchone() is None:
                raise ValueError(
                    "Selected department does not belong to the selected branch "
                    "or the department is inactive."
                )

            # ------------------------------------------
            # 10.2 Confirm branch is active
            # ------------------------------------------

            cur.execute(
                """
                SELECT id
                FROM public.branches
                WHERE id = %s
                  AND is_active = TRUE
                """,
                (branch_id,),
            )

            if cur.fetchone() is None:
                raise ValueError(
                    "Selected branch does not exist or is inactive."
                )

            # ------------------------------------------
            # 10.3 Final Add Permission Check
            # ------------------------------------------

            if role != "super_admin":

                cur.execute(
                    """
                    SELECT 1
                    FROM public.user_department_permissions
                    WHERE user_id = %s
                      AND branch_id = %s
                      AND department_id = %s
                      AND can_add = TRUE
                    LIMIT 1
                    """,
                    (
                        user_id,
                        branch_id,
                        department_id,
                    ),
                )

                if cur.fetchone() is None:
                    raise PermissionError(
                        "You don't have permission to add candidates "
                        "in this department."
                    )

            # ------------------------------------------
            # 10.4 Check duplicate phone and email
            # ------------------------------------------

            phone_exists, email_exists = (
                check_duplicate_candidates(cur)
            )

            if phone_exists or email_exists:

                duplicate_candidates = (
                    fetch_duplicate_previews(cur)
                )

                duplicate_detected = True

                if phone_exists and email_exists:
                    raise ValueError(
                        "This phone number and email address are both already registered."
                    )

                elif phone_exists:
                    raise ValueError(
                        "This phone number is already registered."
                    )

                else:
                    raise ValueError(
                        "This email address is already registered."
                    )

            # ------------------------------------------
            # 10.5 Save files AFTER duplicate check
            # ------------------------------------------

            cv_path = save_upload(cv_file, "cv")
            uploaded_paths.append(cv_path)

            if attachment_file and attachment_file.filename:

                attachment_path = save_upload(
                    attachment_file,
                    "attachment"
                )

                uploaded_paths.append(attachment_path)

            # ------------------------------------------
            # 10.6 Insert candidate
            # ------------------------------------------

            cur.execute(
                """
                INSERT INTO public.candidates (
                    name,
                    phone,
                    email,
                    address,
                    nrc,
                    position,
                    channel,
                    additional_data,
                    department_id,
                    branch_id,
                    cv_file_path,
                    other_attachment_path,
                    status
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, 'applied'
                )
                RETURNING id
                """,
                (
                    name,
                    phone,
                    email,
                    address,
                    nrc,
                    position,
                    channel,
                    additional_data,
                    department_id,
                    branch_id,
                    cv_path,
                    attachment_path,
                ),
            )

            new_id = cur.fetchone()[0]

            # ------------------------------------------
            # 10.7 Save initial status history
            # ------------------------------------------

            record_status_history(
                cur,
                new_id,
                None,
                "applied",
                "status_change",
                remarks="Candidate created",
            )

        conn.commit()
        committed = True

        success_message = "Candidate added successfully."

        # ----------------------------------------------
        # AJAX success response
        # ----------------------------------------------

        if is_ajax:
            return jsonify({
                "ok": True,
                "message": success_message,
                "candidate_id": new_id,
                "redirect_url": url_for(
                    "candidate.view_candidates",
                    toast="success"
                )
            }), 200

        flash(success_message, "success")

        return redirect(
            url_for(
                "candidate.view_candidates",
                toast="success",
            )
        )

    # --------------------------------------------------
    # 11. Handle permission denied
    # --------------------------------------------------

    except PermissionError as exc:

        if conn:
            conn.rollback()

        current_app.logger.warning(
            "Add candidate permission denied: "
            "user_id=%s department_id=%s branch_id=%s",
            user_id,
            department_id,
            branch_id
        )

        if is_ajax:
            return json_error(
                str(exc),
                status=403,
                permission_denied=True
            )

        return render_form(
            permission_denied=True,
            permission_message=str(exc)
        ), 403

    # --------------------------------------------------
    # 12. Handle validation / business errors
    # --------------------------------------------------

    except ValueError as exc:

        if conn:
            conn.rollback()

        errors.append(str(exc))

    # --------------------------------------------------
    # 13. Handle database and unexpected errors
    # --------------------------------------------------

    except Exception as exc:

        if conn:
            conn.rollback()

        if getattr(exc, "pgcode", None) == "23505":

            constraint_name = getattr(
                getattr(exc, "diag", None),
                "constraint_name",
                "",
            )

            duplicate_constraints = (
                "uq_candidates_phone",
                "uq_candidates_phone_normalized",
                "uq_candidates_email_lower",
                "uq_candidates_email_normalized",
            )

            if constraint_name in duplicate_constraints:

                duplicate_detected = True

                try:

                    with conn.cursor() as cur:

                        phone_exists, email_exists = (
                            check_duplicate_candidates(cur)
                        )

                        duplicate_candidates = (
                            fetch_duplicate_previews(cur)
                        )

                    if phone_exists and email_exists:
                        errors.append(
                            "This phone number and email address are both already registered."
                        )

                    elif phone_exists:
                        errors.append(
                            "This phone number is already registered."
                        )

                    elif email_exists:
                        errors.append(
                            "This email address is already registered."
                        )

                    else:
                        errors.append(
                            "Duplicate candidate information detected. "
                            "Please check the phone and email."
                        )

                except Exception:

                    current_app.logger.exception(
                        "Failed to retrieve duplicate candidate information"
                    )

                    errors.append(
                        "Duplicate phone number or email address detected."
                    )

            else:

                current_app.logger.exception(
                    "Failed to add candidate due to a database constraint"
                )

                errors.append(
                    "Could not save candidate because of a database constraint."
                )

        else:

            current_app.logger.exception(
                "Failed to add candidate"
            )

            errors.append(
                "Could not save candidate. "
                "Please check the database and server log."
            )

    # --------------------------------------------------
    # 14. Close database and clean up uploaded files
    # --------------------------------------------------

    finally:

        if conn:
            conn.close()

        if not committed:

            for relative_path in uploaded_paths:

                try:
                    remove_uploaded_file(relative_path)

                except OSError:

                    current_app.logger.exception(
                        "Failed to clean up uploaded candidate file"
                    )

    # --------------------------------------------------
    # 15. Return AJAX duplicate / error response
    # --------------------------------------------------

    if is_ajax:

        message = (
            errors[0]
            if errors
            else "Could not save candidate."
        )

        return json_error(
            message,
            status=409 if duplicate_detected else 400,
            errors=errors,
            duplicate=duplicate_detected,
            duplicates=duplicate_candidates
        )

    # --------------------------------------------------
    # 16. Normal non-AJAX form response
    # --------------------------------------------------

    return render_form(
        errors=errors,
        duplicates=duplicate_candidates
    )


# ======================================================
# GET: ADD CANDIDATE PAGE
# ======================================================

@candidate_bp.route("/candidates/add", methods=["GET"])
@login_required
def add_candidate_page():

    role = current_role()
    department_id = current_department_id()
    user_id = session.get("user_id")

    conn = get_connection()

    try:

        with conn.cursor() as cur:

            # ------------------------------------------
            # 1. Get available departments
            # ------------------------------------------

            departments = get_available_candidate_departments(
                role,
                department_id,
                user_id
            )

            department_ids = [
                int(department["id"])
                for department in departments
            ]

            # ------------------------------------------
            # 2. Attach branch_id to departments
            # ------------------------------------------

            if department_ids:

                cur.execute(
                    """
                    SELECT id, branch_id
                    FROM public.departments
                    WHERE id = ANY(%s)
                    """,
                    (department_ids,)
                )

                department_branch_map = {
                    row[0]: row[1]
                    for row in cur.fetchall()
                }

                for department in departments:
                    department["branch_id"] = (
                        department_branch_map.get(
                            int(department["id"])
                        )
                    )

            # ------------------------------------------
            # 3. Get branches
            # ------------------------------------------

            if role == "super_admin":

                cur.execute(
                    """
                    SELECT id, name
                    FROM public.branches
                    WHERE is_active = TRUE
                    ORDER BY name
                    """
                )

                branches = rows_as_dicts(cur)

            else:

                allowed_branch_ids = sorted({
                    int(department["branch_id"])
                    for department in departments
                    if department.get("branch_id") is not None
                })

                if allowed_branch_ids:

                    cur.execute(
                        """
                        SELECT id, name
                        FROM public.branches
                        WHERE is_active = TRUE
                          AND id = ANY(%s)
                        ORDER BY name
                        """,
                        (allowed_branch_ids,)
                    )

                    branches = rows_as_dicts(cur)

                else:
                    branches = []

            # ------------------------------------------
            # 4. Check Add Candidate Permission
            # ------------------------------------------

            permission_denied = False
            permission_message = ""

            if role != "super_admin":

                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM public.user_department_permissions
                        WHERE user_id = %s
                          AND can_add = TRUE
                    )
                    """,
                    (user_id,)
                )

                has_add_permission = cur.fetchone()[0]

                if not has_add_permission:

                    permission_denied = True

                    permission_message = (
                        "You don't have permission to add candidates. "
                        "Please contact your administrator."
                    )

        return render_template(
            "add_candidate.html",
            departments=departments,
            branches=branches,
            role=role,
            form_mode="auto",
            auto_form_data={},
            manual_form_data={},
            form_errors=[],
            duplicate_candidates=[],
            permission_denied=permission_denied,
            permission_message=permission_message,
        )

    finally:
        conn.close()


def get_available_candidate_departments(
    role,
    department_id=None,
    user_id=None
):
    """
    Super Admin:
        Return all active departments.

    Department Head:
        Return only assigned active departments.
        If no user_departments mapping exists,
        use legacy users.department_id.
    """

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            # Super Admin can access all active departments
            if role == "super_admin":

                cur.execute(
                    """
                    SELECT
                        id,
                        name,
                        short_name,
                        branch_id
                    FROM public.departments
                    WHERE is_active = TRUE
                    ORDER BY name
                    """
                )

                return rows_as_dicts(cur)

            # Department Head can access assigned departments
            elif role == "department_head" and user_id:

                cur.execute(
                    """
                    SELECT
                        d.id,
                        d.name,
                        d.short_name,
                        d.branch_id
                    FROM public.user_departments ud
                    JOIN public.departments d
                        ON d.id = ud.department_id
                    WHERE ud.user_id = %s
                      AND d.is_active = TRUE
                    ORDER BY d.name
                    """,
                    (user_id,)
                )

                departments = rows_as_dicts(cur)

                # Backward compatibility:
                # Use the old users.department_id
                # if no active assigned department is found.
                if not departments:

                    cur.execute(
                        """
                        SELECT
                            d.id,
                            d.name,
                            d.short_name,
                            d.branch_id
                        FROM public.users u
                        JOIN public.departments d
                            ON d.id = u.department_id
                        WHERE u.id = %s
                          AND u.role = 'department_head'
                          AND d.is_active = TRUE
                        ORDER BY d.name
                        """,
                        (user_id,)
                    )

                    departments = rows_as_dicts(cur)

                return departments

            # No permission
            return []

    finally:
        conn.close()

def get_available_branches():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name
                FROM branches
                WHERE is_active = TRUE
                ORDER BY name
                """
            )
            return rows_as_dicts(cur)
    finally:
        conn.close()

# --------------------------------------------------
# CANDIDATE DECISION EMAIL HELPER
# --------------------------------------------------
def _send_candidate_decision_email(recipient, subject, body):
    """
    Send the candidate decision email through SMTP.

    Raises an exception if SMTP configuration is invalid
    or if the email cannot be sent.
    """

    recipient = (recipient or "").strip()
    subject = (subject or "").strip()
    body = (body or "").strip()

    if not recipient or not subject or not body:
        raise ValueError(
            "Email recipient, subject and message are required."
        )

    smtp_host = os.getenv(
        "SMTP_HOST",
        "smtp.gmail.com",
    ).strip()

    smtp_port = int(
        os.getenv("SMTP_PORT", "587")
    )

    smtp_username = os.getenv(
        "SMTP_USERNAME",
        "",
    ).strip()

    smtp_password = os.getenv(
        "SMTP_PASSWORD",
        "",
    )

    smtp_sender = (
        os.getenv("SMTP_SENDER", "").strip()
        or smtp_username
    )

    use_ssl = (
        os.getenv("SMTP_USE_SSL", "False")
        .strip()
        .lower()
        in ("1", "true", "yes", "on")
    )

    use_tls = (
        os.getenv("SMTP_USE_TLS", "True")
        .strip()
        .lower()
        in ("1", "true", "yes", "on")
    )
    # print("SMTP HOST:", smtp_host)
    # print("SMTP PORT:", smtp_port)
    # print("SMTP USERNAME:", smtp_username)
    # print("SMTP SENDER:", smtp_sender)
    # print("SMTP TLS:", use_tls)
    # print("SMTP SSL:", use_ssl)
    if (
        not smtp_host
        or not smtp_username
        or not smtp_password
        or not smtp_sender
    ):
        raise ValueError(
            "SMTP configuration is incomplete."
        )

    if use_ssl and use_tls:
        raise ValueError(
            "SMTP_USE_SSL and SMTP_USE_TLS cannot "
            "both be enabled."
        )

    # Prepare email message.
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_sender
    message["To"] = recipient
    message.set_content(body)

    # Send email before changing candidate status.
    if use_ssl:
        with smtplib.SMTP_SSL(
            smtp_host,
            smtp_port,
            timeout=20,
            context=ssl.create_default_context(),
        ) as server:
            server.login(
                smtp_username,
                smtp_password,
            )

            server.send_message(message)

    else:
        with smtplib.SMTP(
            smtp_host,
            smtp_port,
            timeout=20,
        ) as server:
            server.ehlo()

            if use_tls:
                server.starttls(
                    context=ssl.create_default_context()
                )
                server.ehlo()

            server.login(
                smtp_username,
                smtp_password,
            )

            server.send_message(message)

# --------------------------------------------------
# CANDIDATE PIPELINE
# URL: /candidates
# --------------------------------------------------
@candidate_bp.route("/candidates", methods=["GET"])
@login_required
def candidates():

    role = current_role()
    department_id = current_department_id()
    user_id = session.get("user_id")

    active_tab = (
        request.args.get("tab") or "candidates"
    ).strip()

    if active_tab not in PIPELINE_TABS:
        active_tab = "candidates"

    tab_config = PIPELINE_TABS[active_tab]

    global_search = (
        request.args.get("global_search") or ""
    ).strip()

    stage_search = (
        request.args.get("stage_search") or ""
    ).strip()

    selected_status = (
        request.args.get("status") or ""
    ).strip()

    if selected_status not in tab_config["statuses"]:
        selected_status = ""

    if global_search:
        stage_search = ""
        selected_status = ""

    # --------------------------------------------------
    # USER DEPARTMENT ACCESS
    # --------------------------------------------------

    available_departments = get_available_candidate_departments(
        role,
        department_id,
        user_id
    )

    allowed_department_ids = [
        int(department["id"])
        for department in available_departments
    ]

    # Super Admin: all departments
    # Department Head: assigned departments only
    if role == "super_admin":
        department_scope_sql = ""
        department_scope_params = []

    elif role == "department_head" and allowed_department_ids:
        department_scope_sql = " AND c.department_id = ANY(%s)"
        department_scope_params = [allowed_department_ids]

    else:
        # No valid department access
        department_scope_sql = " AND 1 = 0"
        department_scope_params = []

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            # --------------------------------------------------
            # DEPARTMENT DROPDOWN DATA
            # --------------------------------------------------

            departments = available_departments

            # --------------------------------------------------
            # MAIN CANDIDATE QUERY
            # --------------------------------------------------

            sql = """
                SELECT
                    c.id,
                    c.name,
                    c.phone,
                    c.email,
                    c.address,
                    c.nrc,
                    c.department_id,
                    c.branch_id,
                    c.position,
                    c.channel,
                    c.cv_file_path,
                    c.other_attachment_path,
                    c.status,
                    c.created_at,
                    c.updated_at,
                    (
                        SELECT h.created_at
                        FROM public.candidate_status_history h
                        WHERE h.candidate_id = c.id
                        AND h.to_status = 'joined'
                        ORDER BY h.created_at ASC
                        LIMIT 1
                    ) AS joined_date,
                    d.name AS department_name,
                    b.name AS branch_name,

                    -- FIRST INTERVIEW
                    fi.interview_date AS first_interview_date,
                    fi.interview_time AS first_interview_time,
                    fi.interviewer AS first_interviewer,
                    fi.status AS first_interview_status,

                    (
                        fi.interview_date + fi.interview_time
                        < (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Yangon')
                        AND fi.status <> 'cancelled'
                    ) AS first_interview_passed,

                    -- SECOND INTERVIEW
                    si.interview_date AS second_interview_date,
                    si.interview_time AS second_interview_time,
                    si.interviewer AS second_interviewer,
                    si.status AS second_interview_status,

                    (
                        si.interview_date + si.interview_time
                        < (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Yangon')
                        AND si.status <> 'cancelled'
                    ) AS second_interview_passed

                FROM public.candidates c

                JOIN public.departments d
                    ON d.id = c.department_id

                LEFT JOIN public.branches b
                    ON b.id = c.branch_id

                LEFT JOIN public.candidate_interviews fi
                    ON fi.candidate_id = c.id
                    AND fi.interview_stage = 'first_interview'

                LEFT JOIN public.candidate_interviews si
                    ON si.candidate_id = c.id
                    AND si.interview_stage = 'second_interview'

                WHERE 1 = 1
            """
                        
            params = []

            # Apply user department permission
            sql += department_scope_sql
            params.extend(department_scope_params)

            # --------------------------------------------------
            # GLOBAL SEARCH
            # --------------------------------------------------

            if global_search:

                sql += """
                    AND (
                        c.name ILIKE %s
                        OR c.phone ILIKE %s
                        OR c.email ILIKE %s
                        OR COALESCE(c.nrc, '') ILIKE %s
                        OR d.name ILIKE %s
                        OR c.id::text ILIKE %s
                    )
                """

                keyword = f"%{global_search}%"
                params.extend([keyword] * 6)

            else:

                # --------------------------------------------------
                # PIPELINE STATUS FILTER
                # --------------------------------------------------

                sql += " AND c.status = ANY(%s)"
                params.append(list(tab_config["statuses"]))

                # --------------------------------------------------
                # STAGE SEARCH
                # --------------------------------------------------

                if stage_search:

                    sql += """
                        AND (
                            c.name ILIKE %s
                            OR c.phone ILIKE %s
                            OR c.email ILIKE %s
                            OR COALESCE(c.nrc, '') ILIKE %s
                            OR d.name ILIKE %s
                            OR b.name ILIKE %s
                            OR c.id::text ILIKE %s
                        )
                    """

                    keyword = f"%{stage_search}%"

                    # Seven search conditions
                    params.extend([keyword] * 7)

                # --------------------------------------------------
                # SELECTED STATUS FILTER
                # --------------------------------------------------

                if selected_status:

                    sql += " AND c.status = %s"
                    params.append(selected_status)

            sql += """
                ORDER BY
                    c.created_at DESC NULLS LAST,
                    c.id DESC
            """

            cur.execute(sql, tuple(params))

            candidate_list = rows_as_dicts(cur)

            # --------------------------------------------------
            # TOTAL CANDIDATE COUNT
            # --------------------------------------------------

            scope_sql = """
                FROM public.candidates
                WHERE 1 = 1
            """

            scope_params = []

            if role == "department_head":

                if allowed_department_ids:

                    scope_sql += """
                        AND department_id = ANY(%s)
                    """

                    scope_params.append(allowed_department_ids)

                else:

                    scope_sql += " AND 1 = 0"

            cur.execute(
                "SELECT COUNT(*) " + scope_sql,
                tuple(scope_params)
            )

            total_count = cur.fetchone()[0]

            # --------------------------------------------------
            # JOINED COUNT
            # --------------------------------------------------

            joined_sql = (
                "SELECT COUNT(*) "
                + scope_sql
                + " AND status = 'joined'"
            )

            cur.execute(
                joined_sql,
                tuple(scope_params)
            )

            joined_count = cur.fetchone()[0]

            # --------------------------------------------------
            # NEW CANDIDATE COUNT
            # --------------------------------------------------

            new_sql = (
                "SELECT COUNT(*) "
                + scope_sql
                + " AND status = 'applied'"
            )

            cur.execute(
                new_sql,
                tuple(scope_params)
            )

            new_count = cur.fetchone()[0]

            # --------------------------------------------------
            # PIPELINE TAB COUNTS
            # --------------------------------------------------

            tab_counts = {}

            for key, config in PIPELINE_TABS.items():

                count_sql = """
                    SELECT COUNT(*)
                    FROM public.candidates
                    WHERE status = ANY(%s)
                """

                count_params = [
                    list(config["statuses"])
                ]

                if role == "department_head":

                    if allowed_department_ids:

                        count_sql += """
                            AND department_id = ANY(%s)
                        """

                        count_params.append(
                            allowed_department_ids
                        )

                    else:

                        count_sql += " AND 1 = 0"

                cur.execute(
                    count_sql,
                    tuple(count_params)
                )

                tab_counts[key] = cur.fetchone()[0]

        # --------------------------------------------------
        # RENDER PAGE
        # --------------------------------------------------

        return render_template(
            "candidate.html",
            candidates=candidate_list,
            departments=departments,
            statuses=CANDIDATE_STATUSES,
            search=global_search,
            global_search=global_search,
            stage_search=stage_search,
            selected_status=selected_status,
            active_tab=active_tab,
            tab_config=tab_config,
            pipeline_tabs=PIPELINE_TABS,
            total_count=total_count,
            joined_count=joined_count,
            new_count=new_count,
            tab_counts=tab_counts,
            current_role=role,
        )

    finally:
        conn.close()

# --------------------------------------------------
# INTERVIEW DECISION VALIDATION HELPER
# --------------------------------------------------
def _check_interview_decision_allowed(cur, candidate_id, active_tab):
    """
    Check whether Accept / Reject is allowed for a candidate.

    First Interview and Second Interview stages:
    The scheduled interview date + time must be in the past
    (using Asia/Yangon timezone).
    """

    interview_stage = None

    if active_tab == "first_interview":
        interview_stage = "first_interview"

    elif active_tab == "second_interview":
        interview_stage = "second_interview"

    else:
        return True, None

    cur.execute(
        """
        SELECT
            interview_date,
            interview_time,
            interviewer,
            status,
            (
                interview_date + interview_time
                < (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Yangon')
            ) AS interview_passed

        FROM public.candidate_interviews

        WHERE candidate_id = %s
          AND interview_stage = %s

        FOR UPDATE
        """,
        (candidate_id, interview_stage),
    )

    interview = cur.fetchone()

    if not interview:
        return (
            False,
            "Interview schedule not found. "
            "Accept and Reject are unavailable.",
        )

    # Handle tuple vs dict
    if isinstance(interview, dict):
        interview_date = interview["interview_date"]
        interview_time = interview["interview_time"]
        interview_passed = interview["interview_passed"]
    else:
        interview_date = interview[0]
        interview_time = interview[1]
        interview_passed = interview[4]

    if not interview_passed:
        formatted_date = interview_date.strftime("%d %b %Y")
        formatted_time = interview_time.strftime("%I:%M %p")
        
        return (
            False,
            f"You can only Accept or Reject after the scheduled "
            f"interview time has passed. Scheduled: {formatted_date} at {formatted_time}.",
        )

    return True, None

# --------------------------------------------------
# ACCEPT CANDIDATE
# URL: /candidates/<id>/accept (POST)
# --------------------------------------------------
@candidate_bp.route(
    "/candidates/<int:candidate_id>/accept",
    methods=["POST"],
)
@login_required
def accept_candidate(candidate_id):

    import re
    from datetime import datetime
    from zoneinfo import ZoneInfo

    active_tab = (
        request.form.get("tab") or "candidates"
    ).strip()

    if active_tab not in PIPELINE_TABS:
        abort(400)

    config = PIPELINE_TABS[active_tab]
    next_status = config.get("next_status")

    if not next_status:
        abort(400)

    recipient = (
        request.form.get("email_to") or ""
    ).strip()

    subject = (
        request.form.get("email_subject") or ""
    ).strip()

    body = (
        request.form.get("email_body") or ""
    ).strip()

    remarks = (
        request.form.get("remarks") or ""
    ).strip() or None

    # ==================================================
    # INTERVIEW SCHEDULE INPUT
    # ==================================================

    interview_stage = None
    interview_date = None
    interview_time = None
    interviewer = None

    if active_tab == "interview_request":
        interview_stage = "first_interview"

    elif active_tab == "first_interview":
        interview_stage = "second_interview"

    if interview_stage:

        interview_date_input = (
            request.form.get("interview_date") or ""
        ).strip()

        interview_time_input = (
            request.form.get("interview_time") or ""
        ).strip()

        interviewer = (
            request.form.get("interviewer") or ""
        ).strip()

        if (
            not interview_date_input
            or not interview_time_input
            or not interviewer
        ):
            flash(
                "Interview date, time and interviewer are required. "
                "Candidate status was not changed.",
                "error",
            )
            return _candidate_list_redirect(active_tab)

        if len(interviewer) > 150:
            flash(
                "Interviewer name cannot exceed 150 characters.",
                "error",
            )
            return _candidate_list_redirect(active_tab)

        try:
            interview_date = datetime.strptime(
                interview_date_input,
                "%Y-%m-%d",
            ).date()

            interview_time = datetime.strptime(
                interview_time_input,
                "%H:%M",
            ).time()

        except ValueError:
            flash(
                "Invalid interview date or time. "
                "Candidate status was not changed.",
                "error",
            )
            return _candidate_list_redirect(active_tab)

        # ----------------------------------------------
        # CHECK FUTURE DATE AND TIME (YANGON TIMEZONE)
        # ----------------------------------------------

        now_yangon = datetime.now(
            ZoneInfo("Asia/Yangon")
        )

        scheduled_datetime = datetime.combine(
            interview_date,
            interview_time,
        )

        if scheduled_datetime <= now_yangon.replace(
            tzinfo=None
        ):
            flash(
                "Interview date and time must be in the future.",
                "error",
            )
            return _candidate_list_redirect(active_tab)

        # ==================================================
        # REMOVE ALL EXISTING INTERVIEW SCHEDULE SECTIONS
        # ==================================================

        # Normalize line endings first.
        body = (
            body
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )

        # Remove every existing Interview Schedule section.
        # Supports:
        # Interview Schedule
        # Date: 11/11/2026
        # Time: 10:30 AM
        # Interviewer: HR
        #
        # Also supports:
        # Interview Schedule
        # Date: 11 November 2026
        # Time: 10:30 AM
        # Interviewer: HR

        body = re.sub(
            r"(?im)"
            r"^[ \t]*Interview Schedule[ \t]*\n"
            r"(?:"
            r"^[ \t]*(?:Date|Time|Interviewer)[ \t]*:.*\n?"
            r"){1,3}",
            "",
            body,
        )

        # Clean excessive blank lines.
        body = re.sub(
            r"\n[ \t]*\n(?:[ \t]*\n)+",
            "\n\n",
            body,
        ).strip()

        # ==================================================
        # APPEND EXACTLY ONE INTERVIEW SCHEDULE
        # ==================================================

        formatted_date = interview_date.strftime(
            "%d %B %Y"
        )

        formatted_time = interview_time.strftime(
            "%I:%M %p"
        )

        schedule_text = (
            "Interview Schedule\n"
            f"Date: {formatted_date}\n"
            f"Time: {formatted_time}\n"
            f"Interviewer: {interviewer}"
        )

        if body:
            body = body + "\n\n" + schedule_text
        else:
            body = schedule_text

    # ==================================================
    # EMAIL VALIDATION
    # ==================================================

    if not recipient or not subject or not body:

        flash(
            "Email recipient, subject and message are required. "
            "Candidate status was not changed.",
            "error",
        )

        return _candidate_list_redirect(active_tab)

    conn = get_connection()

    try:

        with conn.cursor() as cur:

            # ==================================================
            # CHECK CANDIDATE ACCESS
            # ==================================================

            candidate = candidate_is_accessible(
                cur,
                candidate_id,
                lock=True,
            )

            if candidate["status"] not in config["statuses"]:

                conn.rollback()

                flash(
                    "Candidate status has changed or is not in this stage.",
                    "error",
                )

                return _candidate_list_redirect(active_tab)

            # ==================================================
            # CHECK PREVIOUS INTERVIEW TIME
            # ==================================================

            allowed, error_message = (
                _check_interview_decision_allowed(
                    cur,
                    candidate_id,
                    active_tab,
                )
            )

            if not allowed:

                conn.rollback()

                flash(
                    error_message,
                    "error",
                )

                return _candidate_list_redirect(active_tab)

            # ==================================================
            # STEP 1: SEND EMAIL
            # ==================================================

            try:

                _send_candidate_decision_email(
                    recipient,
                    subject,
                    body,
                )

            except Exception:

                conn.rollback()

                current_app.logger.exception(
                    "Candidate acceptance email failed."
                )

                flash(
                    "Email could not be sent. Candidate status "
                    "and history were not changed.",
                    "error",
                )

                return _candidate_list_redirect(active_tab)

            # ==================================================
            # STEP 2: SAVE INTERVIEW SCHEDULE
            # ==================================================

            if interview_stage:

                cur.execute(
                    """
                    INSERT INTO public.candidate_interviews
                    (
                        candidate_id,
                        interview_stage,
                        interview_date,
                        interview_time,
                        interviewer,
                        status,
                        created_by
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, 'scheduled', %s
                    )

                    ON CONFLICT (
                        candidate_id,
                        interview_stage
                    )

                    DO UPDATE SET
                        interview_date = EXCLUDED.interview_date,
                        interview_time = EXCLUDED.interview_time,
                        interviewer = EXCLUDED.interviewer,
                        status = 'scheduled',
                        reminder_sent_at = NULL,
                        overdue_reminder_sent_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        candidate_id,
                        interview_stage,
                        interview_date,
                        interview_time,
                        interviewer,
                        session.get("user_id"),
                    ),
                )

            # ==================================================
            # STEP 3: UPDATE CANDIDATE STATUS
            # ==================================================

            cur.execute(
                """
                UPDATE candidates
                SET status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    next_status,
                    candidate_id,
                ),
            )

            # ==================================================
            # STEP 4: RECORD STATUS HISTORY
            # ==================================================

            record_status_history(
                cur,
                candidate_id,
                candidate["status"],
                next_status,
                "accept",
                remarks=remarks,
            )

        # ==================================================
        # COMMIT TRANSACTION
        # ==================================================

        conn.commit()

        flash(
            "Email sent and candidate moved to "
            + next_status.replace("_", " ").title()
            + (
                ". Interview schedule and interviewer saved."
                if interview_stage
                else "."
            ),
            "success",
        )

    except HTTPException:

        conn.rollback()
        raise

    except Exception:

        conn.rollback()

        current_app.logger.exception(
            "Failed to accept candidate."
        )

        flash(
            "Could not complete candidate acceptance. "
            "Please check the server log.",
            "error",
        )

    finally:

        conn.close()

    return _candidate_list_redirect(active_tab)




@candidate_bp.route("/interview-schedules", methods=["GET"])
@login_required
def interview_schedules():

    from flask import request, session, render_template, flash, redirect, url_for
    from datetime import datetime
    from werkzeug.exceptions import HTTPException

    conn = get_connection()

    try:
        # --------------------------------------------------
        # 1. GET FILTER PARAMETERS
        # --------------------------------------------------

        stage_filter = (request.args.get("stage") or "").strip()
        search_query = (request.args.get("search") or "").strip()

        from_date_filter = (request.args.get("from_date") or "").strip()
        to_date_filter = (request.args.get("to_date") or "").strip()

        status_filter = (request.args.get("status") or "").strip()

        branch_filter = (request.args.get("branch_id") or "").strip()
        department_filter = (request.args.get("department_id") or "").strip()

        # --------------------------------------------------
        # 1.1 PAGINATION PARAMETERS
        # --------------------------------------------------

        PER_PAGE = 10

        try:
            page = int(request.args.get("page", 1))
        except (TypeError, ValueError):
            page = 1

        page = max(page, 1)

        # --------------------------------------------------
        # 2. VALIDATE DATE FILTERS
        # --------------------------------------------------

        if from_date_filter:
            try:
                datetime.strptime(from_date_filter, "%Y-%m-%d")
            except ValueError:
                from_date_filter = ""

        if to_date_filter:
            try:
                datetime.strptime(to_date_filter, "%Y-%m-%d")
            except ValueError:
                to_date_filter = ""

        if (
            from_date_filter
            and to_date_filter
            and from_date_filter > to_date_filter
        ):
            flash("From Date cannot be later than To Date.", "warning")
            from_date_filter = ""
            to_date_filter = ""

        # --------------------------------------------------
        # 3. VALIDATE BRANCH / DEPARTMENT IDs
        # --------------------------------------------------

        try:
            branch_filter = int(branch_filter) if branch_filter else None
        except (TypeError, ValueError):
            branch_filter = None

        try:
            department_filter = (
                int(department_filter)
                if department_filter
                else None
            )
        except (TypeError, ValueError):
            department_filter = None

        user_role = session.get("role")
        user_id = session.get("user_id")
        session_department_id = session.get("department_id")

        is_super_admin = user_role == "super_admin"

        # --------------------------------------------------
        # 4. GET ACCESSIBLE DEPARTMENTS
        # --------------------------------------------------

        available_department_ids = []

        if not is_super_admin:

            available_departments = get_available_candidate_departments(
                user_role,
                session_department_id,
                user_id
            )

            for department in available_departments:

                if isinstance(department, dict):
                    department_id = (
                        department.get("id")
                        or department.get("department_id")
                    )

                elif isinstance(department, (tuple, list)):
                    department_id = department[0]

                else:
                    department_id = department

                try:
                    department_id = int(department_id)

                    if department_id not in available_department_ids:
                        available_department_ids.append(department_id)

                except (TypeError, ValueError):
                    continue

        # --------------------------------------------------
        # 5. LOAD AVAILABLE BRANCHES
        # --------------------------------------------------

        with conn.cursor() as cur:

            if is_super_admin:

                cur.execute("""
                    SELECT
                        id,
                        name
                    FROM branches
                    WHERE is_active = TRUE
                    ORDER BY name
                """)

            else:

                if available_department_ids:

                    cur.execute("""
                        SELECT DISTINCT
                            b.id,
                            b.name
                        FROM branches b
                        INNER JOIN departments d
                            ON d.branch_id = b.id
                        WHERE
                            b.is_active = TRUE
                            AND d.id = ANY(%s)
                            AND d.is_active = TRUE
                        ORDER BY b.name
                    """, (available_department_ids,))

                else:

                    cur.execute("""
                        SELECT
                            id,
                            name
                        FROM branches
                        WHERE 1 = 0
                    """)

            branches = rows_as_dicts(cur)

        available_branch_ids = []

        for branch in branches:
            try:
                available_branch_ids.append(int(branch["id"]))
            except (TypeError, ValueError):
                continue

        # --------------------------------------------------
        # 6. VALIDATE SELECTED BRANCH
        # --------------------------------------------------

        if branch_filter and branch_filter not in available_branch_ids:

            branch_filter = None
            department_filter = None

            flash(
                "You do not have permission to access this branch.",
                "warning"
            )

        # --------------------------------------------------
        # 7. LOAD DEPARTMENTS FOR SELECTED BRANCH
        # --------------------------------------------------

        departments = []

        if branch_filter:

            with conn.cursor() as cur:

                if is_super_admin:

                    cur.execute("""
                        SELECT
                            id,
                            name,
                            short_name,
                            branch_id
                        FROM departments
                        WHERE
                            branch_id = %s
                            AND is_active = TRUE
                        ORDER BY name
                    """, (branch_filter,))

                elif available_department_ids:

                    cur.execute("""
                        SELECT
                            id,
                            name,
                            short_name,
                            branch_id
                        FROM departments
                        WHERE
                            branch_id = %s
                            AND is_active = TRUE
                            AND id = ANY(%s)
                        ORDER BY name
                    """, (
                        branch_filter,
                        available_department_ids
                    ))

                else:

                    cur.execute("""
                        SELECT
                            id,
                            name,
                            short_name,
                            branch_id
                        FROM departments
                        WHERE 1 = 0
                    """)

                departments = rows_as_dicts(cur)

        # --------------------------------------------------
        # 8. VALIDATE SELECTED DEPARTMENT
        # --------------------------------------------------

        available_selected_department_ids = []

        for department in departments:
            try:
                available_selected_department_ids.append(
                    int(department["id"])
                )
            except (TypeError, ValueError):
                continue

        if department_filter:

            if not branch_filter:
                department_filter = None

            elif department_filter not in available_selected_department_ids:

                department_filter = None

                flash(
                    "Selected department is not available for this branch.",
                    "warning"
                )

        # --------------------------------------------------
        # 9. COMMON SQL CONDITIONS
        # --------------------------------------------------

        common_conditions = []
        common_params = []

        # Department permission
        if not is_super_admin:

            if available_department_ids:

                common_conditions.append(
                    "c.department_id = ANY(%s)"
                )

                common_params.append(available_department_ids)

            else:
                common_conditions.append("1 = 0")

        # Branch filter
        if branch_filter:

            common_conditions.append(
                "c.branch_id = %s"
            )

            common_params.append(branch_filter)

        # Department filter
        if department_filter:

            common_conditions.append(
                "c.department_id = %s"
            )

            common_params.append(department_filter)

        # Interview stage
        if stage_filter:

            common_conditions.append(
                "ci.interview_stage = %s"
            )

            common_params.append(stage_filter)

        # Search filter
        if search_query:

            search_value = f"%{search_query}%"

            common_conditions.append("""
                (
                    c.name ILIKE %s
                    OR c.email ILIKE %s
                    OR c.phone ILIKE %s
                    OR c.position ILIKE %s
                    OR b.name ILIKE %s
                    OR d.name ILIKE %s
                    OR ci.interviewer ILIKE %s
                )
            """)

            common_params.extend([
                search_value,
                search_value,
                search_value,
                search_value,
                search_value,
                search_value,
                search_value
            ])

        # From Date
        if from_date_filter:

            common_conditions.append(
                "ci.interview_date >= %s::date"
            )

            common_params.append(from_date_filter)

        # To Date
        if to_date_filter:

            common_conditions.append(
                "ci.interview_date <= %s::date"
            )

            common_params.append(to_date_filter)

        common_where = (
            "WHERE " + " AND ".join(common_conditions)
            if common_conditions
            else ""
        )

        # --------------------------------------------------
        # 10. LOAD INTERVIEW STATISTICS
        # --------------------------------------------------

        stats = {
            "total": 0,
            "upcoming": 0,
            "completed": 0,
            "cancelled": 0
        }

        with conn.cursor() as cur:

            cur.execute(f"""
                SELECT
                    COUNT(*) AS total,

                    COUNT(*) FILTER (
                        WHERE
                            ci.status = 'scheduled'
                            AND (
                                ci.interview_date,
                                COALESCE(ci.interview_time, TIME '00:00')
                            ) >= (
                                CURRENT_DATE,
                                CURRENT_TIME
                            )
                    ) AS upcoming,

                    COUNT(*) FILTER (
                        WHERE ci.status = 'completed'
                    ) AS completed,

                    COUNT(*) FILTER (
                        WHERE ci.status = 'cancelled'
                    ) AS cancelled

                FROM candidate_interviews ci

                INNER JOIN candidates c
                    ON c.id = ci.candidate_id

                LEFT JOIN branches b
                    ON b.id = c.branch_id

                LEFT JOIN departments d
                    ON d.id = c.department_id

                {common_where}
            """, tuple(common_params))

            stats_row = cur.fetchone()

            if stats_row:

                if isinstance(stats_row, dict):

                    stats = {
                        "total": stats_row.get("total", 0),
                        "upcoming": stats_row.get("upcoming", 0),
                        "completed": stats_row.get("completed", 0),
                        "cancelled": stats_row.get("cancelled", 0)
                    }

                else:

                    stats = {
                        "total": stats_row[0] or 0,
                        "upcoming": stats_row[1] or 0,
                        "completed": stats_row[2] or 0,
                        "cancelled": stats_row[3] or 0
                    }

        # --------------------------------------------------
        # 11. INTERVIEW STATUS FILTER
        # --------------------------------------------------

        list_conditions = list(common_conditions)
        list_params = list(common_params)

        if status_filter == "scheduled":

            list_conditions.append("""
                ci.status = 'scheduled'
                AND (
                    ci.interview_date,
                    COALESCE(ci.interview_time, TIME '00:00')
                ) >= (
                    CURRENT_DATE,
                    CURRENT_TIME
                )
            """)

        elif status_filter in ("completed", "cancelled"):

            list_conditions.append(
                "ci.status = %s"
            )

            list_params.append(status_filter)

        elif status_filter == "overdue":

            list_conditions.append("""
                ci.status = 'scheduled'
                AND (
                    ci.interview_date,
                    COALESCE(ci.interview_time, TIME '00:00')
                ) < (
                    CURRENT_DATE,
                    CURRENT_TIME
                )
            """)

        list_where = (
            "WHERE " + " AND ".join(list_conditions)
            if list_conditions
            else ""
        )

        # --------------------------------------------------
        # 12. COUNT FILTERED INTERVIEW SCHEDULES
        # --------------------------------------------------

        with conn.cursor() as cur:

            cur.execute(f"""
                SELECT COUNT(*) AS total

                FROM candidate_interviews ci

                INNER JOIN candidates c
                    ON c.id = ci.candidate_id

                LEFT JOIN branches b
                    ON b.id = c.branch_id

                LEFT JOIN departments d
                    ON d.id = c.department_id

                {list_where}
            """, tuple(list_params))

            count_row = cur.fetchone()

            if isinstance(count_row, dict):
                total_records = count_row.get("total", 0) or 0
            else:
                total_records = count_row[0] if count_row else 0

        # --------------------------------------------------
        # 12.1 CALCULATE PAGINATION
        # --------------------------------------------------

        total_pages = max(
            1,
            (total_records + PER_PAGE - 1) // PER_PAGE
        )

        # Prevent invalid page numbers
        page = min(page, total_pages)

        offset = (page - 1) * PER_PAGE

        start_record = (
            offset + 1 if total_records > 0 else 0
        )

        end_record = min(
            offset + PER_PAGE,
            total_records
        )

        # --------------------------------------------------
        # 13. LOAD PAGINATED INTERVIEW SCHEDULES
        # --------------------------------------------------

        with conn.cursor() as cur:

            cur.execute(f"""
                SELECT
                    ci.id AS interview_id,
                    ci.candidate_id,
                    ci.interview_stage,
                    ci.interview_date,
                    ci.interview_time,
                    ci.interviewer,
                    ci.meeting_link,
                    ci.created_at,

                    c.name AS candidate_name,
                    c.email AS email,
                    c.phone AS phone,
                    c.position AS position,
                    ci.status AS status,
                    c.department_id,
                    c.branch_id,

                    b.name AS branch_name,
                    d.name AS department_name

                FROM candidate_interviews ci

                INNER JOIN candidates c
                    ON c.id = ci.candidate_id

                LEFT JOIN branches b
                    ON b.id = c.branch_id

                LEFT JOIN departments d
                    ON d.id = c.department_id

                {list_where}

                ORDER BY
                    ci.interview_date ASC,
                    ci.interview_time ASC,
                    ci.id DESC

                LIMIT %s
                OFFSET %s

            """, tuple(list_params) + (PER_PAGE, offset))

            interviews = rows_as_dicts(cur)

        # --------------------------------------------------
        # 14. RENDER PAGE
        # --------------------------------------------------

        return render_template(
            "interview_schedules.html",

            # Interview records
            schedules=interviews,
            interviews=interviews,

            # Statistics
            total_interviews=stats.get("total", 0),
            upcoming_interviews=stats.get("upcoming", 0),
            completed_interviews=stats.get("completed", 0),
            cancelled_interviews=stats.get("cancelled", 0),
            stats=stats,

            # Branch / Department
            branches=branches,
            departments=departments,

            # Filters
            selected_stage=stage_filter,
            stage_filter=stage_filter,

            search=search_query,
            search_query=search_query,

            from_date_filter=from_date_filter,
            to_date_filter=to_date_filter,

            status_filter=status_filter,

            branch_filter=branch_filter,
            department_filter=department_filter,

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

    except HTTPException:
        raise

    except Exception as e:

        current_app.logger.exception(
            "Error loading interview schedules"
        )

        flash(
            "Unable to load interview schedules. Please try again.",
            "danger"
        )

        return redirect(
            url_for("candidate.interview_schedules")
        )

    finally:
        conn.close()



@candidate_bp.route(
    "/interview-schedules/<int:interview_id>/status",
    methods=["POST"]
)
@login_required
def update_interview_status(interview_id):

    from flask import request, session, flash, redirect, url_for
    from werkzeug.exceptions import HTTPException

    conn = get_connection()

    try:
        new_status = (
            request.form.get("status") or ""
        ).strip().lower()

        allowed_statuses = {
            "scheduled",
            "completed",
            "cancelled"
        }

        if new_status not in allowed_statuses:

            flash(
                "Invalid interview status.",
                "danger"
            )

            return redirect(
                url_for("candidate.interview_schedules")
            )

        user_role = session.get("role")
        user_id = session.get("user_id")
        session_department_id = session.get("department_id")

        is_super_admin = user_role == "super_admin"

        with conn.cursor() as cur:

            # ----------------------------------------------
            # CHECK INTERVIEW EXISTS
            # ----------------------------------------------

            cur.execute("""
                SELECT
                    ci.id,
                    ci.candidate_id,
                    c.department_id

                FROM candidate_interviews ci

                INNER JOIN candidates c
                    ON c.id = ci.candidate_id

                WHERE ci.id = %s
            """, (interview_id,))

            interview = cur.fetchone()

            if not interview:

                flash(
                    "Interview schedule not found.",
                    "danger"
                )

                return redirect(
                    url_for("candidate.interview_schedules")
                )

            if isinstance(interview, dict):

                candidate_id = interview["candidate_id"]
                candidate_department_id = interview["department_id"]

            else:

                candidate_id = interview[1]
                candidate_department_id = interview[2]

            # ----------------------------------------------
            # CHECK PERMISSION
            # ----------------------------------------------

            if not is_super_admin:

                available_departments = get_available_candidate_departments(
                    user_role,
                    session_department_id,
                    user_id
                )

                available_department_ids = []

                for department in available_departments:

                    if isinstance(department, dict):

                        department_id = (
                            department.get("id")
                            or department.get("department_id")
                        )

                    elif isinstance(department, (tuple, list)):

                        department_id = department[0]

                    else:

                        department_id = department

                    try:
                        available_department_ids.append(
                            int(department_id)
                        )
                    except (TypeError, ValueError):
                        continue

                try:
                    candidate_department_id = int(
                        candidate_department_id
                    )
                except (TypeError, ValueError):
                    candidate_department_id = None

                if candidate_department_id not in available_department_ids:

                    flash(
                        "You do not have permission to update this interview.",
                        "danger"
                    )

                    return redirect(
                        url_for("candidate.interview_schedules")
                    )

            # ----------------------------------------------
            # UPDATE STATUS
            # ----------------------------------------------

            cur.execute("""
                UPDATE candidate_interviews
                SET
                    status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (
                new_status,
                interview_id
            ))

        conn.commit()

        flash(
            "Interview status updated successfully.",
            "success"
        )

    except HTTPException:
        raise

    except Exception:

        conn.rollback()

        current_app.logger.exception(
            "Error updating interview status"
        )

        flash(
            "Unable to update interview status.",
            "danger"
        )

    finally:
        conn.close()

    # --------------------------------------------------
    # PRESERVE ALL FILTERS AFTER STATUS UPDATE
    # --------------------------------------------------

    return redirect(url_for(
        "candidate.interview_schedules",

        stage=request.form.get("stage") or None,
        search=request.form.get("search") or None,

        branch_id=request.form.get("branch_id") or None,
        department_id=request.form.get("department_id") or None,

        from_date=request.form.get("from_date") or None,
        to_date=request.form.get("to_date") or None,

        status=request.form.get("status_filter") or None
    ))

# --------------------------------------------------
# REJECT CANDIDATE
# URL: /candidates/<id>/reject (POST)
# --------------------------------------------------
@candidate_bp.route(
    "/candidates/<int:candidate_id>/reject",
    methods=["POST"],
)
@login_required
def reject_candidate(candidate_id):

    active_tab = (
        request.form.get("tab") or "candidates"
    ).strip()

    if active_tab not in PIPELINE_TABS:
        abort(400)

    config = PIPELINE_TABS[active_tab]
    reject_status = config.get("reject_status")

    if not reject_status:
        abort(400)

    recipient = (
        request.form.get("email_to") or ""
    ).strip()

    subject = (
        request.form.get("email_subject") or ""
    ).strip()

    body = (
        request.form.get("email_body") or ""
    ).strip()

    # Required reason - Database only
    remarks = (
        request.form.get("remarks") or ""
    ).strip()

    if not remarks:
        flash(
            "Please enter a rejection reason. "
            "Candidate status was not changed.",
            "error",
        )
        return _candidate_list_redirect(active_tab)

    if len(remarks) > 1000:
        flash(
            "Reason cannot exceed 1000 characters. "
            "Candidate status was not changed.",
            "error",
        )
        return _candidate_list_redirect(active_tab)

    if not recipient or not subject or not body:
        flash(
            "Email recipient, subject and message are required. "
            "Candidate status was not changed.",
            "error",
        )
        return _candidate_list_redirect(active_tab)

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            candidate = candidate_is_accessible(
                cur,
                candidate_id,
                lock=True,
            )

            if candidate["status"] not in config["statuses"]:
                conn.rollback()

                flash(
                    "Candidate could not be rejected from this stage.",
                    "error",
                )

                return _candidate_list_redirect(active_tab)

            # ==================================================
            # CHECK INTERVIEW TIME
            # ==================================================

            allowed, error_message = (
                _check_interview_decision_allowed(
                    cur,
                    candidate_id,
                    active_tab,
                )
            )

            if not allowed:
                conn.rollback()

                flash(error_message, "error")

                return _candidate_list_redirect(active_tab)
            
            # STEP 1: Send rejection email.
            # IMPORTANT: Reason is NOT included in email.
            try:
                _send_candidate_decision_email(
                    recipient,
                    subject,
                    body,
                )

            except Exception:
                conn.rollback()

                current_app.logger.exception(
                    "Candidate rejection email failed. "
                    "Candidate status was not updated."
                )

                flash(
                    "Rejection email could not be sent. "
                    "Candidate status and history were not changed. "
                    "Please check SMTP settings and try again.",
                    "error",
                )

                return _candidate_list_redirect(active_tab)

            # STEP 2: Change candidate status.
            cur.execute(
                """
                UPDATE candidates
                SET status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (reject_status, candidate_id),
            )

            # STEP 3: Save reason in database remarks only.
            record_status_history(
                cur,
                candidate_id,
                candidate["status"],
                reject_status,
                "reject",
                remarks=remarks,
            )

        conn.commit()

        flash(
            "Rejection email sent and candidate status changed to "
            + reject_status.replace("_", " ").title()
            + ".",
            "success",
        )

    except HTTPException:
        conn.rollback()
        raise

    except Exception:
        conn.rollback()

        current_app.logger.exception(
            "Failed to reject candidate."
        )

        flash(
            "Could not complete candidate rejection. "
            "Please check the server log.",
            "error",
        )

    finally:
        conn.close()

    return _candidate_list_redirect(active_tab)


# --------------------------------------------------
# SKIP CANDIDATE
# URL: /candidates/<id>/skip (POST)
# --------------------------------------------------
@candidate_bp.route(
    "/candidates/<int:candidate_id>/skip",
    methods=["POST"],
)
@login_required
def skip_candidate(candidate_id):

    active_tab = (
        request.form.get("tab") or ""
    ).strip()

    if active_tab not in PIPELINE_TABS:
        abort(400)

    config = PIPELINE_TABS[active_tab]
    skip_status = config.get("skip_status")

    if not skip_status:
        abort(400)

    recipient = (
        request.form.get("email_to") or ""
    ).strip()

    subject = (
        request.form.get("email_subject") or ""
    ).strip()

    body = (
        request.form.get("email_body") or ""
    ).strip()

    # Required reason - Database only
    remarks = (
        request.form.get("remarks") or ""
    ).strip()

    if not remarks:
        flash(
            "Please enter a skip reason. "
            "Candidate status was not changed.",
            "error",
        )
        return _candidate_list_redirect(active_tab)

    if len(remarks) > 1000:
        flash(
            "Reason cannot exceed 1000 characters. "
            "Candidate status was not changed.",
            "error",
        )
        return _candidate_list_redirect(active_tab)

    if not recipient or not subject or not body:
        flash(
            "Email recipient, subject and message are required. "
            "Candidate status was not changed.",
            "error",
        )
        return _candidate_list_redirect(active_tab)

    conn = get_connection()

    try:
        with conn.cursor() as cur:

            candidate = candidate_is_accessible(
                cur,
                candidate_id,
                lock=True,
            )

            # Candidate must still be in this stage.
            if candidate["status"] not in config["statuses"]:
                conn.rollback()

                flash(
                    "Candidate could not be skipped from this stage.",
                    "error",
                )

                return _candidate_list_redirect(active_tab)

            # ==================================================
            # CHECK INTERVIEW TIME FOR SECOND INTERVIEW
            # ==================================================
            if active_tab == "second_interview":
                allowed, error_message = _check_interview_decision_allowed(
                    cur,
                    candidate_id,
                    active_tab,
                )
                if not allowed:
                    conn.rollback()
                    flash(error_message, "error")
                    return _candidate_list_redirect(active_tab)

            # STEP 1: Send skip email.
            # IMPORTANT: Reason is NOT included in email.
            try:
                _send_candidate_decision_email(
                    recipient,
                    subject,
                    body,
                )

            except Exception:
                conn.rollback()

                current_app.logger.exception(
                    "Candidate skip email failed. "
                    "Candidate status was not updated."
                )

                flash(
                    "Skip email could not be sent. Candidate status "
                    "and history were not changed. Please check "
                    "SMTP settings and try again.",
                    "error",
                )

                return _candidate_list_redirect(active_tab)

            # STEP 2: Move candidate to skip destination.
            cur.execute(
                """
                UPDATE candidates
                SET status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (skip_status, candidate_id),
            )

            # STEP 3: Save reason in database remarks only.
            record_status_history(
                cur,
                candidate_id,
                candidate["status"],
                skip_status,
                "skip",
                remarks=remarks,
            )

        conn.commit()

        flash(
            "Skip email sent and candidate moved to "
            + skip_status.replace("_", " ").title()
            + ".",
            "success",
        )

    except HTTPException:
        conn.rollback()

        raise

    except Exception:
        conn.rollback()

        current_app.logger.exception(
            "Failed to skip candidate stage."
        )

        flash(
            "Could not complete candidate skip. "
            "Please check the server log.",
            "error",
        )

    finally:
        conn.close()

    return _candidate_list_redirect(active_tab)
# --------------------------------------------------
# CANCEL CANDIDATE FROM A PIPELINE STAGE
# URL: /candidates/<id>/cancel (POST)
# --------------------------------------------------
@candidate_bp.route(
    "/candidates/<int:candidate_id>/cancel",
    methods=["POST"],
)
@login_required
def cancel_candidate(candidate_id):
    active_tab = (
        request.form.get("tab") or "candidates"
    ).strip()

    if active_tab not in PIPELINE_TABS:
        abort(400)

    config = PIPELINE_TABS[active_tab]
    cancel_status = config["cancel_status"]

    # Candidates and Joined do not have a Cancel action.
    if not cancel_status:
        abort(400)

    remarks = (
        request.form.get("remarks") or ""
    ).strip() or None

    conn = get_connection()

    try:
        with conn.cursor() as cur:
            candidate = candidate_is_accessible(
                cur,
                candidate_id,
                lock=True,
            )

            if candidate["status"] not in config["statuses"]:
                conn.rollback()

                flash(
                    "Candidate could not be cancelled from this stage.",
                    "error",
                )

                return _candidate_list_redirect(active_tab)

            cur.execute(
                """
                UPDATE candidates
                SET status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (cancel_status, candidate_id),
            )

            record_status_history(
                cur,
                candidate_id,
                candidate["status"],
                cancel_status,
                "cancel",
                remarks=remarks,
            )

        conn.commit()

        flash(
            "Candidate status changed to "
            + cancel_status.replace("_", " ").title()
            + ".",
            "success",
        )

    except HTTPException:
        conn.rollback()
        raise

    except Exception:
        conn.rollback()

        current_app.logger.exception("Failed to cancel candidate")

        flash(
            "Could not cancel candidate. Please check the server log.",
            "error",
        )

    finally:
        conn.close()

    return _candidate_list_redirect(active_tab)

# --------------------------------------------------
# MARK CANDIDATE AS RESIGNED
# URL: /candidates/<id>/resign (POST)
# --------------------------------------------------
@candidate_bp.route(
    "/candidates/<int:candidate_id>/resign",
    methods=["POST"],
)
@login_required
def resign_candidate(candidate_id):
    active_tab = (request.form.get("tab") or "joined").strip()

    if active_tab != "joined":
        abort(400)

    # Required reason
    remarks = (
        request.form.get("remarks") or ""
    ).strip()

    if not remarks:
        flash(
            "Please enter a reason before marking this candidate as resign.",
            "error",
        )
        return _candidate_list_redirect("joined")

    if len(remarks) > 1000:
        flash(
            "Reason cannot exceed 1000 characters.",
            "error",
        )
        return _candidate_list_redirect("joined")

    conn = get_connection()

    try:
        with conn.cursor() as cur:
            candidate = candidate_is_accessible(
                cur,
                candidate_id,
                lock=True,
            )

            if candidate["status"] != "joined":
                conn.rollback()
                flash(
                    "Only joined candidates can be marked as resign.",
                    "error",
                )
                return _candidate_list_redirect("joined")

            cur.execute(
                """
                UPDATE candidates
                SET status = 'resign',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (candidate_id,),
            )

            record_status_history(
                cur,
                candidate_id,
                "joined",
                "resign",
                "resign",
                remarks=remarks,
            )

        conn.commit()

        flash(
            "Candidate status changed to Resign.",
            "success",
        )

    except HTTPException:
        conn.rollback()
        raise

    except Exception:
        conn.rollback()
        current_app.logger.exception(
            "Failed to mark candidate as resign"
        )
        flash(
            "Could not mark candidate as resign. "
            "Please check the server log.",
            "error",
        )

    finally:
        conn.close()

    return _candidate_list_redirect("resign")

# --------------------------------------------------
# EDIT CANDIDATE
# URL: /candidates/<id>/edit (GET / POST)
# --------------------------------------------------


@candidate_bp.route(
    "/candidates/<int:candidate_id>/edit",
    methods=["GET", "POST"],
)
@login_required
def edit_candidate(candidate_id):

    role = current_role()
    department_id = current_department_id()
    user_id = session.get("user_id")

    available_departments = get_available_candidate_departments(
        role,
        department_id,
        user_id,
    )

    allowed_department_ids = [
        int(department["id"])
        for department in available_departments
    ]

    conn = get_connection()
    uploaded_paths = []
    old_paths_to_remove = []
    committed = False

    try:
        with conn.cursor() as cur:

            # ------------------------------------------
            # Check candidate edit permission
            # ------------------------------------------
            if role == "department_head":

                cur.execute(
                    """
                    SELECT c.id
                    FROM public.candidates c
                    WHERE c.id = %s
                      AND EXISTS (
                          SELECT 1
                          FROM public.user_department_permissions udp
                          WHERE udp.user_id = %s
                            AND udp.branch_id = c.branch_id
                            AND udp.department_id = c.department_id
                            AND udp.can_edit = TRUE
                      )
                    """ + (
                        " FOR UPDATE OF c"
                        if request.method == "POST"
                        else ""
                    ),
                    (candidate_id, user_id),
                )

                edit_permission = cur.fetchone()

                if edit_permission is None:

                    cur.execute(
                        """
                        SELECT
                            c.id,
                            c.name,
                            c.phone,
                            c.email,
                            c.position,
                            c.address,
                            c.nrc,
                            c.department_id,
                            c.branch_id,
                            c.cv_file_path,
                            c.other_attachment_path,
                            c.status,
                            c.created_at,
                            c.updated_at,
                            d.name AS department_name,
                            b.name AS branch_name
                        FROM public.candidates c
                        JOIN public.departments d
                            ON d.id = c.department_id
                        LEFT JOIN public.branches b
                            ON b.id = c.branch_id
                        WHERE c.id = %s
                        """,
                        (candidate_id,),
                    )

                    denied_candidate = row_as_dict(cur)

                    if denied_candidate is None:
                        abort(404)

                    conn.rollback()

                    return render_template(
                        "edit_candidate.html",
                        candidate=denied_candidate,
                        departments=[],
                        branches=[],
                        permission_denied=True,
                    )

            else:

                candidate_is_accessible(
                    cur,
                    candidate_id,
                    lock=(request.method == "POST"),
                    permission="can_edit",
                )

            # ------------------------------------------
            # GET: Display edit form
            # ------------------------------------------
            if request.method == "GET":

                cur.execute(
                    """
                    SELECT
                        c.id,
                        c.name,
                        c.phone,
                        c.email,
                        c.position,
                        c.address,
                        c.nrc,
                        c.department_id,
                        c.branch_id,
                        c.cv_file_path,
                        c.other_attachment_path,
                        c.status,
                        c.created_at,
                        c.updated_at,
                        d.name AS department_name,
                        b.name AS branch_name
                    FROM public.candidates c
                    JOIN public.departments d
                        ON d.id = c.department_id
                    LEFT JOIN public.branches b
                        ON b.id = c.branch_id
                    WHERE c.id = %s
                    """,
                    (candidate_id,),
                )

                candidate = row_as_dict(cur)

                if candidate is None:
                    abort(404)

                # --------------------------------------
                # Load departments by branch
                # --------------------------------------
                if role == "department_head":

                    cur.execute(
                        """
                        SELECT DISTINCT
                            d.id,
                            d.name,
                            d.short_name,
                            d.branch_id
                        FROM public.departments d
                        JOIN public.user_department_permissions udp
                            ON udp.department_id = d.id
                           AND udp.branch_id = d.branch_id
                        WHERE udp.user_id = %s
                          AND udp.can_edit = TRUE
                          AND d.is_active = TRUE
                        ORDER BY d.name
                        """,
                        (user_id,),
                    )

                else:

                    cur.execute(
                        """
                        SELECT
                            d.id,
                            d.name,
                            d.short_name,
                            d.branch_id
                        FROM public.departments d
                        WHERE d.is_active = TRUE
                        ORDER BY d.name
                        """
                    )

                departments = rows_as_dicts(cur)

                # --------------------------------------
                # Load available branches
                # --------------------------------------
                if role == "department_head":

                    cur.execute(
                        """
                        SELECT DISTINCT
                            b.id,
                            b.name
                        FROM public.branches b
                        JOIN public.user_department_permissions udp
                            ON udp.branch_id = b.id
                        WHERE udp.user_id = %s
                          AND udp.can_edit = TRUE
                          AND b.is_active = TRUE
                        ORDER BY b.name
                        """,
                        (user_id,),
                    )

                else:

                    cur.execute(
                        """
                        SELECT id, name
                        FROM public.branches
                        WHERE is_active = TRUE
                        ORDER BY name
                        """
                    )

                branches = rows_as_dicts(cur)

                return render_template(
                    "edit_candidate.html",
                    candidate=candidate,
                    departments=departments,
                    branches=branches,
                    permission_denied=False,
                )

            # ------------------------------------------
            # POST: Read submitted form
            # ------------------------------------------
            name = (
                request.form.get("name") or ""
            ).strip()

            phone = (
                request.form.get("phone") or ""
            ).strip()

            email = (
                request.form.get("email") or ""
            ).strip()

            position = (
                request.form.get("position") or ""
            ).strip()

            address = (
                request.form.get("address") or ""
            ).strip() or None

            nrc = (
                request.form.get("nrc") or ""
            ).strip() or None

            department_raw = (
                request.form.get("department_id") or ""
            ).strip()

            branch_raw = (
                request.form.get("branch_id") or ""
            ).strip()

            cv_file = request.files.get("cv_file")
            attachment_file = request.files.get(
                "other_attachment"
            )

            # ------------------------------------------
            # Validate submitted data
            # ------------------------------------------
            errors = []

            if not name or len(name) > 150:
                errors.append(
                    "Full name is required and must be "
                    "150 characters or fewer."
                )

            if not validate_phone(phone):
                errors.append(
                    "Enter a valid phone number containing "
                    "7-15 digits."
                )

            if not validate_email(email):
                errors.append(
                    "Enter a valid email address."
                )

            if not position:
                errors.append(
                    "Position is required."
                )

            elif len(position) > 150:
                errors.append(
                    "Position must be 150 characters or fewer."
                )

            if nrc and len(nrc) > 50:
                errors.append(
                    "NRC must be 50 characters or fewer."
                )

            if address and len(address) > 5000:
                errors.append(
                    "Address must be 5,000 characters or fewer."
                )

            # ------------------------------------------
            # Validate department
            # ------------------------------------------
            if (
                not department_raw.isdigit()
                or int(department_raw or "0") <= 0
            ):
                errors.append(
                    "Please select a valid department."
                )

            elif (
                role == "department_head"
                and int(department_raw) not in allowed_department_ids
            ):
                errors.append(
                    "You can only assign candidates "
                    "to your assigned departments."
                )

            # ------------------------------------------
            # Validate branch
            # ------------------------------------------
            if (
                not branch_raw.isdigit()
                or int(branch_raw or "0") <= 0
            ):
                errors.append(
                    "Please select a valid branch."
                )

            # ------------------------------------------
            # Validate branch + department relationship
            # ------------------------------------------
            if branch_raw.isdigit() and department_raw.isdigit():

                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM public.departments d
                        WHERE d.id = %s
                          AND d.branch_id = %s
                          AND d.is_active = TRUE
                    )
                    """,
                    (
                        int(department_raw),
                        int(branch_raw),
                    ),
                )

                branch_department_exists = cur.fetchone()[0]

                if not branch_department_exists:
                    errors.append(
                        "The selected department does not belong "
                        "to the selected branch."
                    )

                elif role == "department_head":

                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM public.user_department_permissions udp
                            WHERE udp.user_id = %s
                              AND udp.branch_id = %s
                              AND udp.department_id = %s
                              AND udp.can_edit = TRUE
                        )
                        """,
                        (
                            user_id,
                            int(branch_raw),
                            int(department_raw),
                        ),
                    )

                    if not cur.fetchone()[0]:
                        errors.append(
                            "You can only select branches and departments "
                            "assigned to you with edit permission."
                        )

            if errors:

                conn.rollback()

                for message in errors:
                    flash(message, "error")

                return redirect(
                    url_for(
                        "candidate.edit_candidate",
                        candidate_id=candidate_id,
                    )
                )

            # ------------------------------------------
            # Read existing file paths
            # ------------------------------------------
            cur.execute(
                """
                SELECT
                    cv_file_path,
                    other_attachment_path
                FROM public.candidates
                WHERE id = %s
                """,
                (candidate_id,),
            )

            existing_files = cur.fetchone()

            if existing_files is None:
                abort(404)

            old_cv_path, old_attachment_path = existing_files

            new_cv_path = old_cv_path
            new_attachment_path = old_attachment_path

            # ------------------------------------------
            # Replace CV
            # ------------------------------------------
            if cv_file and cv_file.filename:

                new_cv_path = save_upload(
                    cv_file,
                    "cv",
                )

                uploaded_paths.append(new_cv_path)

                if old_cv_path:
                    old_paths_to_remove.append(old_cv_path)

            # ------------------------------------------
            # Replace attachment
            # ------------------------------------------
            if attachment_file and attachment_file.filename:

                new_attachment_path = save_upload(
                    attachment_file,
                    "attachment",
                )

                uploaded_paths.append(new_attachment_path)

                if old_attachment_path:
                    old_paths_to_remove.append(
                        old_attachment_path
                    )

            # ------------------------------------------
            # Verify active department and branch
            # ------------------------------------------
            cur.execute(
                """
                SELECT id
                FROM public.departments
                WHERE id = %s
                  AND branch_id = %s
                  AND is_active = TRUE
                """,
                (
                    int(department_raw),
                    int(branch_raw),
                ),
            )

            if cur.fetchone() is None:
                raise ValueError(
                    "Selected department does not belong to "
                    "the selected branch or is inactive."
                )

            cur.execute(
                """
                SELECT id
                FROM public.branches
                WHERE id = %s
                  AND is_active = TRUE
                """,
                (int(branch_raw),),
            )

            if cur.fetchone() is None:
                raise ValueError(
                    "Selected branch does not exist or is inactive."
                )

            # ------------------------------------------
            # Read current values BEFORE update
            # ------------------------------------------
            cur.execute(
                """
                SELECT
                    name,
                    phone,
                    email,
                    position,
                    address,
                    nrc,
                    department_id,
                    branch_id,
                    cv_file_path,
                    other_attachment_path
                FROM public.candidates
                WHERE id = %s
                """,
                (candidate_id,),
            )

            old_values = cur.fetchone()

            if old_values is None:
                abort(404)

            (
                old_name,
                old_phone,
                old_email,
                old_position,
                old_address,
                old_nrc,
                old_department_id,
                old_branch_id,
                old_cv_file_path,
                old_other_attachment_path,
            ) = old_values

            # ------------------------------------------
            # Update candidate
            # ------------------------------------------
            new_department_id = int(department_raw)
            new_branch_id = int(branch_raw)

            cur.execute(
                """
                UPDATE public.candidates
                SET
                    name = %s,
                    phone = %s,
                    email = %s,
                    position = %s,
                    address = %s,
                    nrc = %s,
                    department_id = %s,
                    branch_id = %s,
                    cv_file_path = %s,
                    other_attachment_path = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    name,
                    phone,
                    email,
                    position,
                    address,
                    nrc,
                    new_department_id,
                    new_branch_id,
                    new_cv_path,
                    new_attachment_path,
                    candidate_id,
                ),
            )

            # ------------------------------------------
            # Build changed fields
            # ------------------------------------------
            changed_fields = {}

            if (old_name or "") != (name or ""):
                changed_fields["name"] = {
                    "old": old_name,
                    "new": name,
                }

            if (old_phone or "") != (phone or ""):
                changed_fields["phone"] = {
                    "old": old_phone,
                    "new": phone,
                }

            if (old_email or "") != (email or ""):
                changed_fields["email"] = {
                    "old": old_email,
                    "new": email,
                }

            if (old_position or "") != (position or ""):
                changed_fields["position"] = {
                    "old": old_position,
                    "new": position,
                }

            if (old_address or "") != (address or ""):
                changed_fields["address"] = {
                    "old": old_address,
                    "new": address,
                }

            if (old_nrc or "") != (nrc or ""):
                changed_fields["nrc"] = {
                    "old": old_nrc,
                    "new": nrc,
                }

            if old_department_id != new_department_id:
                changed_fields["department_id"] = {
                    "old": old_department_id,
                    "new": new_department_id,
                }

            if old_branch_id != new_branch_id:
                changed_fields["branch_id"] = {
                    "old": old_branch_id,
                    "new": new_branch_id,
                }

            if (
                cv_file
                and cv_file.filename
                and new_cv_path != old_cv_file_path
            ):
                changed_fields["cv_file"] = {
                    "old": "replaced",
                    "new": "uploaded",
                }

            if (
                attachment_file
                and attachment_file.filename
                and new_attachment_path != old_other_attachment_path
            ):
                changed_fields["other_attachment"] = {
                    "old": "replaced",
                    "new": "uploaded",
                }

            # ------------------------------------------
            # Record edit history
            # ------------------------------------------
            record_edit_history(
                cur,
                candidate_id,
                changed_fields,
            )

        # ------------------------------------------
        # Commit
        # ------------------------------------------
        conn.commit()
        committed = True

        # Remove old files after successful commit
        for old_path in old_paths_to_remove:
            try:
                remove_uploaded_file(old_path)
            except OSError:
                current_app.logger.exception(
                    "Failed to remove replaced candidate file"
                )

        flash(
            "Candidate details updated successfully.",
            "success",
        )

        return redirect(
            url_for("candidate.view_candidates")
        )

    except HTTPException:
        conn.rollback()
        raise

    except ValueError as exc:
        conn.rollback()

        flash(str(exc), "error")

        return redirect(
            url_for(
                "candidate.edit_candidate",
                candidate_id=candidate_id,
            )
        )

    except Exception:
        conn.rollback()

        current_app.logger.exception(
            "Failed to edit candidate"
        )

        flash(
            "Could not update candidate. "
            "Please check the database and server log.",
            "error",
        )

        return redirect(
            url_for(
                "candidate.edit_candidate",
                candidate_id=candidate_id,
            )
        )

    finally:
        conn.close()

        if not committed:
            for relative_path in uploaded_paths:
                try:
                    remove_uploaded_file(relative_path)
                except OSError:
                    current_app.logger.exception(
                        "Failed to clean up replaced candidate file"
                    )


@candidate_bp.route("/<int:candidate_id>/delete-permission", methods=["GET"])
@login_required
def check_delete_permission(candidate_id):

    from flask import jsonify, session

    role = current_role()

    # Super Admin can delete all accessible candidates
    if role == "super_admin":
        return jsonify({"can_delete": True})

    if role != "department_head":
        return jsonify({"can_delete": False})

    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM public.candidates c
                    JOIN public.user_department_permissions udp
                      ON udp.branch_id = c.branch_id
                     AND udp.department_id = c.department_id
                    WHERE c.id = %s
                      AND udp.user_id = %s
                      AND udp.can_delete = TRUE
                )
                """,
                (candidate_id, session.get("user_id")),
            )

            can_delete = cur.fetchone()[0]

        return jsonify({"can_delete": bool(can_delete)})

    finally:
        conn.close()

# --------------------------------------------------
# DELETE CANDIDATE
# URL: /candidates/<id>/delete (POST)
# --------------------------------------------------
@candidate_bp.route(
    "/candidates/<int:candidate_id>/delete",
    methods=["POST"],
)
@login_required
def delete_candidate(candidate_id):
    conn = get_connection()
    file_paths = []

    try:
        with conn.cursor() as cur:

            # Check delete permission for Department Head
            if current_role() == "department_head":
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM public.candidates c
                        JOIN public.user_department_permissions udp
                            ON udp.branch_id = c.branch_id
                        AND udp.department_id = c.department_id
                        WHERE c.id = %s
                        AND udp.user_id = %s
                        AND udp.can_delete = TRUE
                    )
                    """,
                    (candidate_id, session.get("user_id")),
                )

                can_delete = cur.fetchone()[0]

                if not can_delete:
                    conn.rollback()

                    return redirect(
                        url_for(
                            "candidate.view_candidates",
                            permission_denied=1,
                        )
                    )

            else:
                candidate_is_accessible(
                    cur,
                    candidate_id,
                    lock=True,
                    permission="can_delete",
                )

            cur.execute(
                """
                SELECT cv_file_path, other_attachment_path
                FROM candidates
                WHERE id = %s
                """,
                (candidate_id,),
            )

            row = cur.fetchone()

            if row is None:
                abort(404)

            file_paths = [p for p in row if p]

            # Delete status history first.
            cur.execute(
                """
                DELETE FROM candidate_status_history
                WHERE candidate_id = %s
                """,
                (candidate_id,),
            )

            # Delete edit history only.
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_name = 'candidate_edit_history'
                )
                """
            )

            if cur.fetchone()[0]:
                cur.execute(
                    """
                    DELETE FROM candidate_edit_history
                    WHERE candidate_id = %s
                    """,
                    (candidate_id,),
                )

            # Delete the candidate.
            cur.execute(
                """
                DELETE FROM candidates
                WHERE id = %s
                """,
                (candidate_id,),
            )

        conn.commit()

        for relative_path in file_paths:
            try:
                remove_uploaded_file(relative_path)
            except OSError:
                current_app.logger.exception(
                    "Failed to remove deleted candidate file"
                )

        flash(
            "Candidate and related history deleted successfully.",
            "success",
        )

    except HTTPException:
        conn.rollback()
        raise

    except Exception:
        conn.rollback()

        current_app.logger.exception("Failed to delete candidate")

        flash(
            "Could not delete candidate. "
            "The record may be referenced by another table.",
            "error",
        )

    finally:
        conn.close()

    return redirect(url_for("candidate.view_candidates"))

# --------------------------------------------------
# VIEW CANDIDATES PAGE
# URL: /candidates/view
# Super Admin: All departments
# Department Head: Own department only
# --------------------------------------------------


@candidate_bp.route("/candidates/view", methods=["GET"])
@login_required
def view_candidates():
    conn = None

    try:
        role = (session.get("role") or current_role() or "").strip().lower()
        user_id = session.get("user_id")

        conn = get_connection()

        with conn.cursor() as cur:
            sql = """
                SELECT
                    c.id,
                    c.name,
                    c.phone,
                    c.email,
                    c.address,
                    c.nrc,
                    c.position,
                    c.channel,
                    c.department_id,
                    c.branch_id,
                    c.cv_file_path,
                    c.other_attachment_path,
                    c.status,
                    c.created_at,
                    c.updated_at,
                    d.name AS department_name,
                    b.name AS branch_name,

                    CASE
                        WHEN %s = 'super_admin' THEN TRUE
                        ELSE COALESCE(udp.can_view, FALSE)
                    END AS can_view,

                    CASE
                        WHEN %s = 'super_admin' THEN TRUE
                        ELSE COALESCE(udp.can_edit, FALSE)
                    END AS can_edit,

                    CASE
                        WHEN %s = 'super_admin' THEN TRUE
                        ELSE COALESCE(udp.can_delete, FALSE)
                    END AS can_delete

                FROM public.candidates c

                JOIN public.departments d
                    ON d.id = c.department_id

                LEFT JOIN public.branches b
                    ON b.id = c.branch_id

                LEFT JOIN LATERAL (
                    SELECT
                        BOOL_OR(p.can_view) AS can_view,
                        BOOL_OR(p.can_edit) AS can_edit,
                        BOOL_OR(p.can_delete) AS can_delete
                    FROM public.user_department_permissions p
                    WHERE p.user_id = %s
                      AND p.branch_id = c.branch_id
                      AND p.department_id = c.department_id
                ) udp ON TRUE

                ORDER BY
                    c.created_at DESC NULLS LAST,
                    c.id DESC
            """

            cur.execute(sql, (role, role, role, user_id))
            candidates = rows_as_dicts(cur)

        return render_template(
            "view_candidate.html",
            candidates=candidates,
        )

    except Exception:
        current_app.logger.exception("View candidate page loading failed")
        flash("Unable to load candidate records. Please try again.", "error")
        return redirect(url_for("main.dashboard"))

    finally:
        if conn:
            conn.close()

# --------------------------------------------------
# VIEW CANDIDATE DETAILS
# URL: /candidates/<id>
# View Only: can_view permission
# Super Admin: Full Access
# --------------------------------------------------

@candidate_bp.route(
    "/candidates/<int:candidate_id>",
    methods=["GET"],
)
@login_required
def view_candidate(candidate_id):
    conn = get_connection()

    try:
        with conn.cursor() as cur:

            # ------------------------------------------
            # CHECK CANDIDATE VIEW PERMISSION
            # SUPER ADMIN = FULL ACCESS
            # ------------------------------------------
            candidate_is_accessible(
                cur,
                candidate_id,
                permission="can_view",
            )

            # ------------------------------------------
            # FETCH CANDIDATE DETAILS
            # ------------------------------------------
            cur.execute(
                """
                SELECT
                    c.id,
                    c.name,
                    c.phone,
                    c.email,
                    c.address,
                    c.nrc,
                    c.department_id,
                    c.branch_id,
                    c.cv_file_path,
                    c.other_attachment_path,
                    c.status,
                    c.created_at,
                    c.updated_at,

                    -- Joined Date
                    (
                        SELECT h.created_at
                        FROM public.candidate_status_history h
                        WHERE h.candidate_id = c.id
                        AND h.to_status = 'joined'
                        ORDER BY h.created_at ASC
                        LIMIT 1
                    ) AS joined_date,

                    -- Resigned Date
                    (
                        SELECT h.created_at
                        FROM public.candidate_status_history h
                        WHERE h.candidate_id = c.id
                        AND h.to_status IN ('resigned', 'resign')
                        ORDER BY h.created_at DESC
                        LIMIT 1
                    ) AS resigned_date,

                    d.name AS department_name,
                    b.name AS branch_name

                FROM public.candidates c

                JOIN public.departments d
                    ON d.id = c.department_id

                LEFT JOIN public.branches b
                    ON b.id = c.branch_id

                WHERE c.id = %s
                """,
                (candidate_id,),
            )

            candidate = row_as_dict(cur)

            if candidate is None:
                abort(404)

            # ------------------------------------------
            # FETCH INTERVIEW SCHEDULES
            # ------------------------------------------
            cur.execute(
                """
                SELECT
                    interview_stage,
                    interview_date,
                    interview_time,
                    (
                        interview_date + interview_time
                        < (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Yangon')
                        AND status <> 'cancelled'
                    ) AS interview_passed
                FROM public.candidate_interviews
                WHERE candidate_id = %s
                """,
                (candidate_id,),
            )

            interviews = cur.fetchall()

            candidate["first_interview_date"] = None
            candidate["first_interview_time"] = None
            candidate["first_interview_passed"] = False

            candidate["second_interview_date"] = None
            candidate["second_interview_time"] = None
            candidate["second_interview_passed"] = False

            for row in interviews:
                stage = row[0]

                if stage == "first_interview":
                    candidate["first_interview_date"] = row[1]
                    candidate["first_interview_time"] = row[2]
                    candidate["first_interview_passed"] = row[3]

                elif stage == "second_interview":
                    candidate["second_interview_date"] = row[1]
                    candidate["second_interview_time"] = row[2]
                    candidate["second_interview_passed"] = row[3]

            # ------------------------------------------
            # RETURN PAGE INFORMATION
            # ------------------------------------------
            return_to = request.args.get(
                "return_to",
                "candidates"
            )

            return_tab = request.args.get(
                "tab",
                "candidates"
            )

            return_global_search = request.args.get(
                "global_search",
                ""
            )

            return_stage_search = request.args.get(
                "stage_search",
                ""
            )

            return_status = request.args.get(
                "status",
                ""
            )

        # ----------------------------------------------
        # RENDER CANDIDATE DETAILS PAGE
        # ----------------------------------------------
        return render_template(
            "candidate_details.html",
            candidate=candidate,
            return_to=return_to,
            return_tab=return_tab,
            return_global_search=return_global_search,
            return_stage_search=return_stage_search,
            return_status=return_status,
        )

    finally:
        conn.close()


# --------------------------------------------------
# CANDIDATE STATUS HISTORY
# URL: /candidates/<id>/history
# --------------------------------------------------
EDIT_HISTORY_PER_PAGE = 5

# Human-readable field labels for edit history diff.
EDIT_FIELD_LABELS = {
    "name": "Full Name",
    "phone": "Phone Number",
    "email": "Email Address",
    "address": "Address",
    "nrc": "NRC Number",
    "branch_id": "Branch",
    "department_id": "Department",
    "cv_file": "CV / Resume",
    "other_attachment": "Other Attachment",
}

@candidate_bp.route(
    "/candidates/<int:candidate_id>/history",
    methods=["GET"],
)
@login_required
def candidate_history(candidate_id):
    conn = get_connection()

    # ------------------------------------------
    # RETURN PAGE INFORMATION
    # ------------------------------------------
    return_to = request.args.get("return_to", "candidates")
    return_tab = request.args.get("tab", "candidates")
    return_global_search = request.args.get("global_search", "")
    return_stage_search = request.args.get("stage_search", "")
    return_status = request.args.get("status", "")

    try:
        with conn.cursor() as cur:

            # ------------------------------------------
            # CANDIDATE INFORMATION
            # CHECK EXISTENCE FIRST
            # ------------------------------------------
            cur.execute(
                """
                SELECT
                    c.id,
                    c.name,
                    c.phone,
                    c.email,
                    c.status,
                    c.branch_id,
                    c.department_id,
                    d.name AS department_name
                FROM public.candidates c
                JOIN public.departments d
                    ON d.id = c.department_id
                WHERE c.id = %s
                """,
                (candidate_id,),
            )

            candidate = row_as_dict(cur)

            # Candidate does not exist → real 404
            if candidate is None:
                abort(404)

            # ------------------------------------------
            # CHECK CANDIDATE VIEW PERMISSION
            # ------------------------------------------
            permission_denied = False

            if current_role() == "department_head":

                user_id = session.get("user_id")

                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM public.user_department_permissions udp
                        WHERE udp.user_id = %s
                          AND udp.branch_id = %s
                          AND udp.department_id = %s
                          AND udp.can_view = TRUE
                    )
                    """,
                    (
                        user_id,
                        candidate["branch_id"],
                        candidate["department_id"],
                    ),
                )

                permission_allowed = cur.fetchone()[0]

                if not permission_allowed:
                    permission_denied = True

            # ------------------------------------------
            # PERMISSION DENIED → ABORT (No modal)
            # ------------------------------------------
            if permission_denied:
                abort(403)

            # ------------------------------------------
            # STATUS HISTORY
            # ------------------------------------------
            cur.execute(
                """
                SELECT
                    h.from_status,
                    h.to_status,
                    h.action,
                    h.performed_by_name,
                    h.performed_role,
                    h.remarks,
                    h.created_at
                FROM public.candidate_status_history h
                WHERE h.candidate_id = %s
                ORDER BY h.created_at DESC, h.id DESC
                """,
                (candidate_id,),
            )

            status_history = rows_as_dicts(cur)

                        # ------------------------------------------
            # EXAM RESULTS — LINK BY INTERVIEW ID
            # Show latest submitted result per interview
            # ------------------------------------------
            cur.execute(
                """
                SELECT DISTINCT ON (ea.interview_id)
                    ea.interview_id,
                    ci.interview_stage,
                    ea.total_questions,
                    ea.correct_answers,
                    ea.score,
                    ea.submitted_at
                FROM public.exam_assignments ea
                INNER JOIN public.candidate_interviews ci
                    ON ci.id = ea.interview_id
                WHERE ea.candidate_id = %s
                  AND ea.status = 'submitted'
                ORDER BY
                    ea.interview_id,
                    ea.submitted_at DESC NULLS LAST,
                    ea.id DESC
                """,
                (candidate_id,),
            )

            exam_rows = rows_as_dicts(cur)

            # ------------------------------------------
            # MAP EXAM RESULT BY INTERVIEW STAGE
            # ------------------------------------------
            exam_result_map = {}

            def normalize_exam_stage(value):
                value = str(value or "").strip().lower()
                value = value.replace(" ", "_").replace("-", "_")

                if "first" in value:
                    return "first_interview"

                if "second" in value:
                    return "second_interview"

                return None

            for exam in exam_rows:
                stage = normalize_exam_stage(
                    exam.get("interview_stage")
                )

                if stage:
                    exam_result_map[stage] = exam

            # Attach exam result to matching status event
            for event in status_history:
                stage = normalize_exam_stage(
                    event.get("to_status")
                )

                event["exam_result"] = (
                    exam_result_map.get(stage)
                    if stage else None
                )
                
            # ------------------------------------------
            # EDIT HISTORY — PAGINATION
            # ------------------------------------------
            try:
                edit_page = int(
                    request.args.get("edit_page", 1)
                )
            except (TypeError, ValueError):
                edit_page = 1

            edit_page = max(edit_page, 1)

            # Count total edit history
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.candidate_edit_history
                WHERE candidate_id = %s
                """,
                (candidate_id,),
            )

            edit_total = cur.fetchone()[0] or 0

            edit_total_pages = max(
                1,
                (
                    edit_total + EDIT_HISTORY_PER_PAGE - 1
                ) // EDIT_HISTORY_PER_PAGE,
            )

            if edit_page > edit_total_pages:
                edit_page = edit_total_pages

            edit_offset = (
                (edit_page - 1) * EDIT_HISTORY_PER_PAGE
            )

            # Fetch edit history
            cur.execute(
                """
                SELECT
                    eh.changed_fields,
                    eh.performed_by_name,
                    eh.performed_role,
                    eh.created_at
                FROM public.candidate_edit_history eh
                WHERE eh.candidate_id = %s
                ORDER BY eh.created_at DESC, eh.id DESC
                LIMIT %s OFFSET %s
                """,
                (
                    candidate_id,
                    EDIT_HISTORY_PER_PAGE,
                    edit_offset,
                ),
            )

            edit_history = rows_as_dicts(cur)

            # ------------------------------------------
            # ENRICH EDIT HISTORY
            # Resolve:
            # department_id → department name
            # branch_id     → branch name
            # ------------------------------------------
            dept_ids = set()
            branch_ids = set()

            for event in edit_history:
                fields = event.get("changed_fields") or {}

                if "department_id" in fields:
                    change = fields["department_id"]

                    for key in ("old", "new"):
                        val = change.get(key)

                        if val is not None:
                            try:
                                dept_ids.add(int(val))
                            except (TypeError, ValueError):
                                pass

                if "branch_id" in fields:
                    change = fields["branch_id"]

                    for key in ("old", "new"):
                        val = change.get(key)

                        if val is not None:
                            try:
                                branch_ids.add(int(val))
                            except (TypeError, ValueError):
                                pass

            # ------------------------------------------
            # DEPARTMENT NAME MAPPING
            # ------------------------------------------
            dept_map = {}

            if dept_ids:
                cur.execute(
                    """
                    SELECT id, name
                    FROM public.departments
                    WHERE id = ANY(%s)
                    """,
                    (list(dept_ids),),
                )

                for row in cur.fetchall():
                    dept_map[row[0]] = row[1]

            # ------------------------------------------
            # BRANCH NAME MAPPING
            # ------------------------------------------
            branch_map = {}

            if branch_ids:
                cur.execute(
                    """
                    SELECT id, name
                    FROM public.branches
                    WHERE id = ANY(%s)
                    """,
                    (list(branch_ids),),
                )

                for row in cur.fetchall():
                    branch_map[row[0]] = row[1]

            # ------------------------------------------
            # DISPLAY FIELD ENRICHMENT
            # ------------------------------------------
            for event in edit_history:
                raw_fields = event.get("changed_fields") or {}
                display_fields = []

                for field, change in raw_fields.items():

                    old_val = change.get("old")
                    new_val = change.get("new")

                    # Resolve department IDs
                    if field == "department_id":

                        try:
                            if old_val is not None:
                                old_val = dept_map.get(
                                    int(old_val),
                                    f"Department #{old_val}",
                                )
                        except (TypeError, ValueError):
                            pass

                        try:
                            if new_val is not None:
                                new_val = dept_map.get(
                                    int(new_val),
                                    f"Department #{new_val}",
                                )
                        except (TypeError, ValueError):
                            pass

                    # Resolve branch IDs
                    if field == "branch_id":

                        try:
                            if old_val is not None:
                                old_val = branch_map.get(
                                    int(old_val),
                                    f"Branch #{old_val}",
                                )
                        except (TypeError, ValueError):
                            pass

                        try:
                            if new_val is not None:
                                new_val = branch_map.get(
                                    int(new_val),
                                    f"Branch #{new_val}",
                                )
                        except (TypeError, ValueError):
                            pass

                    display_fields.append(
                        {
                            "key": field,
                            "label": EDIT_FIELD_LABELS.get(
                                field,
                                field.replace(
                                    "_", " "
                                ).title(),
                            ),
                            "old": old_val,
                            "new": new_val,
                        }
                    )

                event["display_fields"] = display_fields

        # ------------------------------------------
        # EDIT HISTORY PAGE NUMBERS
        # ------------------------------------------
        edit_page_numbers = []

        if edit_total_pages <= 7:
            edit_page_numbers = list(
                range(1, edit_total_pages + 1)
            )

        else:
            edit_page_numbers = [1, 2, 3]

            if edit_page > 5:
                edit_page_numbers.append(None)

            start_page = max(4, edit_page - 1)
            end_page = min(
                edit_total_pages - 2,
                edit_page + 1,
            )

            for p in range(start_page, end_page + 1):
                if p not in edit_page_numbers:
                    edit_page_numbers.append(p)

            if edit_page < edit_total_pages - 3:
                edit_page_numbers.append(None)

            edit_page_numbers.extend(
                [
                    edit_total_pages - 1,
                    edit_total_pages,
                ]
            )

        # ------------------------------------------
        # RENDER TEMPLATE
        # ------------------------------------------
        return render_template(
            "candidate_history.html",
            candidate=candidate,
            history=status_history,
            edit_history=edit_history,
            edit_page=edit_page,
            edit_total=edit_total,
            edit_total_pages=edit_total_pages,
            edit_page_numbers=edit_page_numbers,
            edit_per_page=EDIT_HISTORY_PER_PAGE,
            edit_offset=edit_offset,
            return_to=return_to,
            return_tab=return_tab,
            return_global_search=return_global_search,
            return_stage_search=return_stage_search,
            return_status=return_status,
        )

    finally:
        conn.close()

# --------------------------------------------------
# OPEN CV / OTHER ATTACHMENT
# URL: /candidates/<id>/file/cv
# URL: /candidates/<id>/file/attachment
# --------------------------------------------------
@candidate_bp.route(
    "/candidates/<int:candidate_id>/file/<string:file_type>",
    methods=["GET"],
)
@login_required
def candidate_file(candidate_id, file_type):
    if file_type not in ("cv", "attachment"):
        abort(404)

    conn = get_connection()

    try:
        with conn.cursor() as cur:
            candidate_is_accessible(cur, candidate_id)

            cur.execute(
                """
                SELECT cv_file_path, other_attachment_path
                FROM candidates
                WHERE id = %s
                """,
                (candidate_id,),
            )

            candidate = row_as_dict(cur)

        if candidate is None:
            abort(404)

        relative_path = (
            candidate["cv_file_path"]
            if file_type == "cv"
            else candidate["other_attachment_path"]
        )

        expected_folder = (
            "cv" if file_type == "cv" else "attachments"
        )

        if (
            not relative_path
            or ".." in Path(relative_path).parts
        ):
            abort(404)

        path = Path(relative_path)

        if (
            len(path.parts) != 2
            or path.parts[0] != expected_folder
        ):
            abort(404)

        upload_root = get_upload_root().resolve()
        file_path = (upload_root / path).resolve()

        if (
            not file_path.is_relative_to(upload_root)
            or not file_path.is_file()
        ):
            abort(404)

        return send_from_directory(
            upload_root / expected_folder,
            file_path.name,
            as_attachment=False,
        )

    finally:
        conn.close()


# ==================================================
# STAGE TIMELINE HELPERS
# ==================================================

STAGE_ORDER = [
    "applied",
    "interview_requested",
    "first_interview",
    "second_interview",
    "offer",
    "joined",
    "resign",
]

STAGE_LABELS = {
    "applied": "Applied",
    "interview_requested": "Interview Requested",
    "first_interview": "1st Interview",
    "second_interview": "2nd Interview",
    "offer": "Offer",
    "joined": "Joined",
    "resign": "Resign",
}

# Rejected / cancelled status → ဘယ် stage မှာ ရပ်သွားလဲ
REJECTED_STAGE_MAP = {
    "candidate_reject": "applied",
    "interview_request_reject": "interview_requested",
    "interview_request_cancel": "interview_requested",
    "first_interview_reject": "first_interview",
    "first_interview_cancel": "first_interview",
    "second_interview_reject": "second_interview",
    "second_interview_cancel": "second_interview",
    "offer_reject": "offer",
    "offer_cancel": "offer",
}


def build_stage_timeline(status, skipped_from=None):
    """
    status: current status
    skipped_from: list of stage keys that were skipped (from history)
    """
    status = (status or "").strip()
    skipped_from = set(skipped_from or [])

    # --- SKIPPED (candidate ကို skip လုပ်ခဲ့တယ်) ---
    if status == "skipped":
        stages = []
        for i, k in enumerate(STAGE_ORDER):
            stages.append({
                "key": k,
                "label": STAGE_LABELS[k],
                "state": "pending",
                "skipped": k in skipped_from,
            })
        return {
            "stages": stages,
            "skipped": True,
            "rejected": False,
            "current": None,
        }

    # --- REJECTED / CANCELLED ---
    if status in REJECTED_STAGE_MAP:
        stop_stage = REJECTED_STAGE_MAP[status]
        stop_idx = STAGE_ORDER.index(stop_stage)
        stages = []
        for i, k in enumerate(STAGE_ORDER):
            if i < stop_idx:
                state = "done"
            elif i == stop_idx:
                state = "rejected"
            else:
                state = "pending"
            stages.append({
                "key": k,
                "label": STAGE_LABELS[k],
                "state": state,
                "skipped": False,
            })
        return {
            "stages": stages,
            "skipped": False,
            "rejected": True,
            "current": stop_stage,
        }

    # --- NORMAL PROGRESSION ---
    current_idx = STAGE_ORDER.index(status) if status in STAGE_ORDER else 0
    stages = []
    for i, k in enumerate(STAGE_ORDER):
        if i < current_idx:
            state = "done"
        elif i == current_idx:
            state = "current"
        else:
            state = "pending"

        stages.append({
            "key": k,
            "label": STAGE_LABELS[k],
            "state": state,
            "skipped": k in skipped_from,
        })

    return {
        "stages": stages,
        "skipped": bool(skipped_from),
        "rejected": False,
        "current": status,
    }

# ==================================================
# ROUTE
# ==================================================

@candidate_bp.route("/records", methods=["GET"])
@login_required
def records():

    role = current_role()
    session_department_id = current_department_id()
    user_id = session.get("user_id")

    search = (request.args.get("search") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    department_filter = (request.args.get("department_id") or "").strip()
    branch_filter = (request.args.get("branch_id") or "").strip()

    per_page = 10

    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    page = max(page, 1)
    conn = None

    valid_statuses = [
        "applied",
        "interview_requested",
        "candidate_reject",
        "first_interview",
        "second_interview",
        "interview_request_reject",
        "interview_request_cancel",
        "first_interview_reject",
        "first_interview_cancel",
        "second_interview_reject",
        "second_interview_cancel",
        "offer",
        "offer_reject",
        "offer_cancel",
        "joined",
        "resign",
    ]

    reject_statuses = [
        "candidate_reject",
        "interview_request_reject",
        "interview_request_cancel",
        "first_interview_reject",
        "first_interview_cancel",
        "second_interview_reject",
        "second_interview_cancel",
        "offer_reject",
        "offer_cancel",
    ]

    in_progress_statuses = [
        "applied",
        "interview_requested",
        "first_interview",
        "second_interview",
        "offer",
    ]

    status_group = (
        request.args.get("status_group") or ""
    ).strip()

    try:
        from psycopg2.extras import RealDictCursor

        # --------------------------------------------------
        # GET AVAILABLE DEPARTMENTS
        # --------------------------------------------------
        available_departments = get_available_candidate_departments(
            role,
            session_department_id,
            user_id,
        )

        allowed_department_ids = [
            int(department["id"])
            for department in available_departments
        ]

        conn = get_connection()

                # ==================================================
        # EXPORT PERMISSION
        # ==================================================
        can_export_records = False

        if role == "super_admin":

            # Super Admin always has full access
            can_export_records = True

        elif role == "department_head" and allowed_department_ids:

            # Check export permission for assigned departments
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        p.department_id,
                        p.can_export
                    FROM public.user_department_permissions p
                    INNER JOIN public.departments d
                        ON d.id = p.department_id
                       AND d.branch_id = p.branch_id
                    WHERE p.user_id = %s
                      AND p.department_id = ANY(%s)
                      AND d.is_active = TRUE
                    """,
                    (
                        user_id,
                        allowed_department_ids,
                    ),
                )

                export_permissions = {
                    int(row[0]): bool(row[1])
                    for row in cur.fetchall()
                }

            # Only one assigned department:
            # Show button if that department has export permission
            if len(allowed_department_ids) == 1:

                only_department_id = allowed_department_ids[0]

                can_export_records = export_permissions.get(
                    only_department_id,
                    False,
                )

            # Multiple assigned departments:
            # Require selecting a department with export permission
            elif len(allowed_department_ids) > 1 and department_filter:

                try:
                    selected_department_id = int(department_filter)

                    can_export_records = (
                        selected_department_id in allowed_department_ids
                        and export_permissions.get(
                            selected_department_id,
                            False,
                        )
                    )

                except (TypeError, ValueError):
                    can_export_records = False

        # ==================================================
        # BRANCHES AND DEPARTMENTS
        # ==================================================
        branches = []

        if role == "super_admin":

            # Get active branches for Super Admin
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, name
                    FROM public.branches
                    WHERE is_active = TRUE
                    ORDER BY name ASC
                    """
                )
                branches = cur.fetchall()

            # Validate selected branch
            available_branch_ids = {
                int(branch["id"])
                for branch in branches
            }

            try:
                selected_branch_id = int(branch_filter)

                if (
                    selected_branch_id > 0
                    and selected_branch_id in available_branch_ids
                ):
                    branch_filter = str(selected_branch_id)
                else:
                    branch_filter = ""

            except (TypeError, ValueError):
                branch_filter = ""

            # Show departments only after selecting a valid branch
            departments = []

            if branch_filter:

                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT
                            id,
                            name,
                            short_name,
                            branch_id
                        FROM public.departments
                        WHERE is_active = TRUE
                          AND branch_id = %s
                        ORDER BY name ASC
                        """,
                        (int(branch_filter),),
                    )
                    departments = cur.fetchall()

        else:
            # Keep Department Head permissions unchanged
            departments = available_departments
            branch_filter = ""

        # ==================================================
        # BASE WHERE: ROLE ACCESS ONLY
        # ==================================================
        base_conditions = []
        base_params = []

        if role == "department_head":

            if allowed_department_ids:
                base_conditions.append(
                    "c.department_id = ANY(%s)"
                )
                base_params.append(allowed_department_ids)

            else:
                base_conditions.append("1 = 0")

        base_where_sql = (
            "WHERE " + " AND ".join(base_conditions)
            if base_conditions
            else ""
        )

        # ==================================================
        # LIST WHERE: ROLE ACCESS + CURRENT FILTERS
        # ==================================================
        where_conditions = list(base_conditions)
        params = list(base_params)

        # --------------------------------------------------
        # BRANCH FILTER
        # Super Admin only
        # --------------------------------------------------
        if role == "super_admin" and branch_filter:

            where_conditions.append(
                "c.branch_id = %s"
            )
            params.append(int(branch_filter))

        # --------------------------------------------------
        # DEPARTMENT FILTER
        # Super Admin: Departments from selected branch only
        # Department Head: Assigned departments only
        # --------------------------------------------------
        if department_filter:

            try:
                selected_department_id = int(department_filter)

                if selected_department_id > 0:

                    available_department_ids = {
                        int(department["id"])
                        for department in departments
                    }

                    if selected_department_id in available_department_ids:

                        where_conditions.append(
                            "c.department_id = %s"
                        )
                        params.append(selected_department_id)

                    else:
                        department_filter = ""

                else:
                    department_filter = ""

            except (TypeError, ValueError):
                department_filter = ""

        # ==================================================
        # SEARCH: NAME, PHONE, EMAIL AND BRANCH NAME
        # ==================================================
        if search:

            where_conditions.append(
                """
                (
                    c.name ILIKE %s
                    OR c.phone ILIKE %s
                    OR c.email ILIKE %s
                    OR b.name ILIKE %s
                )
                """
            )

            search_value = f"%{search}%"

            params.extend([
                search_value,
                search_value,
                search_value,
                search_value,
            ])

        # --------------------------------------------------
        # STATUS GROUP FILTER
        # --------------------------------------------------
        if status_group == "in_progress":

            where_conditions.append("c.status = ANY(%s)")
            params.append(in_progress_statuses)

        elif status_group == "rejected_cancel":

            where_conditions.append("c.status = ANY(%s)")
            params.append(reject_statuses)

        # --------------------------------------------------
        # EXACT STATUS FILTER
        # --------------------------------------------------
        if status_filter:

            if status_filter == "skipped":

                where_conditions.append(
                    """
                    EXISTS (
                        SELECT 1
                        FROM candidate_status_history h
                        WHERE h.candidate_id = c.id
                          AND h.action = 'skip'
                    )
                    """
                )

            elif status_filter in valid_statuses:

                where_conditions.append("c.status = %s")
                params.append(status_filter)

            else:
                status_filter = ""

        where_sql = (
            "WHERE " + " AND ".join(where_conditions)
            if where_conditions
            else ""
        )

        # ==================================================
        # SUMMARY COUNTS
        # Keep original role-based summary behavior
        # ==================================================
        summary_sql = """
            SELECT
                COUNT(*) AS total,

                COUNT(*) FILTER (
                    WHERE c.status IN (
                        'applied',
                        'interview_requested',
                        'first_interview',
                        'second_interview',
                        'offer'
                    )
                ) AS in_progress,

                COUNT(*) FILTER (
                    WHERE c.status IN (
                        'candidate_reject',
                        'interview_request_reject',
                        'interview_request_cancel',
                        'first_interview_reject',
                        'first_interview_cancel',
                        'second_interview_reject',
                        'second_interview_cancel',
                        'offer_reject',
                        'offer_cancel'
                    )
                ) AS rejected,

                COUNT(*) FILTER (
                    WHERE c.status = 'joined'
                ) AS joined

            FROM public.candidates c
            {base_where_sql}
        """.format(
            base_where_sql=base_where_sql
        )

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(summary_sql, tuple(base_params))
            summary = cur.fetchone()

        total_records = int(summary["total"] or 0)
        summary_total = total_records
        in_progress = int(summary["in_progress"] or 0)
        rejected = int(summary["rejected"] or 0)
        joined = int(summary["joined"] or 0)

        # ==================================================
        # MATCHING COUNT
        # ==================================================
        count_sql = """
            SELECT COUNT(*) AS total
            FROM public.candidates c

            LEFT JOIN public.branches b
                ON b.id = c.branch_id

            {where_sql}
        """.format(
            where_sql=where_sql
        )

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(count_sql, tuple(params))
            matching_result = cur.fetchone()

        matching_count = int(
            matching_result["total"] or 0
        )

        # ==================================================
        # PAGINATION
        # ==================================================
        total_pages = max(
            1,
            (matching_count + per_page - 1) // per_page
        )

        if page > total_pages:
            page = total_pages

        offset = (page - 1) * per_page

        # ==================================================
        # CANDIDATE LIST
        # ==================================================
        candidates_sql = """
            SELECT
                c.id,
                c.name,
                c.phone,
                c.email,
                c.address,
                c.nrc,
                c.department_id,

                b.name AS branch_name,
                d.name AS department_name,

                c.cv_file_path,
                c.other_attachment_path,
                c.status,
                c.created_at,
                c.updated_at,

                EXISTS (
                    SELECT 1
                    FROM public.candidate_status_history h
                    WHERE h.candidate_id = c.id
                      AND h.action = 'skip'
                ) AS was_skipped,

                CASE
                    WHEN %s = 'super_admin' THEN TRUE
                    ELSE COALESCE(udp.can_view, FALSE)
                END AS can_view

            FROM public.candidates c

            LEFT JOIN public.branches b
                ON b.id = c.branch_id

            LEFT JOIN public.departments d
                ON d.id = c.department_id

            LEFT JOIN LATERAL (
                SELECT
                    BOOL_OR(p.can_view) AS can_view
                FROM public.user_department_permissions p
                WHERE p.user_id = %s
                  AND p.branch_id = c.branch_id
                  AND p.department_id = c.department_id
            ) udp ON TRUE

            {where_sql}

            ORDER BY
                c.created_at DESC,
                c.id DESC

            LIMIT %s
            OFFSET %s

        """.format(
            where_sql=where_sql
        )

        candidate_params = [role, user_id] + list(params) + [per_page, offset]

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                candidates_sql,
                tuple(candidate_params),
            )
            candidates = cur.fetchall()

        # ==================================================
        # FETCH SKIPPED STAGES FROM HISTORY
        # ==================================================
        candidate_ids = [
            candidate["id"]
            for candidate in candidates
        ]

        skipped_map = {}

        if candidate_ids:

            with conn.cursor(cursor_factory=RealDictCursor) as cur:

                cur.execute(
                    """
                    SELECT
                        candidate_id,
                        from_status,
                        to_status
                    FROM public.candidate_status_history
                    WHERE candidate_id = ANY(%s)
                      AND action = 'skip'
                    """,
                    (candidate_ids,),
                )

                for row in cur.fetchall():

                    cid = row["candidate_id"]
                    from_stage = row["from_status"]
                    to_stage = row["to_status"]

                    if (
                        from_stage in STAGE_ORDER
                        and to_stage in STAGE_ORDER
                    ):

                        from_idx = STAGE_ORDER.index(from_stage)
                        to_idx = STAGE_ORDER.index(to_stage)

                        skipped_set = skipped_map.setdefault(
                            cid,
                            set(),
                        )

                        for i in range(from_idx + 1, to_idx):
                            skipped_set.add(STAGE_ORDER[i])

        # ==================================================
        # ATTACH STAGE TIMELINE
        # ==================================================
        for candidate in candidates:

            cid = candidate["id"]
            candidate_status = candidate.get("status") or ""

            if (
                candidate.get("was_skipped")
                and candidate_status != "joined"
            ):

                if candidate_status not in REJECTED_STAGE_MAP:
                    candidate_status = "skipped"

            candidate["stage_timeline"] = build_stage_timeline(
                candidate_status,
                skipped_from=skipped_map.get(cid, set()),
            )

        # ==================================================
        # PAGE NUMBERS
        # ==================================================
        page_numbers = []

        if total_pages <= 7:

            page_numbers = list(
                range(1, total_pages + 1)
            )

        else:

            page_numbers = [1, 2, 3]

            if page > 5:
                page_numbers.append(None)

            start_page = max(4, page - 1)
            end_page = min(total_pages - 2, page + 1)

            for p in range(start_page, end_page + 1):

                if p not in page_numbers:
                    page_numbers.append(p)

            if page < total_pages - 3:
                page_numbers.append(None)

            page_numbers.extend([
                total_pages - 1,
                total_pages,
            ])

        # ==================================================
        # RENDER
        # ==================================================
        return render_template(
            "records.html",

            candidates=candidates,
            branches=branches,
            departments=departments,

            # Pagination
            page=page,
            per_page=per_page,
            total_records=total_records,
            total_count=matching_count,
            total_pages=total_pages,
            offset=offset,
            page_numbers=page_numbers,

            # Four fixed summary counts
            summary_total=summary_total,
            in_progress=in_progress,
            rejected=rejected,
            joined=joined,

            # Status lists
            valid_statuses=valid_statuses,
            reject_statuses=reject_statuses,
            in_progress_statuses=in_progress_statuses,

            # Current search and filters
            search=search,
            status_filter=status_filter,
            department_filter=department_filter,
            branch_filter=branch_filter,
            status_group=status_group,
            can_export_records=can_export_records,
        )

    except Exception:

        current_app.logger.exception(
            "Failed to load candidate records"
        )

        flash(
            "Could not load candidate records. "
            "Please check the database and server log.",
            "error",
        )

        return redirect(
            url_for("main.dashboard")
        )

    finally:

        if conn:
            conn.close()


# ============================================================
# EXPORT CANDIDATE RECORDS (CSV)
# URL: /records/export
#
# Applies the same filters as records() but exports ALL
# matching records (not just the current page).
# Includes a report header describing what was exported.
# ============================================================

@candidate_bp.route("/records/export", methods=["GET"])
@login_required
def export_records():

    import csv
    import io
    from datetime import datetime

    from flask import Response
    from psycopg2.extras import RealDictCursor

    role = current_role()
    session_department_id = current_department_id()
    user_id = session.get("user_id")

    search = (request.args.get("search") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    department_filter = (request.args.get("department_id") or "").strip()
    status_group = (request.args.get("status_group") or "").strip()

    valid_statuses = [
        "applied",
        "interview_requested",
        "candidate_reject",
        "first_interview",
        "second_interview",
        "interview_request_reject",
        "interview_request_cancel",
        "first_interview_reject",
        "first_interview_cancel",
        "second_interview_reject",
        "second_interview_cancel",
        "offer",
        "offer_reject",
        "offer_cancel",
        "joined",
        "resign",
    ]

    reject_statuses = [
        "candidate_reject",
        "interview_request_reject",
        "interview_request_cancel",
        "first_interview_reject",
        "first_interview_cancel",
        "second_interview_reject",
        "second_interview_cancel",
        "offer_reject",
        "offer_cancel",
    ]

    in_progress_statuses = [
        "applied",
        "interview_requested",
        "first_interview",
        "second_interview",
        "offer",
    ]

    conn = None

    try:

        # --------------------------------------------------
        # GET AVAILABLE DEPARTMENTS
        # --------------------------------------------------
        available_departments = get_available_candidate_departments(
            role,
            session_department_id,
            user_id,
        )

        allowed_department_ids = [
            int(department["id"])
            for department in available_departments
        ]

        conn = get_connection()

        where_conditions = []
        params = []
        
        # --------------------------------------------------
        # ROLE ACCESS
        # --------------------------------------------------
        if role == "department_head":

            if allowed_department_ids:
                where_conditions.append(
                    "c.department_id = ANY(%s)"
                )
                params.append(allowed_department_ids)

            else:
                where_conditions.append("1 = 0")

        # --------------------------------------------------
        # DEPARTMENT FILTER
        # Super Admin: All available departments
        # Department Head: Assigned departments only
        # --------------------------------------------------
        if department_filter:

            try:
                selected_department_id = int(department_filter)

                if selected_department_id > 0:

                    available_department_ids = {
                        int(department["id"])
                        for department in available_departments
                    }

                    if selected_department_id in available_department_ids:

                        where_conditions.append(
                            "c.department_id = %s"
                        )
                        params.append(selected_department_id)

                    else:
                        department_filter = ""

            except (TypeError, ValueError):
                department_filter = ""

        # --------------------------------------------------
        # SEARCH: NAME, PHONE, EMAIL, BRANCH NAME
        # --------------------------------------------------
        if search:

            where_conditions.append(
                """
                (
                    c.name ILIKE %s
                    OR c.phone ILIKE %s
                    OR c.email ILIKE %s
                    OR b.name ILIKE %s
                )
                """
            )

            search_value = f"%{search}%"

            params.extend([
                search_value,
                search_value,
                search_value,
                search_value,
            ])

        # --------------------------------------------------
        # STATUS GROUP
        # --------------------------------------------------
        if status_group == "in_progress":

            where_conditions.append(
                "c.status = ANY(%s)"
            )
            params.append(in_progress_statuses)

        elif status_group == "rejected_cancel":

            where_conditions.append(
                "c.status = ANY(%s)"
            )
            params.append(reject_statuses)

        # --------------------------------------------------
        # EXACT STATUS / SKIPPED
        # --------------------------------------------------
        if status_filter:

            if status_filter == "skipped":

                where_conditions.append(
                    """
                    EXISTS (
                        SELECT 1
                        FROM public.candidate_status_history h
                        WHERE h.candidate_id = c.id
                          AND h.action = 'skip'
                    )
                    """
                )

            elif status_filter in valid_statuses:

                where_conditions.append(
                    "c.status = %s"
                )
                params.append(status_filter)

            else:
                status_filter = ""

        where_sql = (
            "WHERE " + " AND ".join(where_conditions)
            if where_conditions
            else ""
        )

        # --------------------------------------------------
        # FETCH ALL MATCHING ROWS
        # --------------------------------------------------
        sql = """
            SELECT
                c.id,
                c.name,
                c.phone,
                c.email,
                c.address,
                c.nrc,
                c.position,
                c.channel,

                b.name AS branch_name,
                d.name AS department_name,

                c.status,
                c.created_at

            FROM public.candidates c

            LEFT JOIN public.branches b
                ON b.id = c.branch_id

            LEFT JOIN public.departments d
                ON d.id = c.department_id

            {where_sql}

            ORDER BY
                c.created_at DESC,
                c.id DESC

        """.format(
            where_sql=where_sql
        )

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute(sql, tuple(params))
            rows = cur.fetchall()

        # --------------------------------------------------
        # DEPARTMENT LABEL
        # --------------------------------------------------
        department_label = ""

        if department_filter:

            try:
                selected_department_id = int(department_filter)

                with conn.cursor(cursor_factory=RealDictCursor) as cur:

                    cur.execute(
                        """
                        SELECT name
                        FROM public.departments
                        WHERE id = %s
                        """,
                        (selected_department_id,),
                    )

                    dept_row = cur.fetchone()

                    if dept_row:
                        department_label = dept_row["name"]

            except (TypeError, ValueError):
                pass

        # --------------------------------------------------
        # REPORT TITLE
        # --------------------------------------------------
        report_title = "Candidate Records Report"

        generated_at = datetime.now().strftime(
            "%d %b %Y, %I:%M %p"
        )

        # --------------------------------------------------
        # STATUS LABEL
        # --------------------------------------------------
        status_label = ""

        if status_filter == "skipped":
            status_label = "Skipped"

        elif status_filter == "resign":
            status_label = "Resign"

        elif status_filter:
            status_label = status_filter.replace(
                "_", " "
            ).title()

        elif status_group == "in_progress":
            status_label = "In Progress"

        elif status_group == "rejected_cancel":
            status_label = "Rejected / Cancelled"

        # --------------------------------------------------
        # DEPARTMENT DISPLAY
        # --------------------------------------------------
        if department_label:

            dept_display = department_label

        elif role == "department_head":

            if available_departments:

                dept_display = ", ".join(
                    department["name"]
                    for department in available_departments
                )

            else:
                dept_display = "My Department"

        else:

            dept_display = "All Departments"

        # --------------------------------------------------
        # FILTER DESCRIPTION
        # --------------------------------------------------
        filter_lines = [
            "Exported: " + generated_at,
            "Department: " + dept_display,
        ]

        if search:
            filter_lines.append(
                'Search: "' + search + '"'
            )

        if status_label:
            filter_lines.append(
                "Status: " + status_label
            )

        filter_lines.append(
            "Total records: " + str(len(rows))
        )

        # --------------------------------------------------
        # BUILD CSV
        # --------------------------------------------------
        output = io.StringIO()

        writer = csv.writer(
            output,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\r\n",
        )

        writer.writerow([report_title])
        writer.writerow(["=" * len(report_title)])

        for line in filter_lines:
            writer.writerow([line])

        writer.writerow([])

        writer.writerow([
            "ID",
            "Name",
            "Phone",
            "Email",
            "Address",
            "NRC",
            "Position",
            "Channel",
            "Branch",
            "Department",
            "Status",
            "Applied Date",
        ])

        # --------------------------------------------------
        # DATA ROWS
        # --------------------------------------------------
        for row in rows:

            status = (
                row["status"] or ""
            ).replace("_", " ").title()

            applied_date = ""

            if row["created_at"]:

                applied_date = row["created_at"].strftime(
                    "%d %b %Y %I:%M %p"
                )

            writer.writerow([
                row["id"],
                row["name"] or "",
                row["phone"] or "",
                row["email"] or "",
                row["address"] or "",
                row["nrc"] or "",
                row["position"] or "",
                row["channel"] or "",
                row["branch_name"] or "",
                row["department_name"] or "",
                status,
                applied_date,
            ])

        csv_content = output.getvalue()
        output.close()

        # --------------------------------------------------
        # FILENAME
        # --------------------------------------------------
        timestamp = datetime.now().strftime(
            "%Y%m%d_%H%M%S"
        )

        filename_parts = [
            "candidate_records"
        ]

        if status_label:

            safe_status = (
                status_label.lower()
                .replace(" ", "_")
                .replace("/", "_")
                .replace("\\", "_")
            )

            filename_parts.append(safe_status)

        if search:

            safe_search = (
                search.lower()
                .replace(" ", "_")
                .replace("/", "_")
                .replace("\\", "_")
            )[:30]

            filename_parts.append(
                "q_" + safe_search
            )

        filename_parts.append(timestamp)

        filename = "_".join(
            filename_parts
        ) + ".csv"

        # --------------------------------------------------
        # RESPONSE
        # --------------------------------------------------
        return Response(
            "\ufeff" + csv_content,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"'
                ),
                "Cache-Control": "no-store",
            },
        )

    except Exception:

        current_app.logger.exception(
            "Failed to export candidate records"
        )

        flash(
            "Could not export candidate records. "
            "Please check the server log.",
            "error",
        )

        return redirect(
            url_for("candidate.records")
        )

    finally:

        if conn:
            conn.close()
                
# ============================================================
# OLLAMA SETTINGS
# ============================================================

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

# Use the model that is actually installed in Ollama.
OLLAMA_MODEL = "llama3.1:8b"

MAX_CV_BYTES = 10 * 1024 * 1024

# If PDF text is shorter than this, OCR will be attempted.
MIN_TEXT_CHARS = 40


# ============================================================
# PDF TEXT EXTRACTION
# ============================================================

def _extract_text_from_pdf(pdf_bytes):
    """
    Extract text from a PDF.

    1. Try normal PDF text extraction.
    2. If extracted text is too short, use OCR.
    """

    text_parts = []

    # --------------------------------------------------------
    # STEP 1: Normal PDF text layer
    # --------------------------------------------------------

    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:

            for page in doc:
                page_text = page.get_text("text") or ""

                if page_text.strip():
                    text_parts.append(page_text)

    except Exception:
        current_app.logger.exception(
            "PyMuPDF PDF text extraction failed"
        )

    text = "\n".join(text_parts).strip()

    # --------------------------------------------------------
    # If text layer is usable, return it
    # --------------------------------------------------------

    if len(text) >= MIN_TEXT_CHARS:
        return text, "text"

    # --------------------------------------------------------
    # STEP 2: OCR fallback
    # --------------------------------------------------------

    try:

        images = convert_from_bytes(
            pdf_bytes,
            dpi=200
        )

        ocr_parts = []

        for image in images:

            ocr_text = pytesseract.image_to_string(
                image
            )

            if ocr_text:
                ocr_parts.append(ocr_text)

        ocr_text = "\n".join(ocr_parts).strip()

        if len(ocr_text) >= 20:
            return ocr_text, "ocr"

    except Exception:
        current_app.logger.exception(
            "OCR fallback failed"
        )
    # --------------------------------------------------------
    # Nothing useful found
    # --------------------------------------------------------
    if text:
        return text, "text"

    return "", "none"

#prompt

EXTRACT_PROMPT = r"""
You are a CV information extraction system.

Your task is to extract information from the CV text.

IMPORTANT:
Return ONLY ONE valid JSON object.

Do NOT return:
- Markdown
- ```json
- ```
- Explanations
- Comments
- Extra text before or after JSON

The JSON must use EXACTLY these keys:

{{
  "name": null,
  "phone": null,
  "email": null,
  "address": null,
  "nrc": null,
  "position": null,
  "channel": "CV Upload",
  "confidence": "medium"
}}

RULES:

1. name
   - Candidate's full name only.
   - Do not use company names.
   - Do not use job titles.
   - If not clearly found, use null.

2. phone
   - Extract the candidate's phone number.
   - Keep the original format when possible.
   - If multiple phone numbers exist, choose the first candidate phone number.
   - If not found, use null.

3. email
   - Extract the candidate's email address.
   - If not found, use null.

4. address
   - Extract the candidate's address if clearly present.
   - Do not invent an address.
   - If not found, use null.

5. nrc
   - Extract NRC / National ID / Identification number if clearly present.
   - If not found, use null.

6. position
   - Prefer the position/job title the candidate is applying for.
   - If an applied position is not stated, use the candidate's most recent job title.
   - If not found, use null.

7. channel
   - MUST ALWAYS be:
     "CV Upload"

8. confidence
   - Use "high" when most important fields are clearly readable.
   - Use "medium" when some fields are uncertain.
   - Use "low" when the CV is difficult to read.

VERY IMPORTANT:
Use null for missing values.
Never invent information.

CV TEXT:
----------------
{cv_text}
----------------
"""


# ============================================================
# CLEAN LLM OUTPUT
# ============================================================

def _clean_llm_json(raw):
    """
    Safely convert Ollama's response into a Python dictionary.

    Handles:
    - normal JSON
    - ```json ... ```
    - ``` ... ```
    - extra text around JSON
    - whitespace
    """

    if raw is None:
        raise ValueError(
            "Ollama returned empty content"
        )

    if not isinstance(raw, str):
        raise ValueError(
            "Ollama content is not a string"
        )

    raw = raw.strip()

    if not raw:
        raise ValueError(
            "Ollama returned empty content"
        )

    # --------------------------------------------------------
    # Remove BOM if present
    # --------------------------------------------------------

    raw = raw.lstrip("\ufeff")

    # --------------------------------------------------------
    # Remove markdown code fences
    # --------------------------------------------------------

    # ```json
    raw = re.sub(
        r"^\s*```json\s*",
        "",
        raw,
        flags=re.IGNORECASE
    )

    # ```JSON
    raw = re.sub(
        r"^\s*```JSON\s*",
        "",
        raw
    )

    # generic ```
    raw = re.sub(
        r"^\s*```\s*",
        "",
        raw
    )

    # closing ```
    raw = re.sub(
        r"\s*```\s*$",
        "",
        raw
    )

    raw = raw.strip()

    # --------------------------------------------------------
    # FIRST ATTEMPT:
    # Parse entire response directly
    # --------------------------------------------------------

    try:

        parsed = json.loads(raw)

        if not isinstance(parsed, dict):
            raise ValueError(
                "JSON root is not an object"
            )

        return parsed

    except json.JSONDecodeError:
        pass

    # --------------------------------------------------------
    # SECOND ATTEMPT:
    # Find outermost JSON object
    # --------------------------------------------------------

    start = raw.find("{")
    end = raw.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError(
            "No JSON object found in model output"
        )

    json_text = raw[start:end + 1].strip()

    try:

        parsed = json.loads(json_text)

    except json.JSONDecodeError as exc:

        current_app.logger.error(
            "Invalid JSON returned by Ollama: %s",
            raw[:5000]
        )

        raise ValueError(
            f"Invalid JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            "JSON root is not an object"
        )

    return parsed


# ============================================================
# NORMALIZE EXTRACTED DATA
# ============================================================

def _normalize_extracted_data(data):
    """
    Validate and normalize the model response.
    """

    if not isinstance(data, dict):
        raise ValueError(
            "AI response is not a JSON object"
        )

    allowed_fields = {
        "name",
        "phone",
        "email",
        "address",
        "nrc",
        "position",
    }

    cleaned = {}

    # --------------------------------------------------------
    # Normal fields
    # --------------------------------------------------------

    for field in allowed_fields:

        value = data.get(field)

        if value is None:
            cleaned[field] = None
            continue

        if isinstance(value, str):

            value = value.strip()

            cleaned[field] = value if value else None

        else:

            # Never pass numbers/lists/dicts into form fields.
            cleaned[field] = str(value).strip() or None

    # --------------------------------------------------------
    # Channel
    # --------------------------------------------------------

    # Always force this value.
    # We don't trust the AI for this field.

    cleaned["channel"] = "CV Upload"

    # --------------------------------------------------------
    # Confidence
    # --------------------------------------------------------

    confidence = data.get(
        "confidence",
        "medium"
    )

    if not isinstance(confidence, str):
        confidence = "medium"

    confidence = confidence.strip().lower()

    if confidence not in {
        "high",
        "medium",
        "low",
    }:
        confidence = "medium"
    cleaned["confidence"] = confidence
    return cleaned


# ============================================================
# EXTRACT CANDIDATE FROM CV
# ============================================================

@candidate_bp.route("/candidates/extract", methods=["POST"])
@login_required
def extract_candidate_from_cv():
    """Extract candidate details from an uploaded PDF using Ollama."""

    file = request.files.get("cv_file")

    if not file or not file.filename:
        return jsonify(ok=False, error="No file uploaded."), 400

    if not file.filename.lower().endswith(".pdf"):
        return jsonify(
            ok=False,
            error="Only PDF files are supported for auto-extract.",
        ), 400

    try:
        pdf_bytes = file.read()
    except Exception:
        current_app.logger.exception("Failed to read uploaded CV")
        return jsonify(ok=False, error="Could not read uploaded PDF."), 400

    if not pdf_bytes:
        return jsonify(ok=False, error="Uploaded PDF is empty."), 400

    if len(pdf_bytes) > MAX_CV_BYTES:
        return jsonify(ok=False, error="File exceeds 10 MB."), 400

    try:
        text, source = _extract_text_from_pdf(pdf_bytes)
    except Exception:
        current_app.logger.exception("CV text extraction failed")
        return jsonify(ok=False, error="Could not read this PDF."), 422

    if not text or len(text.strip()) < 20:
        return jsonify(
            ok=False,
            error=(
                "Could not read enough text from this PDF. "
                "The PDF may be scanned, encrypted, or too low quality. "
                "Please fill the form manually."
            ),
        ), 422

    trimmed = text[:12000]

    current_app.logger.info(
        "CV extraction started: model=%s source=%s text_length=%s",
        OLLAMA_MODEL,
        source,
        len(text),
    )

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a CV data extraction system. "
                    "Return only valid JSON. "
                    "Never return markdown or explanations."
                ),
            },
            {
                "role": "user",
                "content": EXTRACT_PROMPT.format(cv_text=trimmed),
            },
        ],
    }

    try:
        response = requests.post(
            OLLAMA_URL,
            json=payload,
            timeout=120,
        )

        response.raise_for_status()

        try:
            ollama_data = response.json()
        except ValueError as exc:
            current_app.logger.error(
                "Ollama returned non-JSON HTTP response: %s",
                response.text[:1000],
            )
            raise ValueError(
                "Ollama HTTP response is not valid JSON."
            ) from exc

        message = ollama_data.get("message")

        if not isinstance(message, dict):
            current_app.logger.error(
                "Unexpected Ollama response: %s",
                ollama_data,
            )
            raise ValueError("Ollama response is missing the message object.")

        content = message.get("content")

        if not isinstance(content, str) or not content.strip():
            raise ValueError("Ollama returned empty content.")

        current_app.logger.info(
            "Ollama response received: content_length=%s",
            len(content),
        )

        data = _clean_llm_json(content)
        cleaned = _normalize_extracted_data(data)

    except requests.HTTPError as exc:
        http_response = exc.response
        status_code = (
            http_response.status_code if http_response is not None else 502
        )
        response_text = (
            http_response.text[:1000]
            if http_response is not None
            else str(exc)
        )

        current_app.logger.error(
            "Ollama HTTP error: status=%s url=%s response=%s",
            status_code,
            OLLAMA_URL,
            response_text,
        )

        return jsonify(
            ok=False,
            error=(
                f"Ollama returned HTTP {status_code}. "
                "Check OLLAMA_URL, the API endpoint, and the Ollama logs. "
                f"Details: {response_text[:300]}"
            ),
        ), 502

    except requests.RequestException as exc:
        current_app.logger.exception(
            "Ollama connection/request failed: %s",
            exc,
        )

        return jsonify(
            ok=False,
            error=(
                "Could not connect to Ollama. "
                "Check that Ollama is running at "
                "http://127.0.0.1:11434."
            ),
        ), 502

    except ValueError as exc:
        current_app.logger.exception("Failed to parse Ollama CV response")

        return jsonify(
            ok=False,
            error=f"AI returned invalid data. Please try again. Details: {exc}",
        ), 502

    except Exception:
        current_app.logger.exception("Unexpected CV extraction error")

        return jsonify(
            ok=False,
            error=(
                "Unexpected error while extracting the CV. "
                "Please check the Flask terminal log."
            ),
        ), 500

    current_app.logger.info(
        "CV extraction successful: source=%s confidence=%s",
        source,
        cleaned.get("confidence"),
    )

    return jsonify(
        ok=True,
        fields={
            "name": cleaned.get("name"),
            "phone": cleaned.get("phone"),
            "email": cleaned.get("email"),
            "address": cleaned.get("address"),
            "nrc": cleaned.get("nrc"),
            "position": cleaned.get("position"),
            "channel": "CV Upload",
            "confidence": cleaned.get("confidence"),
            "source": source,
        },
        preview=trimmed[:300],
    ), 200

