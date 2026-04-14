import os
import smtplib
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from app import create_app


class AppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = str(Path(tempfile.gettempdir()) / f"csco-test-{uuid.uuid4().hex}.db")
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": self.db_path,
                "MAIL_SUPPRESS_SEND": True,
                "OUTBOX": [],
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

    def extract_invitation_path(self) -> str:
        outbox = self.app.config["OUTBOX"]
        self.assertTrue(outbox)
        match = re.search(r"http://localhost(?P<path>/complete-registration/[^\s]+)", outbox[-1]["body"])
        self.assertIsNotNone(match)
        return match.group("path")

    def test_login_page_loads(self) -> None:
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("تسجيل الدخول".encode("utf-8"), response.data)
        self.assertIn("إرسال رابط إكمال التسجيل".encode("utf-8"), response.data)

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
        self.assertNotIn("موظفون مفعلون".encode("utf-8"), dashboard_response.data)

    def test_admin_adds_employee_and_employee_completes_registration(self) -> None:
        self.login("aljawhara.ali@competitive.sa", "Admin@123")
        response = self.client.post(
            "/employees/create",
            data={"full_name": "موظف دعوة", "email": "invite.employee@competitive.sa"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("بانتظار التفعيل".encode("utf-8"), response.data)
        self.client.post("/logout", follow_redirects=True)

        request_response = self.client.post(
            "/register-request",
            data={"email": "invite.employee@competitive.sa"},
            follow_redirects=True,
        )
        self.assertEqual(request_response.status_code, 200)
        self.assertIn("تم إرسال رابط إكمال التسجيل".encode("utf-8"), request_response.data)
        self.assertEqual(len(self.app.config["OUTBOX"]), 1)

        complete_path = self.extract_invitation_path()
        form_response = self.client.get(complete_path)
        self.assertEqual(form_response.status_code, 200)
        self.assertIn("إنشاء كلمة المرور".encode("utf-8"), form_response.data)

        activate_response = self.client.post(
            complete_path,
            data={"password": "Employee@456", "confirm_password": "Employee@456"},
            follow_redirects=True,
        )
        self.assertEqual(activate_response.status_code, 200)
        self.assertIn("تم تفعيل حسابك بنجاح".encode("utf-8"), activate_response.data)

        login_response = self.login("invite.employee@competitive.sa", "Employee@456")
        self.assertEqual(login_response.status_code, 200)
        self.assertIn("لوحة تشغيل الموارد البشرية".encode("utf-8"), login_response.data)

    def test_register_request_handles_mail_delivery_failure(self) -> None:
        self.login("aljawhara.ali@competitive.sa", "Admin@123")
        self.client.post(
            "/employees/create",
            data={"full_name": "موظف بريد", "email": "mail.fail@competitive.sa"},
            follow_redirects=True,
        )
        self.client.post("/logout", follow_redirects=True)
        with mock.patch("app.send_account_email", side_effect=smtplib.SMTPException("boom")):
            response = self.client.post(
                "/register-request",
                data={"email": "mail.fail@competitive.sa"},
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("تعذر إرسال رابط التفعيل حاليًا".encode("utf-8"), response.data)

    def test_register_request_uses_resend_api_when_configured(self) -> None:
        self.login("aljawhara.ali@competitive.sa", "Admin@123")
        self.client.post(
            "/employees/create",
            data={"full_name": "Ù…ÙˆØ¸Ù Resend", "email": "resend.employee@competitive.sa"},
            follow_redirects=True,
        )
        self.client.post("/logout", follow_redirects=True)
        self.app.config.update(
            MAIL_SUPPRESS_SEND=False,
            MAIL_FROM="no-reply@mail.competitive.sa",
            RESEND_API_KEY="re_test_key",
            RESEND_API_URL="https://api.resend.com/emails",
        )

        response_mock = mock.MagicMock()
        response_mock.__enter__.return_value = response_mock
        response_mock.__exit__.return_value = None
        response_mock.status = 200

        with mock.patch("app.urllib_request.urlopen", return_value=response_mock) as urlopen_mock, mock.patch(
            "app.smtplib.SMTP"
        ) as smtp_mock:
            response = self.client.post(
                "/register-request",
                data={"email": "resend.employee@competitive.sa"},
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        urlopen_mock.assert_called_once()
        smtp_mock.assert_not_called()

    def test_inactive_employee_cannot_login_before_activation(self) -> None:
        self.login("aljawhara.ali@competitive.sa", "Admin@123")
        self.client.post(
            "/employees/create",
            data={"full_name": "موظف غير مفعل", "email": "inactive.employee@competitive.sa"},
            follow_redirects=True,
        )
        self.client.post("/logout", follow_redirects=True)
        response = self.login("inactive.employee@competitive.sa", "Employee@123")
        self.assertIn("لم يكتمل تفعيله".encode("utf-8"), response.data)

    def test_employee_can_reset_password(self) -> None:
        response = self.client.post(
            "/reset-password",
            data={
                "email": "ahmed@competitive.local",
                "password": "Employee@789",
                "confirm_password": "Employee@789",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        login_response = self.login("ahmed@competitive.local", "Employee@789")
        self.assertEqual(login_response.status_code, 200)
        self.assertIn("لوحة تشغيل الموارد البشرية".encode("utf-8"), login_response.data)

    def test_payroll_download_is_html(self) -> None:
        self.login("ahmed@competitive.local", "Employee@123")
        response = self.client.get("/payroll/1/download")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["Content-Type"])
        self.assertIn("مسير راتب الموظف".encode("utf-8"), response.data)

    def test_admin_can_delete_employee_account_and_related_data(self) -> None:
        self.login("aljawhara.ali@competitive.sa", "Admin@123")
        self.client.post(
            "/employees/create",
            data={"full_name": "موظف للحذف", "email": "delete.me@example.com"},
            follow_redirects=True,
        )
        dashboard_response = self.client.get("/dashboard")
        self.assertIn("delete.me@example.com".encode("utf-8"), dashboard_response.data)

        response = self.client.post("/employees/5/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("تم حذف حساب الموظف".encode("utf-8"), response.data)

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
