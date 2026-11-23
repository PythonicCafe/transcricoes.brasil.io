.DEFAULT_GOAL := help
COMPOSE ?= docker compose
SERVICE ?= main
SHELL := /bin/bash

# Tudo roda no contêiner de desenvolvimento (`make build`), menos `make publish`, que usa o git desta máquina.
# Valores que mudam de máquina para máquina podem ir num `config.mk`, que o git ignora, ou na linha de comando.
# As chaves (YOUTUBE_API_KEY, GROQ_API_KEY, MARITACA_API_KEY) vêm do ambiente; o compose.yml só as repassa.

CSV ?= videos.csv
HOURS ?= 100
EXCLUDE ?= _M8DeaEaUuo,nBlqVDlwHWU
TRANSCRIBER ?= groq
BACKEND ?= maritaca
MODEL ?= S5-nano
# Pausa entre downloads, para o YouTube não bloquear a sequência.
YT_DLP_ARGS ?= --sleep-interval 30 --max-sleep-interval 90
# Por padrão o contêiner não vê o navegador. `make capture COOKIES=1` monta o perfil do Firefox (só leitura) e manda
# o yt-dlp usar os cookies dele; serve para quando o YouTube exige login ("Sign in to confirm you're not a bot").
COOKIES ?=
FIREFOX ?= $(HOME)/.mozilla/firefox
PORT ?= 8000

-include config.mk

ifeq ($(CI),true)
	COMPOSE_RUN_FLAGS := --rm -T
else
	COMPOSE_RUN_FLAGS := --rm -it
endif
COMPOSE_RUN := $(COMPOSE) run $(COMPOSE_RUN_FLAGS)
MAIN_RUN := $(COMPOSE_RUN) $(SERVICE)
OFFLINE_RUN := $(COMPOSE_RUN) offline

ifneq ($(strip $(COOKIES)),)
	FIREFOX_MOUNT := -v "$(FIREFOX)":/home/dev/.mozilla/firefox:ro
	YT_DLP_COOKIES := --cookies-from-browser firefox
endif

.PHONY: bash bash-root build build-no-cache capture container-clean help lint lint-check measure publish select select-dry-run serve site test test-js test-py

bash:                   # Open a Bash shell in the development container
	$(MAIN_RUN) bash

bash-root:              # Open a Bash shell as root in a new development container
	$(COMPOSE_RUN) --user root $(SERVICE) bash

build:                  # Pull the base image and build the development image
	$(COMPOSE) build --pull

build-no-cache:         # Build the development image without cache
	$(COMPOSE) build --pull --no-cache

capture:                # Download, transcribe and summarize what is missing from the CSV (COOKIES=1 to use Firefox cookies)
	$(COMPOSE_RUN) $(FIREFOX_MOUNT) $(SERVICE) python transcripts.py capture --transcriber $(TRANSCRIBER) \
		--backend $(BACKEND) --model $(MODEL) --delete-audio \
		--yt-dlp-args "$(strip $(YT_DLP_COOKIES) $(YT_DLP_ARGS))" $(CSV)

container-clean:        # Stop containers and remove Compose volumes and orphans
	$(COMPOSE) down --volumes --remove-orphans

help:                   # List all make commands
	@awk -F ':.*#' '/^[a-zA-Z_-]+:.*?#/ { printf "\033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST) | sort

lint:                   # Format and lint the code in the development container
	$(OFFLINE_RUN) bash -c 'ruff check --fix . && ruff format .'

lint-check:             # Check formatting and lint without modifying files
	$(OFFLINE_RUN) bash -c 'ruff check . && ruff format --check .'

measure:                # Measure the generated site in Chromium (no network) and write docs-medicao.json
	$(OFFLINE_RUN) python transcripts.py measure docs docs-medicao.json

# Só docs/ e o CSV entram no commit, mesmo que haja outras mudanças na pasta.
publish:                # Commit docs/ and the CSV, then push (runs on this machine, with your git)
	git add -A docs $(CSV)
	git diff --cached --quiet -- docs $(CSV) || git commit -m 'Atualiza `docs/` com novas sessões' -- docs $(CSV)
	git push

select:                 # Add new sessions to the CSV (HOURS=100)
	$(OFFLINE_RUN) python transcripts.py select $(CSV) $(HOURS)

select-dry-run:         # Show which sessions would be selected, without writing anything (HOURS=100)
	$(OFFLINE_RUN) python transcripts.py select --dry-run $(CSV) $(HOURS)

serve:                  # Serve docs/ at http://localhost:8000 (PORT=8000), reachable only from this machine
	$(COMPOSE_RUN) --publish 127.0.0.1:$(PORT):8000 $(SERVICE) python -m http.server --directory docs 8000

# Com rede, porque as capas dos vídeos novos são baixadas do YouTube.
site:                   # Generate the site in docs/
	$(MAIN_RUN) python transcripts.py site --csv $(CSV) --output docs --exclude "$(EXCLUDE)"

test: test-py test-js   # Run all the tests

test-js:                # Run the JavaScript tests of the site with Deno in the development container, without network
	$(OFFLINE_RUN) deno test tests/js/

test-py:                # Run the Python tests with coverage in the development container, without network
	$(OFFLINE_RUN) bash -c 'coverage run -m pytest && coverage report'
