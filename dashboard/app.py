from __future__ import annotations

import csv
import io
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

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

            if not user or not check_password_hash(user["password_hash"], password):
                flash("بيانات الدخول غير صحيحة.", "error")
            else:
                session.clear()
                session["user_id"] = user["id"]
                flash(f"مرحبًا {user['full_name']}. تم تسجيل الدخول بنجاح.", "success")
                return redirect(url_for("dashboard"))

        return render_template("login.html")

    @app.post("/register")
    def register() -> Response:
        if g.user:
            return redirect(url_for("dashboard"))

        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not full_name or not email or not password:
            flash("الرجاء إدخال الاسم الكامل والبريد الإلكتروني وكلمة المرور.", "error")
            return redirect(url_for("login"))

        if password != confirm_password:
            flash("تأكيد كلمة المرور غير مطابق.", "error")
            return redirect(url_for("login"))

        if len(password) < 8:
            flash("يجب أن تكون كلمة المرور 8 أحرف على الأقل.", "error")
            return redirect(url_for("login"))

        if query_one("SELECT id FROM users WHERE email = ?", (email,)):
            flash("هذا البريد مسجل بالفعل. يمكنك تسجيل الدخول مباشرة.", "error")
            return redirect(url_for("login"))

        execute_db(
            """
            INSERT INTO users (full_name, email, password_hash, role, created_at)
            VALUES (?, ?, ?, 'employee', ?)
            """,
            (
                full_name,
                email,
                generate_password_hash(password),
                utcnow(),
            ),
        )
        flash(f"تم إنشاء حساب الموظف {full_name} بنجاح. يمكنك الآن تسجيل الدخول مباشرة.", "success")
        return redirect(url_for("login"))

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

        user = query_one("SELECT id FROM users WHERE email = ?", (email,))
        if not user:
            flash("لا يوجد حساب مرتبط بهذا البريد الإلكتروني.", "error")
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
            "SELECT id, full_name, email, role FROM users WHERE role = 'employee' ORDER BY full_name"
        )

        if is_admin():
            attendance_records = query_all(
                """
                SELECT attendance.id, attendance.action, attendance.recorded_at, users.full_name
                FROM attendance
                JOIN users ON users.id = attendance.user_id
                ORDER BY attendance.recorded_at DESC
                """
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
            attendance_records = query_all(
                """
                SELECT attendance.id, attendance.action, attendance.recorded_at, users.full_name
                FROM attendance
                JOIN users ON users.id = attendance.user_id
                WHERE attendance.user_id = ?
                ORDER BY attendance.recorded_at DESC
                """,
                (g.user["id"],),
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
            "attendance_count": len(attendance_records),
            "today_count": sum(
                record["recorded_at"].startswith(datetime.now().strftime("%Y-%m-%d"))
                for record in attendance_records
            ),
            "payroll_count": len(payroll_records),
        }

        return render_template(
            "dashboard.html",
            users=users,
            attendance_records=attendance_records,
            payroll_records=payroll_records,
            current_payroll=current_payroll,
            selected_employee_id=selected_employee_id,
            dashboard_stats=dashboard_stats,
            is_admin=is_admin(),
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

    @app.get("/attendance/export")
    @login_required
    @admin_required
    def export_attendance() -> Response:
        rows = query_all(
            """
            SELECT users.full_name, attendance.action, attendance.recorded_at
            FROM attendance
            JOIN users ON users.id = attendance.user_id
            ORDER BY attendance.recorded_at DESC
            """
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["اسم الموظف", "العملية", "التاريخ والوقت"])
        for row in rows:
            writer.writerow([row["full_name"], row["action"], row["recorded_at"]])

        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=attendance-records.csv"
            },
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

        content = render_template("payslip.txt", payroll=payroll, format_currency=format_currency)
        filename = f"payslip-{payroll['full_name'].replace(' ', '-')}-{payroll['month_label']}.txt"
        return Response(
            content,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    with app.app_context():
        init_db()

    return app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
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


def init_db() -> None:
    db = sqlite3.connect(current_app.config["DATABASE"])
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'employee')),
            created_at TEXT NOT NULL
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
    db.commit()

    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        seed_users(db)
    ensure_default_admin(db)
    if db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 0:
        seed_business_data(db)

    db.close()


def seed_users(db: sqlite3.Connection) -> None:
    now = utcnow()
    users = [
        ("الجوهرة علي", "aljawhara.ali@competitive.sa", "Admin@123", "admin"),
        ("أحمد علي", "ahmed@competitive.local", "Employee@123", "employee"),
        ("سارة خالد", "sara@competitive.local", "Employee@123", "employee"),
        ("محمد سالم", "mohammed@competitive.local", "Employee@123", "employee"),
    ]
    db.executemany(
        "INSERT OR IGNORE INTO users (full_name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
        [
            (name, email, generate_password_hash(password), role, now)
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
            SET full_name = ?, email = ?, password_hash = ?, role = 'admin'
            WHERE id = ?
            """,
            (*payload, admin["id"]),
        )
    else:
        db.execute(
            """
            INSERT INTO users (full_name, email, password_hash, role, created_at)
            VALUES (?, ?, ?, 'admin', ?)
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
