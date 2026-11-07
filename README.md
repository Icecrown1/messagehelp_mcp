# STELLA MessageHelp MCP

Дополнительный модуль для Threads **Astro Orbi** описан в
[инструкции подключения](docs/astro-orbi-setup.md). Он выключен по умолчанию
(`ENABLE_THREADS=false`) и использует отдельные права публикации и пространство
хранения. Первая версия готовит черновики через ассистента в чате и публикует
только согласованные тексты по расписанию; автономной генерации текстов пока нет.

MCP-сервер для Render. Редактирует существующие шаблоны интеграции Altegio:
до/после визита, отзывы, события записи и дни рождения.

## Возможности и ограничения

- Чтение, превью изменений, сохранение с проверкой результата.
- Доступ только указанному GitHub-пользователю и указанным проектам MessageHelp.
- По умолчанию `ENABLE_WRITES=false`. Включать после проверки подключения.
- Нет отправки сообщений, удаления шаблонов, изменения платежей или произвольных HTTP-запросов.
- Изменение активного шаблона влияет на последующие автоматические отправки MessageHelp.
- Сложные поля `conditionrule`, `confirmationrule`, `config` сохраняются как есть:
  их изменение пока запрещено из-за неполной документации API.
- Проверка revision защищает от уже произошедших изменений, но API не даёт атомарный
  compare-and-swap: не редактируйте один шаблон одновременно в интерфейсе и через MCP.
- Render Free засыпает при простое; первое обращение может завершиться таймаутом.
  После пробуждения повторите подключение. OAuth хранится отдельно в зашифрованном
  Key Value через `OAUTH_REDIS_URL`, поэтому обновление веб-сервиса не стирает регистрации.
  Render Free Key Value теряет данные при собственном перезапуске или обновлении тарифа:
  Для постоянной работы используйте платный Key Value с режимом Journal + Snapshot
  и постоянный ключ шифрования. Потерянную регистрацию существующего клиента можно
  восстановить по процедуре ниже; повторное подключение само её не восстанавливает.

## Развёртывание

1. Используйте отдельный GitHub-репозиторий `messagehelp_mcp`.
   Загрузите в его корень содержимое этой папки, включая `.gitignore`.
2. В Render создайте Web Service из этого репозитория, выберите **0.5c-512mb**.
   Runtime: Python, build: `pip install -r requirements-lock.txt`, start: `python server.py`.
   Health check: `/health`. Альтернатива — Blueprint из `render.yaml`.
3. Получите URL, присвоенный сервису Render. Запишите его без завершающего `/`.
   Для первого запуска до ввода секретов можно задать `SETUP_MODE=true`:
   сервер показывает `setup_required`, а `/mcp` отвечает 503. В этом режиме
   инструменты и авторизация MCP не запускаются, обращения к MessageHelp невозможны.
4. В GitHub Settings → Developer settings → OAuth Apps создайте приложение:
   - Application name: `STELLA MessageHelp MCP`
   - Homepage URL: URL сервиса Render
   - Authorization callback URL: URL сервиса Render + `/auth/callback`
   Это отдельная авторизация MCP, не существующее подключение GitHub к ChatGPT.
5. В Render Environment добавьте переменные:

| Переменная | Значение |
|---|---|
| `PYTHON_VERSION` | `3.12.10` |
| `GITHUB_CLIENT_ID` | Client ID созданного OAuth App |
| `GITHUB_CLIENT_SECRET` | Client Secret созданного OAuth App |
| `FASTMCP_SERVER_AUTH_OAUTH_PROXY_JWT_SIGNING_KEY` | Случайный секрет минимум 32 байта; Blueprint генерирует его сам |
| `ALLOWED_GITHUB_LOGIN` | `Icecrown1` |
| `MESSAGEHELP_TOKEN` | Bearer-токен MessageHelp |
| `MESSAGEHELP_PROJECT_IDS` | Разрешённые ID проектов, через запятую |
| `OAUTH_REDIS_URL` | Internal URL отдельного Render Key Value в том же workspace и регионе |
| `ENABLE_WRITES` | `false` при первом запуске |
| `SETUP_MODE` | `false` после заполнения всех ключей доступа |

`RENDER_EXTERNAL_URL` и `PORT` Render задаёт автоматически. Для другого хостинга задайте `BASE_URL`.
Не загружайте токены, пароли и Client Secret в репозиторий или переписку.
MessageHelp-токен получается через описанный в документации `POST /api/app/user/login`;
срок действия ограничен. Эта версия не хранит пароль и не обновляет токен автоматически.

6. Создайте Key Value в том же workspace и регионе: план 256mb, policy `noeviction`,
   Persistence Mode: Journal + Snapshot, внешний доступ выключен.
   Задайте его Internal URL в `OAUTH_REDIS_URL`.
   Установите `SETUP_MODE=false` и примените переменные через Save and deploy.
   Проверьте `GET /health`: `{"status":"ok","oauth_storage":"redis"}`.
   Отсутствие или недоступность хранилища не переключает сервер на временный диск.
