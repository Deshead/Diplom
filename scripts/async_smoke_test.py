"""Настоящая очередь Redis, отдельный Celery worker и локальная почта Mailpit."""

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from email import policy
from email.parser import BytesParser
from pathlib import Path

import redis
import requests
from smoke_test import ROOT, scenario, smoke_environment


def free_ports(count):
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(count)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]


def wait_ready(check, description, processes, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, process in processes:
            if process.poll() is not None:
                raise RuntimeError(f"{name} завершился с кодом {process.returncode}")
        try:
            if check():
                return
        except (OSError, requests.RequestException, redis.exceptions.RedisError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Не удалось дождаться {description} за {timeout} секунд")


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def show_logs(folder):
    for path in sorted(folder.glob("*.log")):
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        print(f"\nПоследние строки {path.name}:", file=sys.stderr)
        print("\n".join(tail), file=sys.stderr)


def mailpit_messages(session, base):
    cache = {}

    def fetch():
        response = session.get(base + "/api/v1/messages", params={"limit": 100}, timeout=3)
        response.raise_for_status()
        messages = response.json()["messages"]
        for message in messages:
            message_id = message["ID"]
            if message_id not in cache:
                raw = session.get(base + f"/api/v1/message/{message_id}/raw", timeout=3)
                raw.raise_for_status()
                cache[message_id] = BytesParser(policy=policy.default).parsebytes(raw.content)
        return [cache[message["ID"]] for message in messages]

    return fetch


def run(redis_server, mailpit):
    for binary in (redis_server, mailpit):
        if not binary.is_file():
            raise FileNotFoundError(f"Не найден исполняемый файл: {binary}")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryDirectory(prefix="diplom-async-smoke-") as directory:
        folder = Path(directory)
        redis_port, smtp_port, mail_port, http_port = free_ports(4)
        env = smoke_environment(folder)
        env.update(
            {
                "CELERY_TASK_ALWAYS_EAGER": "false",
                "CELERY_BROKER_URL": f"redis://127.0.0.1:{redis_port}/0",
                "CELERY_RESULT_BACKEND": f"redis://127.0.0.1:{redis_port}/1",
                "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                "EMAIL_PORT": str(smtp_port),
            }
        )
        base = f"http://127.0.0.1:{http_port}"
        mail_base = f"http://127.0.0.1:{mail_port}"
        processes = []
        completed = False
        with ExitStack() as stack:
            session = stack.enter_context(requests.Session())
            session.trust_env = False

            def start(name, command, cwd=ROOT):
                log = stack.enter_context((folder / f"{name}.log").open("wb"))
                process = subprocess.Popen(
                    [str(value) for value in command],
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    creationflags=flags,
                )
                processes.append((name, process))
                return process

            try:
                print("Запуск изолированных Redis и Mailpit...", flush=True)
                start(
                    "redis",
                    [
                        redis_server,
                        "--bind",
                        "127.0.0.1",
                        "--port",
                        redis_port,
                        "--protected-mode",
                        "yes",
                        "--save",
                        "",
                        "--appendonly",
                        "no",
                        "--dir",
                        folder,
                    ],
                    cwd=folder,
                )
                redis_client = redis.Redis(
                    host="127.0.0.1", port=redis_port, socket_timeout=1, socket_connect_timeout=1
                )
                stack.callback(redis_client.close)
                wait_ready(redis_client.ping, "Redis", processes)
                start(
                    "mailpit",
                    [
                        mailpit,
                        "--listen",
                        f"127.0.0.1:{mail_port}",
                        "--smtp",
                        f"127.0.0.1:{smtp_port}",
                        "--database",
                        folder / "mailpit.sqlite3",
                        "--disable-version-check",
                        "--smtp-disable-rdns",
                        "--smtp-allowed-recipients",
                        r"@example\.com$",
                    ],
                    cwd=folder,
                )
                wait_ready(
                    lambda: (
                        session.get(mail_base + "/api/v1/messages", timeout=1).status_code == 200
                    ),
                    "Mailpit",
                    processes,
                )
                setup_log = stack.enter_context((folder / "setup.log").open("wb"))
                for command in (["migrate", "--noinput"], ["seed_demo"]):
                    subprocess.run(
                        [sys.executable, "manage.py", *command],
                        cwd=ROOT,
                        env=env,
                        stdout=setup_log,
                        stderr=setup_log,
                        check=True,
                        timeout=60,
                        creationflags=flags,
                    )
                print("Запуск отдельного Celery worker и HTTP-сервера...", flush=True)
                start(
                    "worker",
                    [
                        sys.executable,
                        "-m",
                        "celery",
                        "-A",
                        "orders",
                        "worker",
                        "--pool=solo",
                        "--concurrency=1",
                        "--loglevel=info",
                        "--without-gossip",
                        "--without-mingle",
                        "--hostname=smoke@localhost",
                    ],
                )
                wait_ready(
                    lambda: (
                        " ready."
                        in (folder / "worker.log").read_text(encoding="utf-8", errors="replace")
                    ),
                    "готового worker",
                    processes,
                )
                start(
                    "server",
                    [
                        sys.executable,
                        "manage.py",
                        "runserver",
                        f"127.0.0.1:{http_port}",
                        "--noreload",
                    ],
                )
                wait_ready(
                    lambda: session.get(base + "/health/", timeout=1).status_code == 200,
                    "Django",
                    processes,
                )
                fetch_messages = mailpit_messages(session, mail_base)
                scenario(base, fetch_messages=fetch_messages)
                assert len(fetch_messages()) == 5, "Ожидалось ровно пять писем в Mailpit"
                completed = True
                print("Async smoke: Redis + Celery worker + SMTP Mailpit — OK", flush=True)
            finally:
                for name, process in reversed(processes):
                    try:
                        stop_process(process)
                    except OSError as exc:
                        print(f"Не удалось остановить {name}: {exc}", file=sys.stderr)
                if not completed:
                    show_logs(folder)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-server", type=Path, required=True, help="Путь к redis-server")
    parser.add_argument("--mailpit", type=Path, required=True, help="Путь к Mailpit")
    args = parser.parse_args()
    run(args.redis_server.resolve(), args.mailpit.resolve())
