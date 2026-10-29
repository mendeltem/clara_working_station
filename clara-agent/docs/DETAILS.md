# Details

Everything that would make the README long: how the pieces fit, every option, where the defaults come from, and what each test proves.

## How a turn runs

```
you ──► clara (terminal)  or  phone ──► clara serve
             │
             ▼
        Agent.run(text)
             │  messages = [system, …history…, user]      (append-only; prefix stays byte-identical)
             ▼
   POST /v1/chat/completions  stream=true, tools=[…]     (127.0.0.1 only; netguard checks the host)
             │  reasoning_content / content / tool_calls deltas
             ▼
      tool calls?  ──no──► answer, done
             │ yes
             ▼
   Permissions.decide(tool, args)   deny rules → read tools → allow rules → mode → ask you
             │
             ▼
   tool.run(args)   read_file · write_file · edit_file · list_files · search · bash · ask_user · remember · todo · delegate
             │
             └──► result appended as role=tool, back to the model (max_turns = 60)
```

Facts about the server (context size, sleep state, busy slots) come from llama-server's `/props`, `/health` and `/slots`, which are served while the model sleeps and do not wake it. The only request that wakes it is a chat completion.

## Choices taken from measurements in the user's wiki

| Choice | Why (wiki entry) |
|---|---|
| System prompt under 200 words, no rule lists, no timestamps | a list of escalation rules in the system prompt made Qwen3.6 refuse ordinary tasks (rule-lists-make-small-models-refuse); Hermes' 13k-token prompt is re-read on every cache miss (minimal-harness-prompt-size) |
| Append-only history, `/compact` only by hand and with a warning | prompt compaction throws the prefix cache away on a single slot (prefix-cache-single-slot) |
| Context warning from the server's own `n_ctx`; truncation detector from `usage.prompt_tokens` | Ollama keeps `num_ctx/2` tokens from the end and says nothing (ollama-silently-truncates-context) |
| Thinking left on, sampling left to the server | counting needs thinking (counting-needs-thinking); vendor sampling parameters brought no gain (vendor-sampling-params-no-gain) |
| Landlock for writes and TCP, applied before any thread starts | the filesystem was the missing sandbox under yolo; bubblewrap fails on this Ubuntu (landlock-write-lock-for-a-yolo-agent, local-agent-harness-review-2026-10) |
| Deny rules checked before the tool runs, plus a kernel read lock (`read_deny`) | a pre-tool hook is the barrier, a prompt rule is a request (pre-tool-hook-blocks-data-paths-for-remote-model) |
| `--no-record` | Hermes writes every message to its state database with no switch (hermes-keeps-every-conversation-in-state-db) |
| `--wait SECONDS`, errors named instead of dying | every agent crash becomes a lesson; the agent waits instead of closing the window (every-agent-crash-becomes-a-lesson) |
| stdin of shell commands is /dev/null | `opencode run` hangs without `</dev/null` (opencode-run-hangs-on-open-stdin) |
| `/slots` shown in the banner | with one slot a second client waits and looks hung (opencode-against-llama-server-one-slot) |

## Configuration (`~/.clara/config.toml`)

| Key | Default | Meaning |
|---|---|---|
| `model.base_url` | `http://127.0.0.1:8000/v1` | OpenAI-compatible endpoint. Ollama `http://127.0.0.1:11434/v1`, LM Studio `http://127.0.0.1:1234/v1` |
| `model.model` | `""` | model id exactly as `/v1/models` lists it; empty takes the first |
| `model.tool_mode` | `native` | `native` sends `tools` in the request (llama-server `--jinja`, Ollama, vLLM); `prompt` puts the tool list into the system prompt in Qwen's own wording and parses `<tool_call>` text, which works with every server |
| `model.timeout` | `600` | seconds to wait for the first byte (sleeping server, busy slot) |
| `model.temperature` | unset | only sent when set |
| `network.allowed_hosts` | loopback | hosts Clara's process may connect to; a LAN model server must be listed here explicitly |
| `sandbox.mode` | `auto` | `auto` = Landlock with `--yolo`; `on` = always; `off` = never |
| `sandbox.extra_write_paths` | `[]` | more writable folders |
| `sandbox.read_deny` | `[]` | folders no process of Clara may read while the sandbox is on (kernel-enforced, symlink-proof) |
| `agent.max_turns` | `60` | model calls per user message |
| `agent.context_warn_fraction` | `0.85` | warn when the estimated prompt exceeds this share of `n_ctx` |
| `agent.show_reasoning` | `true` | show `<think>` / `reasoning_content` dimmed |
| `serve.host`, `serve.port` | `127.0.0.1`, `8765` | app address; `--lan` overrides the host |

