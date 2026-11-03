# Prefer the project venv, but fall back to the interpreter on PATH so a fresh
# clone can run `make run-fake` without `make venv` (and so the container works).
PYTHON ?= $(shell if [ -x ./.venv/bin/python ]; then echo ./.venv/bin/python; else echo python3; fi)
PIP ?= ./.venv/bin/pip

.PHONY: help venv test test-engine test-provisioning test-led test-os lint check-public \
        doctor run-fake image wheels clean docker-build docker-test docker-run docker-down

help:
	@echo "make venv          create .venv with pinned dev dependencies"
	@echo "make test          run every suite (engine, provisioning, LED, os)"
	@echo "make check-public  fail if internal commercial material is in the tree"
	@echo "make docker-test   build the dev image and run the suite inside it"
	@echo "make docker-run    engine :8095 (fake child) + MJPEG stand-in :8090"
	@echo "make run-fake      run the WSS edge against the fake engine natively"
	@echo "make doctor        handshake against the real a-coder-cli on this machine"
	@echo "make wheels        build the three service wheels into os/image/wheels"
	@echo "make image         build the disk image (Linux host only)"
	@echo "make clean         remove build output and caches"

venv:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

test:
	$(PYTHON) -m pytest

test-engine:
	$(PYTHON) -m pytest services/engine/tests

test-provisioning:
	$(PYTHON) -m pytest services/provisioning/tests

test-led:
	$(PYTHON) -m pytest services/led/tests

test-os:
	$(PYTHON) -m pytest os/tests

lint:
	$(PYTHON) -m ruff check services os scripts dev

check-public:
	$(PYTHON) scripts/check_public_tree.py

# Exercise the real bridge against the fake child: no LLM, no provider auth.
run-fake:
	PYTHONPATH=services/engine/src \
	$(PYTHON) -m openpc_engine --state-dir ./.devstate --log-format text serve \
	  --bind 127.0.0.1:8095 \
	  --engine-command "$(PYTHON) services/engine/tests/fake_engine.py" \
	  --agent-cwd ./.devstate/agent

doctor:
	PYTHONPATH=services/engine/src \
	$(PYTHON) -m openpc_engine --state-dir ./.devstate --log-format text doctor

wheels:
	rm -rf os/image/wheels && mkdir -p os/image/wheels
	$(PIP) wheel --no-deps -w os/image/wheels ./services/engine ./services/provisioning ./services/led

image:
	./os/scripts/build-image

docker-build:
	docker build --tag open-pc-one-dev .

docker-test: docker-build
	docker run --rm open-pc-one-dev make test PYTHON=python3

docker-run:
	docker compose -f docker-compose.dev.yml up --build

docker-down:
	docker compose -f docker-compose.dev.yml down --volumes

clean:
	rm -rf .devstate .pytest_cache os/image/wheels
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
