import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib import admin, messages
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.forms.models import model_to_dict
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from backend.admin import ChangeUserForm
from backend.models import (
    CatalogJob,
    Category,
    Contact,
    Order,
    OrderItem,
    Product,
    ProductInfo,
    Shop,
    User,
)
from backend.order_services import checkout


def make_offer(supplier, external_id=1, name="Чай", price="100.00", quantity=10):
    shop, _ = Shop.objects.get_or_create(user=supplier, defaults={"name": supplier.email})
    category, _ = Category.objects.get_or_create(name="Продукты")
    product, _ = Product.objects.get_or_create(name=name, category=category)
    return ProductInfo.objects.create(
        shop=shop,
        product=product,
        external_id=external_id,
        price=price,
        price_rrc=price,
        quantity=quantity,
    )


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=True,
    ADMIN_EMAIL="warehouse@example.com",
)
class OrdersTests(APITestCase):
    def setUp(self):
        self.buyer = User.objects.create_user(
            "buyer@example.com",
            "Passw0rd!",
            is_active=True,
            phone="+79991234567",
            first_name="Иван",
            last_name="Иванов",
        )
        self.other = User.objects.create_user(
            "other@example.com", "Passw0rd!", is_active=True, phone="+79997654321"
        )
        self.supplier = User.objects.create_user(
            "shop@example.com", "Passw0rd!", type="shop", is_active=True
        )
        supplier2 = User.objects.create_user(
            "shop2@example.com", "Passw0rd!", type="shop", is_active=True
        )
        self.offer = make_offer(self.supplier)
        self.offer2 = make_offer(supplier2, name="Кофе", price="250.00")
        self.contact = Contact.objects.create(
            user=self.buyer,
            city="Москва",
            street="Мира",
            house="1",
            first_name="Иван",
            last_name="Иванов",
            email=self.buyer.email,
        )
        self.staff = User.objects.create_superuser("admin@example.com", "Passw0rd!")
        self.client.force_authenticate(self.buyer)

    def basket(self, mixed=False):
        items = [{"product_info": self.offer.pk, "quantity": 2}]
        if mixed:
            items.append({"product_info": self.offer2.pk, "quantity": 1})
        response = self.client.post("/api/v1/basket", {"items": items}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return Order.objects.get(pk=response.data["id"])

    def place_order(self, mixed=False):
        order = self.basket(mixed)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/order", {"id": order.pk, "contact": self.contact.pk}
            )
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        return order

    def test_cart_accepts_postman_json_string_and_update_delete(self):
        response = self.client.post(
            "/api/v1/basket",
            {
                "items": json.dumps(
                    [
                        {"product_info": self.offer.pk, "quantity": 2},
                        {"product_info": self.offer2.pk, "quantity": 1},
                    ]
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 200)
        basket = self.client.get("/api/v1/basket").data[0]
        self.assertEqual(basket["total_sum"], "450.00")
        line = basket["ordered_items"][0]
        response = self.client.put(
            "/api/v1/basket",
            {
                "items": json.dumps([{"id": line["id"], "quantity": 3}]),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.delete(
            "/api/v1/basket", {"items": str(line["id"])}, format="multipart"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(OrderItem.objects.count(), 1)
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.quantity, 10)

    def test_invalid_cart_batch_is_atomic(self):
        for items in (
            "oops",
            [],
            [{"product_info": self.offer.pk, "quantity": 0}],
            [
                {"product_info": self.offer.pk, "quantity": 1},
                {"product_info": self.offer2.pk, "quantity": 999},
            ],
        ):
            response = self.client.post("/api/v1/basket", {"items": items}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(OrderItem.objects.count(), 0)
            self.assertEqual(Order.objects.count(), 0)

    def test_checkout_two_suppliers_sends_invoice_and_confirmation(self):
        order = self.place_order(mixed=True)
        self.assertEqual(order.state, "new")
        self.assertEqual(order.total_sum, Decimal("450.00"))
        self.offer.refresh_from_db()
        self.offer2.refresh_from_db()
        self.assertEqual((self.offer.quantity, self.offer2.quantity), (8, 9))
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            {message.to[0] for message in mail.outbox}, {self.buyer.email, "warehouse@example.com"}
        )
        for text in ("Чай", "Кофе", "2 шт.", "100.00", "450.00", "Москва", "+79991234567"):
            self.assertIn(text, mail.outbox[1].body)
        self.assertEqual(self.client.get("/api/v1/basket").data, [])
        self.assertEqual(self.client.get("/api/v1/order").data[0]["id"], order.pk)
        self.assertEqual(self.client.get(f"/api/v1/order/{order.pk}").status_code, 200)

    def test_history_survives_catalog_and_contact_changes(self):
        order = self.place_order()
        self.offer.price = Decimal("999.00")
        self.offer.is_active = False
        self.offer.save()
        self.offer.product.name = "Другое название"
        self.offer.product.save()
        self.offer.shop.name = "Другое название магазина"
        self.offer.shop.save()
        self.buyer.phone = "+79990000000"
        self.buyer.save()
        self.contact.delete()
        data = self.client.get(f"/api/v1/order/{order.pk}").data
        self.assertEqual(data["total_sum"], "200.00")
        self.assertEqual(data["ordered_items"][0]["product_name"], "Чай")
        self.assertEqual(data["ordered_items"][0]["shop_name"], "shop@example.com")
        self.assertEqual(data["contact"]["phone"], "+79991234567")
        self.assertEqual(data["contact"]["city"], "Москва")

    def test_stock_shortage_rolls_back_entire_order(self):
        order = self.basket(mixed=True)
        self.offer2.quantity = 0
        self.offer2.save()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/order", {"id": order.pk, "contact": self.contact.pk}
            )
        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.offer.refresh_from_db()
        self.assertEqual(order.state, "basket")
        self.assertEqual(self.offer.quantity, 10)
        self.assertEqual(len(mail.outbox), 0)

    def test_disabled_shop_and_removed_offer_cannot_be_ordered(self):
        order = self.basket()
        self.offer.shop.state = False
        self.offer.shop.save()
        data = {"id": order.pk, "contact": self.contact.pk}
        self.assertEqual(self.client.post("/api/v1/order", data).status_code, 400)
        self.offer.shop.state = True
        self.offer.shop.save()
        self.offer.is_active = False
        self.offer.save()
        self.assertEqual(self.client.post("/api/v1/order", data).status_code, 400)

    def test_supplier_sees_only_their_items_and_subtotal(self):
        order = self.place_order(mixed=True)
        self.client.force_authenticate(self.supplier)
        response = self.client.get("/api/v1/partner/orders")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], order.pk)
        self.assertEqual(response.data[0]["total_sum"], "200.00")
        self.assertEqual(len(response.data[0]["ordered_items"]), 1)
        self.assertEqual(response.data[0]["ordered_items"][0]["product_info"], self.offer.pk)
        self.assertEqual(self.client.get("/api/v1/basket").status_code, 403)

    def test_ownership_and_admin_permissions(self):
        order = self.basket()
        line = order.ordered_items.get()
        self.client.force_authenticate(self.other)
        self.assertEqual(
            self.client.post(
                "/api/v1/order", {"id": order.pk, "contact": self.contact.pk}
            ).status_code,
            404,
        )
        response = self.client.put("/api/v1/user/contact", {"id": self.contact.pk, "city": "Омск"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.client.delete("/api/v1/basket", {"items": str(line.pk)}).status_code, 400
        )
        self.assertEqual(self.client.get("/api/v1/partner/orders").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/admin/orders").status_code, 403)
        self.assertEqual(
            self.client.patch(
                f"/api/v1/admin/orders/{order.pk}/status", {"state": "confirmed"}
            ).status_code,
            403,
        )
        self.client.force_authenticate(self.buyer)
        other_contact = Contact.objects.create(
            user=self.other, city="Омск", street="Мира", house="3"
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/order", {"id": order.pk, "contact": other_contact.pk}
            ).status_code,
            404,
        )

    def test_address_limit_and_single_phone(self):
        for number in range(4):
            response = self.client.post(
                "/api/v1/user/contact", {"city": "Москва", "street": "Мира", "house": str(number)}
            )
            self.assertEqual(response.status_code, 201)
        self.assertEqual(
            self.client.post(
                "/api/v1/user/contact", {"city": "Москва", "street": "Мира", "house": "6"}
            ).status_code,
            400,
        )
        response = self.client.put(
            "/api/v1/user/contact", {"id": self.contact.pk, "phone": "+78005553535"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["phone"] for item in self.client.get("/api/v1/user/contact").data},
            {"+78005553535"},
        )
        response = self.client.delete("/api/v1/user/contact", {"items": str(self.contact.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.buyer.contacts.count(), 4)

    def test_cancellation_returns_stock_only_once(self):
        order = self.place_order()
        self.client.force_authenticate(self.staff)
        url = f"/api/v1/admin/orders/{order.pk}/status"
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.patch(url, {"state": "canceled"}).status_code, 200)
            self.assertEqual(self.client.post(url, {"state": "canceled"}).status_code, 200)
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.quantity, 10)
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(self.client.patch(url, {"state": "new"}).status_code, 400)

    def test_statuses_follow_delivery_sequence(self):
        order = self.place_order()
        self.client.force_authenticate(self.staff)
        url = f"/api/v1/admin/orders/{order.pk}/status"
        self.assertEqual(self.client.patch(url, {"state": "delivered"}).status_code, 400)
        with self.captureOnCommitCallbacks(execute=True):
            for state in ("confirmed", "assembled", "sent", "delivered"):
                self.assertEqual(self.client.patch(url, {"state": state}).status_code, 200)
        self.assertEqual(self.client.patch(url, {"state": "canceled"}).status_code, 400)
        self.assertEqual(len(mail.outbox), 6)

    def test_repeated_checkout_does_not_take_stock_twice(self):
        order = self.place_order()
        self.assertEqual(
            self.client.post(
                "/api/v1/order", {"id": order.pk, "contact": self.contact.pk}
            ).status_code,
            404,
        )
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.quantity, 8)

    def test_admin_created_user_can_log_in_with_normalized_email(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("admin:backend_user_add"),
            {
                "email": "MixedCase@example.com",
                "password1": "CartPassword81!",
                "password2": "CartPassword81!",
                "type": "buyer",
                "is_active": "on",
                "_save": "Сохранить",
            },
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="mixedcase@example.com")
        self.assertTrue(user.is_active)
        self.client.logout()
        self.client.force_authenticate(None)
        response = self.client.post(
            "/api/v1/user/login",
            {"email": "MixedCase@example.com", "password": "CartPassword81!"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["Status"])
        self.assertIn("Token", response.data)

    def test_admin_edit_rejects_email_duplicate_with_different_case(self):
        data = model_to_dict(self.buyer)
        data["email"] = self.other.email.upper()
        form = ChangeUserForm(data=data, instance=self.buyer)
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["email"], ["Пользователь с таким email уже существует."])
        self.buyer.refresh_from_db()
        data["email"] = self.buyer.email.upper()
        form = ChangeUserForm(data=data, instance=self.buyer)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["email"], self.buyer.email)

    def test_admin_import_queues_job_and_protects_rows(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("admin:backend_shop_import"))
        self.assertEqual(response.status_code, 200)
        with patch("backend.catalog_tasks.do_import.delay") as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("admin:backend_shop_import"),
                    {
                        "supplier": self.supplier.pk,
                        "file": SimpleUploadedFile(
                            "price.yaml", b"shop: Test\ncategories: []\ngoods: []\n"
                        ),
                    },
                )
            self.assertEqual(response.status_code, 302)
            job = CatalogJob.objects.get()
            task.assert_called_once_with(job.pk, content="shop: Test\ncategories: []\ngoods: []\n")
        self.assertEqual(job.user_id, self.supplier.pk)
        request = response.wsgi_request
        self.assertFalse(admin.site._registry[Order].has_add_permission(request))
        self.assertFalse(admin.site._registry[OrderItem].has_delete_permission(request))
        for name in (
            "backend_shop_changelist",
            "backend_productinfo_changelist",
            "backend_user_changelist",
        ):
            self.assertEqual(self.client.get(reverse(f"admin:{name}")).status_code, 200)

    def test_admin_import_marks_job_failed_when_broker_is_unavailable(self):
        self.client.force_login(self.staff)
        with patch("backend.catalog_tasks.do_import.delay", side_effect=OSError("broker down")):
            with self.assertLogs("backend.admin", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(
                        reverse("admin:backend_shop_import"),
                        {
                            "supplier": self.supplier.pk,
                            "file": SimpleUploadedFile("price.yaml", b"shop: Test\n"),
                        },
                    )
        self.assertEqual(response.status_code, 302)
        job = CatalogJob.objects.get()
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error, "Не удалось запустить задачу. Проверьте очередь Celery.")
        notifications = list(messages.get_messages(response.wsgi_request))
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].level, messages.ERROR)
        self.assertEqual(str(notifications[0]), job.error)

    def test_admin_order_form_uses_status_service(self):
        order = self.place_order()
        self.client.force_login(self.staff)
        url = reverse("admin:backend_order_change", args=[order.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        line = order.ordered_items.get()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                url,
                {
                    "state": "confirmed",
                    "_save": "Сохранить",
                    "ordered_items-TOTAL_FORMS": "1",
                    "ordered_items-INITIAL_FORMS": "1",
                    "ordered_items-MIN_NUM_FORMS": "0",
                    "ordered_items-MAX_NUM_FORMS": "0",
                    "ordered_items-0-id": line.pk,
                    "ordered_items-0-order": order.pk,
                },
            )
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.state, "confirmed")
        self.assertEqual(len(mail.outbox), 3)


