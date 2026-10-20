# Индексы для чтения активностей

Исправьте `migration.sql`: добавьте идемпотентную миграцию индексов для двух запросов из `queries.py`. Доступны Python 3.9+ и sqlite3 из стандартной библиотеки; внешних зависимостей нет. Это уменьшенная SQL-fixture по классу оптимизации БД, не воспроизведение PostgreSQL/Liquibase из рабочего сервиса.

Схема v1 в `schema.sql`. Основные запросы:

1. `SELECT id, scheduled_at FROM activities WHERE owner_id=? AND status='PLANNED' AND scheduled_at>=? ORDER BY scheduled_at, id LIMIT ?`
2. `SELECT id FROM activities WHERE owner_id=? AND status=? AND scheduled_at>=? AND scheduled_at<? ORDER BY scheduled_at, id LIMIT ?`

Требования:

- Выполнение `connection.executescript(migration_sql)` на схеме v1 создаёт необходимые индексы; повторное выполнение безопасно и не создаёт дубликатов.
- После миграции SQLite с `PRAGMA automatic_index=OFF` должен выполнять **оба** запроса с `SEARCH` по составному индексу, ограничивая owner_id, status и диапазон scheduled_at. Без полного SCAN activities и без временной B-tree для сортировки. Один индекс может обслуживать оба запроса; имена индексов произвольные.
- Уникальный первичный ключ id и существующий `idx_activities_external_id` сохраняются. Данные, значения и набор столбцов activities не меняются, нельзя добавлять таблицы, представления, триггеры или переписывать queries.py/schema.sql. Нельзя удалять прежние индексы. Добавлять можно только неуникальные индексы activities.
- Оба запроса после миграции возвращают те же строки в том же порядке; новые записи и повторы external_id должны по-прежнему вставляться. Результаты и лимиты нельзя подменять хардкодом.

Видимая проверка: `python3 smoke.py`. Она выводит EXPLAIN QUERY PLAN и проверяет базовый выбор индекса, но не заменяет приёмку. Исправляйте только migration.sql. Не читайте файлы вне workspace.
