FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY agentlab ./agentlab
COPY data ./data
COPY config ./config

RUN useradd --create-home --uid 10001 agent && mkdir -p /tmp/agentlab && chown agent /tmp/agentlab
USER agent

CMD ["python", "-m", "agentlab.scheduler"]
