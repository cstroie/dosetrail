# Dozimetrix: one image for every role (web, DICOM receiver, query/retrieve, HIS worker).
# See docs/deploy.md, "Docker". Configuration: /etc/dozimetrix/dozimetrix.toml (mounted);
# data and the site secret: /var/lib/dozimetrix (a volume: back it up).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN useradd --system --home-dir /var/lib/dozimetrix --create-home --uid 10001 dozimetrix

WORKDIR /opt/dozimetrix
COPY pyproject.toml README.md LICENSE ./
COPY dozimetrix ./dozimetrix
RUN pip install . && rm -rf /opt/dozimetrix/dozimetrix /root/.cache

COPY deploy/docker/entrypoint.sh /usr/local/bin/dozimetrix-entrypoint
RUN chmod 0755 /usr/local/bin/dozimetrix-entrypoint

USER dozimetrix
WORKDIR /var/lib/dozimetrix
VOLUME ["/var/lib/dozimetrix"]
EXPOSE 8104 11112

ENTRYPOINT ["dozimetrix-entrypoint"]
CMD ["serve"]
