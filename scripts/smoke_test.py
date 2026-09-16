"""Проверка полного сценария через настоящий HTTP-сервер и отдельную временную БД."""

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent


def smoke_environment(folder):
    # Личные настройки почты и БД не должны попасть в учебную проверку.
    ignored_prefixes = ("MP_", "CELERY_", "DJANGO_", "EMAIL_", "POSTGRES_")
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(ignored_prefixes)
    }
    env.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "DJANGO_SETTINGS_MODULE": "orders.settings",
            "DJANGO_DEBUG": "true",
            "SECRET_KEY": "isolated-smoke-test-secret-key",
            "ALLOWED_HOSTS": "127.0.0.1,localhost",
            "POSTGRES_HOST": "",
            "SQLITE_NAME": str(folder / "smoke.sqlite3"),
            "EMAIL_BACKEND": "django.core.mail.backends.filebased.EmailBackend",
            "EMAIL_FILE_PATH": str(folder / "emails"),
            "EMAIL_HOST": "127.0.0.1",
            "EMAIL_HOST_USER": "",
            "EMAIL_HOST_PASSWORD": "",
            "EMAIL_USE_TLS": "false",
            "EMAIL_USE_SSL": "false",
            "DEFAULT_FROM_EMAIL": "orders@example.com",
            "ADMIN_EMAIL": "admin@example.com",
            "CELERY_TASK_ALWAYS_EAGER": "true",
            "CELERY_BROKER_URL": "memory://",
            "CELERY_RESULT_BACKEND": "cache+memory://",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    return env


