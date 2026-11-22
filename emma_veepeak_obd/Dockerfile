FROM ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.21
RUN apk add --no-cache python3 py3-pip dbus bluez-libs && pip3 install --no-cache-dir --break-system-packages bleak==3.0.2 paho-mqtt==2.1.0
COPY rootfs /
RUN chmod a+x /etc/services.d/veepeak/run
