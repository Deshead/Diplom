from datetime import timedelta

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from backend.models import EmailToken, User
from backend.tasks import send_email
from backend.validators import validate_phone


def check_password(password, user):
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({"password": exc.messages}) from exc


class RegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = ["email", "password", "first_name", "last_name", "company", "position", "type"]
        extra_kwargs = {"first_name": {"required": True}, "last_name": {"required": True}}

    def validate_email(self, value):
        # Регистр букв в email не учитываем.
        value = value.lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("Этот email уже зарегистрирован")
        return value

    def validate(self, attrs):
        user = User(
            email=attrs["email"],
            first_name=attrs["first_name"],
            last_name=attrs["last_name"],
        )
        check_password(attrs["password"], user)
        return attrs


class AccountSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "company",
            "position",
            "phone",
            "type",
            "password",
        ]
        read_only_fields = ["id", "email", "type"]

    def validate_password(self, value):
        check_password(value, self.instance)
        return value

    def validate_phone(self, value):
        if value:
            validate_phone(value)
        return value

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        instance.first_name = validated_data.get("first_name", instance.first_name)
        instance.last_name = validated_data.get("last_name", instance.last_name)
        instance.company = validated_data.get("company", instance.company)
        instance.position = validated_data.get("position", instance.position)
        instance.phone = validated_data.get("phone", instance.phone)
        if password is not None:
            instance.set_password(password)
            # После смены пароля нужно войти заново.
            Token.objects.filter(user=instance).delete()
        instance.save()
        return instance


class PublicAuthView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


def send_confirmation(user):
    EmailToken.objects.filter(user=user, purpose="register").delete()
    token = EmailToken.objects.create(user=user, purpose="register")
    body = (
        f"Подтверждение регистрации для {user.email}\nТокен: {token.key}\n"
        "Отправьте email и token на POST /api/v1/user/register/confirm.\n"
        "Токен действует 24 часа."
    )

    def send_after_save():
        send_email.delay("Подтверждение регистрации", body, [user.email])

    # Если сохранение не удалось, письмо не отправляем.
    transaction.on_commit(send_after_save, robust=True)


def send_password_reset(user):
    # При повторном запросе старый код больше не работает.
    EmailToken.objects.filter(user=user, purpose="reset").delete()
    token = EmailToken.objects.create(user=user, purpose="reset")
    body = (
        f"Сброс пароля для {user.email}\nТокен: {token.key}\n"
        "Отправьте token и password на POST /api/v1/user/password_reset/confirm.\n"
        "Токен действует один час."
    )

    def send_after_save():
        send_email.delay("Сброс пароля", body, [user.email])

    transaction.on_commit(send_after_save, robust=True)


class RegisterAccount(PublicAuthView):
    def post(self, request):
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            user = User.objects.create_user(**serializer.validated_data)
            send_confirmation(user)
        return Response(
            {"Status": True, "message": "Проверьте email для подтверждения"}, status=201
        )


class ConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    token = serializers.CharField(max_length=64)


class ConfirmAccount(PublicAuthView):
    def post(self, request):
        serializer = ConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            valid_from = timezone.now() - timedelta(hours=24)
            tokens = EmailToken.objects.select_for_update().filter(
                key=data["token"],
                purpose="register",
                user__email__iexact=data["email"],
                created_at__gte=valid_from,
            )
            token = tokens.first()
            if not token:
                raise serializers.ValidationError({"token": "Токен неверен или просрочен"})
            User.objects.filter(pk=token.user_id).update(is_active=True)
            # Код можно использовать только один раз.
            token.delete()
        return Response({"Status": True})


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False)


class LoginAccount(PublicAuthView):
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = authenticate(
            request,
            email=data["email"].lower(),
            password=data["password"],
        )
        if user is None:
            return Response(
                {"Status": False, "Error": "Неверные данные или email не подтвержден"}, status=401
            )
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"Status": True, "Token": token.key})


class AccountDetails(APIView):
    def get(self, request):
        return Response(AccountSerializer(request.user).data)

    def post(self, request):
        serializer = AccountSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"Status": True, "user": serializer.data})

    def patch(self, request):
        return self.post(request)


class LogoutAccount(APIView):
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response({"Status": True})


class ResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ResendConfirmation(PublicAuthView):
    def post(self, request):
        serializer = ResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        with transaction.atomic():
            users = User.objects.select_for_update().filter(email__iexact=email, is_active=False)
            user = users.first()
            if user:
                # Письмо повторяем только для неподтвержденной регистрации.
                registration_pending = user.email_tokens.filter(purpose="register").exists()
                if registration_pending:
                    send_confirmation(user)
        return Response({"Status": True, "message": "Запрос принят. Проверьте email"})


class PasswordReset(PublicAuthView):
    def post(self, request):
        serializer = ResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        with transaction.atomic():
            users = User.objects.select_for_update().filter(email__iexact=email, is_active=True)
            user = users.first()
            if user:
                send_password_reset(user)
        # Не сообщаем, зарегистрирован ли этот email.
        return Response({"Status": True, "message": "Если аккаунт существует, письмо отправлено"})


class ResetConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False)
    token = serializers.CharField(max_length=64)
    password = serializers.CharField(trim_whitespace=False)


class PasswordResetConfirm(PublicAuthView):
    def post(self, request):
        serializer = ResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            valid_from = timezone.now() - timedelta(hours=1)
            tokens = EmailToken.objects.select_for_update().filter(
                key=data["token"],
                purpose="reset",
                user__is_active=True,
                created_at__gte=valid_from,
            )
            token = tokens.first()
            if not token:
                raise serializers.ValidationError({"token": "Токен неверен или просрочен"})
            user = token.user
            email = data.get("email")
            if email and email.lower() != user.email:
                raise serializers.ValidationError({"token": "Токен неверен или просрочен"})
            check_password(data["password"], user)
            user.set_password(data["password"])
            user.save(update_fields=["password"])
            EmailToken.objects.filter(user=user, purpose="reset").delete()
            Token.objects.filter(user=user).delete()
        return Response({"Status": True})
