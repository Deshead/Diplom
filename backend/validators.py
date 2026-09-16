import re

from rest_framework.exceptions import ValidationError


def validate_phone(value):
    # Номер можно записать с пробелами, скобками и дефисами.
    digits = re.sub(r"\D", "", value)
    allowed_characters = re.fullmatch(r"[+\d() -]+", value)
    if not allowed_characters or len(digits) < 7 or len(digits) > 15:
        raise ValidationError("Укажите телефон из 7–15 цифр.")
    return value
