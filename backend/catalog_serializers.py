from rest_framework import serializers

from .models import CatalogJob, Category, Product, ProductInfo, ProductParameter, Shop


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name")


class ShopSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shop
        fields = ("id", "name", "state")


class ProductSerializer(serializers.ModelSerializer):
    category = CategorySerializer(read_only=True)

    class Meta:
        model = Product
        fields = ("id", "name", "description", "category")


class ProductParameterSerializer(serializers.ModelSerializer):
    parameter = serializers.CharField(source="parameter.name", read_only=True)

    class Meta:
        model = ProductParameter
        fields = ("parameter", "value")


class ProductInfoSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)
    shop = ShopSerializer(read_only=True)
    product_parameters = ProductParameterSerializer(many=True, read_only=True)

    class Meta:
        model = ProductInfo
        fields = (
            "id",
            "model",
            "external_id",
            "product",
            "shop",
            "quantity",
            "price",
            "price_rrc",
            "product_parameters",
        )


class CatalogJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = CatalogJob
        fields = ("id", "kind", "status", "result", "error", "created_at")
