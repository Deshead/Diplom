from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

# Импорт нужен, чтобы Celery worker нашел задачи каталога.
from backend.catalog_tasks import do_export, do_import  # noqa: F401


@shared_task(autoretry_for=(OSError,), retry_backoff=True, max_retries=3)
def send_email(subject, body, recipients):
    return send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients)
