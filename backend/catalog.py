"""Чтение прайса и обновление каталога поставщика."""

import http.client
import ipaddress
import socket
import ssl
import time
from decimal import Decimal, InvalidOperation
from pathlib import PureWindowsPath
from urllib.parse import urljoin, urlsplit

import yaml
from django.conf import settings
from django.db import transaction

from .models import Category, Parameter, Product, ProductInfo, ProductParameter, Shop, User


class CatalogError(ValueError):
    pass


def validate_text(value, field, max_length, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise CatalogError(f"{field}: нужна непустая строка")
    value = value.strip()
    if len(value) > max_length:
        raise CatalogError(f"{field}: не больше {max_length} символов")
    return value


def validate_integer(value, field, minimum=1, maximum=9223372036854775807):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CatalogError(f"{field}: нужно целое число от {minimum} до {maximum}")
    return value


def validate_price(value, field):
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


def validate_categories(categories):
    category_map = {}
    for category in categories:
        if not isinstance(category, dict):
            raise CatalogError("Каждая категория должна быть словарем")
        category_id = validate_integer(category.get("id"), "category.id")
        if category_id in category_map:
            raise CatalogError(f"Повторяется категория {category_id}")
        category_map[category_id] = validate_text(category.get("name"), "category.name", 100)
    return category_map


def validate_parameters(parameters, external_id):
    if not isinstance(parameters, dict):
        raise CatalogError(f"У товара {external_id} parameters должен быть словарем")
    clean_parameters = {}
    for name, value in parameters.items():
        name = validate_text(name, "parameter.name", 100)
        if not isinstance(value, (str, int, float, bool)):
            raise CatalogError(f"Характеристика {name}: нужно строковое или числовое значение")
        clean_parameters[name] = validate_text(str(value), "parameter.value", 255, allow_empty=True)
    return clean_parameters


def validate_goods(goods, category_map):
    clean_goods = []
    external_ids = set()
    for item in goods:
        if not isinstance(item, dict):
            raise CatalogError("Каждый товар должен быть словарем")
        external_id = validate_integer(item.get("id"), "goods.id")
        if external_id in external_ids:
            raise CatalogError(f"Повторяется товар {external_id}")
        external_ids.add(external_id)
        category_id = validate_integer(item.get("category"), "goods.category")
        if category_id not in category_map:
            raise CatalogError(f"У товара {external_id} неизвестная категория {category_id}")
        parameters = validate_parameters(item.get("parameters", {}), external_id)
        good = {
            "external_id": external_id,
            "category_id": category_id,
            "name": validate_text(item.get("name"), "goods.name", 255),
            "description": validate_text(
                item.get("description", ""), "goods.description", 10000, allow_empty=True
            ),
            "model": validate_text(item.get("model", ""), "goods.model", 100, allow_empty=True),
            "quantity": validate_integer(
                item.get("quantity"), "goods.quantity", minimum=0, maximum=2147483647
            ),
            "price": validate_price(item.get("price"), "goods.price"),
            "price_rrc": validate_price(item.get("price_rrc"), "goods.price_rrc"),
            "parameters": parameters,
        }
        clean_goods.append(good)
    return clean_goods


def read_catalog(content):
    """Проверяем весь прайс до записи в базу."""
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

    shop_name = validate_text(data.get("shop"), "shop", 100)
    categories = data.get("categories")
    goods = data.get("goods")
    if not isinstance(categories, list) or not isinstance(goods, list):
        raise CatalogError("categories и goods должны быть списками")
    if len(categories) > 10000 or len(goods) > 10000:
        raise CatalogError("В прайсе должно быть не больше 10000 категорий и товаров")

    category_map = validate_categories(categories)
    clean_goods = validate_goods(goods, category_map)
    return {"shop": shop_name, "categories": category_map, "goods": clean_goods}


def import_catalog(content, user, url="", filename=""):
    if user.type != "shop":
        raise CatalogError("Импорт доступен только поставщику")
    if len(url) > 200:
        raise CatalogError("Адрес прайса должен быть не длиннее 200 символов")
    if url and filename:
        raise CatalogError("Укажите один источник прайса: адрес или файл")
    if filename:
        # Полный путь с компьютера поставщика нам не нужен.
        filename = validate_text(PureWindowsPath(filename).name, "filename", 255)
    data = read_catalog(content)
    with transaction.atomic():
        # Два прайса одного поставщика загружаем по очереди.
        User.objects.select_for_update().get(pk=user.pk)
        for category in Category.objects.filter(pk__in=data["categories"]):
            if category.name != data["categories"][category.pk]:
                raise CatalogError(f"Категория {category.pk} уже существует с другим названием")
        shop, _ = Shop.objects.get_or_create(user=user, defaults={"name": data["shop"]})
        shop.name = data["shop"]
        changed_fields = ["name"]
        if url:
            shop.url = url
            shop.filename = ""
            changed_fields.extend(["url", "filename"])
        elif filename:
            shop.filename = filename
            shop.url = ""
            changed_fields.extend(["url", "filename"])
        # Поставщик мог выключить магазин, пока загружался прайс.
        shop.save(update_fields=changed_fields)
        categories = []
        for category_id, name in data["categories"].items():
            category, _ = Category.objects.get_or_create(pk=category_id, defaults={"name": name})
            if category.name != name:
                raise CatalogError(f"Категория {category.pk} уже существует с другим названием")
            categories.append(category)
        shop.categories.set(categories)

        # Блокируем по id, как при оформлении заказа.
        list(shop.product_infos.select_for_update().order_by("pk"))
        imported_ids = []
        for item in data["goods"]:
            # Один товар могут продавать разные поставщики.
            product, _ = Product.objects.get_or_create(
                name=item["name"],
                category_id=item["category_id"],
                defaults={"description": item["description"]},
            )
            has_other_shops = product.product_infos.exclude(shop=shop).exists()
            if not has_other_shops:
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
            # Заменяем старые характеристики новыми.
            info.product_parameters.all().delete()
            for name, value in item["parameters"].items():
                parameter, _ = Parameter.objects.get_or_create(name=name)
                ProductParameter.objects.create(product_info=info, parameter=parameter, value=value)
        # Предложения нужны для истории заказов, поэтому не удаляем их.
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
        # В админке товар могли перенести в другую категорию.
        category_ids.add(info.product.category_id)
        parameters = {}
        for item in info.product_parameters.all():
            parameters[item.parameter.name] = item.value
        good = {
            "id": info.external_id,
            "category": info.product.category_id,
            "model": info.model,
            "name": info.product.name,
            "description": info.product.description,
            "price": str(info.price),
            "price_rrc": str(info.price_rrc),
            "quantity": info.quantity,
            "parameters": parameters,
        }
        goods.append(good)

    categories = []
    for category in Category.objects.filter(pk__in=category_ids).order_by("id"):
        categories.append({"id": category.pk, "name": category.name})
    data = {
        "shop": shop.name,
        "categories": categories,
        "goods": goods,
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _public_address(url):
    """Проверяем, что адрес не ведет в локальную сеть."""
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
    """Скачиваем прайс с ограничением времени и размера."""
    deadline = time.monotonic() + 30
    for _ in range(4):
        parsed, port, address = _public_address(url)
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(parsed.hostname, port=port, timeout=10)
        else:
            connection = http.client.HTTPConnection(parsed.hostname, port=port, timeout=10)
        try:
            # Используем проверенный IP без повторного запроса DNS.
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
        except CatalogError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise CatalogError("Не удалось скачать прайс") from exc
        finally:
            connection.close()
    raise CatalogError("Слишком много перенаправлений")
