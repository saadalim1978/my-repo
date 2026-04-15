from __future__ import annotations

import csv
import io
import json
import os
import secrets
import sqlite3
import smtplib
import tempfile
from datetime import UTC, date, datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
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
SAUDI_TZ = timezone(timedelta(hours=3), name="Asia/Riyadh")
WEEKDAY_NAMES_AR = {
    0: "الاثنين",
    1: "الثلاثاء",
    2: "الأربعاء",
    3: "الخميس",
    4: "الجمعة",
    5: "السبت",
    6: "الأحد",
}


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "change-me-in-production"),
        DATABASE=str(DATABASE_PATH),
        MAIL_PROVIDER=os.environ.get("MAIL_PROVIDER", "resend_api"),
        MAIL_SERVER=os.environ.get("MAIL_SERVER", "smtp.resend.com"),
        MAIL_PORT=int(os.environ.get("MAIL_PORT", "587")),
        MAIL_USE_TLS=os.environ.get("MAIL_USE_TLS", "true").lower() == "true",
        MAIL_USERNAME=os.environ.get("MAIL_USERNAME", "resend"),
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
        MAIL_FROM=os.environ.get("MAIL_FROM", "no-reply@mail.competitive.sa"),
        RESEND_API_KEY=os.environ.get("RESEND_API_KEY", ""),
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
            "format_datetime_display": format_datetime_display,
            "format_time_display": format_time_display,
            "format_month_label": format_month_label,
            "now_iso_local": saudi_now().strftime("%Y-%m-%dT%H:%M"),
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

        invitation_link = prepare_invitation_link(user["id"])
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
        except (smtplib.SMTPException, OSError, RuntimeError, urllib_error.URLError) as exc:
            current_app.logger.exception("Email delivery failed for invitation link.")
            flash(f"تعذر إرسال رابط التفعيل حاليًا. {exc}", "error")
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

    @app.post("/reset-password-request")
    def reset_password_request() -> Response:
        if g.user:
            return redirect(url_for("dashboard"))

        email = request.form.get("email", "").strip().lower()

        if not email:
            flash("الرجاء إدخال البريد الإلكتروني المعتمد.", "error")
            return redirect(url_for("login"))

        user = query_one("SELECT id, is_active FROM users WHERE email = ?", (email,))
        if not user:
            flash("لا يوجد حساب مرتبط بهذا البريد الإلكتروني.", "error")
            return redirect(url_for("login"))

        if not user["is_active"]:
            flash("هذا الحساب لم يكتمل تفعيله بعد. استخدم رابط إكمال التسجيل المرسل إلى البريد.", "error")
            return redirect(url_for("login"))

        reset_link = prepare_reset_link(user["id"])
        try:
            send_account_email(
                recipient=email,
                subject="إعادة تعيين كلمة المرور في نظام الموارد البشرية",
                body=(
                    "تم استلام طلب إعادة تعيين كلمة المرور الخاصة بك.\n\n"
                    "لإكمال تعيين كلمة مرور جديدة افتح الرابط التالي:\n"
                    f"{reset_link}\n\n"
                    "صلاحية الرابط 24 ساعة."
                ),
            )
        except (smtplib.SMTPException, OSError, RuntimeError, urllib_error.URLError) as exc:
            current_app.logger.exception("Password reset email delivery failed.")
            flash(f"تعذر إرسال رابط إعادة التعيين حاليًا. {exc}", "error")
            return redirect(url_for("login"))

        flash("تم إرسال رابط إعادة تعيين كلمة المرور إلى بريدك الإلكتروني المعتمد.", "success")
        return redirect(url_for("login"))

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def complete_password_reset(token: str) -> Response | str:
        if g.user:
            return redirect(url_for("dashboard"))

        user = validate_reset_token(token)
        if not user:
            flash("رابط إعادة تعيين كلمة المرور غير صالح أو منتهي الصلاحية.", "error")
            return redirect(url_for("login"))

        if request.method == "POST":
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not password or not confirm_password:
                flash("الرجاء إدخال كلمة المرور وتأكيدها.", "error")
                return redirect(url_for("complete_password_reset", token=token))

            if password != confirm_password:
                flash("تأكيد كلمة المرور غير مطابق.", "error")
                return redirect(url_for("complete_password_reset", token=token))

            if len(password) < 8:
                flash("يجب أن تكون كلمة المرور 8 أحرف على الأقل.", "error")
                return redirect(url_for("complete_password_reset", token=token))

            execute_db(
                """
                UPDATE users
                SET password_hash = ?, invitation_token = NULL, password_set_at = ?
                WHERE id = ?
                """,
                (generate_password_hash(password), utcnow(), user["id"]),
            )
            flash("تم تحديث كلمة المرور بنجاح. يمكنك الآن تسجيل الدخول بكلمتك الجديدة.", "success")
            return redirect(url_for("login"))

        return render_template("reset_password.html", reset_user=user)

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
        attendance_date = request.args.get("attendance_date", saudi_today().strftime("%Y-%m-%d")).strip()
        try:
            anchor_date = datetime.strptime(attendance_date, "%Y-%m-%d").date()
        except ValueError:
            anchor_date = date.today()
            attendance_date = anchor_date.isoformat()
        selected_employee_id = request.args.get("employee_id", type=int)
        if not selected_employee_id and users:
            selected_employee_id = users[0]["id"]
        if not selected_employee_id:
            flash("الرجاء اختيار الموظف المطلوب قبل تنزيل ملف الحضور والانصراف.", "error")
            return redirect(
                url_for(
                    "dashboard",
                    attendance_period=attendance_period,
                    attendance_date=attendance_date,
                )
            )
        employee = query_one(
            "SELECT id, full_name FROM users WHERE id = ? AND role = 'employee'",
            (selected_employee_id,),
        )
        if not employee:
            flash("تعذر العثور على الموظف المطلوب للتصدير.", "error")
            return redirect(
                url_for(
                    "dashboard",
                    attendance_period=attendance_period,
                    attendance_date=attendance_date,
                )
            )
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
                    MAX(CASE WHEN attendance.action = 'دخول' THEN attendance.recorded_at END) AS first_check_in,
                    MAX(CASE WHEN attendance.action = 'خروج' THEN attendance.recorded_at END) AS last_check_out
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
            if selected_employee_id:
                export_query["employee_id"] = selected_employee_id
        else:
            attendance_summary = query_all(
                """
                SELECT
                    users.id AS user_id,
                    users.full_name,
                    DATE(attendance.recorded_at) AS attendance_date,
                    MAX(CASE WHEN attendance.action = 'دخول' THEN attendance.recorded_at END) AS first_check_in,
                    MAX(CASE WHEN attendance.action = 'خروج' THEN attendance.recorded_at END) AS last_check_out
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
                row["attendance_date"] == saudi_today().isoformat()
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
        attendance_date = recorded_at[:10]
        if not is_admin() and action == "\u062e\u0631\u0648\u062c" and attendance_date != saudi_today().isoformat():
            flash("\u064a\u0645\u0643\u0646\u0643 \u062a\u0633\u062c\u064a\u0644 \u0627\u0644\u062e\u0631\u0648\u062c \u0641\u0642\u0637 \u0641\u064a \u0646\u0641\u0633 \u0627\u0644\u064a\u0648\u0645.", "error")
            return redirect(url_for("dashboard"))
        existing_record = query_one(
            """
            SELECT id
            FROM attendance
            WHERE user_id = ? AND action = ? AND substr(recorded_at, 1, 10) = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, action, attendance_date),
        )

        if existing_record:
            execute_db(
                """
                UPDATE attendance
                SET recorded_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (recorded_at, utcnow(), existing_record["id"]),
            )
            flash("تم تحديث آخر حركة لهذا اليوم بنجاح.", "success")
        else:
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
        attendance_date = request.args.get("attendance_date", saudi_today().strftime("%Y-%m-%d")).strip()
        selected_employee_id = request.args.get("employee_id", type=int)
        try:
            anchor_date = datetime.strptime(attendance_date, "%Y-%m-%d").date()
        except ValueError:
            anchor_date = date.today()
            attendance_date = anchor_date.isoformat()
        if not selected_employee_id:
            flash("الرجاء اختيار الموظف المطلوب قبل تنزيل ملف الحضور والانصراف.", "error")
            return redirect(
                url_for(
                    "dashboard",
                    attendance_period=attendance_period,
                    attendance_date=attendance_date,
                )
            )
        employee = query_one(
            "SELECT id, full_name FROM users WHERE id = ? AND role = 'employee'",
            (selected_employee_id,),
        )
        if not employee:
            flash("تعذر العثور على الموظف المطلوب للتصدير.", "error")
            return redirect(
                url_for(
                    "dashboard",
                    attendance_period=attendance_period,
                    attendance_date=attendance_date,
                )
            )
        range_start, range_end = get_attendance_range(anchor_date, attendance_period)
        rows = query_all(
            """
            SELECT
                DATE(attendance.recorded_at) AS attendance_date,
                MIN(attendance.recorded_at) AS first_check_in,
                MAX(attendance.recorded_at) AS last_check_out
            FROM attendance
            WHERE attendance.user_id = ? AND attendance.recorded_at >= ? AND attendance.recorded_at < ?
            GROUP BY DATE(attendance.recorded_at)
            ORDER BY attendance_date ASC
            """,
            (selected_employee_id, range_start, range_end),
        )

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Attendance"
        sheet.append(["اسم الموظف", "التاريخ", "وقت الدخول", "وقت الخروج"])

        for row in rows:
            sheet.append(
                [
                    employee["full_name"],
                    row["attendance_date"],
                    format_time_display(row["first_check_in"]) if row["first_check_in"] else "",
                    format_time_display(row["last_check_out"]) if row["last_check_out"] else "",
                ]
            )

        sheet.column_dimensions["A"].width = 24
        sheet.column_dimensions["B"].width = 16
        sheet.column_dimensions["C"].width = 16
        sheet.column_dimensions["D"].width = 16
        sheet.sheet_view.rightToLeft = True

        buffer = io.BytesIO()
        workbook.save(buffer)
        buffer.seek(0)

        return Response(
            buffer.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=attendance-employee-{employee['id']}-{anchor_date.strftime('%Y-%m')}.xlsx"
                )
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

    @app.get("/employees/<int:user_id>/invitation-link")
    @login_required
    @admin_required
    def employee_invitation_link(user_id: int) -> str | Response:
        employee = query_one(
            """
            SELECT id, full_name, email, is_active
            FROM users
            WHERE id = ? AND role = 'employee'
            """,
            (user_id,),
        )
        if not employee:
            flash("تعذر العثور على الموظف المطلوب.", "error")
            return redirect(url_for("dashboard"))
        if employee["is_active"]:
            flash("هذا الحساب مفعّل بالفعل.", "error")
            return redirect(url_for("dashboard"))

        invitation_link = prepare_invitation_link(employee["id"])
        return render_template(
            "invitation_link.html",
            employee=employee,
            invitation_link=invitation_link,
        )

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


def saudi_now() -> datetime:
    return datetime.now(SAUDI_TZ)


def saudi_today() -> date:
    return saudi_now().date()


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def format_currency(value: float | int) -> str:
    return f"{float(value):,.2f} ر.س"


def format_datetime_display(value: str | None) -> str:
    parsed = parse_display_datetime(value)
    if not parsed:
        return "-" if value else ""
    return parsed.strftime("%m/%d/%Y %I:%M %p")


def format_time_display(value: str | None) -> str:
    parsed = parse_display_datetime(value)
    if not parsed:
        return ""
    return parsed.strftime("%I:%M %p")


def parse_display_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.strip().replace(" ", "T").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.strptime(normalized, fmt)
            except ValueError:
                continue
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(SAUDI_TZ).replace(tzinfo=None)
    return parsed


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


def build_action_token(user_id: int, nonce: str, purpose: str) -> str:
    return get_serializer().dumps({"user_id": user_id, "nonce": nonce, "purpose": purpose})


def prepare_action_link(user_id: int, purpose: str, endpoint: str) -> str:
    invitation_nonce = secrets.token_urlsafe(16)
    execute_db(
        """
        UPDATE users
        SET invitation_token = ?, invitation_sent_at = ?
        WHERE id = ?
        """,
        (invitation_nonce, utcnow(), user_id),
    )
    return url_for(
        endpoint,
        token=build_action_token(user_id, invitation_nonce, purpose),
        _external=True,
    )


def prepare_invitation_link(user_id: int) -> str:
    return prepare_action_link(user_id, "invite", "complete_registration")


def prepare_reset_link(user_id: int) -> str:
    return prepare_action_link(user_id, "reset", "complete_password_reset")


def validate_action_token(token: str, purpose: str, require_active: bool) -> sqlite3.Row | None:
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
    if not user:
        return None
    if require_active and not user["is_active"]:
        return None
    if not require_active and user["is_active"]:
        return None
    if payload.get("purpose") != purpose:
        return None
    if not user["invitation_token"] or user["invitation_token"] != payload["nonce"]:
        return None
    return user


def validate_invitation_token(token: str) -> sqlite3.Row | None:
    return validate_action_token(token, "invite", require_active=False)


def validate_reset_token(token: str) -> sqlite3.Row | None:
    return validate_action_token(token, "reset", require_active=True)


def send_account_email(recipient: str, subject: str, body: str) -> None:
    if current_app.config.get("MAIL_SUPPRESS_SEND"):
        current_app.config.setdefault("OUTBOX", []).append(
            {"recipient": recipient, "subject": subject, "body": body}
        )
        return

    mail_provider = (current_app.config.get("MAIL_PROVIDER") or "resend_api").strip().lower()
    mail_server = (current_app.config.get("MAIL_SERVER") or "").strip()
    mail_username = (current_app.config.get("MAIL_USERNAME") or "").strip()
    mail_password = current_app.config.get("MAIL_PASSWORD") or ""
    mail_from = current_app.config.get("MAIL_FROM")
    resend_api_key = (current_app.config.get("RESEND_API_KEY") or "").strip()
    resend_api_url = (current_app.config.get("RESEND_API_URL") or "").strip()
    if not mail_from:
        raise RuntimeError("إعداد MAIL_FROM غير مكتمل.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = recipient
    message.set_content(body)

    current_app.logger.info(
        "Preparing invitation email via provider=%s from=%s to=%s",
        mail_provider,
        mail_from,
        recipient,
    )

    if mail_provider == "resend_api":
        if not resend_api_key:
            raise RuntimeError("إعداد RESEND_API_KEY مفقود في Render.")
        if not resend_api_key.startswith("re_"):
            raise RuntimeError("قيمة RESEND_API_KEY غير صحيحة. يجب أن تبدأ بـ re_.")
        if not resend_api_url:
            raise RuntimeError("إعداد RESEND_API_URL غير مكتمل.")

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
                "User-Agent": "competitive-solutions-hr/1.0",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(resend_request, timeout=20) as response:
                status_code = getattr(response, "status", None) or response.getcode()
                response_body = response.read().decode("utf-8", errors="ignore")
                current_app.logger.info(
                    "Resend API accepted invitation email with status=%s body=%s",
                    status_code,
                    response_body,
                )
                if status_code >= 400:
                    raise RuntimeError("خدمة Resend رفضت طلب الإرسال.")
        except urllib_error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"Resend API error {exc.code}: {details}") from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(f"تعذر الوصول إلى Resend API: {exc.reason}") from exc
        return

    if not mail_server or not mail_username or not mail_password:
        raise RuntimeError("إعدادات SMTP غير مكتملة.")

    with smtplib.SMTP(mail_server, current_app.config["MAIL_PORT"], timeout=20) as smtp:
        if current_app.config.get("MAIL_USE_TLS"):
            smtp.starttls()
        smtp.login(mail_username, mail_password)
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
