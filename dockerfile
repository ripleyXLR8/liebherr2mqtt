FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/ripleyXLR8/liebherr2mqtt"
LABEL org.opencontainers.image.description="Liebherr SmartDevice HomeAPI to MQTT bridge with Home Assistant style discovery"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1

# Web UI for the optional, experimental mobile-API login (off by default).
EXPOSE 8099

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/
COPY liebherr2mqtt.conf.template /app/liebherr2mqtt.conf.template

# The configuration file is optional: every setting can also be supplied
# through environment variables, which is what the Unraid template does.
ENTRYPOINT ["python", "/app/liebherr2mqtt.py", "/config/liebherr2mqtt.conf"]
