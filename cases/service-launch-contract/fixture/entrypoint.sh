#!/bin/sh
java -Xmx${JAVA_HEAP_MB}m -jar ${APP_JAR:-/opt/service/app.jar} $@
