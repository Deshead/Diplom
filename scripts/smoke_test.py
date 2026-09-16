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


def run():
    with tempfile.TemporaryDirectory(prefix="diplom-smoke-") as directory:
        folder = Path(directory)
        env = os.environ.copy()
        env.update(
            {
                "DJANGO_DEBUG": "true",
                "POSTGRES_HOST": "",
                "SQLITE_NAME": str(folder / "smoke.sqlite3"),
                "EMAIL_BACKEND": "django.core.mail.backends.filebased.EmailBackend",
                "EMAIL_FILE_PATH": str(folder / "emails"),
                "CELERY_TASK_ALWAYS_EAGER": "true",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with (folder / "server.log").open("w", encoding="utf-8") as log:
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
                        if requests.get(base + "/health/", timeout=1).status_code == 200:
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


def scenario(base, email_folder):
    def call(method, path, expected=200, token=None, **kwargs):
        headers = {"Authorization": "Token " + token} if token else {}
        response = requests.request(
            method, base + "/api/v1/" + path, headers=headers, timeout=45, **kwargs
        )
        if response.status_code != expected:
            raise AssertionError(f"{method} {path}: HTTP {response.status_code}: {response.text}")
        return response.json()

    def email_messages():
        return [
            BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            for path in email_folder.glob("*")
        ]

    def email_token(subject):
        messages = [message for message in email_messages() if subject in message["Subject"]]
        assert len(messages) == 1, f"Не найдено письмо: {subject}"
        return re.search(r"[a-f0-9]{64}", messages[0].get_content()).group()

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
    result = call("GET", f"partner/jobs/{job['job_id']}", token=supplier)
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
    assert any("Накладная" in message["Subject"] for message in email_messages())
    call(
        "PATCH",
        f"admin/orders/{order['id']}/status",
        expected=403,
        token=buyer,
        json={"state": "confirmed"},
    )
    call("PATCH", f"admin/orders/{order['id']}/status", token=admin, json={"state": "confirmed"})
    assert any("Статус заказа" in message["Subject"] for message in email_messages())
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
    assert call("GET", f"partner/jobs/{export['job_id']}", token=supplier)["status"] == "done"
    call("POST", "user/logout", token=buyer)
    print(
        "HTTP smoke: registration, email, login, import, multi-shop order, invoice, "
        "permissions, status, password reset, export, logout — OK"
    )


if __name__ == "__main__":
    run()
