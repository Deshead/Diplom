from celery import Celery
from celery.contrib.testing.worker import start_worker
from django.core import mail
from django.test import TransactionTestCase, override_settings

from backend.catalog_tasks import do_export, do_import
from backend.models import CatalogJob, ProductInfo, User
from backend.tasks import send_email


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    CELERY_TASK_ALWAYS_EAGER=False,
    CELERY_BROKER_URL="memory://",
    CELERY_RESULT_BACKEND="cache+memory://",
)
class WorkerTests(TransactionTestCase):
    def test_worker_executes_import_export_and_email_from_queue(self):
        """Настоящий worker читает задачи из очереди в памяти, без внешнего Redis."""
        supplier = User.objects.create_user("worker@example.com", type="shop", is_active=True)
        content = """
shop: Worker shop
categories:
  - id: 1
    name: Books
goods:
  - id: 10
    category: 1
    model: book
    name: Python book
    price: 1200
    price_rrc: 1500
    quantity: 3
    parameters:
      pages: 300
"""
        # Отдельное приложение не наследует Redis backend из предыдущих тестов.
        app = Celery(
            "queue-check", broker="memory://", backend="cache+memory://", set_as_current=False
        )
        self.addCleanup(app.close)
        with start_worker(app, pool="solo", perform_ping_check=False, shutdown_timeout=15):
            job = CatalogJob.objects.create(user=supplier, kind="import")
            app.send_task(do_import.name, args=[job.pk], kwargs={"content": content}).get(
                timeout=15
            )
            job.refresh_from_db()
            self.assertEqual(job.status, "done", job.error)
            self.assertEqual(ProductInfo.objects.get().quantity, 3)
            export = CatalogJob.objects.create(user=supplier, kind="export")
            app.send_task(do_export.name, args=[export.pk]).get(timeout=15)
            export.refresh_from_db()
            self.assertEqual(export.status, "done", export.error)
            result = app.send_task(
                send_email.name, args=["Worker test", "Order notification", ["buyer@example.com"]]
            )
            self.assertEqual(result.get(timeout=15), 1)
            self.assertEqual(mail.outbox[-1].subject, "Worker test")
