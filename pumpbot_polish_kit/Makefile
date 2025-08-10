.PHONY: run docker-build docker-run fmt

run:
	python webhook_sniper_bot.py

docker-build:
	docker build -t pumpbot:latest .

docker-run:
	docker run --rm -it --env-file .env pumpbot:latest

fmt:
	python -m pip install black && black .
