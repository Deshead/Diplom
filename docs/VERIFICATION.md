# Проверки

Последняя проверка: 24 сентября 2026 года, код `af0d07d`. Локально: Windows, Python 3.14.6, Django 5.2.17, DRF 3.17.2, Celery 5.6.3. В GitHub Actions: Ubuntu, Python 3.10 и 3.12.

| Проверка | Результат |
| --- | --- |
| Django check и миграции | Ошибок и незаписанных миграций нет |
| `pip check`, Ruff | Ошибок нет |
| SQLite, полный набор в CI на Python 3.10 и 3.12 | На каждой версии 63 теста прошли, один тест блокировок пропущен |
| PostgreSQL 17.11 локально, полный набор | 64 теста прошли без пропусков |
| PostgreSQL 17 в CI | 64 теста прошли без пропусков |
| Покрытие локального запуска PostgreSQL | 93% |
| HTTP через `smoke_test.py` | Сценарий прошел |
| Страницы Django Admin | Списки и формы 13 зарегистрированных моделей и шесть разрешенных форм добавления открываются; права импорта, проверка размера файла и UTF-8 работают |
| Redis 7.2.16 + отдельный Celery worker + Mailpit 1.31.1 | Импорт, экспорт и отправка пяти писем прошли; eager выключен |
| Compose CLI 5.5.1 | `config --quiet` прошел, пять сервисов распознаны |
| Docker Compose в GitHub Actions | Образ собран, пять сервисов запущены; HTTP-сценарий с PostgreSQL, Redis, Celery и SMTP прошел, получены пять писем |
| GitHub Actions | Все четыре проверки прошли; [запуск для проверенного кода](https://github.com/Deshead/Diplom/actions/runs/36019912298) |

HTTP-проверка проходит регистрацию, вход, импорт прайса, заказ из двух магазинов, смену статуса, сброс пароля и экспорт. Проверка с Mailpit сверяет письма регистрации, принятия заказа, накладную, новый статус и сброс пароля.

Исправления после повторной сверки 24 сентября:

- Импорт сохраняет текущее состояние приема заказов. Проверен случай, когда поставщик отключает магазин во время загрузки прайса.
- Смена пароля в админке отзывает прежний API-токен и коды сброса. Старый токен получает 401, старое письмо сброса — 400. Измененные к этому моменту поля профиля сохраняются.

Эти случаи покрыты тремя новыми тестами. Также проверено сохранение URL или имени файла последнего успешного импорта. Имя файла проходит через API, команду, демо-загрузку, админку и отдельный worker.

`data/shop1.yaml` в Git совпадает с исходным файлом Нетологии: SHA256 `0B4090AECBC192712055B640570C3301D801BBD975419ACDDD3E42D15831C804`. При скачивании по публичному URL файл успешно прочитан: 14 товаров и четыре категории. Второй прайс добавляет еще два предложения магазина. Локальная копия на Windows может иметь другой хеш из-за замены переводов строк LF на CRLF; содержимое YAML одинаковое.

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

Docker проверяется отдельным заданием `docker` в `.github/workflows/tests.yml`: сборка, запуск с ожиданием готовности, `seed_demo`, затем `python scripts/docker_smoke_test.py`. Проверка меняет тестовую базу и требует пустой почты Mailpit. В CI после нее удаляются созданные этим заданием контейнеры и тестовые тома.

## Ограничения проверки

- На локальной Windows нет Docker Engine, поэтому контейнеры проверены на Ubuntu в GitHub Actions.
- Внешний почтовый провайдер не подключался: SMTP проверен через Mailpit локально и в Docker.
- Тест блокировок остатка запускается на PostgreSQL; SQLite не поддерживает нужную блокировку строк и пропускает этот тест.
