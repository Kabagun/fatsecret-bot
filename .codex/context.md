# FatSecret Bot Context

Updated: 2026-06-22

## Repository

- Local path: repository root
- Branch: `main`
- Remote: `https://github.com/Kabagun/fatsecret-bot.git`
- Latest deployed commit should be verified with `git log` on `main` and the live checkout.
- Project package: `fatsecret-bot`
- Runtime: Python, `python-telegram-bot`, `httpx`, SQLite storage

Do not commit secrets, tokens, credentials, local caches, logs, generated archives, or temporary deploy helpers.
Keep scratch files under `temp/`.

## Server And Deploy

- Server: `apps` user on the FatSecret bot host (`Helsinki-VPN`)
- Live checkout: bot app checkout directory
- Service: `fatsecret-bot`
- Service manager: `systemctl --user`
- Current process: project venv Python running `run_bot.py`

Deploy only through server-side git. Do not upload tar/scp archives.

Typical deploy:

```bash
cd "$FATSECRET_BOT_APP"
git fetch origin main
git reset --hard origin/main
systemctl --user restart fatsecret-bot
systemctl --user is-active fatsecret-bot
journalctl --user -u fatsecret-bot --since '2 minutes ago' --no-pager | tail -n 60
```

SSH uses 1Password SSH agent. If plain `ssh apps@...` fails with `Too many authentication failures`, constrain OpenSSH to the `apps` key using a public-key selector under `temp/`. Do not create or commit private keys.

## Credentials

- Use 1Password as the primary source for credentials and SSH keys.
- Use the fallback access file only if the user explicitly says to use it.
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

For `app.ftscrt.com/api/...` JSON endpoints, app session auth must match the Android capture:

- query has `c_fl=1`, `dt`, app/build, language/market/device fields
- query does not include `c_id`, `c_s`, or `c_d`
- headers include `Authorization`, `c_id`, `c_s`, `c_d`, `fs_device=android`, `fs_dt`,
  `app_version`, `device`, `market`, `fs_market_locale`, and `fs_language_locale`

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

For recipe creation, mobile-search food ids may be good for display/KБЖУ but rejected by
`ingredientsave`. If an ingredient add returns false, the bot retries once with a compatible id
from legacy `RecipeSearch.aspx` via `search_addable_foods`.

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
Порций: 4
Филе куриное 366
Лук 119
Масло 5

Шаги:
1. Нарезать
2. Запечь
```

Parsing rules:

- The first non-step block must include `Порций: N`.
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
- Recipe creation is all-or-nothing across connected FatSecret accounts.
- If any account rejects an ingredient or metadata save, created remote recipes are deleted and the local draft is deleted.
- Telegram user_data keeps the draft in the chat flow so the user can return to review and replace the bad ingredient.

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

- rollback/addable-id fix after `aa9ee1c`
  - recipe creation rolls back created remote recipes if any ingredient is rejected
  - list-created ingredients retry with legacy addable ids when mobile-search ids are not accepted by `ingredientsave`

- recipe-list portions and kcal display fix after rollback/addable-id fix
  - `Порций: N` is required in list-created recipes and saved to FatSecret

- app food-search auth fix after recipe-list portions
  - fixed `/api/food/v1/search/data` auth to send `c_id/c_s/c_d` in headers like Android
  - removed the UI-level kcal correction that masked bad search auth/parser behavior
  - verified live `сочный` search returns `Фарш Сочный (Green)` as `320/15/29/0`

## Verification Baseline

Latest full local test run before this context file:

```text
python -m pytest
86 passed
```

Latest deploy verification before this context file:

- server checkout was clean on `main...origin/main`
- service `fatsecret-bot` was `active`
- recent logs showed clean stop/start and `telegram.ext.Application: Application started`
