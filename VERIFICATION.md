# Проверка самостоятельного салона и подключения ВК — 2026-10-06

Текущий кандидат основан на LeraBack `bcb69b4b857c397a13ea78da93cf5aca65ba9bac` и LeraFront `a4afcfa3c094fa30b10906f560bcda1af31dc78d`. Ни production-БД, ни действующее сообщество ВК не менялись.

- Backend: синтаксис Python — PASS; 26 локальных unit-проверок — PASS. Из 62 обнаруженных тестов 36 интеграционных без native тестовой БД пропущены явно.
- Дополнительный последовательный прогон `tests/test_vk_connections.py`: **13/13 PASS** на встроенном PostgreSQL 18.3 (PGlite 0.5.8), роль NOSUPERUSER/NOBYPASSRLS проверена через фактический SET ROLE. Подтверждены создание/членство/повтор запроса, переименование в канонической модели, HTTP auth/CSRF/область салона, зашифрованные ключи, подтверждение Callback, readback после неизвестного create, отсутствие слепых дубликатов, изоляция и отключение старой очереди, отказ одному получателю без остановки остальных, восстановление после ошибки ключа. VK API заменён контролируемым двойником; отправок людям нет.
- Независимый проверяющий выполнил 16 групп SQL/HTTP сценариев на финальном кандидате — PASS, включая публичный Callback, политику салона из БД, дедупликацию, горячую доставку, подавление legacy-путей, отказ одному получателю, восстановление после ошибки ключа и отзыв членства во время проверки ВК. SHA runtime-файлов до и после прогона совпали. Frontend независимо прошёл 85/85 контрактных проверок. Невоспроизведённых продуктовых блокеров этого локального прогона не осталось.
- Frontend: контрактные тесты выполняют фактический app.js с DOM/fetch двойниками; покрывают создание при пустом списке салонов, повтор UNKNOWN без нового action_key, редактирование, переключение салона/выход при запросах и очистку ключа ВК перед fetch. Точные результаты в README LeraFront.

PGlite сохраняет одну физическую серверную сессию. Только в локальном harness применены SET ROLE и очистка старых TEMP views перед логическим подключением; runtime pg_store.py не менялся. Этот прогон **не подтверждает** native login, параллельные сессии/advisory-гонки, crash durability, TLS или native RLS при конкурентности. Для обычного PostgreSQL 16 обновлён compose.test.yml: семь свежих *_test БД, включая VK_CONNECTION_TEST_DATABASE_URL. Docker/native PostgreSQL здесь не запущены.

Playwright launch заблокирован отсутствующим Chromium. Попытка загрузить Chromium завершилась ошибкой неполного ZIP; рендер мобильного/десктопного интерфейса не подтверждён. Реальные VK Callback/delivery, OAuth, Timeweb и применение миграции в рабочей БД не проверены. Приёмка кода не является подтверждением выпуска.

Ниже сохранены исторические наблюдения предыдущих версий; их SHA и counts не подтверждают текущую ревизию.

---

# Проверка общей базы, нескольких салонов и отдельной SQL-схемы

Текущая правка сокращает обязательные переменные запуска до DATABASE_URL и bootstrap SALON_ADMIN_PASSWORD. Добавлены runtime defaults и CSRF от случайной сессии без обязательного глобального ключа. SQL/адаптер/prepare_database не менялись. Рабочая БД tg-mcp и Timeweb не изменялись.

| Проверка | Фактический результат | Граница |
| --- | --- | --- |
| Python syntax/import | PASS | AST parse Python и импорты в unittest |
| Backend unittest | 22 PASS, 26 SKIP (48 всего), без ошибок | PostgreSQL integration tests пропущены без тестовых DATABASE_URL |
| Independent minimal-config HTTP/auth | 53 исполненных наблюдения: HTTP 30, отдельные процессы 9, config 14 | Реальные loopback HTTP/auth функции с SQLite connect seam; tenant/domain stubs, startup boundaries mocked |
| Front syntax и contract tests | Предыдущие 20 PASS | Проверенный app.js не менялся; stub DOM, не браузер и не live API |
| Independent namespace/migration/EAV/RLS/UPSERT SQL | Предыдущий прогон: 115 успешных наблюдений в 7 сценариях на commit 38296ae | SQL/адаптер/миграции не изменились; текущий HTTP/config runtime отличается |
| Native PostgreSQL 16 integration | BLOCKED в этой среде | Подготовлен compose.test.yml с шестью одноразовыми БД |
| Browser render и full authenticated multi-salon API | Не проверены | Полный tenant API, браузер и конкурентные запросы не исполнялись |
| Docker build / Timeweb / production migration / TLS / VK delivery | Не выполнены | Публикация кода не подтверждает выпуск |

