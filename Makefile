.PHONY: install test lint all serve-minisite minisite-build minisite-down clean

# Bootstrap a venv with eichi installed in editable mode, resolved from
# the committed uv.lock. Idempotent — re-running just re-syncs the venv
# to the lockfile.
install:
	uv sync

# Run the pytest suite.
test:
	.venv/bin/python -m pytest tests/ -v

# Run ruff against the package + tests. CI is non-blocking on lint, but
# fix what you can locally.
lint:
	.venv/bin/ruff check src tests

# Combined lint + test, matching the CI gate.
all: lint test

# Build the minisite image. Build context is the repo root because the
# Dockerfile copies bin/eichi + minisite/. Re-run after editing
# minisite/app.py or minisite/eichi_worker.py.
minisite-build:
	docker build -t eichi-minisite:dev -f minisite/Dockerfile .

# Build + run the minisite under docker compose if a compose file
# exists; otherwise fall back to a bare docker run. The bare path
# expects a host venv at .venv/ and an index DB at
# ~/.local/share/eichi/.
serve-minisite: minisite-build
	@if [ -f minisite/docker-compose.yml ]; then \
		docker compose -f minisite/docker-compose.yml up; \
	else \
		docker run --rm -it -p 8001:8000 \
			-v "$$HOME/repos/eichi:/opt/eichi/repo:ro" \
			-v "$$HOME/.local/share/eichi:/opt/eichi/data:rw" \
			-v "$$HOME/.cache/huggingface:/opt/eichi/hf-cache:ro" \
			-e EICHI_DB=/opt/eichi/data/index.db \
			-e EICHI_PYTHON=/opt/eichi/repo/.venv/bin/python \
			-e HF_HOME=/opt/eichi/hf-cache \
			eichi-minisite:dev; \
	fi

# Tear down minisite (compose mode only).
minisite-down:
	@if [ -f minisite/docker-compose.yml ]; then \
		docker compose -f minisite/docker-compose.yml down; \
	else \
		echo "No minisite/docker-compose.yml — nothing to do (bare docker run uses --rm)."; \
	fi

# Remove the local venv. The index DB at ~/.local/share/eichi/ is
# intentionally left alone.
clean:
	rm -rf .venv eichi.egg-info build dist
