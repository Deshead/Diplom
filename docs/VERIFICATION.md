# Проверки

Последняя проверка: 21 сентября 2026 года. Windows, Python 3.14.6, Django 5.2.17, DRF 3.17.2, Celery 5.6.3.

| Проверка | Результат |
| --- | --- |
| Django check и миграции | Ошибок и незаписанных миграций нет |
| `pip check`, Ruff | Ошибок нет |
| SQLite, полный набор от 16 сентября | 50 тестов прошли, один тест блокировок пропущен |
| PostgreSQL 17.11 | 51 тест прошел без пропусков |
| Покрытие | 91% |
| HTTP через `smoke_test.py` | Сценарий прошел |
| Redis 7.2.16 + отдельный Celery worker + Mailpit 1.31.1 | Импорт, экспорт и отправка пяти писем прошли; eager выключен |
| Compose CLI 5.5.1 | `config --quiet` прошел, пять сервисов распознаны |

HTTP-проверка проходит регистрацию, вход, импорт прайса, заказ из двух магазинов, смену статуса, сброс пароля и экспорт. Проверка с Mailpit сверяет письма регистрации, принятия заказа, накладную, новый статус и сброс пароля.

21 сентября повторно прошли 51 тест на PostgreSQL и оба HTTP-сценария на временных базах SQLite. Тест профиля также проверяет, что смена пароля не меняет имя, фамилию, компанию, должность и телефон.

Тест PostgreSQL проверяет одновременное оформление двух заказов при недостаточном общем остатке: проходит только один заказ.

## Повторный запуск

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m coverage run manage.py test
.\.venv\Scripts\python.exe -m coverage report
.\.venv\Scripts\python.exe scripts/smoke_test.py
.\.venv\Scripts\python.exe scripts/async_smoke_test.py `
  --redis-server .local/tools/redis/Redis-7.2.16-Windows-x64-msys2/redis-server.exe `
  --mailpit .local/tools/mailpit/mailpit.exe
docker compose config --quiet
```

На Linux/macOS используйте `.venv/bin/python` и пути к своим Redis и Mailpit. Для тестов PostgreSQL нужны переменные `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`; пользователь БД должен иметь право создавать тестовую базу.

## Что не запускалось

- Контейнеры Docker: на машине нет работающего Docker Engine. Проверена только конфигурация Compose.
- Отправка через внешний почтовый сервис: SMTP проверен на локальном Mailpit.
- GitHub Actions: файл проверок есть, удаленного запуска не было.
