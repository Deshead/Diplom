# Сервис закупок

Backend на Django REST Framework по [заданию Нетологии](https://github.com/netology-code/python-final-diplom).

Покупатель выбирает товары нескольких магазинов, собирает корзину и оформляет заказ. Поставщик загружает YAML-прайс и включает или отключает прием заказов. Администратор меняет статусы заказов. Подтверждения и накладные отправляются по email.

Есть регистрация, восстановление пароля, поиск товаров, адреса доставки, импорт и экспорт прайсов. Запросы описаны в [docs/API.md](docs/API.md), примеры — в [requests.http](requests.http).

## Запуск

Нужен Python 3.10 или новее. Команды выполняются из папки проекта. Локально используются SQLite, письма в терминале и задачи без Redis.

### Windows, PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo
.\.venv\Scripts\python.exe manage.py runserver
```

### Linux и macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo
.venv/bin/python manage.py runserver
```

При повторном запуске не перезаписывайте свой `.env`. Переменные окружения имеют приоритет над этим файлом. `SECRET_KEY` записывайте в одинарных кавычках: `SECRET_KEY='ваш-ключ'`. Тогда Compose не будет подставлять переменные вместо знака `$` в ключе.

Каталог: <http://127.0.0.1:8000/api/v1/products>. Админка: <http://127.0.0.1:8000/admin/>.

### Демо

`seed_demo` загружает два прайса и создает аккаунты:

| Роль | Email |
| --- | --- |
| Покупатель | `buyer@example.com` |
| Поставщики | `supplier1@example.com`, `supplier2@example.com` |
| Администратор | `admin@example.com` |

Пароль новых аккаунтов: `Demo-Orders-2026!`. При повторном запуске пароли не меняются, прайсы обновляются. Команда работает только при `DJANGO_DEBUG=true`.

Своего администратора можно создать командой `manage.py createsuperuser`, запустив ее тем же Python из `.venv`.

## Почта и Celery

По умолчанию письма выводятся в терминал Django. Там можно взять токены регистрации и сброса пароля. Повторное письмо регистрации: `POST /api/v1/user/register/resend` с полем `email`.

При `CELERY_TASK_ALWAYS_EAGER=true` задачи выполняются сразу, без отдельного worker. В Docker используется Redis и отдельный Celery worker.

Для настоящей почты при локальном запуске укажите в `.env` backend `django.core.mail.backends.smtp.EmailBackend`, настройки SMTP и `ADMIN_EMAIL`.

В Compose письма идут в Mailpit. Для своего SMTP в контейнерах нужно изменить `EMAIL_*` в общей секции `x-app-environment` файла `docker-compose.yml`: она переопределяет `.env` для `web` и `worker`.

## Docker

Нужен Docker Engine с Compose или Docker Desktop с Linux containers. Сначала создайте `.env` из `.env.example`.

```bash
docker compose up --build -d
docker compose ps
docker compose exec web python manage.py seed_demo
```

Запускаются Django, PostgreSQL, Redis, Celery и Mailpit. API доступен на порту `8000`, письма — на <http://127.0.0.1:8025>. Это учебный запуск с `runserver` и `DJANGO_DEBUG=true`. Миграции выполняются автоматически до запуска worker.

```bash
docker compose logs -f web worker
docker compose exec web python manage.py check
docker compose exec web python manage.py createsuperuser
docker compose down
```

`down` сохраняет данные PostgreSQL. После изменения кода повторите `docker compose up --build -d`.

## Проверки

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m coverage run manage.py test
.\.venv\Scripts\python.exe -m coverage report
.\.venv\Scripts\python.exe scripts/smoke_test.py
```

На Linux/macOS замените `.\.venv\Scripts\python.exe` на `.venv/bin/python`. `smoke_test.py` запускает временную базу и HTTP-сервер, проверяет запросы и письма. Рабочая база не меняется.

Для проверки очереди и SMTP нужны программы Redis и Mailpit. Пример с локальными путями на Windows:

```powershell
.\.venv\Scripts\python.exe scripts/async_smoke_test.py `
  --redis-server .local/tools/redis/Redis-7.2.16-Windows-x64-msys2/redis-server.exe `
  --mailpit .local/tools/mailpit/mailpit.exe
```

На Linux/macOS:

```bash
.venv/bin/python scripts/async_smoke_test.py \
  --redis-server /usr/bin/redis-server \
  --mailpit /usr/local/bin/mailpit
```

Подставьте пути к установленным программам. Папка `.local` не входит в репозиторий. Скрипт сам запускает и останавливает серверы, использует временную базу и проверяет пять писем.

Результаты: [docs/VERIFICATION.md](docs/VERIFICATION.md). Требования задания: [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md).

## Файлы

```text
backend/           модели, API, задачи, админка, тесты
orders/            настройки Django и Celery
data/              прайсы
docs/              документация
scripts/           проверки HTTP, Redis и SMTP
requests.http      примеры запросов
docker-compose.yml запуск сервисов в Docker
```

Исходный прайс `data/shop1.yaml`: [файл из задания Нетологии](https://github.com/netology-code/python-final-diplom/blob/master/data/shop1.yaml).
