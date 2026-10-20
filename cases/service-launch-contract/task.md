# Согласованная упаковка и запуск сервиса

Исправьте Dockerfile, pom.xml, deploy.env и entrypoint.sh. Это автономная fixture класса упаковки из example-project и обновления базовых образов из frontend/backend-обзора. Координаты/версии вымышленные. Maven и Docker скачивать или запускать не требуется.

1. Образ запуска должен быть `registry.example/java17:17.0.13` во всех трёх местах: default ARG RUNTIME_IMAGE в Dockerfile, property runtime.image в pom.xml (используется jib.from.image) и RUNTIME_IMAGE в deploy.env. Сохраните build-stage `registry.example/maven:3.9`, цепочку `FROM ${RUNTIME_IMAGE} AS runtime`, остальные настройки Maven, APP_PORT=8080, COPY артефакта в `/opt/service/app.jar`, USER 10001, ENTRYPOINT для `/opt/service/entrypoint.sh`.
2. entrypoint.sh — POSIX sh. Команда: JAVA_BIN (по умолчанию `java`), затем необязательный `-Xmx${JAVA_HEAP_MB}m`, затем `-jar`, APP_JAR (по умолчанию `/opt/service/app.jar`), затем все входные аргументы без потерь/разбиения, включая пустые и содержащие пробелы. Пустые JAVA_BIN/APP_JAR означают default. Пути могут содержать пробелы.
3. Если JAVA_HEAP_MB задан и не пуст, он должен соответствовать `[1-9][0-9]*`. Иначе завершиться с кодом 2 **до запуска java**. При пустом/отсутствующем значении heap-флаг не передавать. Не интерпретируйте значения env и аргументов как shell-код, не используйте eval.
4. Замените shell процессом java (`exec`), чтобы сигналы доставлялись ему напрямую. Код завершения java сохраняется. Не меняйте окружение вызывающего процесса и не создавайте побочных файлов.

Видимая smoke-проверка: `python3 smoke.py` (подставляет поддельный java, настоящую JVM не запускает). Можно добавлять свои тесты. Не читайте файлы вне workspace.
