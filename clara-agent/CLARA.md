# Clara working in this repository

- Python standard library only in `clara/`; tests in `tests/` with pytest.
- Run `python -m pytest -q` after changes; a test that cannot fail proves nothing.
- Never add a network call outside `clara/providers/openai_local.py` and `clara/server.py`, and never one that is not checked by `clara/netguard.py`.
- Keep the system prompt in `clara/prompts.py` short; no rule lists, no timestamps.
- All text in English.
