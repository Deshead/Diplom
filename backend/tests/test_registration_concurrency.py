from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from backend.auth_views import send_confirmation
from backend.models import EmailToken, User


@skipUnless(connection.vendor == "postgresql", "Блокировки строк проверяются на PostgreSQL.")
class ConcurrentRegistrationTests(TransactionTestCase):
    def test_resend_and_confirmation_do_not_block_each_other_forever(self):
        cache.clear()
        user = User.objects.create_user("registration@example.com")
        old_token = EmailToken.objects.create(user=user, purpose="register")
        resend_locked = Event()
        continue_resend = Event()
        confirm_started = Event()
        confirm_pid = []

        def pause_resend(user):
            # Запрос уже занял строку пользователя, но ещё не заменил код.
            resend_locked.set()
            if not continue_resend.wait(timeout=10):
                raise RuntimeError("Не дождались второго запроса")
            send_confirmation(user)

        def request(path, data, is_confirmation=False):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '10s'")
                    if is_confirmation:
                        cursor.execute("SELECT pg_backend_pid()")
                        confirm_pid.append(cursor.fetchone()[0])
                        confirm_started.set()
                return APIClient().post(path, data, format="json")
            finally:
                connection.close()

        with patch("backend.auth_views.send_confirmation", side_effect=pause_resend):
            with patch("backend.auth_views.send_email.delay"):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    resend = pool.submit(
                        request, "/api/v1/user/register/resend", {"email": user.email}
                    )
                    try:
                        self.assertTrue(resend_locked.wait(timeout=5))
                        confirm = pool.submit(
                            request,
                            "/api/v1/user/register/confirm",
                            {"email": user.email, "token": old_token.key},
                            True,
                        )
                        self.assertTrue(confirm_started.wait(timeout=5))
                        waiting = False
                        deadline = monotonic() + 5
                        while monotonic() < deadline:
                            with connection.cursor() as cursor:
                                cursor.execute(
                                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                                    [confirm_pid[0]],
                                )
                                row = cursor.fetchone()
                            if row and row[0] == "Lock":
                                waiting = True
                                break
                            sleep(0.02)
                        self.assertTrue(waiting, "Подтверждение не дождалось блокировки")
                    finally:
                        continue_resend.set()
                    self.assertEqual(resend.result(timeout=15).status_code, 200)
                    self.assertEqual(confirm.result(timeout=15).status_code, 400)

        user.refresh_from_db()
        self.assertFalse(user.is_active)
        new_token = EmailToken.objects.get(user=user, purpose="register")
        self.assertNotEqual(new_token.key, old_token.key)
        response = APIClient().post(
            "/api/v1/user/register/confirm",
            {"email": user.email, "token": new_token.key},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertFalse(EmailToken.objects.filter(user=user, purpose="register").exists())
