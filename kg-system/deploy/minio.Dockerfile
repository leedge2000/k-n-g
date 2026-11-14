FROM alpine@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf

ARG MINIO_RELEASE=RELEASE.2025-04-22T22-12-26Z
ARG MINIO_SHA256=53e2a2cb16c5366ea6fbbc479c19ddb4c6a0948273e752f740fb1fbf27bb817c

ADD --checksum=sha256:${MINIO_SHA256} --chmod=0755 \
    "https://github.com/minio/minio/releases/download/${MINIO_RELEASE}/minio.linux-amd64.${MINIO_RELEASE}" \
    /usr/local/bin/minio

RUN addgroup -S minio && adduser -S -G minio minio \
    && mkdir -p /data && chown minio:minio /data

USER minio
VOLUME ["/data"]
EXPOSE 9000 9001

ENTRYPOINT ["/usr/local/bin/minio"]
