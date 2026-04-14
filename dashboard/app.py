from __future__ import annotations

import csv
import io
import json
import os
import secrets
import sqlite3
import smtplib
import tempfile
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from flask import (
    Flask,
    Response,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(
    os.environ.get("CSCO_DATA_DIR")
    or tempfile.gettempdir()
)
DATABASE_PATH = DATA_ROOT / "CompetitiveSolutionsHR" / "competitive_solutions.db"


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "change-me-in-production"),
        DATABASE=str(DATABASE_PATH),
        MAIL_SERVER=os.environ.get("MAIL_SERVER", ""),
        MAIL_PORT=int(os.environ.get("MAIL_PORT", "587")),
        MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "true").lower() == "true",
        MAIL_USERNAME=os.environ.get("MAIL_USERNAME", ""),
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
        MAIL_FROM=os.environ.get("MAIL_FROM", os.environ.get("MAIL_USERNAME", "")),
        RESEND_API_KEY=os.environ.get("RESEND_API_KEY", os.environ.get("MAIL_PASSWORD", "")),
        RESEND_API_URL=os.environ.get("RESEND_API_URL", "https://api.resend.com/emails"),
        INVITATION_EXPIRY_SECONDS=int(os.environ.get("INVITATION_EXPIRY_SECONDS", "86400")),
        MAIL_SUPPRESS_SEND=False,
        OUTBOX=[],
    )

    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    app.teardown_appcontext(close_db)

    @app.before_request
    def load_logged_in_user() -> None:
        user_id = session.get("user_id")
        g.user = None
        if user_id is not None:
            g.user = query_one(
                "SELECT id, full_name, email, role FROM users WHERE id = ?",
                (user_id,),
            )

    @app.context_processor
    def inject_helpers() -> dict[str, Any]:
        return {
            "format_currency": format_currency,
            "format_month_label": format_month_label,
            "now_iso_local": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        }

    @app.route("/")
    def index() -> Response | str:
        if g.user:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response | str:
        if g.user:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = query_one("SELECT * FROM users WHERE email = ?", (email,))

            if not user:
                flash("بيانات الدخول غير صحيحة.", "error")
            elif not user["is_active"]:
                flash("هذا الحساب لم يكتمل تفعيله بعد. اطلب رابط إكمال التسجيل باستخدام بريدك.", "error")
            elif not check_password_hash(user["password_hash"], password):
                flash("بيانات الدخول غير صحيحة.", "error")
            else:
                session.clear()
                session["user_id"] = user["id"]
                flash(f"مرحبًا {user['full_name']}. تم تسجيل الدخول بنجاح.", "success")
                return redirect(url_for("dashboard"))

        return render_template("login.html")

    @app.post("/register-request")
    def register_request() -> Response:
        if g.user:
            return redirect(url_for("dashboard"))

        email = request.form.get("email", "").strip().lower()

        if not email:
            flash("الرجاء إدخال البريد الإلكتروني المعتمد.", "error")
            return redirect(url_for("login"))

        user = query_one(
            """
            SELECT id, full_name, email, is_active
            FROM users
            WHERE email = ? AND role = 'employee'
            """,
            (email,),
        )
        if not user:
            flash("هذا البريد غير مضاف مسبقًا من قبل الإدارة.", "error")
            return redirect(url_for("login"))

        if user["is_active"]:
            flash("الحساب مفعل بالفعل. يمكنك تسجيل الدخول مباشرة.", "error")
            return redirect(url_for("login"))

        invitation_nonce = secrets.token_urlsafe(16)
        execute_db(
            """
            UPDATE users
            SET invitation_token = ?, invitation_sent_at = ?
            WHERE id = ?
            """,
            (invitation_nonce, utcnow(), user["id"]),
        )

        invitation_link = url_for(
            "complete_registration",
            token=build_invitation_token(user["id"], invitation_nonce),
            _external=True,
        )
        try:
            send_account_email(
                recipient=user["email"],
                subject="إكمال تسجيل حسابك في نظام الموارد البشرية",
                body=(
                    f"مرحبًا {user['full_name']}\n\n"
                    "تمت إضافتك من قبل إدارة شركة الحلول التنافسية.\n"
                    "لإكمال تسجيل حسابك وإنشاء كلمة المرور افتح الرابط التالي:\n"
                    f"{invitation_link}\n\n"
                    "صلاحية الرابط 24 ساعة."
                ),
            )
        except RuntimeError:
            flash("تم تجهيز رابط التفعيل لكن خدمة البريد غير مهيأة بعد. يرجى استكمال إعدادات البريد في البيئة.", "error")
            return redirect(url_for("login"))
        except (smtplib.SMTPException, OSError, urllib_error.URLError, RuntimeError):
            current_app.logger.exception("Email delivery failed for invitation link.")
            flash("تعذر إرسال رابط التفعيل حاليًا. تحقق من إعدادات البريد في Render ثم أعد المحاولة.", "error")
            return redirect(url_for("login"))
        flash("تم إرسال رابط إكمال التسجيل إلى بريدك الإلكتروني المعتمد.", "success")
        return redirect(url_for("login"))

    @app.route("/complete-registration/<token>", methods=["GET", "POST"])
    def complete_registration(token: str) -> Response | str:
        if g.user:
            return redirect(url_for("dashboard"))

        invited_user = validate_invitation_token(token)
        if not invited_user:
            flash("رابط إكمال التسجيل غير صالح أو منتهي الصلاحية.", "error")
            return redirect(url_for("login"))

        if request.method == "POST":
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not password or not confirm_password:
                flash("الرجاء إدخال كلمة المرور وتأكيدها.", "error")
                return redirect(url_for("complete_registration", token=token))

            if password != confirm_password:
                flash("تأكيد كلمة المرور غير مطابق.", "error")
                return redirect(url_for("complete_registration", token=token))

            if len(password) < 8:
                flash("يجب أن تكون كلمة المرور 8 أحرف على الأقل.", "error")
                return redirect(url_for("complete_registration", token=token))

            execute_db(
                """
                UPDATE users
                SET password_hash = ?, is_active = 1, invitation_token = NULL, password_set_at = ?
                WHERE id = ?
                """,
                (generate_password_hash(password), utcnow(), invited_user["id"]),
            )
            flash("تم تفعيل حسابك بنجاح. يمكنك الآن تسجيل الدخول.", "success")
            return redirect(url_for("login"))

        return render_template("complete_registration.html", invited_user=invited_user)

    @app.post("/reset-password")
    def reset_password() -> Response:
        if g.user:
            return redirect(url_for("dashboard"))

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not email or not password or not confirm_password:
            flash("الرجاء إدخال البريد الإلكتروني وكلمة المرور الجديدة وتأكيدها.", "error")
            return redirect(url_for("login"))

        if password != confirm_password:
            flash("تأكيد كلمة المرور الجديدة غير مطابق.", "error")
            return redirect(url_for("login"))

        user = query_one("SELECT id, is_active FROM users WHERE email = ?", (email,))
        if not user:
            flash("لا يوجد حساب مرتبط بهذا البريد الإلكتروني.", "error")
            return redirect(url_for("login"))

        if not user["is_active"]:
            flash("هذا الحساب لم يكتمل تفعيله بعد. استخدم رابط إكمال التسجيل المرسل إلى البريد.", "error")
            return redirect(url_for("login"))

        execute_db(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), user["id"]),
        )
        flash("تم تحديث كلمة المرور بنجاح. يمكنك الآن تسجيل الدخول بكلمتك الجديدة.", "success")
        return redirect(url_for("login"))

    @app.route("/logout", methods=["POST"])
    def logout() -> Response:
        session.clear()
        flash("تم تسجيل الخروج بنجاح.", "success")
        return redirect(url_for("login"))

    @app.route("/dashboard")
    @login_required
    def dashboard() -> str:
        users = query_all(
            """
            SELECT id, full_name, email, role, is_active, invitation_sent_at
            FROM users
            WHERE role = 'employee' AND is_active = 1
            ORDER BY full_name
            """
        )
        employee_directory = query_all(
            """
            SELECT id, full_name, email, role, is_active, invitation_sent_at
            FROM users
            WHERE role = 'employee'
            ORDER BY full_name
            """
        )
        attendance_period = request.args.get("attendance_period", "day").strip() or "day"
        if attendance_period not in {"day", "week", "month"}:
            attendance_period = "day"
        attendance_date = request.args.get("attendance_date", datetime.now().strftime("%Y-%m-%d")).strip()
        try:
            anchor_date = datetime.strptime(attendance_date, "%Y-%m-%d").date()
        except ValueError:
            anchor_date = date.today()
            attendance_date = anchor_date.isoformat()
        range_start, range_end = get_attendance_range(anchor_date, attendance_period)
        export_query = {
            "attendance_period": attendance_period,
            "attendance_date": attendance_date,
        }

        if is_admin():
            attendance_summary = query_all(
                """
                SELECT
                    users.id AS user_id,
                    users.full_name,
                    DATE(attendance.recorded_at) AS attendance_date,
                    MIN(CASE WHEN attendance.action = 'دخول' THEN attendance.recorded_at END) AS first_check_in,
                    MAX(CASE WHEN attendance.action = 'خروج' THEN attendance.recorded_at END) AS last_check_out,
                    COUNT(*) AS event_count
                FROM attendance
                JOIN users ON users.id = attendance.user_id
                WHERE attendance.recorded_at >= ? AND attendance.recorded_at < ?
                GROUP BY users.id, DATE(attendance.recorded_at)
                ORDER BY attendance_date DESC, users.full_name
                """,
                (range_start, range_end),
            )
            payroll_records = query_all(
                """
                SELECT payrolls.id, payrolls.month_label, payrolls.net_salary, payrolls.created_at, users.full_name
                FROM payrolls
                JOIN users ON users.id = payrolls.user_id
                ORDER BY payrolls.month_label DESC, payrolls.created_at DESC
                """
            )
            selected_employee_id = request.args.get("employee_id", type=int)
            if not selected_employee_id and users:
                selected_employee_id = users[0]["id"]
        else:
            attendance_summary = query_all(
                """
                SELECT
                    users.id AS user_id,
                    users.full_name,
                    DATE(attendance.recorded_at) AS attendance_date,
                    MIN(CASE WHEN attendance.action = 'دخول' THEN attendance.recorded_at END) AS first_check_in,
                    MAX(CASE WHEN attendance.action = 'خروج' THEN attendance.recorded_at END) AS last_check_out,
                    COUNT(*) AS event_count
                FROM attendance
                JOIN users ON users.id = attendance.user_id
                WHERE attendance.user_id = ? AND attendance.recorded_at >= ? AND attendance.recorded_at < ?
                GROUP BY users.id, DATE(attendance.recorded_at)
                ORDER BY attendance_date DESC, users.full_name
                """,
                (g.user["id"], range_start, range_end),
            )
            payroll_records = query_all(
                """
                SELECT payrolls.id, payrolls.month_label, payrolls.net_salary, payrolls.created_at, users.full_name
                FROM payrolls
                JOIN users ON users.id = payrolls.user_id
                WHERE payrolls.user_id = ?
                ORDER BY payrolls.month_label DESC, payrolls.created_at DESC
                """,
                (g.user["id"],),
            )
            selected_employee_id = g.user["id"]

        current_payroll = None
        if selected_employee_id:
            current_payroll = query_one(
                """
                SELECT payrolls.*, users.full_name
                FROM payrolls
                JOIN users ON users.id = payrolls.user_id
                WHERE payrolls.user_id = ?
                ORDER BY payrolls.month_label DESC, payrolls.created_at DESC
                LIMIT 1
                """,
                (selected_employee_id,),
            )

        dashboard_stats = {
            "employees": len(users),
            "attendance_count": len(attendance_summary),
            "today_count": sum(
                row["attendance_date"] == date.today().isoformat()
                for row in attendance_summary
            ),
            "payroll_count": len(payroll_records),
        }

        return render_template(
            "dashboard.html",
            users=users,
            employee_directory=employee_directory,
            attendance_summary=attendance_summary,
            payroll_records=payroll_records,
            current_payroll=current_payroll,
            selected_employee_id=selected_employee_id,
            dashboard_stats=dashboard_stats,
            is_admin=is_admin(),
            attendance_period=attendance_period,
            attendance_date=attendance_date,
            export_query=export_query,
        )

    @app.post("/attendance/create")
    @login_required
    def create_attendance() -> Response:
        if is_admin():
            user_id = request.form.get("user_id", type=int)
        else:
            user_id = g.user["id"]

        action = request.form.get("action", "").strip()
        recorded_at = request.form.get("recorded_at", "").strip()

        if action not in {"دخول", "خروج"}:
            flash("نوع العملية غير صحيح.", "error")
            return redirect(url_for("dashboard"))

        if not user_id or not recorded_at:
            flash("الرجاء إكمال بيانات الحضور والانصراف.", "error")
            return redirect(url_for("dashboard"))

        execute_db(
            "INSERT INTO attendance (user_id, action, recorded_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, action, recorded_at, utcnow(), utcnow()),
        )
        flash("تم حفظ سجل الحضور والانصراف بنجاح.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/attendance/<int:attendance_id>/update")
    @login_required
    @admin_required
    def update_attendance(attendance_id: int) -> Response:
        user_id = request.form.get("user_id", type=int)
        action = request.form.get("action", "").strip()
        recorded_at = request.form.get("recorded_at", "").strip()

        if action not in {"دخول", "خروج"} or not user_id or not recorded_at:
            flash("بيانات التعديل غير مكتملة.", "error")
            return redirect(url_for("dashboard"))

        execute_db(
            """
            UPDATE attendance
            SET user_id = ?, action = ?, recorded_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (user_id, action, recorded_at, utcnow(), attendance_id),
        )
        flash("تم تعديل السجل بنجاح.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/attendance/<int:attendance_id>/delete")
    @login_required
    @admin_required
    def delete_attendance(attendance_id: int) -> Response:
        execute_db("DELETE FROM attendance WHERE id = ?", (attendance_id,))
        flash("تم حذف السجل.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/attendance/delete-range")
    @login_required
    @admin_required
    def delete_attendance_range() -> Response:
        attendance_period = request.form.get("attendance_period", "day").strip()
        attendance_date = request.form.get("attendance_date", "").strip()
        try:
            anchor_date = datetime.strptime(attendance_date, "%Y-%m-%d").date()
        except ValueError:
            flash("تاريخ الحذف غير صحيح.", "error")
            return redirect(url_for("dashboard"))

        range_start, range_end = get_attendance_range(anchor_date, attendance_period)
        deleted_count = execute_db_count(
            "DELETE FROM attendance WHERE recorded_at >= ? AND recorded_at < ?",
            (range_start, range_end),
        )
        flash(f"تم حذف {deleted_count} من سجلات الحضور والانصراف للفترة المحددة.", "success")
        return redirect(
            url_for(
                "dashboard",
                attendance_period=attendance_period,
                attendance_date=attendance_date,
            )
        )

    @app.get("/attendance/export")
    @login_required
    @admin_required
    def export_attendance() -> Response:
        attendance_period = request.args.get("attendance_period", "day").strip()
        attendance_date = request.args.get("attendance_date", datetime.now().strftime("%Y-%m-%d")).strip()
        try:
            anchor_date = datetime.strptime(attendance_date, "%Y-%m-%d").date()
        except ValueError:
            anchor_date = date.today()
            attendance_date = anchor_date.isoformat()
        range_start, range_end = get_attendance_range(anchor_date, attendance_period)
        rows = query_all(
            """
            SELECT
                users.full_name,
                DATE(attendance.recorded_at) AS attendance_date,
                MIN(CASE WHEN attendance.action = 'دخول' THEN attendance.recorded_at END) AS first_check_in,
                MAX(CASE WHEN attendance.action = 'خروج' THEN attendance.recorded_at END) AS last_check_out
            FROM attendance
            JOIN users ON users.id = attendance.user_id
            WHERE attendance.recorded_at >= ? AND attendance.recorded_at < ?
            GROUP BY users.id, DATE(attendance.recorded_at)
            ORDER BY attendance_date DESC, users.full_name
            """,
            (range_start, range_end),
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["اسم الموظف", "التاريخ", "وقت الدخول", "وقت الخروج"])
        for row in rows:
            writer.writerow([
                row["full_name"],
                row["attendance_date"],
                row["first_check_in"] or "-",
                row["last_check_out"] or "-",
            ])

        return Response(
            ("\ufeff" + buffer.getvalue()).encode("utf-16le"),
            mimetype="text/csv; charset=utf-16",
            headers={
                "Content-Disposition": "attachment; filename=attendance-records.csv"
            },
        )

    @app.post("/employees/<int:user_id>/delete")
    @login_required
    @admin_required
    def delete_employee(user_id: int) -> Response:
        employee = query_one("SELECT id, full_name FROM users WHERE id = ? AND role = 'employee'", (user_id,))
        if not employee:
            flash("الموظف غير موجود أو لا يمكن حذفه.", "error")
            return redirect(url_for("dashboard"))
        execute_db("DELETE FROM users WHERE id = ?", (user_id,))
        flash(f"تم حذف حساب الموظف {employee['full_name']} وجميع سجلاته.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/employees/create")
    @login_required
    @admin_required
    def create_employee() -> Response:
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()

        if not full_name or not email:
            flash("الرجاء إدخال اسم الموظف وبريده الإلكتروني.", "error")
            return redirect(url_for("dashboard"))

        existing = query_one("SELECT id, is_active FROM users WHERE email = ?", (email,))
        if existing:
            flash("هذا البريد مستخدم بالفعل داخل النظام.", "error")
            return redirect(url_for("dashboard"))

        execute_db(
            """
            INSERT INTO users (
                full_name, email, password_hash, role, created_at, is_active, invitation_token, invitation_sent_at, password_set_at
            )
            VALUES (?, ?, ?, 'employee', ?, 0, NULL, NULL, NULL)
            """,
            (
                full_name,
                email,
                generate_password_hash(secrets.token_urlsafe(24)),
                utcnow(),
            ),
        )
        flash(f"تمت إضافة الموظف {full_name}. يمكنه الآن طلب رابط إكمال التسجيل عبر بريده.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/payroll/save")
    @login_required
    @admin_required
    def save_payroll() -> Response:
        user_id = request.form.get("user_id", type=int)
        month_label = request.form.get("month_label", "").strip()
        base_salary = request.form.get("base_salary", type=float) or 0.0
        housing_allowance = request.form.get("housing_allowance", type=float) or 0.0
        transport_allowance = request.form.get("transport_allowance", type=float) or 0.0
        insurance_deduction = request.form.get("insurance_deduction", type=float) or 0.0

        if not user_id or not month_label:
            flash("الرجاء اختيار الموظف والشهر قبل الحفظ.", "error")
            return redirect(url_for("dashboard", employee_id=user_id))

        net_salary = (
            base_salary + housing_allowance + transport_allowance - insurance_deduction
        )
        existing = query_one(
            "SELECT id FROM payrolls WHERE user_id = ? AND month_label = ?",
            (user_id, month_label),
        )

        if existing:
            execute_db(
                """
                UPDATE payrolls
                SET base_salary = ?, housing_allowance = ?, transport_allowance = ?,
                    insurance_deduction = ?, net_salary = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    base_salary,
                    housing_allowance,
                    transport_allowance,
                    insurance_deduction,
                    net_salary,
                    utcnow(),
                    existing["id"],
                ),
            )
            flash("تم تحديث مسير الراتب بنجاح.", "success")
        else:
            execute_db(
                """
                INSERT INTO payrolls (
                    user_id, month_label, base_salary, housing_allowance,
                    transport_allowance, insurance_deduction, net_salary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    month_label,
                    base_salary,
                    housing_allowance,
                    transport_allowance,
                    insurance_deduction,
                    net_salary,
                    utcnow(),
                    utcnow(),
                ),
            )
            flash("تم إنشاء مسير الراتب بنجاح.", "success")

        return redirect(url_for("dashboard", employee_id=user_id))

    @app.post("/payroll/<int:payroll_id>/delete")
    @login_required
    @admin_required
    def delete_payroll(payroll_id: int) -> Response:
        payroll = query_one(
            """
            SELECT payrolls.id, payrolls.user_id, payrolls.month_label, users.full_name
            FROM payrolls
            JOIN users ON users.id = payrolls.user_id
            WHERE payrolls.id = ?
            """,
            (payroll_id,),
        )
        if not payroll:
            flash("مسير الراتب غير موجود.", "error")
            return redirect(url_for("dashboard"))

        execute_db("DELETE FROM payrolls WHERE id = ?", (payroll_id,))
        flash(
            f"تم حذف مسير راتب {payroll['full_name']} لشهر {format_month_label(payroll['month_label'])}.",
            "success",
        )
        return redirect(url_for("dashboard", employee_id=payroll["user_id"]))

    @app.get("/payroll/<int:payroll_id>/download")
    @login_required
    def download_payroll(payroll_id: int) -> Response:
        payroll = query_one(
            """
            SELECT payrolls.*, users.full_name, users.id AS employee_id
            FROM payrolls
            JOIN users ON users.id = payrolls.user_id
            WHERE payrolls.id = ?
            """,
            (payroll_id,),
        )
        if not payroll:
            flash("مسير الراتب غير موجود.", "error")
            return redirect(url_for("dashboard"))

        if not is_admin() and payroll["employee_id"] != g.user["id"]:
            flash("لا يمكنك تنزيل مسير راتب لموظف آخر.", "error")
            return redirect(url_for("dashboard"))

        return Response(
            render_template("payslip_download.html", payroll=payroll),
            mimetype="text/html",
        )

    with app.app_context():
        init_db()

    return app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

def close_db(_: Any = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_all(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return list(get_db().execute(query, params).fetchall())


def query_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return get_db().execute(query, params).fetchone()


def execute_db(query: str, params: tuple[Any, ...] = ()) -> None:
    db = get_db()
    db.execute(query, params)
    db.commit()


def execute_db_count(query: str, params: tuple[Any, ...] = ()) -> int:
    db = get_db()
    cursor = db.execute(query, params)
    db.commit()
    return cursor.rowcount


def init_db() -> None:
    db = sqlite3.connect(current_app.config["DATABASE"])
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'employee')),
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            invitation_token TEXT,
            invitation_sent_at TEXT,
            password_set_at TEXT
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('دخول', 'خروج')),
            recorded_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS payrolls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            month_label TEXT NOT NULL,
            base_salary REAL NOT NULL,
            housing_allowance REAL NOT NULL,
            transport_allowance REAL NOT NULL,
            insurance_deduction REAL NOT NULL,
            net_salary REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, month_label),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    ensure_user_columns(db)
    db.commit()

    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        seed_users(db)
    ensure_default_admin(db)
    if db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 0:
        seed_business_data(db)

    db.close()


def ensure_user_columns(db: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
    }
    if "is_active" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "invitation_token" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN invitation_token TEXT")
    if "invitation_sent_at" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN invitation_sent_at TEXT")
    if "password_set_at" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN password_set_at TEXT")


def seed_users(db: sqlite3.Connection) -> None:
    now = utcnow()
    users = [
        ("الجوهرة علي", "aljawhara.ali@competitive.sa", "Admin@123", "admin"),
        ("أحمد علي", "ahmed@competitive.local", "Employee@123", "employee"),
        ("سارة خالد", "sara@competitive.local", "Employee@123", "employee"),
        ("محمد سالم", "mohammed@competitive.local", "Employee@123", "employee"),
    ]
    db.executemany(
        """
        INSERT OR IGNORE INTO users (
            full_name, email, password_hash, role, created_at, is_active, invitation_token, invitation_sent_at, password_set_at
        ) VALUES (?, ?, ?, ?, ?, 1, NULL, NULL, ?)
        """,
        [
            (name, email, generate_password_hash(password), role, now, now if role == "employee" else None)
            for name, email, password, role in users
        ],
    )
    db.commit()


def ensure_default_admin(db: sqlite3.Connection) -> None:
    admin = db.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()
    payload = (
        "الجوهرة علي",
        "aljawhara.ali@competitive.sa",
        generate_password_hash("Admin@123"),
    )
    if admin:
        db.execute(
            """
            UPDATE users
            SET full_name = ?, email = ?, password_hash = ?, role = 'admin', is_active = 1
            WHERE id = ?
            """,
            (*payload, admin["id"]),
        )
    else:
        db.execute(
            """
            INSERT INTO users (full_name, email, password_hash, role, created_at, is_active)
            VALUES (?, ?, ?, 'admin', ?, 1)
            """,
            (*payload, utcnow()),
        )
    db.commit()


def seed_business_data(db: sqlite3.Connection) -> None:
    employees = db.execute("SELECT id, full_name FROM users WHERE role = 'employee'").fetchall()
    names = {row["full_name"]: row["id"] for row in employees}
    now = utcnow()
    db.executemany(
        """
        INSERT INTO attendance (user_id, action, recorded_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (names["أحمد علي"], "دخول", "2026-04-13T08:01", now, now),
            (names["سارة خالد"], "دخول", "2026-04-13T08:16", now, now),
            (names["أحمد علي"], "خروج", "2026-04-13T17:05", now, now),
        ],
    )
    db.executemany(
        """
        INSERT INTO payrolls (
            user_id, month_label, base_salary, housing_allowance, transport_allowance,
            insurance_deduction, net_salary, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (names["أحمد علي"], "2026-03", 6500, 1800, 700, 630, 8370, now, now),
            (names["سارة خالد"], "2026-03", 7200, 2000, 800, 690, 9310, now, now),
        ],
    )
    db.commit()


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def format_currency(value: float | int) -> str:
    return f"{float(value):,.2f} ر.س"


def format_month_label(value: str) -> str:
    try:
        dt = datetime.strptime(value, "%Y-%m")
    except ValueError:
        return value
    month_names = {
        1: "يناير",
        2: "فبراير",
        3: "مارس",
        4: "أبريل",
        5: "مايو",
        6: "يونيو",
        7: "يوليو",
        8: "أغسطس",
        9: "سبتمبر",
        10: "أكتوبر",
        11: "نوفمبر",
        12: "ديسمبر",
    }
    return f"{month_names[dt.month]} {dt.year}"


def get_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="employee-invitation")


def build_invitation_token(user_id: int, nonce: str) -> str:
    return get_serializer().dumps({"user_id": user_id, "nonce": nonce})


def validate_invitation_token(token: str) -> sqlite3.Row | None:
    try:
        payload = get_serializer().loads(
            token,
            max_age=current_app.config["INVITATION_EXPIRY_SECONDS"],
        )
    except (BadSignature, SignatureExpired):
        return None

    user = query_one(
        """
        SELECT id, full_name, email, is_active, invitation_token
        FROM users
        WHERE id = ? AND role = 'employee'
        """,
        (payload["user_id"],),
    )
    if not user or user["is_active"]:
        return None
    if not user["invitation_token"] or user["invitation_token"] != payload["nonce"]:
        return None
    return user


def send_account_email(recipient: str, subject: str, body: str) -> None:
    if current_app.config.get("MAIL_SUPPRESS_SEND"):
        current_app.config.setdefault("OUTBOX", []).append(
            {"recipient": recipient, "subject": subject, "body": body}
        )
        return

    mail_from = current_app.config.get("MAIL_FROM")
    if not mail_from:
        raise RuntimeError("Mail configuration is incomplete.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = recipient
    message.set_content(body)

    resend_api_key = current_app.config.get("RESEND_API_KEY", "")
    resend_api_url = current_app.config.get("RESEND_API_URL", "")
    if resend_api_key and mail_from.endswith("@mail.competitive.sa"):
        payload = json.dumps(
            {
                "from": mail_from,
                "to": [recipient],
                "subject": subject,
                "text": body,
            }
        ).encode("utf-8")
        resend_request = urllib_request.Request(
            resend_api_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {resend_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib_request.urlopen(resend_request, timeout=20) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            if status_code >= 400:
                raise RuntimeError("Resend API rejected the email request.")
        return

    mail_server = current_app.config.get("MAIL_SERVER")
    if not mail_server:
        raise RuntimeError("Mail configuration is incomplete.")

    with smtplib.SMTP(mail_server, current_app.config["MAIL_PORT"], timeout=20) as smtp:
        if current_app.config.get("MAIL_USE_TLS"):
            smtp.starttls()
        if current_app.config.get("MAIL_USERNAME"):
            smtp.login(
                current_app.config["MAIL_USERNAME"],
                current_app.config["MAIL_PASSWORD"],
            )
        smtp.send_message(message)


def get_attendance_range(anchor_date: date, period: str) -> tuple[str, str]:
    start = anchor_date
    if period == "week":
        start = anchor_date - timedelta(days=anchor_date.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        start = anchor_date.replace(day=1)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    else:
        end = start + timedelta(days=1)
    return (
        datetime.combine(start, datetime.min.time()).strftime("%Y-%m-%dT%H:%M"),
        datetime.combine(end, datetime.min.time()).strftime("%Y-%m-%dT%H:%M"),
    )


def login_required(view):
    @wraps(view)
    def wrapped_view(*args: Any, **kwargs: Any):
        if not g.get("user"):
            flash("يرجى تسجيل الدخول أولاً.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args: Any, **kwargs: Any):
        if not is_admin():
            flash("هذه الصفحة متاحة للإدمن فقط.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped_view


def is_admin() -> bool:
    return bool(g.get("user") and g.user["role"] == "admin")


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
