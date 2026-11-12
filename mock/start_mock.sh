#!/usr/bin/env bash
# Mock-Endpunkt (vLLM + LiteLLM nachgebildet) auf http://127.0.0.1:4000, Modell "mock-llm".
cd "$(dirname "$0")/.." && exec python3 mock/mock_server.py "$@"