## Текущая авторская проверка конфигурации

Команда: `PYTHONPATH=/tmp/lera-test-deps python -B -m unittest discover -s tests`, Python 3.12. Четыре новых теста проверяют минимальные входы app.main для bootstrap/повтора (runtime boundaries mocked), значения по умолчанию и невалидную policy, CSRF разных сессий/поддельный токен и стабильность вычисления в новом Python-процессе, прежний explicit-secret режим. Существующие SQLite auth units подтверждают сохранение пароля/сессии и запрет автоматического восстановления отключённого аккаунта; это не PostgreSQL acceptance.

## Независимая проверка минимальных переменных

53 исполненных наблюдения подтверждают работу без глобального CSRF-ключа, отдельные токены для разных сессий одного/разных аккаунтов, отсутствие cookie в JSON, отказ с отсутствующим/чужим/поддельным CSRF или cookie, logout и отзыв сессий при смене пароля, прежний explicit-secret режим и получение нового токена после его удаления. Сессия и точное значение CSRF сохранились между двумя отдельными Python-процессами HTTP сервера; прежний токен позволил выйти, а отозванный cookie затем получил 401.

Это фактические admin_http.Handler/server и admin_auth функции на loopback HTTP с явной заменой connect на синтетическую SQLite auth fixture. Доступ к салонам/политике и защищённая domain mutation заменены ограниченными stubs. Конфигурация app.main исполнена с mocked prepare_database/server: это проверяет требуемые входы и валидацию, а не подключение PostgreSQL или первый реальный деплой. Docker ENV schema lera и public override тестового compose проверены только статически; это ещё два отдельных source observations, не исполнение Docker.

Формы/text/plain без допустимого JSON и CSRF отказали, cross-origin без/с чужим токеном отказал, OPTIONS не выдал CORS-разрешения. Диагностический запрос с уже известными валидными cookie/CSRF и чужим Origin был принят: сервер не проверяет Origin самостоятельно. Это сохранённая граница текущего API; фактический браузер/preflight и Nginx здесь не запускались, browser exploit не заявляется. Non-ASCII токен получил 422 без mutation; обычные ошибочные CSRF — 403.

Во всех трёх независимых сценариях before/after/current совпали с freeze 25 файлов, manifest SHA-256 `7b78469aeb2c62683d4e245628734528885476baf7f0d1eca6b24139d8f10151`. compose.test.yml проверен отдельно: `448cc2f037a6170fbf43ba622e9cee2eca8a4cc59b4377648909a43834e64616`. Полный PostgreSQL tenant API, concurrency, браузер, TLS, Docker, Timeweb и backup/restore остаются непроверенными.

## Ранее выполненные проверки SQL

Движок: PostgreSQL 18.3 WASM (PGlite 0.5.8), pglite-socket 0.2.11, psycopg 3.3.6 ClientCursor. Использовались фактические адаптер, миграции, startup, ops и seed приложения.

| Сценарий | Успешные наблюдения |
| --- | ---: |
| Перенос legacy v3 → v4, повтор миграции, EAV/RLS и пересечения | 61 |
| Свежая схема без тестового каталога | 3 |
| Bootstrap и повтор startup | 6 |
| Явный тестовый seed | 6 |
| Ошибки имени/доступа схемы и scoped UPSERT | 25 |
| Обновление существующей v4 с воспроизведением исходного 23505 | 5 |
| Отказ для будущей версии и read-only health/verify | 9 |
| **Всего** | **115** |

Миграция, TEMP views, INSERT RETURNING и функции разрешают объекты выбранной `lera` без fallback на `public`. Сохраняются ID, пароли, сессии, политика, каталог и миграционные строки. Проверены отдельные данные салонов, FORCE RLS под владельцем NOSUPERUSER NOBYPASSRLS, отсутствие контекста, scoped FK и ссылки, типы и обязательные EAV поля, глобальная занятость общего мастера и локальные пересечения клиентов. Эти сценарии исполнялись последовательно.

Некорректные, системные и инъекционные имена, отсутствующая схема и отсутствие USAGE отклоняются до DDL приложения. Версия выше 4 не мигрируется; health и verify требуют ровно 4 и не изменяют данные.

Исходная ошибка повторного `vk_dialog` UPSERT (23505) фактически воспроизведена архивной функцией в существующей v4. Повторная миграция обновляет функции/триггеры и исправляет её, сохраняя аккаунты и данные. INSERT с ON CONFLICT повторно использует каноническую сущность; пропущенные generated-ID вставки не оставляют пустых сущностей.

