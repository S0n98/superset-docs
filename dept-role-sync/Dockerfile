# Department role sync job image. The script is stdlib-only, so this is just
# the official Python base plus one file.
#
#   docker build -t <registry>/dept-role-sync:0.1.0 dept-role-sync/
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="dept-role-sync" \
      org.opencontainers.image.description="Sync Superset roles from a department API"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN adduser -D -H -u 10001 app
# root-owned, world-readable: the app user can run it but not modify it
COPY sync_roles.py /app/sync_roles.py
USER 10001

ENTRYPOINT ["python", "/app/sync_roles.py"]
