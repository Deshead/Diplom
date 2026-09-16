import re

from rest_framework.exceptions import ValidationError


def validate_phone(value):
    # Скобки и пробелы оставляем: человеку так удобнее читать номер.
    digits = re.sub(r"\D", "", value)
    if not re.fullmatch(r"[+\d() -]+", value) or not 7 <= len(digits) <= 15:
        raise ValidationError("Укажите телефон из 7–15 цифр.")
    return value
