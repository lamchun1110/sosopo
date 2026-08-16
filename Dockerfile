FROM python:3.14.7-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

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
