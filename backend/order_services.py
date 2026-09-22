from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError

from .models import Contact, Order, ProductInfo, User
from .order_serializers import ContactSerializer

TRANSITIONS = {
    "new": {"confirmed", "canceled"},
    "confirmed": {"assembled", "canceled"},
    "assembled": {"sent", "canceled"},
    "sent": {"delivered"},
    "delivered": set(),
    "canceled": set(),
}


def check_offer(offer, quantity):
    if not offer.is_active or not offer.shop.state or not offer.shop.user.is_active:
        raise ValidationError(f"Товар {offer.pk} недоступен для заказа.")
    if quantity > offer.quantity:
        raise ValidationError(f"Недостаточно товара {offer.pk}: в наличии {offer.quantity}.")


def queue_email(subject, body, recipients):
    from .tasks import send_email

    def send_after_commit():
        send_email.delay(subject, body, recipients)

    # Письмо отправляется только после успешного сохранения заказа.
    # TODO: сохранять письма на повторную отправку, если Redis был недоступен.
    transaction.on_commit(send_after_commit, robust=True)


def invoice_text(order):
    lines = [f"Заказ №{order.pk}", f"Покупатель: {order.user.email}", "", "Состав заказа:"]
    for item in order.ordered_items.all():
        lines.append(
            f"{item.product_name} ({item.shop_name}): {item.quantity} шт. × "
            f"{item.price:.2f} руб. = {item.total_sum:.2f} руб."
        )
    lines.extend(["", f"Итого: {order.total_sum:.2f} руб.", "", "Доставка:"])
    labels = {
        "first_name": "Имя",
        "last_name": "Фамилия",
        "middle_name": "Отчество",
        "email": "Email",
        "phone": "Телефон",
        "city": "Город",
        "street": "Улица",
        "house": "Дом",
        "structure": "Корпус",
        "building": "Строение",
        "apartment": "Квартира",
    }
    for field, label in labels.items():
        if order.contact_snapshot.get(field):
            lines.append(f"{label}: {order.contact_snapshot[field]}")
    return "\n".join(lines)


@transaction.atomic
def checkout(user, basket_id, contact_id):
    # Один порядок блокировок используется и в корзине, и при смене статуса.
    User.objects.select_for_update().get(pk=user.pk)
    order = get_object_or_404(
        Order.objects.select_for_update(), pk=basket_id, user=user, state="basket"
    )
    contact = get_object_or_404(Contact.objects.select_related("user"), pk=contact_id, user=user)
    items = list(order.ordered_items.order_by("product_info_id"))
    if not items:
        raise ValidationError("Корзина пуста.")
    if not contact.phone:
        raise ValidationError("Укажите телефон в контакте доставки.")
    offer_ids = [item.product_info_id for item in items]
    offers_query = ProductInfo.objects.select_for_update().filter(pk__in=offer_ids)
    offers = {}
    for offer in offers_query.order_by("pk"):
        offers[offer.pk] = offer
    # При нехватке товара транзакция отменит все списания этого заказа.
    for item in items:
        offer = offers[item.product_info_id]
        check_offer(offer, item.quantity)
        offer.quantity -= item.quantity
        offer.save(update_fields=["quantity"])
        # Сохраняем цену и название на момент заказа.
        item.price = offer.price
        item.product_name = offer.product.name
        item.shop_name = offer.shop.name
        item.save(update_fields=["price", "product_name", "shop_name"])
    order.contact = contact
    order.contact_snapshot = dict(ContactSerializer(contact).data)
    order.state = "new"
    order.save(update_fields=["contact", "contact_snapshot", "state"])
    body = invoice_text(order)
    queue_email(f"Заказ №{order.pk} принят", body, [user.email])
    queue_email(f"Накладная для заказа №{order.pk}", body, [settings.ADMIN_EMAIL])
    return order


@transaction.atomic
def change_order_status(order, new_state):
    User.objects.select_for_update().get(pk=order.user_id)
    order = Order.objects.select_for_update().get(pk=order.pk)
    if new_state == order.state and order.state != "basket":
        # Повторная отмена не должна повторно вернуть товар на склад.
        return order
    allowed_states = TRANSITIONS.get(order.state, set())
    if new_state not in allowed_states:
        raise ValidationError({"state": f"Нельзя изменить статус {order.state} на {new_state}."})
    if new_state == "canceled":
        items = list(order.ordered_items.order_by("product_info_id"))
        offer_ids = [item.product_info_id for item in items]
        offers_query = ProductInfo.objects.select_for_update().filter(pk__in=offer_ids)
        offers = {}
        for offer in offers_query.order_by("pk"):
            offers[offer.pk] = offer
        for item in items:
            offer = offers[item.product_info_id]
            offer.quantity += item.quantity
            offer.save(update_fields=["quantity"])
    order.state = new_state
    order.save(update_fields=["state"])
    queue_email(
        f"Статус заказа №{order.pk} изменён",
        f"Заказ №{order.pk}: {order.get_state_display()}.",
        [order.user.email],
    )
    return order