Полные снимки relations, functions, owners, ACLs и строк `public`, отдельной TG-заглушки и каталога расширения движка не изменились. Реальные расширения рабочей БД tg-mcp не инспектировались.

## Ограничения доказательств

PGlite использует одну физическую серверную сессию на движок; логические переподключения её сохраняют. Поэтому проверки не подтверждают независимость подключений и TEMP/context, native login, параллельные RLS/booking гонки, пул соединений, TLS, crash durability или восстановление резервной копии. Поведение полного HTTP/auth/attach API не подтверждено этими SQL-наблюдениями.

Все семь сценариев предыдущей namespace-версии прошли с официальным параметром max-connections=16; вызовы приложения оставались последовательными. В них совпали SHA-256 всех 22 runtime-файлов до и после прогона. Итоговый прежний manifest SHA-256: `8c9aa5418a4bc14d1c4b1e719e1ea55a6808edc1b2088249ea00911410e298d6`. Теперь изменены app.py/admin_http.py/admin_auth.py и добавлен runtime_config.py; прежние SHA и SQL-наблюдения не подтверждают новое HTTP/config поведение.

## Следующий воспроизводимый этап

Запустить из LeraBack:

```sh
docker compose -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.yml down
```

Шесть БД `*_test` свежие и одноразовые; роль обычная, без обхода RLS. Все 26 интеграционных тестов должны исполниться, а не быть skipped. Затем проверить оба приложения в браузере с двумя аккаунтами/салонами, выбор салона во время загрузки, отказ доступа чужому салону/ID, отзыв членства, общий мастер на одно время и независимую запись одного телефона в разных салонах.

Перед рабочим применением нужны результаты обычного PostgreSQL, проверки подключения к целевой БД и резервного копирования/восстановления. Старый backend несовместим со схемой 4; откат кода не откатывает БД. Резервная копия общей базы затрагивает tg-mcp и Lera.

## SHA-256 предыдущей namespace-версии (исторический прогон)

```text
8481e43a6d3711abcdceec45b7a89d5e75deb88e7a907f71059f5cd31f8e5487  admin_auth.py
80b0dfd08a3f217c74a5c3d97235927b839f97fe93951a21c78cebff352ce768  admin_http.py
de1c511b674868c9fc9917baf8ecabdf42a1d85adfe8bb1aa3db9f23af381ccf  admin_service.py
dd19f95b5861799aeb15e0b0925d5401b05db8891944d54dc441d286945336d3  app.py
39d058b93b8c71b23366878ab307e223774b76db242d39ba092f5041780e4b66  booking_core.py
cd7ae1433f04f6690b5ea95745cfb709b278d1ba3371ea2f7b5860d54a20b7b1  constructor_store.py
ac3d83ac06379b0e997939615bfd9aa21a7c956e46b0d9e3bb05d18eef189ad5  database_namespace.py
a9c051cbf22ffd0573f837ad02a07588d5a11bfa5f32c5b21a485f3bb6bd3bfd  initialize.py
77b042ae32e6892f7633edcff6603390f74039f0f0322d186cdbd1c3275b17e6  manage_salons.py
67f4cf6d45a2515d0eb80f3428f5eb4971be0ad77df67d72b9c445af5d321a02  ops.py
d2c1679f794c83a3102c6eeee730f0dc6c25a8803d915dcf6397374c389884d8  pg_store.py
1297939e86a6440e4281563812a7d4197849164664845a10144afc2546783f5d  provision_admin.py
0c175d72e29a310f04d80312899db032c1fc13687090725550c44792e218273a  salon_service.py
7284caa065a952da9e6d048028290c68dba45c04e577e7dacf9323fa80abeee1  schema_postgres.sql
33489b4646ee46eacbfb5bc939fa5af087b13dc703e6cbd8c9caf54d05ee20ec  seed_starter.py
b7aa544e1f8e1704bf051438ed3093357e516f410526f7b949652adb4b054be6  send_outbox.py
f02045b9e95d6998f60aa440cad815cd18e2c155c486672e4c8dac75e22f9da5  shared_schema.py
00a1dd936c623b4b0b6c2897b688122363eeb37d30ab4ccec91f87d5c78510c1  shared_schema.sql
1d333a82e8a4b043634a1b841b0ad1a2abeae470298b8e8040895e0a295cccb2  startup.py
c5123b3e98bc42495c91d3de20fccdb3de256afc059413ceffcc320d72030a0b  vk_config.py
0212562c7fe77c3b6e1ad629ccdd8cdb31812e19d6fbaaa5d361cf842e290a7b  vk_gateway.py
c9f53d2af92b83e1519c4d11031b16a63d3ce1e174b17c03de2a9eb98886a728  vk_sender.py
```
