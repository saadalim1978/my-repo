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

    def test_employee_cannot_export_attendance(self) -> None:
        self.login("ahmed@competitive.local", "Employee@123")
        response = self.client.get("/attendance/export", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("متاحة".encode("utf-8"), response.data)

    def test_employee_can_register(self) -> None:
        response = self.client.post(
            "/register",
            data={
                "email": "new.employee@competitive.sa",
                "password": "Employee@456",
                "confirm_password": "Employee@456",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("تم إنشاء حساب الموظف بنجاح".encode("utf-8"), response.data)


if __name__ == "__main__":
    unittest.main()
