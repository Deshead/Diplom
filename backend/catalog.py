"""Чтение прайса и обновление каталога поставщика."""

import http.client
import ipaddress
import socket
import ssl
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit

import yaml
from django.conf import settings
from django.db import transaction

from .models import Category, Parameter, Product, ProductInfo, ProductParameter, Shop, User


class CatalogError(ValueError):
    pass


def _text(value, field, max_length, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise CatalogError(f"{field}: нужна непустая строка")
    value = value.strip()
    if len(value) > max_length:
        raise CatalogError(f"{field}: не больше {max_length} символов")
    return value


def _integer(value, field, minimum=1, maximum=9223372036854775807):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CatalogError(f"{field}: нужно целое число от {minimum} до {maximum}")
    return value


def _price(value, field):
    if isinstance(value, bool):
        raise CatalogError(f"{field}: неверная цена")
    try:
        result = Decimal(str(value))
        if (
            not result.is_finite()
            or result < 0
            or result > Decimal("9999999999.99")
            or result != result.quantize(Decimal("0.01"))
        ):
            raise CatalogError(f"{field}: нужна неотрицательная цена с точностью до копеек")
    except (InvalidOperation, ValueError):
        raise CatalogError(f"{field}: неверная цена") from None
    return result


def read_catalog(content):
    """Сначала проверяем весь файл, чтобы ошибка не оставила половину прайса."""
    if isinstance(content, bytes):
        if len(content) > settings.CATALOG_MAX_BYTES:
            raise CatalogError("Файл слишком большой")
        try:
            content = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CatalogError("Файл должен быть в кодировке UTF-8") from None
    if not isinstance(content, str) or len(content.encode("utf-8")) > settings.CATALOG_MAX_BYTES:
        raise CatalogError("Файл слишком большой или имеет неверный формат")
    try:
        data = yaml.safe_load(content)
    except (yaml.YAMLError, RecursionError):
        raise CatalogError("Не удалось прочитать YAML") from None
    if not isinstance(data, dict):
        raise CatalogError("В корне YAML должен быть словарь")
    shop_name = _text(data.get("shop"), "shop", 100)
    categories = data.get("categories")
    goods = data.get("goods")
    if not isinstance(categories, list) or not isinstance(goods, list):
        raise CatalogError("categories и goods должны быть списками")
    if len(categories) > 10000 or len(goods) > 10000:
        raise CatalogError("В прайсе должно быть не больше 10000 категорий и товаров")

    category_map = {}
    for category in categories:
        if not isinstance(category, dict):
            raise CatalogError("Каждая категория должна быть словарем")
        category_id = _integer(category.get("id"), "category.id")
        if category_id in category_map:
            raise CatalogError(f"Повторяется категория {category_id}")
        category_map[category_id] = _text(category.get("name"), "category.name", 100)

    result = []
    external_ids = set()
    for item in goods:
        if not isinstance(item, dict):
            raise CatalogError("Каждый товар должен быть словарем")
        external_id = _integer(item.get("id"), "goods.id")
        if external_id in external_ids:
            raise CatalogError(f"Повторяется товар {external_id}")
        external_ids.add(external_id)
        category_id = _integer(item.get("category"), "goods.category")
        if category_id not in category_map:
            raise CatalogError(f"У товара {external_id} неизвестная категория {category_id}")
        parameters = item.get("parameters", {})
        # У телефона память, у телевизора диагональ. Отдельные колонки не нужны.
        if not isinstance(parameters, dict):
            raise CatalogError(f"У товара {external_id} parameters должен быть словарем")
        clean_parameters = {}
        for name, value in parameters.items():
            name = _text(name, "parameter.name", 100)
            if not isinstance(value, (str, int, float, bool)):
                raise CatalogError(f"Характеристика {name}: нужно строковое или числовое значение")
            clean_parameters[name] = _text(str(value), "parameter.value", 255, allow_empty=True)
        result.append(
            {
                "external_id": external_id,
                "category_id": category_id,
                "name": _text(item.get("name"), "goods.name", 255),
                "description": _text(
                    item.get("description", ""), "goods.description", 10000, allow_empty=True
                ),
                "model": _text(item.get("model", ""), "goods.model", 100, allow_empty=True),
                "quantity": _integer(
                    item.get("quantity"), "goods.quantity", minimum=0, maximum=2147483647
                ),
                "price": _price(item.get("price"), "goods.price"),
                "price_rrc": _price(item.get("price_rrc"), "goods.price_rrc"),
                "parameters": clean_parameters,
            }
        )
    return {"shop": shop_name, "categories": category_map, "goods": result}


def import_catalog(content, user, url=""):
    if user.type != "shop":
        raise CatalogError("Импорт доступен только поставщику")
    if len(url) > 200:
        raise CatalogError("Адрес прайса должен быть не длиннее 200 символов")
    data = read_catalog(content)
    with transaction.atomic():
        # Параллельные импорты одного поставщика выполняются по очереди.
        User.objects.select_for_update().get(pk=user.pk)
        for category in Category.objects.filter(pk__in=data["categories"]):
            if category.name != data["categories"][category.pk]:
                raise CatalogError(f"Категория {category.pk} уже существует с другим названием")
        shop, _ = Shop.objects.get_or_create(user=user, defaults={"name": data["shop"]})
        shop.name = data["shop"]
        if url:
            shop.url = url
        shop.save()
        categories = []
        for category_id, name in data["categories"].items():
            category, _ = Category.objects.get_or_create(pk=category_id, defaults={"name": name})
            if category.name != name:
                raise CatalogError(f"Категория {category.pk} уже существует с другим названием")
            categories.append(category)
        shop.categories.set(categories)

        # Такой же порядок блокировок используется при оформлении заказа.
        list(shop.product_infos.select_for_update().order_by("pk"))
        imported_ids = []
        for item in data["goods"]:
            # Общий товар может продаваться у нескольких поставщиков.
            # При смене названия связываем предложение с другим Product.
            product, _ = Product.objects.get_or_create(
                name=item["name"],
                category_id=item["category_id"],
                defaults={"description": item["description"]},
            )
            if not product.product_infos.exclude(shop=shop).exists():
                product.description = item["description"]
                product.save(update_fields=["description"])
            info, _ = ProductInfo.objects.update_or_create(
                shop=shop,
                external_id=item["external_id"],
                defaults={
                    "product": product,
                    "model": item["model"],
                    "quantity": item["quantity"],
                    "price": item["price"],
                    "price_rrc": item["price_rrc"],
                    "is_active": True,
                },
            )
            imported_ids.append(info.pk)
            info.product_parameters.all().delete()
            # Старые характеристики убираем, иначе они переживут новый прайс.
            for name, value in item["parameters"].items():
                parameter, _ = Parameter.objects.get_or_create(name=name)
                ProductParameter.objects.create(product_info=info, parameter=parameter, value=value)
        # Не удаляем предложения: на них ссылаются старые заказы.
        shop.product_infos.exclude(pk__in=imported_ids).update(is_active=False, quantity=0)
    return {"shop_id": shop.pk, "products": len(imported_ids), "categories": len(categories)}


def export_catalog(user):
    if user.type != "shop":
        raise CatalogError("Экспорт доступен только поставщику")
    try:
        shop = Shop.objects.get(user=user)
    except Shop.DoesNotExist:
        raise CatalogError("Сначала загрузите прайс магазина") from None
    goods = []
    category_ids = set(shop.categories.values_list("id", flat=True))
    offers = (
        shop.product_infos.filter(is_active=True)
        .select_related("product")
        .prefetch_related("product_parameters__parameter")
        .order_by("external_id")
    )
    for info in offers:
        # В админке товар могли перенести. Эту категорию тоже надо выгрузить.
        category_ids.add(info.product.category_id)
        goods.append(
            {
                "id": info.external_id,
                "category": info.product.category_id,
                "model": info.model,
                "name": info.product.name,
                "description": info.product.description,
                "price": str(info.price),
                "price_rrc": str(info.price_rrc),
                "quantity": info.quantity,
                "parameters": {
                    item.parameter.name: item.value for item in info.product_parameters.all()
                },
            }
        )
    data = {
        "shop": shop.name,
        "categories": list(
            Category.objects.filter(pk__in=category_ids).order_by("id").values("id", "name")
        ),
        "goods": goods,
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _public_address(url):
    """Разрешаем только обычные публичные HTTP(S)-адреса."""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise CatalogError("Нужен публичный адрес http:// или https:// без логина и пароля")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise CatalogError("Не удалось определить адрес сервера")
        for address in addresses:
            if not ipaddress.ip_address(address[4][0]).is_global:
                raise CatalogError("Локальные и служебные адреса запрещены")
    except (ValueError, OSError) as exc:
        raise CatalogError("Некорректный или недоступный адрес прайса") from exc
    return parsed, port, addresses[0][4][0]


def download_catalog(url):
    """Загружаем ограниченный объем; каждый редирект проверяем заново."""
    deadline = time.monotonic() + 30
    for _ in range(4):
        parsed, port, address = _public_address(url)
        connection_class = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_class(parsed.hostname, port=port, timeout=10)
        try:
            # Подключаемся к проверенному IP, чтобы повторный DNS-запрос
            # не мог подменить публичный адрес локальным.
            connection.sock = socket.create_connection((address, port), timeout=10)
            if parsed.scheme == "https":
                connection.sock = ssl.create_default_context().wrap_socket(
                    connection.sock, server_hostname=parsed.hostname
                )
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request(
                "GET", path, headers={"Accept": "application/yaml, text/yaml, text/plain"}
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise CatalogError("Сервер вернул перенаправление без адреса")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise CatalogError(f"Сервер прайса вернул HTTP {response.status}")
            length = response.getheader("Content-Length")
            if length and int(length) > settings.CATALOG_MAX_BYTES:
                raise CatalogError("Файл слишком большой")
            content = bytearray()
            while True:
                if time.monotonic() > deadline:
                    raise CatalogError("Превышено время загрузки прайса")
                chunk = response.read(65536)
                if not chunk:
                    return bytes(content)
                content.extend(chunk)
                if len(content) > settings.CATALOG_MAX_BYTES:
                    raise CatalogError("Файл слишком большой")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if isinstance(exc, CatalogError):
                raise
            raise CatalogError("Не удалось скачать прайс") from exc
        finally:
            connection.close()
    raise CatalogError("Слишком много перенаправлений")
