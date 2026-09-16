import json

from django.db import transaction
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import BasePermission, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Contact, Order, OrderItem, ProductInfo, User
from .order_serializers import (
    BasketAddSerializer,
    BasketUpdateSerializer,
    CheckoutSerializer,
    ContactSerializer,
    OrderSerializer,
)
from .order_services import change_order_status, check_offer, checkout


class IsBuyer(BasePermission):
    message = "Этот раздел доступен покупателям."

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.type == "buyer"


class IsSupplier(BasePermission):
    message = "Этот раздел доступен поставщикам."

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.type == "shop"


def read_items(data, serializer_class):
    items = data.get("items")
    if isinstance(items, str):
        # В form-data из Postman список приходит обычной строкой, её ещё надо разобрать.
        try:
            items = json.loads(items)
        except (ValueError, TypeError):
            raise ValidationError({"items": "Укажите корректный JSON-список."})
    if not isinstance(items, list) or not items or len(items) > 100:
        raise ValidationError({"items": "Ожидается список от 1 до 100 позиций."})
    serializer = serializer_class(data=items, many=True)
    serializer.is_valid(raise_exception=True)
    field = "product_info" if serializer_class is BasketAddSerializer else "id"
    identifiers = [item[field] for item in serializer.validated_data]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError({"items": "Позиции не должны повторяться."})
    return serializer.validated_data


def read_ids(data):
    values = data.get("items")
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, list) or not values or len(values) > 100:
        raise ValidationError({"items": "Укажите список ID или ID через запятую."})
    field = serializers.IntegerField(min_value=1)
    return list({field.run_validation(value) for value in values})


class BasketView(APIView):
    permission_classes = [IsAuthenticated, IsBuyer]

    def get(self, request):
        orders = Order.objects.filter(user=request.user, state="basket").prefetch_related(
            "ordered_items"
        )
        return Response(OrderSerializer(orders, many=True).data)

    @transaction.atomic
    def post(self, request):
        items = read_items(request.data, BasketAddSerializer)
        User.objects.select_for_update().get(pk=request.user.pk)
        order, _ = Order.objects.get_or_create(user=request.user, state="basket")
        offers = {
            offer.pk: offer
            for offer in ProductInfo.objects.select_for_update()
            .filter(pk__in=[item["product_info"] for item in items])
            .order_by("pk")
        }
        for data in items:
            offer = offers.get(data["product_info"])
            if offer is None:
                raise ValidationError({"product_info": "Товар не найден."})
            item = order.ordered_items.filter(product_info=offer).first()
            # Ещё одно добавление товара увеличивает количество в уже готовой строке.
            quantity = data["quantity"] + (item.quantity if item else 0)
            check_offer(offer, quantity)
            OrderItem.objects.update_or_create(
                order=order,
                product_info=offer,
                defaults={
                    "quantity": quantity,
                    "price": offer.price,
                    "product_name": offer.product.name,
                    "shop_name": offer.shop.name,
                },
            )
        return Response({"Status": True, "id": order.pk, "count": len(items)})

    @transaction.atomic
    def put(self, request):
        items = read_items(request.data, BasketUpdateSerializer)
        User.objects.select_for_update().get(pk=request.user.pk)
        order = get_object_or_404(Order, user=request.user, state="basket")
        lines = {
            item.pk: item
            for item in order.ordered_items.filter(pk__in=[row["id"] for row in items])
        }
        if len(lines) != len(items):
            raise ValidationError({"items": "Позиция не найдена в вашей корзине."})
        offers = {
            offer.pk: offer
            for offer in ProductInfo.objects.select_for_update()
            .filter(pk__in=[item.product_info_id for item in lines.values()])
            .order_by("pk")
        }
        for data in items:
            item = lines[data["id"]]
            offer = offers[item.product_info_id]
            check_offer(offer, data["quantity"])
            item.quantity = data["quantity"]
            item.price = offer.price
            item.product_name = offer.product.name
            item.shop_name = offer.shop.name
            item.save(update_fields=["quantity", "price", "product_name", "shop_name"])
        return Response({"Status": True, "count": len(items)})

    @transaction.atomic
    def delete(self, request):
        ids = read_ids(request.data)
        User.objects.select_for_update().get(pk=request.user.pk)
        items = OrderItem.objects.filter(
            order__user=request.user, order__state="basket", pk__in=ids
        )
        if items.count() != len(ids):
            raise ValidationError({"items": "Позиция не найдена в вашей корзине."})
        count, _ = items.delete()
        return Response({"Status": True, "count": count})


