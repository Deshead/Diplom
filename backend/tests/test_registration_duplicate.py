from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from backend.auth_views import RegistrationSerializer
from backend.models import EmailToken, User


@skipUnless(connection.vendor == "postgresql", "Гонки регистрации проверяются на PostgreSQL.")
class DuplicateRegistrationTests(TransactionTestCase):
    def test_concurrent_duplicate_email_returns_validation_error(self):
        cache.clear()
        validated = Barrier(2)
        original_is_valid = RegistrationSerializer.is_valid

        def validate_together(serializer, *args, **kwargs):
            result = original_is_valid(serializer, *args, **kwargs)
            # Оба запроса проверили свободный email до сохранения любого из них.
            validated.wait(timeout=10)
            return result

        def register(email):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '10s'")
                client = APIClient()
                client.raise_request_exception = False
                return client.post(
                    "/api/v1/user/register",
                    {
                        "email": email,
                        "password": "Study-Orders-2026!",
                        "first_name": "Иван",
                        "last_name": "Иванов",
                    },
                    format="json",
                )
            finally:
                connection.close()

        with patch.object(RegistrationSerializer, "is_valid", validate_together):
            with patch("backend.auth_views.send_email.delay") as send_email:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    responses = list(
                        pool.map(register, ["student@example.com", "STUDENT@example.com"])
                    )

        self.assertEqual(sorted(response.status_code for response in responses), [201, 400])
        rejected = next(response for response in responses if response.status_code == 400)
        self.assertIn("email", rejected.data)
        self.assertEqual(User.objects.count(), 1)
        user = User.objects.get(email="student@example.com")
        self.assertFalse(user.is_active)
        self.assertEqual(EmailToken.objects.filter(user=user, purpose="register").count(), 1)
        send_email.assert_called_once()
