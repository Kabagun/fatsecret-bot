# fatsecret-bot

Telegram-бот для пары пользователей: показывает объединенный список рецептов из двух FatSecret аккаунтов и синхронизирует изменения рецепта в оба аккаунта.

## Что уже реализовано

- Два Telegram-пользователя:
  - если `TELEGRAM_ALLOWED_USER_IDS` задан, пускаются только эти id;
  - если не задан, первые два пользователя, вызвавшие `/start`, регистрируются автоматически.
- Два FatSecret аккаунта через переменные окружения.
- Импорт cookbook из обоих аккаунтов и merge по нормализованному названию рецепта.
- Просмотр общего списка `/recipes`.
- Обновление из FatSecret `/refresh`.
- Создание локального рецепта `/new`.
- Редактирование метаданных рецепта.
- Поиск ингредиента через captured FatSecret endpoints с показом ккал, белков, жиров и углеводов, когда они есть в выдаче.
- Добавление ингредиента в локальную общую модель.
- Синхронизация рецепта в оба аккаунта через captured Android endpoints.

## Важное ограничение MVP

Create-flow нового рецепта подтвержден live-capture: `RecipeActionAndroidPage.aspx` с `action=recipeinitialsave`, `prid=0`; ответ имеет формат `SUCCESS:<recipe_id>`. Затем бот добавляет ингредиенты через `ingredientsave` и сохраняет метаданные через `recipesave`.

Удаление ингредиентов пока не реализовано: в capture был только `ingredientsave`. Чтобы не плодить дубли, перед добавлением бот читает удаленный рецепт и пропускает ингредиент, если такой же `food_id/title + amount` уже есть.

## Установка

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Заполнить `.env`:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS` или оставить пустым для авто-регистрации первых двух пользователей
- `FATSECRET_ACCOUNT_1_USERNAME`
- `FATSECRET_ACCOUNT_1_PASSWORD`
- `FATSECRET_ACCOUNT_2_USERNAME`
- `FATSECRET_ACCOUNT_2_PASSWORD`
- при необходимости `FATSECRET_AUTHORIZATION` и `FATSECRET_C_DESC`

`.env` и runtime SQLite лежат вне git.

## Запуск

```powershell
.\.venv\Scripts\Activate.ps1
python run_bot.py
```

Или после установки пакета в editable-режиме:

```powershell
pip install -e .
fatsecret-bot
```

## Команды

- `/start` - зарегистрироваться/проверить доступ.
- `/refresh` - подтянуть cookbook из двух FatSecret аккаунтов и смержить список.
- `/recipes` - показать объединенный список рецептов.
- `/new` - создать новый рецепт. Формат ответа:

```text
Название | порции | подготовка_мин | готовка_мин | описание
```

В карточке рецепта есть кнопки:

- `Добавить ингредиент`
- `Изменить`
- `Синхронизировать`
- `К списку`

После `/start` бот показывает постоянные кнопки:

- `Рецепты`
- `Обновить`
- `Новый рецепт`

В списке рецептов есть пагинация, а в поиске продуктов каждый результат показывает КБЖУ в формате:

```text
Название | 110 ккал; Б 23.1; Ж 1.2; У 0
```

## Проверки

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest -q
python -c "import run_bot; import httpx, dotenv, telegram"
```
