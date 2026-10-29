# Installation

## Checked environment

Ubuntu-based Linux container, kernel 6.18 with Landlock ABI 7, Python 3.13.17, 2 CPU cores, no GPU, 2026-10-25. Target machine: Ubuntu 24.04 workstation with kernel 7.0 (Landlock ABI 8), Python 3.12, llama-server on port 8000.

## Dependencies

Runtime: none (Python standard library only).
Tests: `pytest>=7`.
Optional: `pywebview>=5` for `clara desktop`; `strace`, `tcpdump`, `openssl`, `qrencode` for the scripts.

## Steps

```bash
git clone https://github.com/mendeltem/clara-agent
cd clara-agent
python3 -m venv ~/.clara-venv
~/.clara-venv/bin/pip install .
~/.clara-venv/bin/clara --version
```
Put `~/.clara-venv/bin` on your PATH, or `ln -s ~/.clara-venv/bin/clara ~/.local/bin/clara`.

First run creates `~/.clara/config.toml` with comments for every option. Then:

```bash
clara doctor            # server reachable? model? context? sandbox?
clara sandbox-check     # Landlock holds on this kernel?
```

## Model server

Clara needs an OpenAI-compatible chat endpoint on your own machine. Any one of:

```bash
# llama.cpp (native tool calls need --jinja; one slot keeps the prefix cache; sleep frees the GPU when idle)
llama-server -m Qwen3.8-27B-Q4_K_M.gguf --port 8000 --jinja -np 1 -c 81920 --sleep-idle-seconds 300

# Ollama (set a large num_ctx in the Modelfile; Ollama truncates silently above it)
ollama serve            # then base_url = "http://127.0.0.1:11434/v1", model = "qwen3.6:35b"
```

Getting a model file is the one step that needs the internet, done once and outside Clara; Clara itself never downloads anything.

## Tests

```bash
~/.clara-venv/bin/pip install pytest
~/.clara-venv/bin/python -m pytest -q                                   # 84 tests, no model needed
CLARA_TEST_URL=http://127.0.0.1:8000/v1 ~/.clara-venv/bin/python -m pytest -q -s tests/test_real_model.py   # with your server
scripts/fresh_install_test.sh                                            # fresh clone + new venv + README commands
```

## Uninstall

`rm -rf ~/.clara-venv ~/.clara` and the `.clara/` folders in projects you used it in.
