FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY data ./data

EXPOSE 8000
# Matches the restaurant build, which has survived real calls.
#
# No --ws-ping-interval or --ws-ping-timeout. Uvicorn pings the client and
# closes the socket when no pong comes back inside the timeout. Twilio's Media
# Streams client does not reliably answer ping frames, so those flags drop a
# live call twenty to forty seconds in, at no particular point in the
# conversation. They look like a keepalive and act as a kill switch.
#
# One worker on purpose: call state lives in the process, so a second worker
# would drop callers onto a process that has never heard of them.
#
# --proxy-headers is what makes X-Forwarded-* visible to the app behind
# Railway's load balancer.
#
# Shell form so $PORT expands: Railway assigns a port and probes the health
# check there.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} \
     --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
