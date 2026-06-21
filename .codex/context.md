# FatSecret Bot Context

Updated: 2026-06-21

## Repository

- Local path: `D:\Projects\fatsecret`
- Branch: `main`
- Remote: `https://github.com/Kabagun/fatsecret-bot.git`
- Latest deployed commit at time of writing: `aa9ee1c fix: keep recipe drafts with unresolved ingredients`
- Project package: `fatsecret-bot`
- Runtime: Python, `python-telegram-bot`, `httpx`, SQLite storage

Do not commit secrets, tokens, credentials, local caches, logs, generated archives, or temporary deploy helpers.
Keep scratch files under `temp/`.

## Server And Deploy

- Server: `apps@204.168.223.123` (`Helsinki-VPN`)
- Live checkout: `/srv/bots/fatsecret-bot/app`
- Service: `fatsecret-bot`
- Service manager: `systemctl --user`
- Current process command: `/srv/bots/fatsecret-bot/venv/bin/python /srv/bots/fatsecret-bot/app/run_bot.py`

Deploy only through server-side git. Do not upload tar/scp archives.

Typical deploy:

```bash
cd /srv/bots/fatsecret-bot/app
git fetch origin main
git reset --hard origin/main
systemctl --user restart fatsecret-bot
systemctl --user is-active fatsecret-bot
journalctl --user -u fatsecret-bot --since '2 minutes ago' --no-pager | tail -n 60
```

SSH uses 1Password SSH agent. If plain `ssh apps@...` fails with `Too many authentication failures`, constrain OpenSSH to the `apps` key using a public-key selector under `temp/`. Do not create or commit private keys.

## Credentials

- Use 1Password as the primary source for credentials and SSH keys.
- Use `D:\Projects\access.llm.codex-ready.yml` only if the user explicitly says to use that fallback file.
- Telegram bot token and FatSecret credentials must stay out of git.
- FatSecret login/password are added through the bot UI.
- FatSecret mobile sessions are persisted in DB and reused; stale cached sessions are retried by relogin.

## Product Behavior

Telegram bot lets a small group manage and sync FatSecret recipes across connected FatSecret accounts.

Main keyboard:

- `Поиск рецептов`
- `Создать из списка`
- `Группы`
- `Аккаунты`

Groups:

- If user is in a group, show group info and leave action.
- If user is not in a group, show create/join actions.
- Recipes sync only inside the active group.

Accounts:

- User can add FatSecret login/password through the bot.
- User can change only nicknames for FatSecret accounts they added.
- User can log out only from their own connected FatSecret accounts.
- Default nickname comes from FatSecret/account username, but can be changed in the bot.

Recipes:

- Recipe list is loaded from connected FatSecret accounts for the active group.
- Pagination is local after loading the current list.
- Sending text while in recipe list performs recipe search.
- Recipe view actions: sync, delete in FatSecret, return to list.
- Batch delete deletes recipes from FatSecret, not just from bot UI.

## FatSecret API Notes

Login endpoint:

```text
https://app.ftscrt.com/api/authenticate/v1/fatsecret
```

Food search now follows the mobile app endpoint:

```text
https://app.ftscrt.com/api/food/v1/search/data
```

Payload:

```json
{
  "searchExpression": "...",
  "pageNumber": 0,
  "pageSize": 10
}
```

Important response fields parsed from `summaries`:

- `id`
- `title`
- `manufacturername` / `manufacturerName`
- `defaultPortionId`
- `servingSize`
- `gramsPerPortion`
- `energyPerPortion`
- `proteinPerPortion`
- `fatPerPortion`
- `carbohydratePerPortion`
- `isOwn`
- `source`

The bot normalizes nutrition values to 100g when `gramsPerPortion` is not 100.
The old XML `RecipeSearch.aspx` path remains only as fallback.

Android form endpoints still used:

- `CookBookAndroidPage.aspx`
- `RecipeAndroidPage.aspx`
- `RecipeActionAndroidPage.aspx`

`RecipeActionAndroidPage.aspx` actions include:

- `recipeinitialsave`
- `recipesave`
- `ingredientsave`
- `recipedelete`

Cached-session retry behavior:

- retry with fresh login on `401`, `403`, `500`
- retry with fresh login on any `3xx`, including observed `302` from `RecipeActionAndroidPage.aspx`

## Ingredient Matching

Daily food usage cache:

- The bot refreshes FatSecret-derived food usage for all groups daily at 12:00 in `Europe/Minsk`.
- Cache is built from live FatSecret cookbook recipes and their ingredients.
- It is used only to improve ranking and pick commonly used user foods.

Candidate order:

- Prefer frequent/local foods when the query matches.
- Prefer `isOwn` mobile-search results after textual match checks.
- Reject weak matches missing requested tokens.
- Avoid selecting foods with extra meaningful words for exact queries, for example `Куриное Филе в Сыре` for `куриное филе`.
- Keep brand/detail matching strict enough so `кетчуп махеев томатный` does not become `кетчуп махеев русский`, but `кетчуп махеев` can prefer the user's frequent Russian ketchup.

List-created recipe ingredients are sent to FatSecret as gram portions:

- `portion_id = "0"`
- `portion_description = "100г"`
- `amount = grams / 100`

This avoids FatSecret interpreting `300` as 300 servings or eggs as 50 pieces.

## Create Recipe From List

Input format:

```text
Филе куриное 366
Лук 119
Масло 5

Шаги:
1. Нарезать
2. Запечь
```

Parsing rules:

- Last number in each ingredient line is grams.
- Everything before the last number is the ingredient query.
- Steps start after `Шаги:`, `Приготовление:`, or `Способ приготовления:`.
- Up to `MAX_RECIPE_STEPS = 100` steps are saved.

Current unresolved ingredient flow:

- If some ingredients are not found in FatSecret, the bot still keeps the draft.
- The draft shows resolved ingredients and a separate `Нужно заполнить или удалить` block.
- Each unresolved line has actions:
  - `Заполнить`: search FatSecret candidates with the original grams preserved.
  - `Удалить`: remove the unresolved line from the draft.
- `Создать рецепт` is hidden/blocked until all unresolved lines are filled or deleted.
- If all FatSecret accounts reject recipe creation, the local draft is deleted.

## Recent Fixes

- `d528b96 fix: use mobile food search and retry redirects`
  - switched primary food search to `/api/food/v1/search/data`
  - parsed mobile search nutrition/brand/ownership fields
  - normalized macros to 100g
  - retried cached sessions on `302`

- `aa9ee1c fix: keep recipe drafts with unresolved ingredients`
  - unresolved list ingredients no longer discard the whole draft
  - added fill/delete controls for unknown ingredients
  - blocked creation until unknown ingredients are fixed

## Verification Baseline

Latest full local test run before this context file:

```text
python -m pytest
81 passed
```

Latest deploy verification before this context file:

- server checkout was clean on `main...origin/main`
- service `fatsecret-bot` was `active`
- recent logs showed clean stop/start and `telegram.ext.Application: Application started`

