FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY cdeh ./cdeh

# Install CDEH + minimal cloud SDKs (MinIO/S3 via boto3).
# Azure / paramiko / pymysql / pyarrow are optional extras — uncomment
# the appropriate line below when you need them.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && pip install --no-cache-dir boto3 pyarrow

# Azure (optional):
# RUN pip install --no-cache-dir azure-storage-blob azure-core

# SFTP (optional):
# RUN pip install --no-cache-dir paramiko

# MySQL (optional):
# RUN pip install --no-cache-dir PyMySQL

ENV HOME=/home/cdeh
RUN useradd -m -u 1000 cdeh && mkdir -p /home/cdeh/.cdeh && chown -R cdeh:cdeh /app /home/cdeh
USER cdeh

EXPOSE 8080
ENTRYPOINT ["cdeh"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]