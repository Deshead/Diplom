# API

Адрес: `http://127.0.0.1:8000/api/v1`. Пути ниже — без завершающего `/`.

Поддерживаются JSON, `application/x-www-form-urlencoded` и `multipart/form-data`. Файл передается через multipart в поле `file`.

После входа добавляйте заголовок:

```http
Authorization: Token значение_токена
```

Основные ошибки: 400 — неверные данные, 401 — нет авторизации, 403 — нет прав, 404 — объект не найден или недоступен.

## Пользователь

| Метод и путь | Поля |
| --- | --- |
| `POST /user/register` | `first_name`, `last_name`, `email`, `password`; необязательные `company`, `position`, `type` (`buyer` или `shop`, по умолчанию `buyer`) |
| `POST /user/register/confirm` | `email`, `token` из письма |
| `POST /user/register/resend` | `email` |
| `POST /user/login` | `email`, `password` |
| `POST /user/logout` | Без полей |
| `GET /user/details` | Профиль |
| `POST /user/details` | `first_name`, `last_name`, `company`, `position`, `phone`, `password` — только изменяемые поля |
| `POST /user/password_reset` | `email` |
| `POST /user/password_reset/confirm` | `token`, `password`; необязательный `email` |

Вход возвращает `{"Status": true, "Token": "..."}`. До подтверждения email вход закрыт. Токен регистрации действует 24 часа, сброса пароля — один час. После смены пароля старый токен входа удаляется. Email и роль нельзя менять через профиль.

`resend` заменяет старый токен регистрации. Ответ для любого адреса одинаковый: HTTP 200, `Status: true`, `message`. Письмо отправляется только для незавершенной регистрации. При сбое почты запрос можно повторить после ее восстановления.

Пример регистрации:

```json
{
  "first_name": "Иван",
  "last_name": "Петров",
  "email": "ivan@example.com",
  "password": "Study-order-2026!"
}
```

## Каталог

Доступен без входа:

| Метод и путь | Ответ |
| --- | --- |
| `GET /categories` | Категории |
| `GET /shops` | Магазины, принимающие заказы |
| `GET /products` | Предложения магазинов |
| `GET /products/<id>` | Одно предложение |

Фильтры: `shop_id`, `category_id`, `search` по названию, описанию или модели. Пример: `/products?shop_id=1&category_id=224&search=iphone`.

Поля предложения: `id`, `external_id`, `model`, `product`, `shop`, `quantity`, `price`, `price_rrc`, `product_parameters`. В `product` находятся название, описание и категория. У характеристик поля `parameter` и `value`. Цены передаются строками, например `"65000.00"`.

`id` предложения — это `product_info` для корзины. Один товар может продаваться в нескольких магазинах по разным ценам.

## Контакты

| Метод и путь | Поля |
| --- | --- |
| `GET /user/contact` | Список своих адресов |
| `POST /user/contact` | `city`, `street`; `phone`, если телефон еще не заполнен |
| `PUT /user/contact` | `id` и изменяемые поля |
| `DELETE /user/contact` | `items`: список ID или строка `"1,2"` |

Необязательные поля: `first_name`, `last_name`, `middle_name`, `email`, `house`, `structure` (корпус), `building` (строение), `apartment`.

Можно сохранить до пяти адресов. Телефон один на пользователя. Его изменение через контакт применяется ко всем адресам.

```json
{
  "phone": "+79991234567",
  "city": "Новосибирск",
  "street": "Советская",
  "house": "10",
  "apartment": "25"
}
```

## Корзина

`GET /basket` возвращает `[]` или список с одной корзиной. Поля: `id`, `state`, `dt`, `ordered_items`, `total_sum`, `contact`.

Добавление через `POST /basket`:

```json
{"items": [{"product_info": 1, "quantity": 2}, {"product_info": 15, "quantity": 1}]}
```

Повторное добавление увеличивает количество. `PUT /basket` устанавливает количество по ID строки корзины:

```json
{"items": [{"id": 1, "quantity": 3}]}
```

Удаление через `DELETE /basket`:

```json
{"items": [1, 2]}
```

Для POST/PUT поле формы `items` также принимает JSON-строку. Для DELETE подходит строка `1,2`. При изменении и удалении нужны ID строк корзины, а не предложений каталога.

Строка корзины: `id`, `product_info`, `product_name`, `shop_name`, `quantity`, `price`, `total_sum`. Количество должно быть положительным и не превышать остаток. Корзина не резервирует товар.

## Заказы

| Метод и путь | Поля / ответ |
| --- | --- |
| `POST /order` | `id` своей корзины, `contact` своего адреса |
| `GET /order` | Свои оформленные заказы |
| `GET /order/<id>` | Один заказ |

```json
{"id": 1, "contact": 1}
```

При оформлении остатки проверяются и уменьшаются. Статус меняется на `new`. Пустую или уже оформленную корзину оформить нельзя. Покупателю отправляется подтверждение, на `ADMIN_EMAIL` — накладная.

В ответе: `id`, `dt`, `state`, `ordered_items`, `total_sum`, `contact`. Цены, названия и адрес сохраняются в заказе и не меняются после нового импорта или правки контактов.

## Поставщик

Нужен подтвержденный аккаунт с `type=shop`.

| Метод и путь | Поля / ответ |
| --- | --- |
| `POST /partner/update` | YAML-файл `file` или публичный HTTP(S)-адрес `url` |
| `GET /partner/export` | Запуск экспорта прайса |
| `GET /partner/jobs/<id>` | Своя задача |
| `GET /partner/state` | Магазин: `id`, `name`, `state` |
| `POST /partner/state` | `state`: boolean или `on`/`off`, `true`/`false`, `1`/`0` |
| `GET /partner/orders` | Заказы с позициями только своего магазина |

Импорт и экспорт возвращают HTTP 202:

```json
{"Status": true, "job_id": 1}
```

Поля задачи: `id`, `kind` (`import`/`export`), `status` (`pending`/`done`/`error`), `result`, `error`, `created_at`.

Успешный импорт возвращает в `result`:

```json
{"shop_id": 1, "products": 14, "categories": 4}
```

При экспорте в `result.yaml` находится текст прайса. Его можно сохранить в UTF-8 как `.yaml`.

Формат прайса:

```yaml
shop: Магазин
categories:
  - id: 224
    name: Смартфоны
goods:
  - id: 1001
    category: 224
    model: sample/phone
    name: Смартфон
    description: Описание товара
    price: 10000
    price_rrc: 12000
    quantity: 5
    parameters:
      Цвет: черный
      Память: 128
```

Предложения сопоставляются по магазину и внешнему `id`. Товары, пропавшие из нового прайса, становятся недоступными. Старые заказы сохраняются. Один ID категории должен иметь одинаковое название у разных магазинов.

Максимальный размер файла — 2 МиБ. Ссылка должна вести на публичный ресурс: локальные и внутренние адреса запрещены.

## Администратор

| Метод и путь | Поля / ответ |
| --- | --- |
| `GET /admin/orders` | Все оформленные заказы |
| `PATCH /admin/orders/<id>/status` | `{"state": "confirmed"}` |

Доступ только сотруднику. Статусы: `new` → `confirmed` → `assembled` → `sent` → `delivered`. До отправки можно отменить заказ (`canceled`), остатки вернутся. `delivered` и `canceled` — конечные статусы.

При смене статуса покупателю отправляется письмо. Повтор того же статуса новое письмо не создает. Просмотр заказов, смена статуса и импорт прайса также доступны через `/admin/`.

Основные пути и поля соответствуют [Postman-примерам задания](https://documenter.getpostman.com/view/5037826/SVfJUrSc).
