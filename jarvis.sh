#!/usr/bin/env bash
# Inicia o JARVIS (Linux). Argumentos são repassados ao main.py.
cd "$(dirname "$0")" && exec .venv/bin/python main.py "$@"
