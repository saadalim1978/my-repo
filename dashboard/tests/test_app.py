import os
import tempfile
import unittest
import uuid
from pathlib import Path

from app import create_app


class AppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = str(Path(tempfile.gettempdir()) / f"csco-test-{uuid.uuid4().hex}.db")
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": self.db_path,
            }
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def login(self, email: str, password: str):
        return self.client.post(
            "/login",
            data={"email": email, "password": password},
            follow_redirects=True,
        )

    def register_employee(self, full_name: str, email: str, password: str = "Employee@456"):
        return self.client.post(
            "/register",
            data={
                "full_name": full_name,
                "email": email,
                "password": password,
                "confirm_password": password,
            },
            follow_redirects=True,
        )

    def test_login_page_loads(self) -> None:
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("تسجيل الدخول".encode("utf-8"), response.data)

    def test_admin_can_login_and_export_attendance(self) -> None:
        response = self.login("aljawhara.ali@competitive.sa", "Admin@123")
        self.assertEqual(response.status_code, 200)
        csv_response = self.client.get("/attendance/export")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("text/csv", csv_response.headers["Content-Type"])
        self.assertTrue(csv_response.data.startswith(b"\xff\xfe"))
        self.assertIn("اسم الموظف".encode("utf-16le"), csv_response.data)

    def test_employee_cannot_export_attendance(self) -> None:
        self.login("ahmed@competitive.local", "Employee@123")
        response = self.client.get("/attendance/export", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("متاحة".encode("utf-8"), response.data)
        dashboard_response = self.client.get("/dashboard")
        self.assertNotIn("موظفون".encode("utf-8"), dashboard_response.data)

    def test_employee_can_register(self) -> None:
        response = self.register_employee("موظف تجريبي جديد", "new.employee@competitive.sa")
        self.assertEqual(response.status_code, 200)
        self.assertIn("موظف تجريبي جديد".encode("utf-8"), response.data)
        login_response = self.login("new.employee@competitive.sa", "Employee@456")
        self.assertEqual(login_response.status_code, 200)
        self.assertIn("لوحة تشغيل الموارد البشرية".encode("utf-8"), login_response.data)

    def test_employee_can_reset_password(self) -> None:
        self.register_employee("موظف إعادة تعيين", "reset.employee@competitive.sa")
        response = self.client.post(
            "/reset-password",
            data={
                "email": "reset.employee@competitive.sa",
                "password": "Employee@789",
                "confirm_password": "Employee@789",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        login_response = self.login("reset.employee@competitive.sa", "Employee@789")
        self.assertEqual(login_response.status_code, 200)
        self.assertIn("لوحة تشغيل الموارد البشرية".encode("utf-8"), login_response.data)

    def test_payroll_download_is_html(self) -> None:
        self.login("ahmed@competitive.local", "Employee@123")
        response = self.client.get("/payroll/1/download")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["Content-Type"])
        self.assertIn("مسير راتب الموظف".encode("utf-8"), response.data)

    def test_admin_can_delete_employee_account_and_related_data(self) -> None:
        self.register_employee("موظف للحذف", "delete.me@example.com")
        self.login("delete.me@example.com", "Employee@456")
        self.client.post(
            "/attendance/create",
            data={"action": "دخول", "recorded_at": "2026-04-13T08:00"},
            follow_redirects=True,
        )
        self.client.post("/logout", follow_redirects=True)
        self.login("aljawhara.ali@competitive.sa", "Admin@123")
        dashboard_response = self.client.get("/dashboard")
        self.assertIn("delete.me@example.com".encode("utf-8"), dashboard_response.data)

        response = self.client.post("/employees/5/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("تم حذف حساب الموظف".encode("utf-8"), response.data)

        self.client.post("/logout", follow_redirects=True)
        login_response = self.login("delete.me@example.com", "Employee@456")
        self.assertIn("بيانات الدخول غير صحيحة".encode("utf-8"), login_response.data)

    def test_admin_can_delete_attendance_range_for_day(self) -> None:
        self.login("aljawhara.ali@competitive.sa", "Admin@123")
        self.client.post(
            "/attendance/create",
            data={"user_id": 2, "action": "دخول", "recorded_at": "2026-04-13T08:00"},
            follow_redirects=True,
        )
        self.client.post(
            "/attendance/create",
            data={"user_id": 2, "action": "خروج", "recorded_at": "2026-04-13T17:00"},
            follow_redirects=True,
        )

        response = self.client.post(
            "/attendance/delete-range",
            data={"attendance_period": "day", "attendance_date": "2026-04-13"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("تم حذف".encode("utf-8"), response.data)
        self.assertIn("لا توجد سجلات للفترة المحددة".encode("utf-8"), response.data)

    def test_dashboard_groups_attendance_by_day(self) -> None:
        self.login("aljawhara.ali@competitive.sa", "Admin@123")
        self.client.post(
            "/attendance/create",
            data={"user_id": 2, "action": "دخول", "recorded_at": "2026-04-20T08:00"},
            follow_redirects=True,
        )
        self.client.post(
            "/attendance/create",
            data={"user_id": 2, "action": "خروج", "recorded_at": "2026-04-20T17:00"},
            follow_redirects=True,
        )

        response = self.client.get("/dashboard?attendance_period=day&attendance_date=2026-04-20")
        self.assertEqual(response.status_code, 200)
        self.assertIn("سجل الدخول".encode("utf-8"), response.data)
        self.assertIn("سجل الخروج".encode("utf-8"), response.data)
        self.assertIn("2026-04-20T08:00".encode("utf-8"), response.data)
        self.assertIn("2026-04-20T17:00".encode("utf-8"), response.data)


if __name__ == "__main__":
    unittest.main()
