# Changelog

## 0.1.1 (2026-10-29)

Running on the target workstation (Ubuntu 24.04, kernel 7.0, Landlock ABI 8, Python 3.13, RTX A5000) in place of Hermes Agent and OpenCode.

- Approval service: `[permissions] approver_url` asks a local gate (Hermes smart-approval format, APPROVE / DENY / ESCALATE) after the rules and the mode and before the user; the request carries the project folder and Clara's own warnings. ESCALATE and any error fall through to the user.
- Socket seal (`clara/netseal.py`): seccomp filter under Landlock, no root. Landlock limits TCP by port but does not see UDP, ICMP or Unix sockets, so a DNS query could carry data and `systemd-run --user` could start a process outside every lock. `sandbox.unix_sockets = "seqpacket"` (default) leaves only TCP and Unix SEQPACKET sockets (libcuda needs the latter); `"allow"` also allows Unix stream sockets, which PyTorch DataLoader workers need, and leaves the systemd/D-Bus route open. UDP, ICMP, raw sockets and io_uring are refused in both.
- `sandbox.extra_connect_ports`: more loopback TCP ports for the locked tree (for example a local research gateway).
- Fix: the project root no longer climbs to `$HOME` because Clara's own `~/.clara` looked like a project marker; under `--yolo` that had made the whole home directory writable.
- Fix: `list_files` with the default pattern `**/*` (and `**/*.py`) now finds files in the project root.
- `clara sandbox-check`: 27 checks (new: UDP to the DNS stub, Unix socket, outside name lookup, `systemd-run --user`, socketpair).

Test results, 2026-10-29, on the workstation: `python -m pytest -q` 85 passed, 5 skipped; `clara sandbox-check` 27 of 27. `scripts/netaudit.sh` on real runs against the hospital model hub through a local relay: only loopback destinations (relay and approval gate); `curl`, a UDP DNS query to 8.8.8.8, `getent hosts` and `systemd-run` refused. `clara serve` API test (auth, origin check, two turns, approve/deny, agent question, mode switch, busy/interrupt, session list): 18 of 18. GPU: CUDA training with DataLoader workers, a spawn pool, nibabel and MONAI under the lock with `unix_sockets = "allow"`; with `"seqpacket"` the DataLoader hangs.

## 0.1.0 (2026-10-25)

First version. Built and tested in an Ubuntu-based container (kernel 6.18, Landlock ABI 7, Python 3.13, 2 CPU cores, no GPU).

- Agent loop with ten tools (read_file, write_file, edit_file, list_files, search, bash, ask_user, remember, todo, delegate), rules-first permissions, append-only sessions, record-less mode, skills, CLARA.md, memory notes, local scheduler.
- OpenAI-compatible client for llama-server, Ollama, LM Studio, vLLM: streamed reasoning and tool calls, Hermes/Qwen `<tool_call>` text parser as fallback, context warnings from the server's `n_ctx`, silent-truncation detector.
- Network guard with audit log; Landlock sandbox for writes, TCP connects and (with `read_deny`) reads, for the whole process tree; `clara sandbox-check`.
- Independent review pass (second model instance): rules moved out of the project, Clara's own files never auto-allowed, saved bash rules exclude compound commands, deny rules follow real paths and shell commands, stream tickets instead of a token in the URL, Origin check, body limit, socket-free LAN address lookup.
- Terminal UI (`clara`, `clara -p`), `doctor`, `audit`, `sessions`.
- Phone app: `clara serve` with token auth, server-sent events, approvals and questions from the phone, installable web app, optional local TLS.
- Scripts: `netaudit.sh` (strace, ss, tcpdump), `nft-clara-egress.sh`, `wireshark-filter.txt`, `fresh_install_test.sh`, `make_bundle.sh`.

Test results, 2026-10-25:
- `python -m pytest -q`: 84 passed, 4 skipped (real-model tests, run separately below); one of the 84 is the jsdom page test, run with node and `cd tests && npm install`.
- `clara sandbox-check`: Landlock ABI 7, TCP lock on, 22 of 22 checks passed (writes, TCP, read-only config/skills/rules/token, and kernel read denial through symlink and child shell).
- `scripts/netaudit.sh -- clara --mock -p …`: strace PASS, 0 destinations; with a deliberate `curl 1.1.1.1` in the same tree: FAIL reported with `1.1.1.1:80`.
- Real model, Qwen3-1.7B Q8_0 on 2 CPU cores behind llama-cpp-python 0.3.36 (OpenAI-compatible server): 4 of 4 passed; plus one `--yolo` run under the real Landlock sandbox that wrote a file and whose `curl` to another port failed. File read through a tool call: 106 s (prompt mode), 130 s (native mode). File created in accept-edits mode with the exact content: 153 s. Headless default mode refused the write ("needs approval and no user is available"), file absent: 281 s. Times are wall clock including the model's thinking; the model answered in the first attempt each time.
- Fresh install (`scripts/fresh_install_test.sh`): passed (fresh clone into a temp folder, new venv, `pip install .`, `clara --version`, `clara doctor` reports the unreachable server, `clara --mock -p`, `clara sandbox-check` passed, pytest 77 passed, 5 skipped: 4 real-model, 1 jsdom page test without node_modules).
