from rest_framework import serializers

from .models import Contact, Order, OrderItem
from .validators import validate_phone


class ContactSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(max_length=20, required=False)

    class Meta:
        model = Contact
        fields = (
            "id",
            "first_name",
            "last_name",
            "middle_name",
            "email",
            "phone",
            "city",
            "street",
            "house",
            "structure",
            "building",
            "apartment",
        )
        read_only_fields = ("id",)

    def validate_phone(self, value):
        return validate_phone(value)

    def validate(self, attrs):
        user = self.context["request"].user
        if not attrs.get("phone", user.phone):
            raise serializers.ValidationError({"phone": "Укажите телефон."})
        return attrs

    def create(self, validated_data):
        # Адресов может быть несколько, а телефон у покупателя всё равно один.
        phone = validated_data.pop("phone", None)
        user = validated_data["user"]
        if phone is not None:
            user.phone = phone
            user.save(update_fields=["phone"])
        return super().create(validated_data)

    def update(self, instance, validated_data):
        phone = validated_data.pop("phone", None)
        if phone is not None:
            instance.user.phone = phone
            instance.user.save(update_fields=["phone"])
        return super().update(instance, validated_data)


class BasketAddSerializer(serializers.Serializer):
    product_info = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1, max_value=1000000)


class BasketUpdateSerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1, max_value=1000000)


class CheckoutSerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1)
    contact = serializers.IntegerField(min_value=1)


class OrderItemSerializer(serializers.ModelSerializer):
    total_sum = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)

    class Meta:
        model = OrderItem
        fields = (
            "id",
            "product_info",
            "product_name",
            "shop_name",
            "quantity",
            "price",
            "total_sum",
        )


class OrderSerializer(serializers.ModelSerializer):
    ordered_items = OrderItemSerializer(many=True, read_only=True)
    total_sum = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)
    contact = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ("id", "dt", "state", "ordered_items", "total_sum", "contact")

    def get_contact(self, obj):
        # Старый заказ показываем с прежним адресом, даже если человек уже переехал.
        if obj.contact_snapshot:
            return obj.contact_snapshot
        return ContactSerializer(obj.contact).data if obj.contact_id else None
