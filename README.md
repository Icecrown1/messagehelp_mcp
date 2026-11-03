# STELLA MessageHelp MCP

Первая версия для Render Free. Редактирует существующие шаблоны интеграции Altegio:
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
  После пробуждения повторите подключение. Данные OAuth на бесплатном диске
  могут теряться при перезапуске: тогда потребуется повторная авторизация.

## Развёртывание

1. Используйте отдельный GitHub-репозиторий `messagehelp_mcp`.
   Загрузите в его корень содержимое этой папки, включая `.gitignore`.
2. В Render создайте Web Service из этого репозитория, выберите **Free**.
   Runtime: Python, build: `pip install -r requirements-lock.txt`, start: `python server.py`.
   Health check: `/health`. Альтернатива — Blueprint из `render.yaml`, также Free.
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
| `ENABLE_WRITES` | `false` при первом запуске |
| `SETUP_MODE` | `false` после заполнения всех ключей доступа |

`RENDER_EXTERNAL_URL` и `PORT` Render задаёт автоматически. Для другого хостинга задайте `BASE_URL`.
Не загружайте токены, пароли и Client Secret в репозиторий или переписку.
MessageHelp-токен получается через описанный в документации `POST /api/app/user/login`;
срок действия ограничен. Эта версия не хранит пароль и не обновляет токен автоматически.

6. Установите `SETUP_MODE=false` и примените переменные через Save and deploy.
   Проверьте `GET /health`: `{"status":"ok"}`.
7. Подключите в ChatGPT собственный MCP по URL сервиса + `/mcp`, выбрав OAuth.
   Авторизуйтесь GitHub-аккаунтом `Icecrown1`.
8. Проверьте чтение шаблона. Затем при необходимости задайте `ENABLE_WRITES=true`.

## Инструменты

`list_templates`, `get_template`, `preview_changes`, `update_template`.
Для обновления передайте `expected_revision` из превью и тот же объект `changes`.
Локальные проверки используют только имитацию API и не меняют реальные шаблоны.

## Тесты

Python 3.12+, `pip install -r requirements.txt pytest pytest-asyncio`, затем `python -m pytest -q`.
Для полной фиксации зависимостей используйте прилагаемый `requirements-lock.txt`.
