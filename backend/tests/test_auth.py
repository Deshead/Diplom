from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.core.cache import cache
from django.forms.models import model_to_dict
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from backend.admin import ChangePasswordForm, ChangeUserForm
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

    def test_profile_password_change_cancels_pending_reset(self):
        user = User.objects.create_user(**self.data, is_active=True)
        api_token = Token.objects.create(user=user)
        reset_token = EmailToken.objects.create(user=user, purpose="reset")
        self.client.credentials(HTTP_AUTHORIZATION="Token " + api_token.key)

        response = self.client.patch("/api/v1/user/details", {"password": "Changed-Study-2026!"})
        self.assertEqual(response.status_code, 200)
        self.client.credentials()
        response = self.client.post(
            "/api/v1/user/password_reset/confirm",
            {"token": reset_token.key, "password": "Old-Link-Password-2026!"},
        )
        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Changed-Study-2026!"))
        self.assertFalse(EmailToken.objects.filter(pk=reset_token.pk).exists())

    def test_profile_edit_keeps_pending_reset_and_login(self):
        user = User.objects.create_user(**self.data, is_active=True)
        api_token = Token.objects.create(user=user)
        reset_token = EmailToken.objects.create(user=user, purpose="reset")
        self.client.credentials(HTTP_AUTHORIZATION="Token " + api_token.key)

        response = self.client.patch("/api/v1/user/details", {"company": "Новая компания"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/user/details").status_code, 200)
        self.assertTrue(EmailToken.objects.filter(pk=reset_token.pk).exists())

    def test_profile_edit_does_not_restore_an_old_password(self):
        user = User.objects.create_user(**self.data, is_active=True)
        self.client.force_authenticate(user)

        # Другой запрос успел поменять пароль после входа пользователя.
        updated_user = User.objects.get(pk=user.pk)
        updated_user.set_password("Changed-Study-2026!")
        updated_user.save(update_fields=["password"])

        response = self.client.patch("/api/v1/user/details", {"company": "Новая компания"})

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.company, "Новая компания")
        self.assertTrue(user.check_password("Changed-Study-2026!"))

    def test_admin_password_change_revokes_login_and_pending_reset(self):
        user = User.objects.create_user(**self.data, is_active=True)
        staff = User.objects.create_superuser("admin@example.com", "Admin-Orders-2026!")
        api_token = Token.objects.create(user=user)
        reset_token = EmailToken.objects.create(user=user, purpose="reset")
        self.client.force_login(staff)

        response = self.client.post(
            reverse("admin:auth_user_password_change", args=[user.pk]),
            {
                "password1": "Changed-Admin-2026!",
                "password2": "Changed-Admin-2026!",
                "usable_password": "true",
            },
        )
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Changed-Admin-2026!"))
        self.client.logout()
        self.client.credentials(HTTP_AUTHORIZATION="Token " + api_token.key)
        self.assertEqual(self.client.get("/api/v1/user/details").status_code, 401)
        self.client.credentials()
        response = self.client.post(
            "/api/v1/user/password_reset/confirm",
            {"token": reset_token.key, "password": "Old-Link-Password-2026!"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(EmailToken.objects.filter(pk=reset_token.pk).exists())
        user.refresh_from_db()
        self.assertTrue(user.check_password("Changed-Admin-2026!"))

    def test_admin_password_change_keeps_recent_profile_changes(self):
        user = User.objects.create_user(**self.data, is_active=True)
        form = ChangePasswordForm(
            user,
            {
                "password1": "Changed-Admin-2026!",
                "password2": "Changed-Admin-2026!",
                "usable_password": "true",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        # За это время другой запрос поправил профиль.
        User.objects.filter(pk=user.pk).update(company="Новая компания", phone="+79991234567")
        saved_user = form.save()
        user.refresh_from_db()
        self.assertTrue(user.check_password("Changed-Admin-2026!"))
        self.assertEqual(user.company, "Новая компания")
        self.assertEqual(user.phone, "+79991234567")
        self.assertEqual(saved_user.company, user.company)

    def test_admin_profile_edit_does_not_restore_old_password_or_phone(self):
        user = User.objects.create_user(**self.data, is_active=True)
        data = model_to_dict(user)
        data["company"] = "Новая компания"
        form = ChangeUserForm(data=data, instance=user)
        self.assertTrue(form.is_valid(), form.errors)

        # Запрос на смену пароля завершился раньше сохранения формы админки.
        current_user = User.objects.get(pk=user.pk)
        current_user.set_password("Changed-Study-2026!")
        current_user.phone = "+79991234567"
        current_user.save(update_fields=["password", "phone"])

        model_admin = admin.site._registry[User]
        changed_user = model_admin.save_form(None, form, change=True)
        model_admin.save_model(None, changed_user, form, change=True)

        user.refresh_from_db()
        self.assertTrue(user.check_password("Changed-Study-2026!"))
        self.assertFalse(user.check_password(self.data["password"]))
        self.assertEqual(user.company, "Новая компания")
        self.assertEqual(user.phone, "+79991234567")

    def test_admin_profile_edit_saves_groups_and_permissions(self):
        user = User.objects.create_user(
            **self.data, is_active=True, date_joined=timezone.now().replace(microsecond=0)
        )
        staff = User.objects.create_superuser("admin@example.com", "Admin-Orders-2026!")
        group = Group.objects.create(name="Сотрудники склада")
        permission = Permission.objects.get(
            content_type__app_label="backend", codename="view_order"
        )
        self.client.force_login(staff)
        local_date_joined = timezone.localtime(user.date_joined)

        response = self.client.post(
            reverse("admin:backend_user_change", args=[user.pk]),
            {
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "type": user.type,
                "is_active": "on",
                "groups": [group.pk],
                "user_permissions": [permission.pk],
                "date_joined_0": local_date_joined.strftime("%Y-%m-%d"),
                "date_joined_1": local_date_joined.strftime("%H:%M:%S"),
                "_save": "Сохранить",
            },
        )

        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(list(user.groups.all()), [group])
        self.assertEqual(list(user.user_permissions.all()), [permission])
        self.assertTrue(user.check_password(self.data["password"]))

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
