.PHONY: install test check run ingest eval point restore

install:
	pip install -r requirements.txt

test:
	python -m pytest tests -q

check:
	python scripts/check.py

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload \
	  --ws-ping-interval 20 --ws-ping-timeout 20

ingest:
	python -m app.ingest $(URL)

# Crawl here, embed elsewhere. For sites that block this machine's network.
archive:
	python -m app.ingest $(URL) --crawl-only --save-html data/pages

from-archive:
	python -m app.ingest --from-dir data/pages

netcheck:
	python scripts/netcheck.py $(URL)

eval:
	python scripts/eval.py

point:
	python scripts/webhook.py point $(URL)

restore:
	python scripts/webhook.py restore