@skipUnless(connection.vendor == "postgresql", "Блокировки строк проверяются на PostgreSQL.")
class ConcurrentCheckoutTests(TransactionTestCase):
    def test_two_buyers_cannot_purchase_the_same_last_items(self):
        supplier = User.objects.create_user("shop@example.com", type="shop", is_active=True)
        offer = make_offer(supplier, quantity=5)
        inputs = []
        for index in range(2):
            user = User.objects.create_user(
                f"buyer{index}@example.com", is_active=True, phone="+79991234567"
            )
            contact = Contact.objects.create(user=user, city="Омск", street="Мира", house="1")
            order = Order.objects.create(user=user)
            OrderItem.objects.create(order=order, product_info=offer, quantity=4, price=offer.price)
            inputs.append((user.pk, order.pk, contact.pk))
        barrier = Barrier(2)

        def purchase(data):
            close_old_connections()
            user_id, order_id, contact_id = data
            try:
                user = User.objects.get(pk=user_id)
                barrier.wait(timeout=10)
                checkout(user, order_id, contact_id)
                return True
            except ValidationError:
                return False
            finally:
                close_old_connections()

        with patch("backend.tasks.send_email.delay"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(purchase, inputs))
        self.assertEqual(sorted(results), [False, True])
        offer.refresh_from_db()
        self.assertEqual(offer.quantity, 1)
        self.assertEqual(Order.objects.filter(state="new").count(), 1)
