.PHONY: install test check run ingest eval point restore

install:
	pip install -r requirements.txt

test:
	python -m pytest tests -q

check:
	python scripts/check.py

# --proxy-headers is what makes X-Forwarded-* visible. Without it Twilio
# signature validation fails behind Codespaces or any load balancer. No
# ws-ping flags: uvicorn closes the socket when Twilio does not pong, which
# drops live calls.
run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload \
	  --proxy-headers --forwarded-allow-ips="*"

ingest:
	python -m app.ingest $(URL)

# Crawl here, embed elsewhere. For sites that block this machine's network.
archive:
	python -m app.ingest $(URL) --crawl-only --save-html data/pages

from-archive:
	python -m app.ingest --from-dir data/pages

netcheck:
	python scripts/netcheck.py $(URL)

# Is the model reachable at all? Answers that without a phone call.
probe:
	python tools/probe_gemini.py

sweep:
	python tools/sweep_latency.py

eval:
	python scripts/eval.py

point:
	python scripts/webhook.py point $(URL)

restore:
	python scripts/webhook.py restore