class ContactView(APIView):
    permission_classes = [IsAuthenticated, IsBuyer]

    def get(self, request):
        contacts = Contact.objects.filter(user=request.user).select_related("user").order_by("pk")
        return Response(ContactSerializer(contacts, many=True).data)

    @transaction.atomic
    def post(self, request):
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if user.contacts.count() >= 5:
            raise ValidationError("Можно сохранить не более пяти адресов доставки.")
        serializer = ContactSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        contact = serializer.save(
            user=user,
            first_name=serializer.validated_data.get("first_name", user.first_name),
            last_name=serializer.validated_data.get("last_name", user.last_name),
            email=serializer.validated_data.get("email", user.email),
        )
        return Response({"Status": True, "id": contact.pk}, status=201)

    @transaction.atomic
    def put(self, request):
        User.objects.select_for_update().get(pk=request.user.pk)
        contact_id = serializers.IntegerField(min_value=1).run_validation(request.data.get("id"))
        contact = get_object_or_404(
            Contact.objects.select_related("user"), pk=contact_id, user=request.user
        )
        serializer = ContactSerializer(
            contact, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"Status": True})

    @transaction.atomic
    def delete(self, request):
        ids = read_ids(request.data)
        User.objects.select_for_update().get(pk=request.user.pk)
        contacts = Contact.objects.filter(user=request.user, pk__in=ids)
        if contacts.count() != len(ids):
            raise ValidationError({"items": "Адрес не найден в вашей адресной книге."})
        count, _ = contacts.delete()
        return Response({"Status": True, "count": count})


class OrderView(APIView):
    permission_classes = [IsAuthenticated, IsBuyer]

    def get(self, request):
        orders = (
            Order.objects.filter(user=request.user)
            .exclude(state="basket")
            .prefetch_related("ordered_items")
        )
        return Response(OrderSerializer(orders, many=True).data)

    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = checkout(
            request.user, serializer.validated_data["id"], serializer.validated_data["contact"]
        )
        return Response({"Status": True, "id": order.pk})


class OrderDetailView(APIView):
    permission_classes = [IsAuthenticated, IsBuyer]

    def get(self, request, pk):
        order = get_object_or_404(
            Order.objects.filter(user=request.user)
            .exclude(state="basket")
            .prefetch_related("ordered_items"),
            pk=pk,
        )
        return Response(OrderSerializer(order).data)


class OrderStatusView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, pk):
        order = get_object_or_404(Order.objects.exclude(state="basket"), pk=pk)
        state = serializers.ChoiceField(
            choices=["new", "confirmed", "assembled", "sent", "delivered", "canceled"]
        )
        new_state = state.run_validation(request.data.get("state"))
        order = change_order_status(order, new_state)
        return Response({"Status": True, "id": order.pk, "state": order.state})

    post = patch


class PartnerOrders(APIView):
    permission_classes = [IsAuthenticated, IsSupplier]

    def get(self, request):
        # Фильтруем сами строки, чтобы не раскрыть товары и сумму других поставщиков.
        items = OrderItem.objects.filter(product_info__shop__user=request.user)
        orders = (
            Order.objects.filter(ordered_items__product_info__shop__user=request.user)
            .exclude(state="basket")
            .distinct()
            .prefetch_related(Prefetch("ordered_items", queryset=items))
        )
        return Response(OrderSerializer(orders, many=True).data)


class AdminOrderView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        orders = Order.objects.exclude(state="basket").prefetch_related("ordered_items")
        if request.query_params.get("state"):
            orders = orders.filter(state=request.query_params["state"])
        return Response(OrderSerializer(orders, many=True).data)
