FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY executor.py /opt/autograde/executor.py
RUN chmod 0555 /opt/autograde/executor.py
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["python3", "-I", "/opt/autograde/executor.py"]
