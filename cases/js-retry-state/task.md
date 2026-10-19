# Повтор операции в UI

Исправь функции в `src/retry-state.mjs`. Это логика состояния карточки операции; внешние зависимости не нужны.

Состояние: `{status, requestId, error, attempts}`. Статусы: `idle`, `pending`, `failed`, `succeeded`. `attempts` — число начатых попыток, целое неотрицательное.

Контракт:

- `canRetry(state)` возвращает true только для `failed`.
- `START` с непустым строковым `requestId` принимается только из `idle` или `failed`. Устанавливает `pending`, новый requestId, error=null и увеличивает attempts на 1.
- Повторный START во время pending и START после succeeded игнорируются.
- `SUCCESS` и `FAILURE` принимаются только в pending и только для совпадающего requestId. Поздний ответ старого запроса не меняет состояние.
- SUCCESS устанавливает succeeded и error=null. FAILURE устанавливает failed и error из события; если error не непустая строка, использовать `Unknown error`.
- SUCCESS/FAILURE сохраняют requestId и attempts. Неизвестное или null-событие игнорируется.
- При любом игнорируемом событии возвращается исходный объект. Не изменяй state и event на месте.
- Дополнительные поля state сохраняются. Входное state всегда корректно.

Сохрани именованные exports. Можно добавить свои тесты и запускать их через Node. Эта fixture проверяет логику UI, а не компоненты React.