Environment: `CLARA_HOME` (default `~/.clara`), `CLARA_BASE_URL`, `CLARA_MODEL` override the file.

## Command line

| Command | What |
|---|---|
| `clara` | interactive session in the current project |
| `clara -p PROMPT` | one answer on stdout, progress on stderr, exit 0/1 |
| `--mode default\|accept-edits\|yolo`, `--yolo` | permission mode |
| `--sandbox auto\|on\|off` | Landlock |
| `--no-record`, `--resume ID\|last` | sessions |
| `--url`, `--model`, `--tool-mode`, `--wait SECONDS`, `--max-turns N`, `--quiet` | per-run overrides |
| `--allow-tool "bash(pytest*)"` | extra allow rule for this run |
| `-C DIR` | project folder (default: nearest folder with `.clara/`, `CLARA.md` or `.git`) |
| `--mock` | scripted model for trying the interface without a server |
| `clara doctor [--probe]` | config, guard, Landlock, server, model, context; `--probe` sends one request with a tool and measures the system prompt in tokens via `/tokenize` |
| `clara audit [--last N] [--clear]` | the network audit log |
| `clara sessions` | recorded sessions |
| `clara sandbox-check` | 22 Landlock checks in a child process (19 without TCP rules) |
| `clara pair [--tls] [--new-token]` | phone link, token, local CA |
| `clara serve [--lan] [--tls] [--port] [--mode] [--yolo]` | API and app |
| `clara desktop` | the app in a window (`pip install 'clara-agent[desktop]'`) or the URL |
| `clara schedule add --every 2h "prompt"` / `list` / `remove ID` / `run` / `loop` | local background routines, each run is a `clara -p` child with its own session and sandbox |

Slash commands inside a session: `/help /status /mode /think /compact /clear /sessions /resume /tools /skills /audit /memory /exit`.

## Project files

- `CLARA.md` (or `AGENTS.md`) in the project root: instructions that go into the system prompt (kept under 12k characters).
- `~/.clara/projects/<project>.json`: `{"allow": ["bash(pytest*)"], "deny": ["read_file(/media/**)"]}`. Patterns are `tool(glob)` matched against the command or path. Kept outside the project (and read-only under the sandbox) so the model cannot edit its own rules; the file name is the project folder name plus a hash of its full path.
- `.clara/memory.md` and `~/.clara/memory.md`: notes saved with the `remember` tool; the last 40 lines of each are in the system prompt.
- `.clara/skills/<name>/SKILL.md` and `~/.clara/skills/<name>/SKILL.md`: skills in the Claude Code / Hermes layout. Only name, description and path enter the prompt; the model reads the file when it matches.

## Phone app protocol

`clara serve` exposes a JSON API with bearer-token auth (`~/.clara/serve-token`):

| Method and path | Purpose |
|---|---|
| `GET /api/status` | model, server facts, sandbox, guard counters |
| `GET /api/sessions`, `POST /api/sessions` | list, create |
| `GET /api/sessions/{id}` | snapshot: messages, pending approvals, mode |
| `POST /api/sessions/{id}/messages` `{"text"}` | start a turn (409 while busy) |
| `POST /api/sessions/{id}/ticket` | single-use ticket (2 min) for the event stream, so the token stays out of URLs |
| `GET /api/sessions/{id}/events?since=N&ticket=T` | server-sent events: `user text reasoning tool_start tool_result status warning todo permission question turn_end idle mode gap` (`gap`: reload the snapshot) |
| `POST /api/permissions/{id}` `{"answer": "allow\|deny\|allow_session\|allow_persist"}` | answer an approval |
| `POST /api/answers/{id}` `{"answer"}` | answer a question from the agent |
| `POST /api/sessions/{id}/interrupt`, `/mode`, `/compact` | control |
| `GET /api/audit` | the audit log |