7. Подключите в ChatGPT собственный MCP по URL сервиса + `/mcp`, выбрав OAuth.
   Авторизуйтесь GitHub-аккаунтом `Icecrown1`.
8. Проверьте чтение шаблона. Затем при необходимости задайте `ENABLE_WRITES=true`.

## Инструменты

`list_templates`, `get_template`, `preview_changes`, `update_template`.
Для обновления передайте `expected_revision` из превью и тот же объект `changes`.
Локальные проверки используют только имитацию API и не меняют реальные шаблоны.

## Восстановление потерянной регистрации ChatGPT

Если ChatGPT повторно использует прежний client_id, а сервер отвечает Client Not
Registered, оператор может задать `OAUTH_RECOVERY_CLIENTS`: JSON-массив объектов
с полями `client_id` и `redirect_uri`. Используйте только проверенный прежний ID
и его точный callback из исходной конфигурации/авторизационных запросов владельца.
Не копируйте state, code, challenge, токены или GitHub Client Secret.

Сервер восстанавливает только явно перечисленных public-клиентов с `read:user`
и точным HTTPS callback `chatgpt.com/connector/oauth/...`, сохраняя регистрацию
в зашифрованном OAuth-хранилище. Неизвестные ID по-прежнему отклоняются; конфликты
с существующими регистрациями не перезаписываются. GitHub-вход, согласие, PKCE и
ограничение `ALLOWED_GITHUB_LOGIN` остаются обязательными. Старые access/refresh
токены после потери хранилища не восстанавливаются: пользователь проходит OAuth
ещё раз. После успешного восстановления переменную можно удалить: регистрация
останется в постоянном хранилище.

## Тесты

Python 3.12+, `pip install -r requirements.txt pytest pytest-asyncio fakeredis`, затем `python -m pytest -q`.
Для полной фиксации зависимостей используйте прилагаемый `requirements-lock.txt`.
# Расширенное чтение и лимиты

`get_template` также возвращает справочники услуг, мастеров и категорий записи,
часы оповещений и шаблоны главного меню каналов. Секреты каналов и телефоны
администраторов в этот ответ не включаются. Недоступные справочники отмечаются
как `unavailable` и не мешают чтению самого шаблона.

В `preview_changes` и `update_template` для типа `before` поддерживается
`message_limit_per_hour`: целое число от 1 до 10. Поле изменяет только
`config.limit`, сохраняя другие вложенные параметры. Лимит относится к одному
шаблону; суммарную нагрузку на номер нужно проверять отдельно.


## AstroOrbi: bounded autonomy (2026-11-07)

The owner authorized daily research, analytics-driven content and bounded autonomous
publication. Existing manually approved drafts retain their schedule and workflow.
Set `THREADS_AUTONOMY_ENABLED=true` to allow the new `threads_queue_autonomous` tool.
The default is false. Set it false again to stop autonomous sends without stopping
previously approved manual posts. `THREADS_ENABLE_PUBLISHING=false` stops all Threads sends.

Hard limits (Asia/Yekaterinburg): 2 supplemental root posts/day, 8 total MCP root posts/day,
45-minute root-post spacing; external replies 5/day, 1/author/day, 1/root-post ever,
60-minute spacing. Autonomous schedules must be within 48 hours and 09:00–22:00.
Checks run atomically at queue time and again before an attempted send. Ambiguous
network outcomes are never retried automatically. External targets must first be
returned by keyword search, cached for 48h, and rechecked unchanged before sending.

External replies are independently disabled by default. Enable
`THREADS_EXTERNAL_REPLIES_ENABLED=true` only after checking Meta access. Required
permissions: threads_basic, threads_content_publish, threads_read_replies,
threads_manage_replies, threads_manage_insights; public search additionally requires
threads_keyword_search with approved Advanced Access. threads_profile_discovery is
optional for a later profile-lookup feature; this release uses keyword search only.
Adding permissions to the app does not update an existing token: reauthorize @astro_orbi
and replace THREADS_ACCESS_TOKEN securely in Render. Never put a token in this repository.

`threads_post_insights` saves encrypted daily snapshots (last 90 days) and returns the
previous day's observation and metric changes. These metrics do not measure site visits.

Primary API references:
- https://www.postman.com/meta/threads/request/34203612-b3b2c12a-7ce6-4d86-a3c6-6d31e3b66ea1
- https://www.postman.com/meta/threads/request/34203612-7abb4419-a216-4e06-8027-f9f4fcf8bee9
- https://developers.facebook.com/docs/threads/keyword-search
- https://github.com/fbsamples/threads_api/issues/72
