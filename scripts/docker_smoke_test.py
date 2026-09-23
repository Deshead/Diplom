"""Проверка свежего Docker Compose после migrate и seed_demo. Меняет тестовую БД."""

import requests
from async_smoke_test import mailpit_messages, wait_ready
from smoke_test import scenario


def run():
    with requests.Session() as session:
        session.trust_env = False
        mail_base = "http://127.0.0.1:8025"
        wait_ready(
            lambda: session.get(mail_base + "/api/v1/messages", timeout=1).status_code == 200,
            "Mailpit",
            processes=[],
        )
        fetch_messages = mailpit_messages(session, mail_base)
        # Старые письма помешают проверить, что каждое уведомление пришло один раз.
        assert not fetch_messages(), "Для проверки нужен свежий Mailpit без писем"
        scenario("http://127.0.0.1:8000", fetch_messages=fetch_messages, timeout=60)
        assert len(fetch_messages()) == 5, "Ожидалось ровно пять писем в Mailpit"
        print("Docker smoke: PostgreSQL + Redis + Celery + HTTP + SMTP Mailpit — OK")


if __name__ == "__main__":
    run()
