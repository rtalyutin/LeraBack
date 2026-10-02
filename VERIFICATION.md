# Проверка общей базы и нескольких салонов

Версия: миграция 4 и соответствующий frontend, подготовленные 02.10.2026.

| Проверка | Фактический результат | Граница |
| --- | --- | --- |
| Python compilation | PASS | Синтаксис Python |
| Backend unittest | 13 PASS, 22 SKIP (35 всего) | 6 SQLite auth units, 2 config/HTTP checks, 3 typed input checks, 2 VK routing checks; интеграционные PostgreSQL tests не выполнены |
| Front syntax и contract tests | 20 PASS | Реальный app.js со stub DOM; не браузер и не live API |
| Independent migration/EAV/RLS/overlap SQL | 55 PASS + fresh/read-only gates PASS | Одна физическая PostgreSQL WASM сессия |
| Native PostgreSQL 16 integration | BLOCKED в этой среде | Нет обычного PostgreSQL/Docker; для выполнения подготовлен compose.test.yml |
| Browser render и full authenticated multi-salon API | Не проверены | Chromium недоступен, cloud browser блокирует localhost |
| Docker build / Timeweb / production migration / VK delivery | Не выполнены | Публикация кода не подтверждает выпуск |

## Независимая SQL-проверка

Доступный движок: PostgreSQL 18.3 WASM (PGlite 0.5.8), официальный pglite-socket 0.2.11, psycopg 3.3.6 ClientCursor. Это реальное выполнение SQL и PL/pgSQL, но не native PostgreSQL 16 и не Timeweb.

Адаптер PGlite использует общую серверную сессию для нескольких TCP клиентов: app.salon_id и TEMP-объекты не разделены. Поэтому неизменённые integration tests приложения в этой среде выполнять как доказательство изоляции/конкурентности нельзя. Независимая проверка использует одну физическую сессию и явное последовательное переключение контекста; результаты не подтверждают параллельные запросы, независимость подключений, TLS, пул соединений, crash durability или restore.

Независимый verifier выполнил 55 наблюдаемых проверок. Дополнительно подтвердил свежую миграцию без тестового каталога и read-only snapshot: запись отклонена с 25006. Файлы исполняемого SQL не изменились во время прогона; последние границы read-only API проверены статически и отдельной SQL-транзакцией. Статическое чтение HTTP/auth/attach не обнаружило подтверждённого критического дефекта; фактическое поведение этих endpoint остаётся непроверенным.

SQL-проверки охватывают перенос существующей версии 3 с явными ID и BYTEA сессий, auth audit, сохранение env policy, повтор миграции, настоящий scoped TEMP view INSERT и RETURNING id, atomic canonical sync, отдельные metadata салонов, FORCE RLS под owner NOSUPERUSER NOBYPASSRLS, отсутствие контекста, scoped FK/reference, typed/required/core guards, общую занятость мастера и локальные клиентские пересечения, соседние интервалы и освобождение после отмены, namespaces диалогов/событий.

## Следующий воспроизводимый этап

Запустите из LeraBack:

```sh
docker compose -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.yml down
```

Пять БД *_test свежие и одноразовые; роль обычная, без обхода RLS. Все 22 интеграционные проверки должны исполниться, а не быть skipped. Затем проверьте оба приложения в браузере с двумя аккаунтами/салонами, смену выбора во время загрузки, отказ доступа чужому салону/ID, отзыв членства, общий мастер на одно время и независимую запись одного телефона в разных салонах.

Исторические результаты предыдущей односалонной версии на PG16/Chrome не переносятся на эту миграцию. До производственного применения нужны результаты native проверки и проверка резервной копии/восстановления. Старый backend несовместим со схемой 4; простой откат кода не откатывает БД.

## Хэши проверенных файлов runtime (SHA-256)

```text
f8056acaa485520c43cfff9511db00d3da13ffc986630cbcc3e2e265fc0a0696  pg_store.py
f279b8044ff45a4c28063fb3144ecfaeee6709ca3b3f24d7a9dc8fee52ab3aa2  booking_core.py
878b0ac02e1f5b38f7be9df490b398fe261f1cc5336ef6eb8f378e78d5b69627  shared_schema.py
1b463c5a4a2a981e1a8f3ce20762bc6a984cf9035ab423bfa4b0fa0e0d739af4  shared_schema.sql
718d4c2faa088dbcd46ab33db5949a081b5685c426909073feba4987834a0d12  constructor_store.py
a1a4bd0649193eb9266a9a11ef41b566db691976f45005a48a35a27e002a2cf3  schema_postgres.sql
80b0dfd08a3f217c74a5c3d97235927b839f97fe93951a21c78cebff352ce768  admin_http.py
617e6c8254ecf36da3f85b27c31c8a8ade26125dcd2fba3671d0032792333b12  admin_service.py
8481e43a6d3711abcdceec45b7a89d5e75deb88e7a907f71059f5cd31f8e5487  admin_auth.py
52ccca9310cbc8ce0cada08eaba53c36fdce91535bb5cff491387215c8d4dd46  salon_service.py
c5123b3e98bc42495c91d3de20fccdb3de256afc059413ceffcc320d72030a0b  vk_config.py
dd19f95b5861799aeb15e0b0925d5401b05db8891944d54dc441d286945336d3  app.py
0212562c7fe77c3b6e1ad629ccdd8cdb31812e19d6fbaaa5d361cf842e290a7b  vk_gateway.py
1d333a82e8a4b043634a1b841b0ad1a2abeae470298b8e8040895e0a295cccb2  startup.py
b37334c12d61f52f3c3fe68861db58d89c358cb007a4e4e9a2d13c1338973a4a  ops.py
```
