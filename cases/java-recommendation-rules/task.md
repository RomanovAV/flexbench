# Выбор рекомендаций по составным правилам

Исправьте `RecommendationSelector.select(List<Candidate> candidates, List<Rule> rules, Instant now, int limit)` в `src/RecommendationSelector.java`. Java 17, без библиотек; публичные records и сигнатуру сохраните.

Это автономный контракт для бенчмарка, а не реконструкция производственных правил рекомендаций.

- candidates/rules/now не null, limit >= 0, иначе `IllegalArgumentException`.
- У Rule поля taskType/source — непустые после trim строки, quota >= 0. Null Rule или повтор составного ключа `(taskType, source)` — `IllegalArgumentException`. Проверять все правила даже при limit=0/пустом candidates. Идентификаторы сравниваются буквально.
- Null Candidate и кандидатов с null/пустыми id/taskType/source, null dueAt пропускать.
- Кандидат подходит, если есть правило для **обоих** taskType и source, quota > 0 и либо dueAt >= now, либо правило разрешает carryOver. Граница now включена.
- Сначала фильтрация, затем дедупликация по id: среди подходящих записей оставить максимальную revision; при равенстве — первую во входе. Неподходящий кандидат не вытесняет подходящий с тем же id.
- Победителей сортировать: priority по убыванию, dueAt по возрастанию, id через String.compareTo. Без вычитания int/long, которое может переполниться. revision и priority могут иметь любое long/int значение.
- Проходите отсортированный список, принимая кандидата, если ещё не достигнут limit и quota его составного ключа. Пропущенный из-за quota элемент не расходует limit. Общий счётчик на taskType вместо пары недопустим.
- Вернуть список выбранных Candidate, не меняя исходные коллекции. Квоты относятся только к одному вызову; глобального состояния нет.

Видимая smoke-проверка: `python3 smoke.py`. Дополнительные тесты приветствуются. Не читайте файлы вне workspace.
