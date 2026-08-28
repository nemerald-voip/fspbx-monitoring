.PHONY: init validate validate-repository up down logs ps reload-prometheus

init:
	./scripts/init-secrets.sh

validate:
	./scripts/check-config.sh

validate-repository:
	./scripts/check-repository.sh

up: validate
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

ps:
	docker compose ps

reload-prometheus:
	curl --fail --silent --show-error -X POST http://127.0.0.1:$${PROMETHEUS_PORT:-9090}/-/reload
