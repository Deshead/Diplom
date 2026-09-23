import logging

from celery import shared_task

from .catalog import CatalogError, download_catalog, export_catalog, import_catalog
from .models import CatalogJob

logger = logging.getLogger(__name__)


def _finish_error(job, error):
    job.status = "error"
    job.error = str(error)
    job.result = {}
    job.save(update_fields=["status", "error", "result"])


@shared_task
def do_import(job_id, content=None, url=None, filename=""):
    job = CatalogJob.objects.select_related("user").get(pk=job_id, kind="import")
    try:
        if url:
            content = download_catalog(url)
        job.result = import_catalog(content, job.user, url=url or "", filename=filename)
    except CatalogError as exc:
        _finish_error(job, exc)
        return
    except Exception:
        logger.exception("Ошибка импорта прайса, задание %s", job.pk)
        _finish_error(job, "Не удалось обработать прайс. Попробуйте позже.")
        return
    job.status = "done"
    job.error = ""
    job.save(update_fields=["status", "result", "error"])
    return job.result


@shared_task
def do_export(job_id):
    job = CatalogJob.objects.select_related("user").get(pk=job_id, kind="export")
    try:
        job.result = {"yaml": export_catalog(job.user)}
    except CatalogError as exc:
        _finish_error(job, exc)
        return
    except Exception:
        logger.exception("Ошибка экспорта прайса, задание %s", job.pk)
        _finish_error(job, "Не удалось выгрузить прайс. Попробуйте позже.")
        return
    job.status = "done"
    job.error = ""
    job.save(update_fields=["status", "result", "error"])
    return job.result
