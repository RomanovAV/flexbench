ARG RUNTIME_IMAGE=registry.example/java17:17.0.12
FROM registry.example/maven:3.9 AS build
WORKDIR /build
COPY . .
RUN mvn package
FROM ${RUNTIME_IMAGE} AS runtime
COPY --from=build /build/target/service.jar /opt/service/app.jar
COPY entrypoint.sh /opt/service/entrypoint.sh
USER 10001
ENTRYPOINT ["/bin/sh", "/opt/service/entrypoint.sh"]
