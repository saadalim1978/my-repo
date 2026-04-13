import os
import tempfile
import unittest
import uuid
from pathlib import Path

from app import create_app


class PayrollDeleteTestCase(unittest.TestCase):
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

    def test_admin_can_delete_payroll(self) -> None:
        self.login("aljawhara.ali@competitive.sa", "Admin@123")
        response = self.client.post("/payroll/1/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("تم حذف مسير راتب".encode("utf-8"), response.data)
        self.assertNotIn("/payroll/1/download".encode("utf-8"), response.data)


if __name__ == "__main__":
    unittest.main()
