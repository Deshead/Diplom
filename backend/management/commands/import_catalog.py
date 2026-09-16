from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from backend.catalog import CatalogError, import_catalog
from backend.models import User


class Command(BaseCommand):
    help = "Загрузить YAML-прайс существующего поставщика"

    def add_arguments(self, parser):
        parser.add_argument("file", type=Path)
        parser.add_argument("--email", required=True, help="Email пользователя с типом shop")

    def handle(self, *args, **options):
        try:
            user = User.objects.get(email=options["email"].lower(), type="shop")
        except User.DoesNotExist:
            raise CommandError("Поставщик с таким email не найден") from None
        try:
            result = import_catalog(options["file"].read_bytes(), user)
        except (OSError, CatalogError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Магазин {result['shop_id']}: категорий {result['categories']}, "
                f"товаров {result['products']}"
            )
        )
