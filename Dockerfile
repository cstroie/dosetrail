# DoseTrail: one image for every role (web, DICOM receiver, query/retrieve, HIS worker).
# See docs/deploy.md, "Docker". Configuration: /etc/dosetrail/dosetrail.toml (mounted);
# data and the site secret: /var/lib/dosetrail (a volume: back it up).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DOSETRAIL_CONFIG=/etc/dosetrail/dosetrail.toml

RUN useradd --system --home-dir /var/lib/dosetrail --create-home --uid 10001 dosetrail

WORKDIR /opt/dosetrail
COPY pyproject.toml README.md LICENSE ./
COPY dosetrail ./dosetrail
RUN pip install . && rm -rf /opt/dosetrail/dosetrail /root/.cache

COPY deploy/docker/entrypoint.sh /usr/local/bin/dosetrail-entrypoint
RUN chmod 0755 /usr/local/bin/dosetrail-entrypoint

USER dosetrail
WORKDIR /var/lib/dosetrail
VOLUME ["/var/lib/dosetrail"]
EXPOSE 8104 11112

ENTRYPOINT ["dosetrail-entrypoint"]
CMD ["serve"]
