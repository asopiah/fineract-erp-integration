# Apache Fineract 1.15.0, built from source with the GL account external id patch.
#
# Used by `docker compose up` when the image is not present yet, so the stack starts on a machine with Docker only.
# `make fineract-image` builds the same application faster with a local JDK and Fineract's own Jib setup.

FROM eclipse-temurin:21.0.5_11-jdk-jammy AS build
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

ARG FINERACT_TAG=1.15.0
ARG FINERACT_COMMIT=d5636847ac556c30b437254c353f05526d172b97
WORKDIR /fineract
RUN git clone --depth 1 --branch "${FINERACT_TAG}" https://github.com/apache/fineract.git . \
    && test "$(git rev-parse HEAD)" = "${FINERACT_COMMIT}"

COPY gl-account-external-id.patch /tmp/
RUN git apply /tmp/gl-account-external-id.patch

# gradle.properties asks for a 12 GB heap; a container build gets a size it can actually have.
RUN sed -i 's/-Xmx12g/-Xmx3g/' gradle.properties \
    && ./gradlew --no-daemon :fineract-provider:bootJar -x test \
    && cp "fineract-provider/build/libs/fineract-provider-${FINERACT_TAG}.jar" /fineract-provider.jar

# Same runtime base as Fineract's own Jib image (fineract-provider/build.gradle), which uses this tag too.
FROM azul/zulu-openjdk-alpine:21
COPY --from=build /fineract-provider.jar /app/fineract-provider.jar
USER nobody:nogroup
EXPOSE 8080 8443
ENTRYPOINT ["java", "-Duser.home=/tmp", "-Dfile.encoding=UTF-8", "-Duser.timezone=UTC", "-jar", "/app/fineract-provider.jar"]
