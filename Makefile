PY ?= python3
PORT ?= 8000

.PHONY: serve uploader watch list test backtest

serve:
	PORT=$(PORT) $(PY) main.py

uploader:
	$(PY) uploader.py

watch:
	$(PY) watch.py

list:
	$(PY) watch.py list

test:
	pytest -q

backtest:
	$(PY) backtest.py
