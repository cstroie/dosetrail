# Dosimetrix: one image for every role (web, DICOM receiver, query/retrieve, HIS worker).
# See docs/deploy.md, "Docker". Configuration: /etc/dosimetrix/dosimetrix.toml (mounted);
# data and the site secret: /var/lib/dosimetrix (a volume: back it up).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DOSIMETRIX_CONFIG=/etc/dosimetrix/dosimetrix.toml

RUN useradd --system --home-dir /var/lib/dosimetrix --create-home --uid 10001 dosimetrix

WORKDIR /opt/dosimetrix
COPY pyproject.toml README.md LICENSE ./
COPY dosimetrix ./dosimetrix
RUN pip install . && rm -rf /opt/dosimetrix/dosimetrix /root/.cache

COPY deploy/docker/entrypoint.sh /usr/local/bin/dosimetrix-entrypoint
RUN chmod 0755 /usr/local/bin/dosimetrix-entrypoint

USER dosimetrix
WORKDIR /var/lib/dosimetrix
VOLUME ["/var/lib/dosimetrix"]
EXPOSE 8104 11112

ENTRYPOINT ["dosimetrix-entrypoint"]
CMD ["serve"]
