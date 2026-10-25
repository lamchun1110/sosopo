FROM python:3.15.0-slim@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58

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