def run():
    with tempfile.TemporaryDirectory(prefix="diplom-smoke-") as directory:
        folder = Path(directory)
        env = smoke_environment(folder)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with (
            (folder / "server.log").open("w", encoding="utf-8") as log,
            requests.Session() as client,
        ):
            client.trust_env = False
            for command in (["migrate", "--noinput"], ["seed_demo"]):
                subprocess.run(
                    [sys.executable, "manage.py", *command],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=log,
                    check=True,
                    creationflags=flags,
                )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            server = subprocess.Popen(
                [sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=log,
                creationflags=flags,
            )
            try:
                for _ in range(100):
                    try:
                        if client.get(base + "/health/", timeout=1).status_code == 200:
                            break
                    except requests.RequestException:
                        pass
                    if server.poll() is not None:
                        raise RuntimeError("Сервер завершился при запуске")
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Сервер не запустился")
                scenario(base, folder / "emails")
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


def scenario(base, email_folder=None, fetch_messages=None, timeout=30):
    if email_folder is None and fetch_messages is None:
        raise ValueError("Укажите папку писем или функцию fetch_messages")
    with requests.Session() as session:
        session.trust_env = False
        run_scenario(session, base, email_folder, fetch_messages, timeout)


def run_scenario(session, base, email_folder, fetch_messages, timeout):
    def call(method, path, expected=200, token=None, **kwargs):
        headers = {"Authorization": "Token " + token} if token else {}
        response = session.request(
            method, base + "/api/v1/" + path, headers=headers, timeout=45, **kwargs
        )
        if response.status_code != expected:
            raise AssertionError(f"{method} {path}: HTTP {response.status_code}: {response.text}")
        return response.json()

    def email_messages():
        if fetch_messages is not None:
            return fetch_messages()
        return [
            BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            for path in email_folder.glob("*")
        ]

    def wait_email(subject):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            messages = [message for message in email_messages() if subject in message["Subject"]]
            if messages:
                assert len(messages) == 1, f"Повторное письмо: {subject}"
                return messages[0]
            time.sleep(0.2)
        raise AssertionError(f"За {timeout} секунд не пришло письмо: {subject}")

    def email_token(subject):
        message = wait_email(subject)
        match = re.search(r"[a-f0-9]{64}", message.get_content())
        assert match, f"В письме нет токена: {subject}"
        return match.group()

    def wait_job(job_id, token):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = call("GET", f"partner/jobs/{job_id}", token=token)
            if result["status"] != "pending":
                assert result["status"] == "done", result
                return result
            time.sleep(0.2)
        raise AssertionError(f"Задание {job_id} не завершилось за {timeout} секунд")

    password = "Smoke-Orders-2026!"
    account = {
        "email": "smoke@example.com",
        "password": password,
        "first_name": "Иван",
        "last_name": "Проверкин",
    }
    call("POST", "user/register", expected=201, data=account)
    call("POST", "user/login", expected=401, data=account)
    call(
        "POST",
        "user/register/confirm",
        data={
            "email": account["email"],
            "token": email_token("Подтверждение регистрации"),
        },
    )
    buyer = call("POST", "user/login", data=account)["Token"]
    supplier = call(
        "POST",
        "user/login",
        data={"email": "supplier1@example.com", "password": "Demo-Orders-2026!"},
    )["Token"]
    admin = call(
        "POST", "user/login", data={"email": "admin@example.com", "password": "Demo-Orders-2026!"}
    )["Token"]
    with (ROOT / "data" / "shop1.yaml").open("rb") as file:
        job = call(
            "POST",
            "partner/update",
            expected=202,
            token=supplier,
            files={"file": ("shop1.yaml", file, "text/yaml")},
        )
    result = wait_job(job["job_id"], supplier)
    assert result["status"] == "done" and result["result"]["products"] == 14
    products = call("GET", "products")
    assert len(products) == 16
    first = products[0]
    second = next(item for item in products if item["shop"] != first["shop"])
    items = [
        {"product_info": first["id"], "quantity": 1},
        {"product_info": second["id"], "quantity": 2},
    ]
    basket = call("POST", "basket", token=buyer, json={"items": items})
    address = call(
        "POST",
        "user/contact",
        expected=201,
        token=buyer,
        data={
            "city": "Новосибирск",
            "street": "Ленина",
            "house": "1",
            "phone": "+79991234567",
        },
    )
    call("POST", "order", token=buyer, data={"id": basket["id"], "contact": address["id"]})
    order = call("GET", f"order/{basket['id']}", token=buyer)
    assert order["state"] == "new" and len(order["ordered_items"]) == 2
    assert Decimal(order["total_sum"]) == Decimal(first["price"]) + Decimal(second["price"]) * 2
    assert len(call("GET", "order", token=buyer)) == 1
    partner_orders = call("GET", "partner/orders", token=supplier)
    assert len(partner_orders) == 1 and len(partner_orders[0]["ordered_items"]) == 1
    confirmation = wait_email(f"Заказ №{order['id']} принят")
    assert confirmation["To"] == account["email"]
    invoice = wait_email(f"Накладная для заказа №{order['id']}")
    assert invoice["To"] == "admin@example.com"
    for value in (order["total_sum"], "Новосибирск", "+79991234567"):
        assert value in invoice.get_content(), f"В накладной нет {value}"
    for item in order["ordered_items"]:
        assert item["product_name"] in invoice.get_content()
        assert f"{item['quantity']} шт." in invoice.get_content()
    call(
        "PATCH",
        f"admin/orders/{order['id']}/status",
        expected=403,
        token=buyer,
        json={"state": "confirmed"},
    )
    call("PATCH", f"admin/orders/{order['id']}/status", token=admin, json={"state": "confirmed"})
    wait_email(f"Статус заказа №{order['id']} изменён")
    call("POST", "user/password_reset", data={"email": account["email"]})
    new_password = "Smoke-Changed-2026!"
    call(
        "POST",
        "user/password_reset/confirm",
        data={
            "token": email_token("Сброс пароля"),
            "password": new_password,
        },
    )
    call("GET", "user/details", expected=401, token=buyer)
    buyer = call("POST", "user/login", data={"email": account["email"], "password": new_password})[
        "Token"
    ]
    export = call("GET", "partner/export", expected=202, token=supplier)
    exported = wait_job(export["job_id"], supplier)
    assert "goods:" in exported["result"]["yaml"]
    call("POST", "user/logout", token=buyer)
    print(
        "HTTP smoke: registration, email, login, import, multi-shop order, invoice, "
        "permissions, status, password reset, export, logout — OK"
    )


if __name__ == "__main__":
    run()
