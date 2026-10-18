FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --system --uid 10001 --create-home app
COPY --chown=app:app . .
RUN mkdir -p charts reports var && chown -R app:app charts reports var
USER app

# Default: run all offline analyses. Override with `docker run ... <command>` e.g. `charts`, `news --dummy`.
ENTRYPOINT ["python", "-m", "india_wireless_toolkit.cli"]
CMD ["all"]
