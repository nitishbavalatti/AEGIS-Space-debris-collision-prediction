FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ git \
    && rm -rf /var/lib/apt/lists/*

# Install CPU-only torch first (avoids pulling in ~3GB of CUDA/GPU packages)
RUN pip install --no-cache-dir --default-timeout=300 --retries=10 \
    torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .

RUN pip install --no-cache-dir --default-timeout=300 --retries=10 --no-cache -r requirements.txt

COPY . /app/backend

EXPOSE 5000
CMD ["python", "-m", "backend.app"]