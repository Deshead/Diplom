# Сервис закупок

Учебный backend на Django REST Framework для заказа товаров у нескольких поставщиков. Покупатель выбирает предложения магазинов, собирает корзину и оформляет заказ. Поставщик загружает YAML-прайс, управляет приемом заказов и получает свои позиции в заказах.

Проект написан по [заданию Нетологии](https://github.com/netology-code/python-final-diplom). Реализованы базовая часть и дополнительные возможности: экспорт, Django Admin, задачи Celery и Docker Compose. Пользовательский интерфейс — REST API; администратор работает через Django Admin.

## Возможности

- Регистрация с подтверждением email, вход по email и паролю, выход, восстановление пароля.
- Каталог с поиском, фильтрами по магазину и категории, описаниями и произвольными характеристиками.
- Импорт и экспорт YAML-прайса; просмотр результата обработки.
- Корзина с товарами нескольких магазинов, изменение количества и удаление позиций.
- До пяти адресов доставки и один телефон у пользователя.
- Проверка наличия и списание остатков при оформлении заказа.
- История заказов со снимками названий, цен и адреса доставки.
- Письмо покупателю и накладная администратору при оформлении, уведомления об изменении статуса.
- Админка со сменой статуса заказа и запуском импорта.

## Локальный запуск

Нужны Python 3.10 или новее и Git. Для первого запуска подходит SQLite: PostgreSQL и Redis устанавливать отдельно не требуется. Команды выполняются из корня проекта.

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

`seed_demo` создает учебных покупателя, двух поставщиков и администратора, выводит данные для входа и импортирует прайсы. В первом прайсе 14 товаров из задания; второй нужен для проверки заказа из нескольких магазинов. Используйте эту команду для знакомства с проектом: она доступна при `DJANGO_DEBUG=true`. Для создания собственного администратора выполните `python manage.py createsuperuser` выбранным интерпретатором.

Учебные аккаунты: `buyer@example.com`, `supplier1@example.com`, `supplier2@example.com`, `admin@example.com`. Пароль для вновь созданных аккаунтов: `Demo-Orders-2026!`. Повторный запуск не меняет пароли существующих пользователей.

Откройте:

- API каталога: <http://127.0.0.1:8000/api/v1/products>
- Django Admin: <http://127.0.0.1:8000/admin/>

Файл `.env` не попадает в Git. При повторном запуске не копируйте `.env.example` поверх собственных настроек.

## Как посмотреть письма и фоновые задачи

В локальном `.env.example` выбран `console.EmailBackend`: письма, в том числе токены подтверждения регистрации и восстановления пароля, выводятся в терминал запущенного Django. Реальное письмо в почтовый ящик в этом режиме не отправляется.

Если подтверждение не пришло или токен просрочен, отправьте `POST /api/v1/user/register/resend` с `{"email": "ivan@example.com"}`. Новый токен заменяет предыдущий. Ответ одинаковый для любого адреса; письмо отправляется только для регистрации, которая еще ожидает подтверждения. При сбое почты или очереди аккаунт сохраняется, поэтому после восстановления сервиса можно повторить этот запрос.

`CELERY_TASK_ALWAYS_EAGER=true` выполняет задачи сразу в текущем процессе. Это удобно для локального знакомства на Windows. Импорт и экспорт сохраняют результат в `CatalogJob`; API возвращает `job_id`, по которому можно получить результат.

В Docker работают отдельный Celery worker и Redis, а письма принимает Mailpit. Для отправки на настоящий email замените `EMAIL_BACKEND` на `django.core.mail.backends.smtp.EmailBackend`, заполните SMTP-настройки в `.env` и укажите `ADMIN_EMAIL`. Настройки SMTP зависят от выбранного почтового сервиса.

## Docker Compose

Нужны Docker Engine с Compose либо Docker Desktop в режиме Linux containers. Это учебное окружение: Django запускается с `runserver` и `DJANGO_DEBUG=true`, чтобы сразу работали API и статика админки.

Если `.env` еще нет, скопируйте его из `.env.example`. Затем:

```bash
docker compose up --build -d
docker compose ps
docker compose exec web python manage.py seed_demo
```

Сервисы:

| Сервис | Назначение |
| --- | --- |
| `web` | Django на <http://127.0.0.1:8000> |
| `db` | PostgreSQL, данные в отдельном volume |
| `redis` | Очередь и результаты Celery |
| `worker` | Отправка писем, импорт и экспорт |
| `mailpit` | Просмотр писем на <http://127.0.0.1:8025> |

Compose переопределяет локальные настройки: использует PostgreSQL, `CELERY_TASK_ALWAYS_EAGER=false` и SMTP Mailpit на порту `1025`. Порт SMTP доступен внутри сети контейнеров. `web` ждет готовности базы и Redis, выполняет миграции; `worker` запускается после проверки готовности `web`.

```bash
docker compose logs -f web worker
docker compose exec web python manage.py check
docker compose exec web python manage.py createsuperuser
docker compose down
```

`docker compose down` останавливает окружение, сохраняя данные в volumes. После изменения кода пересоберите образ командой `docker compose up --build -d`.

## Работа с API

Базовый адрес — `http://127.0.0.1:8000/api/v1`. Пути API используются без завершающего `/`. Поддерживаются JSON и формы из исходной Postman-коллекции.

1. Зарегистрируйтесь через `POST /user/register`.
2. Возьмите токен из письма и отправьте `POST /user/register/confirm`.
3. Войдите через `POST /user/login` и сохраните поле `Token`.
4. Для закрытых запросов добавляйте `Authorization: Token <значение>`.
5. Получите `/products`, добавьте выбранные `product_info` в `/basket`.
6. Создайте адрес через `/user/contact`.
7. Передайте ID корзины и адреса в `POST /order`.
8. Получите `/order` и `/order/<id>`, посмотрите письма.

Подробные поля и примеры: [docs/API.md](docs/API.md). Готовые запросы для HTTP-клиента: [requests.http](requests.http). Значения ID и токенов подставляются из ответов вашего сервера.

Поставщик регистрируется с `type=shop`, подтверждает email и загружает прайс через `POST /partner/update`. Магазин создается при первом успешном импорте. Поставщик видит только свои позиции оформленных заказов.

## Проверка проекта

После установки `requirements-dev.txt` выполните:

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m coverage run manage.py test
.\.venv\Scripts\python.exe -m coverage report
.\.venv\Scripts\python.exe scripts/smoke_test.py
```

На Linux/macOS замените `.\.venv\Scripts\python.exe` на `.venv/bin/python`. Проверки также настроены в GitHub Actions для Python 3.10 и 3.12 с SQLite и отдельным запуском на PostgreSQL 17. Тесты создают отдельную временную базу и используют тестовый почтовый backend. Команды выше — инструкция для повторной проверки; фактический результат определяется выводом запуска.

`scripts/smoke_test.py` сам создает временную базу, применяет миграции, загружает демо и запускает настоящий HTTP-сервер на свободном локальном порту. Он проходит сценарий через HTTP и проверяет письма, сохраненные в файлы. После завершения сервер останавливается, временные файлы удаляются; рабочая база проекта не используется.

Перед защитой пройдите сценарий с регистрацией, двумя магазинами, оформлением заказа и сменой статуса. Отдельно проверьте ошибочные данные, чужой адрес, недостаточный остаток, повторное оформление и повторный импорт прайса. Чеклист задания находится в [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md).

## Структура

```text
backend/           модели, API, бизнес-логика, задачи, админка и тесты
orders/            настройки Django и Celery
data/              YAML-прайсы для импорта
docs/              описание API и соответствие заданию
requests.http      сценарий ручной проверки
docker-compose.yml учебное окружение из пяти сервисов
```

## Исходные материалы и сдача

`data/shop1.yaml` взят из [репозитория задания](https://github.com/netology-code/python-final-diplom/blob/master/data/shop1.yaml). Код приложения написан заново; происхождение данных сохранено. Помощь AI описана в [docs/AI_USAGE.md](docs/AI_USAGE.md).

По правилам Нетологии итоговый проект нужно разместить в своем GitHub-репозитории, доступном дипломному руководителю, и отправить постоянную ссылку в личном кабинете. Публикация в GitHub и сдача в личном кабинете выполняются владельцем аккаунтов.
