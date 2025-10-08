SHELL := /bin/bash

.PHONY: setup
setup:
	pyenv local 3.13.5
	python -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r requirements_bst.txt
