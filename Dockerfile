FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: run all analyses. Override with `docker run ... <command>` e.g. `charts`, `news --dummy`.
ENTRYPOINT ["python", "-m", "india_wireless_toolkit.cli"]
CMD ["all"]
