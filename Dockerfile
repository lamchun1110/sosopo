FROM python:3.14.6-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends default-mysql-client postgresql-client \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app /app/app
COPY docs /app/docs
COPY app/assets /app/docs/assets
COPY scripts /app/scripts

RUN useradd --create-home --uid 10001 sosopo \
    && mkdir -p /data \
    && chown -R sosopo:sosopo /app /data

USER sosopo
EXPOSE 8080
CMD ["python", "/app/app/server.py"]
