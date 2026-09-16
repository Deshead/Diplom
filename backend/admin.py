import logging

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.db import transaction
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from rest_framework.exceptions import ValidationError

from .models import (
    CatalogJob,
    Category,
    Contact,
    Order,
    OrderItem,
    Parameter,
    Product,
    ProductInfo,
    ProductParameter,
    Shop,
    User,
)
from .order_services import TRANSITIONS, change_order_status

logger = logging.getLogger(__name__)


class EmailFormMixin:
    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Пользователь с таким email уже существует.")
        return email


class CreateUserForm(EmailFormMixin, UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email", "type")


class ChangeUserForm(EmailFormMixin, UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = ChangeUserForm
    add_form = CreateUserForm
    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "type", "is_active", "is_staff")
    list_filter = ("type", "is_active", "is_staff")
    search_fields = ("email", "first_name", "last_name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Профиль",
            {"fields": ("first_name", "last_name", "phone", "company", "position", "type")},
        ),
        (
            "Доступ",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Даты", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "type", "is_active"),
            },
        ),
    )


class CatalogImportForm(forms.Form):
    supplier = forms.ModelChoiceField(label="Поставщик", queryset=User.objects.filter(type="shop"))
    file = forms.FileField(label="Прайс в формате YAML")

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > settings.CATALOG_MAX_BYTES:
            raise forms.ValidationError("Размер файла не должен превышать 2 МБ.")
        try:
            self.content = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise forms.ValidationError("Файл должен быть в кодировке UTF-8.")
        return upload


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "state", "url")
    list_filter = ("state",)
    search_fields = ("name", "user__email")
    change_list_template = "admin/backend/shop/change_list.html"

    def get_urls(self):
        urls = [
            path(
                "import/",
                self.admin_site.admin_view(self.import_catalog),
                name="backend_shop_import",
            )
        ]
        return urls + super().get_urls()

    def import_catalog(self, request):
        if not self.has_change_permission(request):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied
        form = CatalogImportForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            from .catalog_tasks import do_import

            def queue_import():
                try:
                    do_import.delay(job.pk, content=form.content)
                except Exception:
                    logger.exception("Не удалось запустить импорт, задание %s", job.pk)
                    job.status = "error"
                    job.error = "Не удалось запустить задачу. Проверьте очередь Celery."
                    job.save(update_fields=["status", "error"])
                else:
                    job.refresh_from_db(fields=["status", "error"])
                if job.status == "error":
                    self.message_user(request, job.error, messages.ERROR)
                else:
                    self.message_user(
                        request,
                        f"Создано задание импорта №{job.pk}. "
                        "Результат доступен в заданиях каталога.",
                        messages.SUCCESS,
                    )

            # Сначала сохраняем задание: иначе быстрый worker может его ещё не найти.
            with transaction.atomic():
                job = CatalogJob.objects.create(user=form.cleaned_data["supplier"], kind="import")
                transaction.on_commit(queue_import, robust=True)
            return HttpResponseRedirect(reverse("admin:backend_catalogjob_change", args=[job.pk]))
        context = {
            **self.admin_site.each_context(request),
            "title": "Импорт прайса поставщика",
            "opts": self.model._meta,
            "form": form,
        }
        return TemplateResponse(request, "admin/backend/shop/import.html", context)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "user":
            kwargs["queryset"] = User.objects.filter(type="shop")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class ReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    fields = ("product_info", "product_name", "shop_name", "quantity", "price", "total_sum")
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class OrderAdminForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ("state",)

    def clean_state(self):
        state = self.cleaned_data["state"]
        previous = Order.objects.get(pk=self.instance.pk).state
        if state != previous and state not in TRANSITIONS.get(previous, set()):
            raise forms.ValidationError(f"Нельзя изменить статус {previous} на {state}.")
        return state


@admin.register(Order)
class OrderAdmin(ReadOnlyAdmin):
    form = OrderAdminForm
    list_display = ("id", "user", "dt", "state", "total_sum")
    list_filter = ("state", "dt")
    search_fields = ("user__email",)
    inlines = (OrderItemInline,)

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return tuple(field for field in fields if field != "state") + ("total_sum",)

    def save_model(self, request, obj, form, change):
        # Тут тоже нужны возврат остатков и письмо, как при смене статуса через API.
        change_order_status(obj, obj.state)

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as exc:
            # Между открытием формы и сохранением другой администратор мог сменить статус.
            self.message_user(request, str(exc.detail), messages.ERROR)
            return HttpResponseRedirect(request.path)


@admin.register(OrderItem)
class OrderItemAdmin(ReadOnlyAdmin):
    list_display = ("order", "product_name", "shop_name", "quantity", "price")
    list_filter = ("order__state",)


@admin.register(CatalogJob)
class CatalogJobAdmin(ReadOnlyAdmin):
    list_display = ("id", "user", "kind", "status", "created_at")
    list_filter = ("kind", "status")


@admin.register(Contact)
class ContactAdmin(ReadOnlyAdmin):
    list_display = ("user", "city", "street", "house")
    search_fields = ("user__email", "city")


@admin.register(ProductInfo)
class ProductInfoAdmin(ReadOnlyAdmin):
    list_display = ("product", "shop", "external_id", "quantity", "price", "is_active")
    list_filter = ("shop", "is_active")
    search_fields = ("product__name", "model")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category")
    list_filter = ("category",)
    search_fields = ("name",)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)

    def has_add_permission(self, request):
        # ID категорий берём из прайса. Автоматический ID может случайно занять чужой.
        return False


admin.site.register(Parameter)
admin.site.register(ProductParameter, ReadOnlyAdmin)
admin.site.site_header = "Управление закупками"
admin.site.site_title = "Закупки"
admin.site.index_title = "Каталог и заказы"
