.PHONY: help install run lint clean

help: ## Zeige diese Hilfe
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Installiere Python-Abhängigkeiten
	pip install -r requirements.txt

run: ## Starte WaveletSpect (JACK muss laufen)
	python3 waveletspect.py --connect

run-dummy: ## Starte mit virtuellem JACK (Dummy-Driver)
	jackd -d dummy -r 48000 -p 1024 &
	sleep 2
	python3 waveletspect.py --connect
	killall jackd 2>/dev/null || true

lint: ## Syntax-Check
	python3 -c "import ast; ast.parse(open('waveletspect.py').read()); print('Syntax OK')"

clean: ## Aufräumen
	rm -rf __pycache__ *.pyc .pytest_cache dist build *.egg-info
	find . -name "*.pyc" -delete
