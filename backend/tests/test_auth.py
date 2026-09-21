from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from backend.models import EmailToken, User


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", CELERY_TASK_ALWAYS_EAGER=True
)
class AuthTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.data = {
            "email": "student@example.com",
            "password": "Study-Orders-2026!",
            "first_name": "Иван",
            "last_name": "Иванов",
        }

    def register(self):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/v1/user/register", self.data)

    def test_registration_confirmation_and_login(self):
        response = self.register()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("token", response.data)
        self.assertNotIn("password", response.data)
        user = User.objects.get(email=self.data["email"])
        self.assertFalse(user.is_active)
        self.assertTrue(user.check_password(self.data["password"]))
        self.assertEqual(self.client.post("/api/v1/user/login", self.data).status_code, 401)
        token = EmailToken.objects.get(user=user)
        self.assertIn(token.key, mail.outbox[0].body)
        response = self.client.post(
            "/api/v1/user/register/confirm", {"email": user.email, "token": token.key}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(EmailToken.objects.filter(pk=token.pk).exists())
        response = self.client.post("/api/v1/user/login", self.data)
        self.assertEqual(response.status_code, 200)
        self.client.credentials(HTTP_AUTHORIZATION="Token " + response.data["Token"])
        self.assertEqual(self.client.get("/api/v1/user/details").data["email"], user.email)
        self.assertEqual(self.client.post("/api/v1/user/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/user/details").status_code, 401)

    def test_invalid_registration_and_case_insensitive_duplicate(self):
        self.assertEqual(self.client.post("/api/v1/user/register", {}).status_code, 400)
        weak = dict(self.data, password="12345678")
        self.assertEqual(self.client.post("/api/v1/user/register", weak).status_code, 400)
        self.register()
        response = self.client.post(
            "/api/v1/user/register", dict(self.data, email="STUDENT@example.com")
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.count(), 1)

    def test_expired_or_wrong_confirmation_does_not_activate(self):
        self.register()
        token = EmailToken.objects.get()
        for email, key in [("someone@example.com", token.key), (self.data["email"], "wrong")]:
            self.assertEqual(
                self.client.post(
                    "/api/v1/user/register/confirm", {"email": email, "token": key}
                ).status_code,
                400,
            )
        EmailToken.objects.update(created_at=timezone.now() - timedelta(days=2))
        self.assertEqual(
            self.client.post(
                "/api/v1/user/register/confirm", {"email": self.data["email"], "token": token.key}
            ).status_code,
            400,
        )
        self.assertFalse(User.objects.get().is_active)

    def test_password_reset_revokes_old_token_and_is_single_use(self):
        user = User.objects.create_user(**self.data, is_active=True)
        old_token = Token.objects.create(user=user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/api/v1/user/password_reset", {"email": user.email})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        token = EmailToken.objects.get(purpose="reset")
        self.assertIn(token.key, mail.outbox[0].body)
        payload = {"token": token.key, "password": "Another-Study-2026!"}
        self.assertEqual(
            self.client.post(
                "/api/v1/user/password_reset/confirm", dict(payload, password="123")
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/api/v1/user/password_reset/confirm", payload).status_code, 200
        )
        user.refresh_from_db()
        self.assertTrue(user.check_password(payload["password"]))
        self.assertFalse(Token.objects.filter(key=old_token.key).exists())
        self.assertEqual(
            self.client.post("/api/v1/user/password_reset/confirm", payload).status_code, 400
        )

    def test_reset_expiry_and_unknown_email(self):
        user = User.objects.create_user(**self.data, is_active=True)
        token = EmailToken.objects.create(user=user, purpose="reset")
        EmailToken.objects.update(created_at=timezone.now() - timedelta(hours=2))
        response = self.client.post(
            "/api/v1/user/password_reset/confirm",
            {"token": token.key, "password": "Another-Study-2026!"},
        )
        self.assertEqual(response.status_code, 400)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/user/password_reset", {"email": "missing@example.com"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_details_cannot_promote_user_and_password_change_revokes_token(self):
        user = User.objects.create_user(**self.data, is_active=True)
        token = Token.objects.create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION="Token " + token.key)
        response = self.client.post(
            "/api/v1/user/details",
            {
                "first_name": "Петр",
                "last_name": "Петров",
                "company": "Магазин",
                "position": "Менеджер",
                "phone": "+79991234567",
                "type": "shop",
                "is_staff": True,
                "email": "changed@example.com",
            },
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Петр")
        self.assertEqual(user.last_name, "Петров")
        self.assertEqual(user.company, "Магазин")
        self.assertEqual(user.position, "Менеджер")
        self.assertEqual(user.phone, "+79991234567")
        self.assertEqual(user.type, "buyer")
        self.assertFalse(user.is_staff)
        self.assertEqual(user.email, self.data["email"])
        response = self.client.post("/api/v1/user/details", {"password": "Changed-Study-2026!"})
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.first_name, "Петр")
        self.assertEqual(user.last_name, "Петров")
        self.assertEqual(user.company, "Магазин")
        self.assertEqual(user.position, "Менеджер")
        self.assertEqual(user.phone, "+79991234567")
        self.assertNotIn("password", response.data["user"])
        self.assertEqual(self.client.get("/api/v1/user/details").status_code, 401)

    def test_profile_phone_must_be_a_phone(self):
        user = User.objects.create_user(**self.data, is_active=True)
        self.client.force_authenticate(user)
        response = self.client.post("/api/v1/user/details", {"phone": "not a number"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/v1/user/details", {"phone": "+7 (999) 123-45-67"})
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.phone, "+7 (999) 123-45-67")

    def test_confirmation_can_be_retried_after_email_failure(self):
        with patch("backend.auth_views.send_email.delay", side_effect=OSError("queue down")):
            with self.assertLogs("django", level="ERROR"):
                self.assertEqual(self.register().status_code, 201)
        old_key = EmailToken.objects.get().key
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/user/register/resend", {"email": self.data["email"]}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        token = EmailToken.objects.get()
        self.assertNotEqual(token.key, old_key)
        self.assertIn(token.key, mail.outbox[0].body)
        response = self.client.post(
            "/api/v1/user/register/confirm", {"email": self.data["email"], "token": token.key}
        )
        self.assertEqual(response.status_code, 200)

    def test_resend_does_not_reactivate_admin_disabled_account(self):
        User.objects.create_user(**self.data, is_active=False)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/user/register/resend", {"email": self.data["email"]}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(EmailToken.objects.exists())
