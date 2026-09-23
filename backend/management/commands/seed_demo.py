from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from backend.catalog import import_catalog
from backend.models import User


class Command(BaseCommand):
    help = "Создать учебные аккаунты и прайсы двух магазинов (только DJANGO_DEBUG=true)"

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Демо-аккаунты можно создавать только при DJANGO_DEBUG=true")
        password = "Demo-Orders-2026!"
        accounts = [
            ("buyer@example.com", "buyer", False),
            ("supplier1@example.com", "shop", False),
            ("supplier2@example.com", "shop", False),
            ("admin@example.com", "buyer", True),
        ]
        users = {}
        for email, user_type, is_admin in accounts:
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "type": user_type,
                    "is_active": True,
                    "is_staff": is_admin,
                    "is_superuser": is_admin,
                    "first_name": "Учебный",
                    "last_name": "Пользователь",
                },
            )
            if created:
                user.set_password(password)
                user.save(update_fields=["password"])
                self.stdout.write(f"Создан {email}, пароль: {password}")
            else:
                self.stdout.write(f"{email} уже существует, пароль не изменен")
            if user.type != user_type:
                raise CommandError(f"У {email} неверная роль для загрузки демо")
            users[email] = user
        for filename, email in [
            ("shop1.yaml", "supplier1@example.com"),
            ("shop2.yaml", "supplier2@example.com"),
        ]:
            content = (Path(settings.BASE_DIR) / "data" / filename).read_text(encoding="utf-8")
            result = import_catalog(content, users[email], filename=filename)
            self.stdout.write(f"{filename}: {result}")
        self.stdout.write(self.style.SUCCESS("Демо готово. Повторный запуск обновляет прайсы."))
