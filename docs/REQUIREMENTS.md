# Требования задания

[Задание Нетологии](https://github.com/netology-code/python-final-diplom).

| Требование | Где сделано |
| --- | --- |
| Django и REST API | `orders/`, `backend/urls.py` |
| Магазины, категории, товары, заказы и контакты | `backend/models.py` |
| Несколько магазинов в одной категории | `Category.shops` |
| Произвольные характеристики товара | `Parameter`, `ProductParameter` |
| Импорт YAML-прайса | `backend/catalog.py`, `backend/catalog_views.py` |
| Регистрация, вход и подтверждение email | `backend/auth_views.py` |
| Восстановление пароля | `backend/auth_views.py` |
| Каталог, фильтры, поиск и детали товара | `backend/catalog_views.py` |
| Корзина с товарами разных магазинов | `backend/order_views.py` |
| До пяти адресов и один телефон | `Contact`, `User.phone`, `backend/order_serializers.py` |
| Оформление заказа и проверка остатков | `backend/order_services.py` |
| Письмо покупателю и накладная администратору | `backend/order_services.py`, `backend/tasks.py` |
| История и детали заказов | `backend/order_views.py` |
| Включение и отключение магазина | `backend/catalog_views.py` |
| Заказы поставщика | `backend/order_views.py` |
| Экспорт YAML | `backend/catalog.py`, `backend/catalog_tasks.py` |
| Админка, смена статуса, импорт из формы | `backend/admin.py` |
| Отправка писем, импорт и экспорт через Celery | `backend/tasks.py`, `backend/catalog_tasks.py`, `orders/celery.py` |
| Docker Compose | `Dockerfile`, `docker-compose.yml` |
| Запуск и описание API | `README.md`, `docs/API.md`, `requests.http` |
| Проверки | `backend/tests/`, `scripts/`, `docs/VERIFICATION.md` |

Исходный прайс `data/shop1.yaml` содержит 14 товаров и четыре категории. `data/shop2.yaml` добавляет второй магазин для проверки общей корзины.
