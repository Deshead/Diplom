import secrets
from decimal import Decimal

from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q

STATE_CHOICES = [
    ("basket", "Корзина"), ("new", "Новый"), ("confirmed", "Подтвержден"),
    ("assembled", "Собран"), ("sent", "Отправлен"), ("delivered", "Доставлен"),
    ("canceled", "Отменен"),
]


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Укажите email")
        user = self.model(email=self.normalize_email(email).lower(), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        if not extra_fields["is_staff"] or not extra_fields["is_superuser"]:
            raise ValueError("Администратору нужны is_staff и is_superuser")
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None
    email = models.EmailField("Email", unique=True)
    company = models.CharField("Компания", max_length=100, blank=True)
    position = models.CharField("Должность", max_length=100, blank=True)
    phone = models.CharField("Телефон", max_length=20, blank=True)
    type = models.CharField(max_length=5, choices=[("buyer", "Покупатель"), ("shop", "Поставщик")],
                            default="buyer")
    is_active = models.BooleanField(default=False)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    def __str__(self):
        return self.email


class Shop(models.Model):
    name = models.CharField("Название", max_length=100)
    url = models.URLField("Адрес прайса", blank=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="shop")
    state = models.BooleanField("Принимает заказы", default=True)

    def __str__(self):
        return self.name


class Category(models.Model):
    name = models.CharField("Название", max_length=100)
    shops = models.ManyToManyField(Shop, related_name="categories", blank=True)

    def __str__(self):
        return self.name


class Product(models.Model):
    name = models.CharField("Название", max_length=255)
    description = models.TextField("Описание", blank=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["category", "name"], name="unique_product")]

    def __str__(self):
        return self.name


class ProductInfo(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="product_infos")
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="product_infos")
    external_id = models.PositiveBigIntegerField()
    model = models.CharField("Модель", max_length=100, blank=True)
    quantity = models.PositiveIntegerField("Остаток")
    price = models.DecimalField("Цена", max_digits=12, decimal_places=2)
    price_rrc = models.DecimalField("Рекомендованная цена", max_digits=12, decimal_places=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["shop", "external_id"], name="unique_shop_offer"),
            models.CheckConstraint(condition=Q(price__gte=0) & Q(price_rrc__gte=0),
                                   name="nonnegative_prices"),
        ]

    def __str__(self):
        return f"{self.product} ({self.shop})"


class Parameter(models.Model):
    name = models.CharField("Характеристика", max_length=100, unique=True)

    def __str__(self):
        return self.name


class ProductParameter(models.Model):
    product_info = models.ForeignKey(ProductInfo, on_delete=models.CASCADE,
                                     related_name="product_parameters")
    parameter = models.ForeignKey(Parameter, on_delete=models.PROTECT)
    value = models.CharField(max_length=255)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["product_info", "parameter"],
                                               name="unique_product_parameter")]


class Contact(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="contacts")
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    middle_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    city = models.CharField(max_length=100)
    street = models.CharField(max_length=150)
    house = models.CharField(max_length=20)
    structure = models.CharField(max_length=20, blank=True)
    building = models.CharField(max_length=20, blank=True)
    apartment = models.CharField(max_length=20, blank=True)

    @property
    def phone(self):
        return self.user.phone

    def __str__(self):
        return f"{self.city}, {self.street}, {self.house}"


class Order(models.Model):
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="orders")
    dt = models.DateTimeField(auto_now_add=True)
    state = models.CharField(max_length=15, choices=STATE_CHOICES, default="basket")
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL)
    # Снимок адреса сохраняет историю после редактирования адресной книги.
    contact_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-dt"]
        constraints = [models.UniqueConstraint(fields=["user"], condition=Q(state="basket"),
                                               name="one_basket_per_user")]

    @property
    def total_sum(self):
        return sum((item.total_sum for item in self.ordered_items.all()), Decimal("0.00"))

    def __str__(self):
        return f"Заказ №{self.pk} — {self.get_state_display()}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="ordered_items")
    product_info = models.ForeignKey(ProductInfo, on_delete=models.PROTECT,
                                     related_name="ordered_items")
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    product_name = models.CharField(max_length=255, blank=True)
    shop_name = models.CharField(max_length=100, blank=True)

    @property
    def total_sum(self):
        return self.price * self.quantity

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "product_info"], name="unique_order_item"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="positive_order_quantity"),
        ]


def generate_token():
    return secrets.token_hex(32)


class EmailToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="email_tokens")
    key = models.CharField(max_length=64, unique=True, default=generate_token)
    purpose = models.CharField(max_length=10, choices=[("register", "Регистрация"),
                                                     ("reset", "Сброс пароля")])
    created_at = models.DateTimeField(auto_now_add=True)


class CatalogJob(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="catalog_jobs")
    kind = models.CharField(max_length=10, choices=[("import", "Импорт"), ("export", "Экспорт")])
    status = models.CharField(max_length=10, default="pending",
                             choices=[("pending", "Ожидает"), ("done", "Готово"), ("error", "Ошибка")])
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

