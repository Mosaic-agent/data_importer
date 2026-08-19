FROM python:3.12-slim

# Copy source into /opt/pkgs/data_importer/ so PYTHONPATH makes it importable.
# /workspace is bind-mounted at runtime (GHA checkout or local dev).
# Install system dependencies required for C extensions (e.g. lxml)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir httpx pytz -r /tmp/requirements-dev.txt

COPY . /opt/pkgs/data_importer/

# data_importer package resolves via PYTHONPATH, not sys.path hacks
ENV PYTHONPATH=/opt/pkgs

WORKDIR /workspace

CMD ["python", "scripts/capture_inav_snapshots.py"]