The page (`clara/app/index.html`) is one file with no external resources; `sw.js` caches it for the home-screen icon.

## Test status

Last full run: 2026-10-25, Ubuntu-based container, kernel 6.18, Landlock ABI 7, Python 3.13, 2 CPU cores, no GPU. `python -m pytest -q`: 78 passed, 4 skipped (real-model tests need `CLARA_TEST_URL`). Real-model run the same day: 4 of 4 passed, times in the table.

| Claim | Test | Rests on |
|---|---|---|
| A real model reads a file through a tool call, in both tool modes | `tests/test_real_model.py::test_model_reads_a_file_through_a_tool_call` | Qwen3-1.7B Q8_0 on CPU behind llama-cpp-python 0.3.36 (OpenAI-compatible server); 106 s prompt mode, 130 s native mode |
| A real model creates a file in accept-edits mode; headless default mode refuses the write | `test_real_model.py::test_model_writes_a_file…`, `…refuses_writes…` | same setup; 153 s and 281 s |
| Clara handles llama-server's wire format: streamed reasoning, tool_calls deltas, usage, timings, HTTP 400 | `tests/test_fake_server.py` | a simulated llama-server (`tests/conftest.py`), not the binary |
| Requests are append-only with a byte-identical prefix | `test_fake_server.py::test_requests_are_append_only_with_a_stable_prefix` | simulated server records every request |
| Silent context truncation is detected from `usage.prompt_tokens` | `test_fake_server.py::test_silent_truncation…` | simulated Ollama answer |
| External connect, UDP send and name resolution are refused before a packet leaves; loopback works; attempts are logged | `tests/test_netguard.py` | real sockets on this machine |
| Landlock refuses writes and TCP connects outside the lists, also inside a child shell; config, skills, rules and token stay read-only | `tests/test_sandbox.py`, `clara sandbox-check` | the kernel of the machine running the test (22/22 here) |
| Rules first, then the user; deny wins over yolo; outside-project writes ask; Clara's own files never auto-allowed; saved bash rules exclude compound commands; deny rules follow symlinks and shell commands | `tests/test_permissions.py` | unit tests |
| Kernel read denial: a listed folder is unreadable for the process tree, also via symlink and child `cat`, while `/etc`, the project and Python stay readable | `tests/test_sandbox.py`, `clara sandbox-check` | the kernel of the test machine |
| Phone API refuses the token in URLs, single-use stream tickets, cross-site POSTs (403) and bodies over 4 MB (413); model output cannot inject HTML into the page | `tests/test_server_api.py`, `tests/app_page_test.mjs` | in-process server; jsdom |
| Tools: paging, binary detection, unique edits, backups, timeouts, closed stdin | `tests/test_tools.py` | real files and a real shell |
| Phone API: token required, streaming events, approval and question round trip, 409 while busy, interrupt | `tests/test_server_api.py` | the real HTTP server in-process with a scripted model |
| System prompt stays under 200 words; prompt-mode prompt under ~1500 estimated tokens | `tests/test_cli_and_misc.py::test_system_prompt_stays_small` | character estimate; exact count on your server via `clara doctor --probe` |
| `clara` runs from a fresh clone in a new venv with only the README commands | `scripts/fresh_install_test.sh` | run on 2026-10-25: passed (77 tests in the fresh venv, 5 skipped) |

Not tested here, open checks: the page in a real phone browser (its API, its logic in jsdom and its static delivery are tested); Qwen3.8-27B on llama-server with `--jinja` (the user's production server); `read_deny` on a kernel other than 6.18; Windows; the `desktop` window (pywebview); scheduler runs over days. An independent review pass (second model instance, read-only) on 2026-10-25 found self-granted permissions through writable rules and memory files, loose saved bash rules, deny rules that symlinks and shell commands got around, the token in the stream URL and a LAN probe that hit the guard; all are fixed above and covered by tests.

## Requirements

Python 3.11 or newer (3.12 on Ubuntu 24.04). Linux with Landlock for the sandbox (ABI 1 for files from kernel 5.13, ABI 4 for TCP from 6.7). `strace` for `scripts/netaudit.sh`; `tcpdump` and root for `--pcap`; `openssl` for `clara pair --tls`; `qrencode` for a QR code. No GPU needed by Clara; the model server has its own requirements.
