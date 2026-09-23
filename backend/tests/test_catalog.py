import json
from copy import deepcopy
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APITestCase

from backend.catalog import CatalogError, download_catalog, export_catalog, import_catalog
from backend.catalog_tasks import do_import
from backend.models import CatalogJob, Category, Order, OrderItem, ProductInfo, Shop, User


def sample_catalog():
    return {
        "shop": "Первый магазин",
        "categories": [{"id": 224, "name": "Смартфоны"}],
        "goods": [
            {
                "id": 11,
                "category": 224,
                "model": "phone/a",
                "name": "Телефон A",
                "price": "100.50",
                "price_rrc": 150,
                "quantity": 5,
                "parameters": {"Память": 64, "Цвет": "черный"},
            },
            {
                "id": 12,
                "category": 224,
                "model": "phone/b",
                "name": "Телефон B",
                "price": 200,
                "price_rrc": 250,
                "quantity": 3,
                "parameters": {"Память": 128},
            },
        ],
    }


def as_yaml(data):
    return yaml.safe_dump(data, allow_unicode=True)


class CatalogServiceTests(TestCase):
    def setUp(self):
        self.supplier = User.objects.create_user(
            "supplier@example.com", type="shop", is_active=True
        )

    def test_original_catalog_and_repeated_import_keep_offer_ids(self):
        content = (Path(settings.BASE_DIR) / "data" / "shop1.yaml").read_bytes()
        result = import_catalog(content, self.supplier)
        self.assertEqual(result["products"], 14)
        self.assertEqual(result["categories"], 4)
        ids = list(ProductInfo.objects.order_by("id").values_list("id", flat=True))
        import_catalog(content, self.supplier)
        self.assertEqual(ids, list(ProductInfo.objects.order_by("id").values_list("id", flat=True)))
        self.assertEqual(Shop.objects.count(), 1)
        self.assertEqual(Category.objects.count(), 4)

    def test_source_changes_only_after_successful_import(self):
        content = as_yaml(sample_catalog())
        url = "https://example.com/catalog.yaml"
        import_catalog(content, self.supplier, url=url)
        shop = Shop.objects.get(user=self.supplier)
        self.assertEqual((shop.url, shop.filename), (url, ""))

        for filename in ("/home/supplier/prices.yaml", r"C:\prices\prices.yaml"):
            with self.subTest(filename=filename):
                import_catalog(content, self.supplier, filename=filename)
                shop.refresh_from_db()
                self.assertEqual((shop.url, shop.filename), ("", "prices.yaml"))

        with self.assertRaises(CatalogError):
            import_catalog("shop: [broken", self.supplier, url=url)
        shop.refresh_from_db()
        self.assertEqual((shop.url, shop.filename), ("", "prices.yaml"))

        import_catalog(content, self.supplier, url=url)
        shop.refresh_from_db()
        self.assertEqual((shop.url, shop.filename), (url, ""))

    def test_changed_offer_and_removed_offer_preserve_order_history(self):
        data = sample_catalog()
        import_catalog(as_yaml(data), self.supplier)
        old = ProductInfo.objects.get(external_id=12)
        buyer = User.objects.create_user("buyer@example.com", is_active=True)
        order = Order.objects.create(user=buyer, state="new")
        order_item = OrderItem.objects.create(
            order=order,
            product_info=old,
            quantity=1,
            price=old.price,
            product_name=old.product.name,
            shop_name=old.shop.name,
        )
        first_id = ProductInfo.objects.get(external_id=11).pk
        data["goods"] = [data["goods"][0]]
        data["goods"][0].update(price="120.75", quantity=9, parameters={"Цвет": "белый"})
        import_catalog(as_yaml(data), self.supplier)
        first = ProductInfo.objects.get(external_id=11)
        self.assertEqual(first.pk, first_id)
        self.assertEqual(first.price, Decimal("120.75"))
        self.assertEqual(first.quantity, 9)
        self.assertEqual(list(first.product_parameters.values_list("value", flat=True)), ["белый"])
        old.refresh_from_db()
        self.assertFalse(old.is_active)
        self.assertEqual(old.quantity, 0)
        order_item.refresh_from_db()
        self.assertEqual(order_item.product_info_id, old.pk)
        self.assertEqual(order_item.product_name, "Телефон B")
        self.assertEqual(order_item.price, Decimal("200.00"))

    def test_other_supplier_is_unchanged_when_common_product_is_renamed(self):
        data = sample_catalog()
        import_catalog(as_yaml(data), self.supplier)
        other = User.objects.create_user("other@example.com", type="shop", is_active=True)
        import_catalog(as_yaml(data), other)
        other_offer = ProductInfo.objects.get(shop__user=other, external_id=11)
        original_product_id = other_offer.product_id
        data["goods"][0]["name"] = "Новое имя"
        data["goods"][0]["price"] = 800
        import_catalog(as_yaml(data), self.supplier)
        other_offer.refresh_from_db()
        self.assertEqual(other_offer.product_id, original_product_id)
        self.assertEqual(other_offer.product.name, "Телефон A")
        self.assertEqual(other_offer.price, Decimal("100.50"))
        self.assertEqual(
            ProductInfo.objects.get(shop__user=self.supplier, external_id=11).product.name,
            "Новое имя",
        )

    def test_invalid_last_product_does_not_update_first_product_or_shop(self):
        data = sample_catalog()
        import_catalog(as_yaml(data), self.supplier)
        data["shop"] = "Новое имя магазина"
        data["goods"][0]["price"] = 800
        for field, value in (
            ("quantity", -1),
            ("category", 999),
            ("price", "NaN"),
            ("price", "0.001"),
            ("id", True),
            ("quantity", "5"),
        ):
            broken = deepcopy(data)
            broken["goods"][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(CatalogError):
                import_catalog(as_yaml(broken), self.supplier)
        self.assertEqual(Shop.objects.get(user=self.supplier).name, "Первый магазин")
        self.assertEqual(ProductInfo.objects.get(external_id=11).price, Decimal("100.50"))

    def test_duplicate_goods_and_unknown_yaml_types_are_rejected(self):
        data = sample_catalog()
        data["goods"].append(data["goods"][0])
        with self.assertRaises(CatalogError):
            import_catalog(as_yaml(data), self.supplier)
        with self.assertRaises(CatalogError):
            import_catalog("!!python/object:subprocess.Popen {}", self.supplier)
        self.assertFalse(Shop.objects.exists())

    def test_export_can_be_imported_without_losing_prices_or_parameters(self):
        import_catalog(as_yaml(sample_catalog()), self.supplier)
        exported = export_catalog(self.supplier)
        second = User.objects.create_user("second@example.com", type="shop", is_active=True)
        result = import_catalog(exported, second)
        self.assertEqual(result["products"], 2)
        self.assertEqual(yaml.safe_load(exported), yaml.safe_load(export_catalog(second)))

    def test_export_can_be_imported_after_product_category_changes_in_admin(self):
        data = sample_catalog()
        data["categories"].append({"id": 15, "name": "Пустая категория"})
        import_catalog(as_yaml(data), self.supplier)
        category = Category.objects.create(id=999, name="Новая категория")
        product = ProductInfo.objects.get(external_id=11).product
        product.category = category
        product.save(update_fields=["category"])
        self.assertFalse(self.supplier.shop.categories.filter(pk=category.pk).exists())

        exported = export_catalog(self.supplier)
        category_ids = {item["id"] for item in yaml.safe_load(exported)["categories"]}
        self.assertEqual(category_ids, {15, 224, 999})
        second = User.objects.create_user("second@example.com", type="shop", is_active=True)
        import_catalog(exported, second)
        imported = ProductInfo.objects.get(shop__user=second, external_id=11)
        self.assertEqual(imported.product.category_id, category.pk)
        self.assertEqual(yaml.safe_load(exported), yaml.safe_load(export_catalog(second)))

    def test_command_imports_original_file(self):
        output = StringIO()
        call_command(
            "import_catalog",
            str(Path(settings.BASE_DIR) / "data" / "shop1.yaml"),
            email=self.supplier.email,
            stdout=output,
        )
        self.assertEqual(ProductInfo.objects.filter(shop__user=self.supplier).count(), 14)
        self.assertEqual(Shop.objects.get(user=self.supplier).filename, "shop1.yaml")
        self.assertIn("14", output.getvalue())

    @override_settings(DEBUG=True)
    def test_demo_shops_keep_source_filenames(self):
        call_command("seed_demo", stdout=StringIO())
        for number in (1, 2):
            shop = Shop.objects.get(user__email=f"supplier{number}@example.com")
            self.assertEqual(shop.filename, f"shop{number}.yaml")
            self.assertEqual(shop.url, "")


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class CatalogAPITests(APITestCase):
    def setUp(self):
        self.supplier = User.objects.create_user(
            "supplier@example.com", type="shop", is_active=True
        )
        self.buyer = User.objects.create_user("buyer@example.com", is_active=True)
        self.client.force_authenticate(self.supplier)

    def upload(self, content=None):
        content = content or as_yaml(sample_catalog())
        uploaded = SimpleUploadedFile(
            "shop.yaml", content.encode("utf-8"), content_type="text/yaml"
        )
        return self.client.post("/api/v1/partner/update", {"file": uploaded}, format="multipart")

    def test_upload_starts_job_and_owner_can_read_result(self):
        response = self.upload()
        self.assertEqual(response.status_code, 202, response.data)
        response = self.client.get(f"/api/v1/partner/jobs/{response.data['job_id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "done")
        self.assertEqual(response.data["result"]["products"], 2)
        self.assertEqual(Shop.objects.get(user=self.supplier).filename, "shop.yaml")

    def test_invalid_yaml_creates_failed_job_without_catalog_changes(self):
        response = self.upload("shop: [broken")
        self.assertEqual(response.status_code, 202)
        job = CatalogJob.objects.get(pk=response.data["job_id"])
        self.assertEqual(job.status, "error")
        self.assertTrue(job.error)
        self.assertFalse(Shop.objects.exists())

    def test_other_user_cannot_read_job_or_import_pricelist(self):
        response = self.upload()
        self.client.force_authenticate(self.buyer)
        self.assertEqual(
            self.client.get(f"/api/v1/partner/jobs/{response.data['job_id']}").status_code, 404
        )
        self.assertEqual(self.upload().status_code, 403)
        self.assertEqual(self.client.get("/api/v1/partner/export").status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.upload().status_code, 401)

    def test_public_filters_and_product_detail(self):
        self.upload()
        self.client.force_authenticate(None)
        shop = Shop.objects.get(user=self.supplier)
        response = self.client.get(
            "/api/v1/products", {"shop_id": shop.pk, "category_id": 224, "search": "phone/a"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        info = response.data[0]
        self.assertEqual(info["product"]["name"], "Телефон A")
        self.assertEqual(info["price"], "100.50")
        self.assertEqual(info["product_parameters"][0]["parameter"], "Память")
        detail = self.client.get(f"/api/v1/products/{info['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data, info)
        self.assertEqual(self.client.get("/api/v1/products", {"shop_id": "bad"}).status_code, 400)
        self.assertEqual(len(self.client.get("/api/v1/categories").data), 1)

    def test_disabling_shop_hides_all_its_offers(self):
        self.upload()
        info = ProductInfo.objects.first()
        response = self.client.post("/api/v1/partner/state", {"state": "off"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.get("/api/v1/partner/state").data["state"])
        self.assertEqual(self.client.get("/api/v1/products").data, [])
        self.assertEqual(self.client.get("/api/v1/shops").data, [])
        self.assertEqual(self.client.get(f"/api/v1/products/{info.pk}").status_code, 404)
        self.client.post("/api/v1/partner/state", {"state": True}, format="json")
        self.assertEqual(len(self.client.get("/api/v1/products").data), 2)

    def test_inactive_supplier_is_hidden_from_public_catalog(self):
        self.upload()
        hidden_offer = ProductInfo.objects.get(shop__user=self.supplier, external_id=11)
        other = User.objects.create_user("other@example.com", type="shop", is_active=True)
        data = sample_catalog()
        data["categories"] = [{"id": 225, "name": "Планшеты"}]
        for item in data["goods"]:
            item["category"] = 225
        import_catalog(as_yaml(data), other)
        visible_offers = list(
            ProductInfo.objects.filter(shop__user=other).order_by("id").values_list("id", flat=True)
        )

        self.supplier.is_active = False
        self.supplier.save(update_fields=["is_active"])
        self.client.force_authenticate(None)
        for path, expected_ids in (
            ("/api/v1/products", visible_offers),
            ("/api/v1/shops", [other.shop.pk]),
            ("/api/v1/categories", [225]),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item["id"] for item in response.data], expected_ids)
        with self.subTest(path="product_detail"):
            response = self.client.get(f"/api/v1/products/{hidden_offer.pk}")
            self.assertEqual(response.status_code, 404)

        self.supplier.is_active = True
        self.supplier.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(f"/api/v1/products/{hidden_offer.pk}").status_code, 200)
        self.assertEqual(len(self.client.get("/api/v1/products").data), 4)

    def test_export_job_returns_reusable_yaml(self):
        self.upload()
        response = self.client.get("/api/v1/partner/export")
        self.assertEqual(response.status_code, 202)
        job = self.client.get(f"/api/v1/partner/jobs/{response.data['job_id']}")
        self.assertEqual(job.data["status"], "done")
        exported = yaml.safe_load(job.data["result"]["yaml"])
        self.assertEqual(exported["shop"], "Первый магазин")
        self.assertEqual(len(exported["goods"]), 2)

    @patch("backend.catalog_views.do_import.delay")
    @patch("backend.catalog_tasks.download_catalog")
    def test_url_download_happens_inside_task(self, download, delay):
        url = "https://raw.githubusercontent.com/example/shop.yaml"
        response = self.client.post("/api/v1/partner/update", {"url": url}, format="json")
        self.assertEqual(response.status_code, 202)
        download.assert_not_called()
        job_id = response.data["job_id"]
        delay.assert_called_once_with(job_id, content=None, url=url, filename="")
        self.assertEqual(CatalogJob.objects.get(pk=job_id).status, "pending")
        download.return_value = as_yaml(sample_catalog()).encode()
        do_import(job_id, url=url)
        self.assertEqual(CatalogJob.objects.get(pk=job_id).status, "done")
        shop = Shop.objects.get(user=self.supplier)
        self.assertEqual((shop.url, shop.filename), (url, ""))
        download.assert_called_once_with(url)

    @override_settings(CATALOG_MAX_BYTES=10)
    def test_oversized_upload_rejected_before_job_creation(self):
        self.assertEqual(self.upload().status_code, 400)
        self.assertFalse(CatalogJob.objects.exists())

    def test_upload_requires_exactly_one_source(self):
        self.assertEqual(
            self.client.post("/api/v1/partner/update", {}, format="json").status_code, 400
        )
        uploaded = SimpleUploadedFile("shop.yaml", b"shop: x")
        response = self.client.post(
            "/api/v1/partner/update",
            {"file": uploaded, "url": "https://example.com/shop.yaml"},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CatalogJob.objects.exists())

    def test_partner_requests_require_json_object(self):
        self.upload()
        jobs_before = CatalogJob.objects.count()
        for path in ("/api/v1/partner/update", "/api/v1/partner/state"):
            for data in ([], 10, "catalog", None):
                with self.subTest(path=path, data=data):
                    response = self.client.post(
                        path, json.dumps(data), content_type="application/json"
                    )
                    self.assertEqual(response.status_code, 400)
        self.assertEqual(CatalogJob.objects.count(), jobs_before)
        self.assertTrue(Shop.objects.get(user=self.supplier).state)


class CatalogDownloadTests(SimpleTestCase):
    @patch("backend.catalog.socket.create_connection")
    @patch("backend.catalog.socket.getaddrinfo")
    @patch("backend.catalog.http.client.HTTPConnection")
    def test_public_catalog_download_uses_validated_ip(self, connection_class, resolve, connect):
        resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        response = MagicMock(status=200)
        content = as_yaml(sample_catalog()).encode("utf-8")
        response.getheader.return_value = str(len(content))
        response.read.side_effect = [content, b""]
        connection_class.return_value.getresponse.return_value = response
        self.assertEqual(download_catalog("http://example.com/catalog.yaml"), content)
        connect.assert_called_once_with(("93.184.216.34", 80), timeout=10)
        connection_class.return_value.close.assert_called_once()

    @patch("backend.catalog.socket.create_connection")
    @patch("backend.catalog.socket.getaddrinfo")
    def test_private_and_loopback_servers_are_never_contacted(self, resolve, connect):
        for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fc00::1"):
            with self.subTest(address=address):
                resolve.return_value = [(2, 1, 6, "", (address, 80))]
                with self.assertRaises(CatalogError):
                    download_catalog("http://example.com/catalog.yaml")
        connect.assert_not_called()

    @patch("backend.catalog.socket.create_connection")
    @patch("backend.catalog.socket.getaddrinfo")
    @patch("backend.catalog.http.client.HTTPConnection")
    def test_redirect_to_internal_server_is_blocked(self, connection_class, resolve, connect):
        resolve.side_effect = [
            [(2, 1, 6, "", ("93.184.216.34", 80))],
            [(2, 1, 6, "", ("127.0.0.1", 80))],
        ]
        response = MagicMock(status=302)
        response.getheader.return_value = "http://localhost/secret"
        connection_class.return_value.getresponse.return_value = response
        with self.assertRaises(CatalogError):
            download_catalog("http://example.com/catalog.yaml")
        connect.assert_called_once_with(("93.184.216.34", 80), timeout=10)

    @override_settings(CATALOG_MAX_BYTES=10)
    @patch("backend.catalog.socket.create_connection")
    @patch("backend.catalog.socket.getaddrinfo")
    @patch("backend.catalog.http.client.HTTPConnection")
    def test_download_without_content_length_still_has_size_limit(
        self, connection_class, resolve, connect
    ):
        resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 80))]
        response = MagicMock(status=200)
        response.getheader.return_value = None
        response.read.return_value = b"x" * 11
        connection_class.return_value.getresponse.return_value = response
        with self.assertRaisesMessage(CatalogError, "Файл слишком большой"):
            download_catalog("http://example.com/catalog.yaml")

    def test_local_file_scheme_is_rejected(self):
        with self.assertRaises(CatalogError):
            download_catalog("file:///etc/passwd")

    @patch("backend.catalog.socket.getaddrinfo", side_effect=OSError("DNS unavailable"))
    def test_dns_failure_has_readable_error(self, resolve):
        with self.assertRaisesMessage(CatalogError, "Некорректный или недоступный адрес прайса"):
            download_catalog("http://example.com/catalog.yaml")
