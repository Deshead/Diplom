from django.conf import settings
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from .catalog_serializers import (
    CatalogJobSerializer,
    CategorySerializer,
    ProductInfoSerializer,
    ShopSerializer,
)
from .catalog_tasks import do_export, do_import
from .models import CatalogJob, Category, ProductInfo, Shop


class IsSupplier(permissions.BasePermission):
    message = "Доступно только поставщику"

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.type == "shop"


class CategoryView(generics.ListAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = CategorySerializer
    queryset = Category.objects.filter(shops__state=True).distinct().order_by("id")


class ShopView(generics.ListAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = ShopSerializer
    queryset = Shop.objects.filter(state=True).order_by("id")


def available_offers():
    offers = ProductInfo.objects.filter(is_active=True, shop__state=True)
    offers = offers.select_related("shop", "product__category")
    offers = offers.prefetch_related("product_parameters__parameter")
    return offers.order_by("id")


def validate_filter_id(value, parameter):
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise serializers.ValidationError({parameter: "Нужно целое положительное число"})
    if number < 1 or number > 9223372036854775807:
        raise serializers.ValidationError({parameter: "Неверный идентификатор"})
    return number


class ProductInfoView(generics.ListAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = ProductInfoSerializer

    def get_queryset(self):
        queryset = available_offers()
        shop_id = self.request.query_params.get("shop_id")
        if shop_id is not None:
            shop_id = validate_filter_id(shop_id, "shop_id")
            queryset = queryset.filter(shop_id=shop_id)

        category_id = self.request.query_params.get("category_id")
        if category_id is not None:
            category_id = validate_filter_id(category_id, "category_id")
            queryset = queryset.filter(product__category_id=category_id)

        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(product__name__icontains=search)
                | Q(product__description__icontains=search)
                | Q(model__icontains=search)
            )
        return queryset


class ProductDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = ProductInfoSerializer

    def get_queryset(self):
        return available_offers()


def start_catalog_task(job, task, **kwargs):
    try:
        task.delay(job.pk, **kwargs)
    except Exception:
        job.status = "error"
        job.error = "Не удалось запустить задачу. Проверьте очередь Celery."
        job.save(update_fields=["status", "error"])
        return Response({"Status": False, "job_id": job.pk, "Error": job.error}, status=503)
    return Response({"Status": True, "job_id": job.pk}, status=202)


class PartnerUpdate(APIView):
    permission_classes = [IsSupplier]

    def post(self, request):
        uploaded = request.FILES.get("file")
        url = request.data.get("url")
        if uploaded and url:
            raise serializers.ValidationError("Укажите ровно один источник: file или url")
        if not uploaded and not url:
            raise serializers.ValidationError("Укажите ровно один источник: file или url")
        content = None
        if uploaded:
            if uploaded.size > settings.CATALOG_MAX_BYTES:
                raise serializers.ValidationError({"file": "Файл слишком большой"})
            try:
                content = uploaded.read(settings.CATALOG_MAX_BYTES + 1).decode("utf-8-sig")
            except UnicodeDecodeError:
                raise serializers.ValidationError({"file": "Нужна кодировка UTF-8"})
        else:
            url = serializers.URLField(max_length=200).run_validation(url)
        job = CatalogJob.objects.create(user=request.user, kind="import")
        return start_catalog_task(job, do_import, content=content, url=url)


class PartnerState(APIView):
    permission_classes = [IsSupplier]

    def get(self, request):
        shop = get_object_or_404(Shop, user=request.user)
        return Response(ShopSerializer(shop).data)

    def post(self, request):
        state = serializers.BooleanField().run_validation(request.data.get("state"))
        shop = get_object_or_404(Shop, user=request.user)
        shop.state = state
        shop.save(update_fields=["state"])
        return Response({"Status": True, "state": shop.state})


class PartnerExport(APIView):
    permission_classes = [IsSupplier]

    def get(self, request):
        get_object_or_404(Shop, user=request.user)
        job = CatalogJob.objects.create(user=request.user, kind="export")
        return start_catalog_task(job, do_export)


class CatalogJobView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = CatalogJobSerializer

    def get_queryset(self):
        return CatalogJob.objects.filter(user=self.request.user)
