# Безопасный сброс задачи

Исправьте `TaskResetService.reset(Task task, ResetCommand command)` в `src/TaskResetService.java`. Java 17, без внешних библиотек. Публичные records и сигнатуру сохраните. Сервис чистый: БД, потоки и HTTP не нужны.

Контракт автономной fixture (он специально спроектирован для бенчмарка):

- Null task/command; null/пустые после trim task.id, task.owner, command.actor, command.requestId; отрицательная task.version; null task.status — `IllegalArgumentException`. Идентификаторы сравниваются буквально. validation выполняется до остальных проверок, даже при повторе запроса.
- Сброс разрешён владельцу задачи или администратору; иначе `SecurityException`. Разрешение проверяется **до** идемпотентности и версии.
- Если command.requestId равен task.lastResetId, вернуть **тот же объект** task: это повтор уже выполненного запроса, expectedVersion и текущее состояние при этом не проверяются.
- В остальных случаях expectedVersion должен равняться task.version; иначе `IllegalStateException`.
- Сбрасывать можно только `FAILED` или `CANCELLED`. `NEW`, `RUNNING`, `DONE` → `IllegalStateException`.
- Новый Task сохраняет id, owner и payload, получает `NEW`, attempt=0, error=null, version+1, lastResetId=requestId. При version=Long.MAX_VALUE — `IllegalStateException`, без переполнения. Поле payload может быть null.
- Исходный объект не меняется. Не добавляйте глобальную историю: повтор определяется исключительно переданным Task. Смена requestId должна снова пройти проверки состояния/версии.

Видимая smoke-проверка: `python3 smoke.py`. Можно добавлять свои тесты. Не читайте файлы вне workspace.
