# Clara

Clara is a coding agent for your own computer: a terminal tool like Claude Code and a phone app over WLAN, driven by a local model server, with no network path anywhere else.

```
 phone (WLAN) ──► clara serve ──┐
                                │        ┌──────────────┐
 terminal: clara ───────────────┼──────► │ model server │  127.0.0.1:8000  (llama-server, Qwen3.8-27B)
                                │        └──────────────┘
                          Clara agent
                 tools: read, write, edit, search, bash …
                 permissions: rules first, then you
                 sandbox: Landlock (writes + TCP), kernel-enforced, no root
                 audit: ~/.clara/netaudit.log, Wireshark filter in scripts/
```

## Install (Ubuntu, Python 3.11+)

```bash
git clone https://github.com/mendeltem/clara-agent
cd clara-agent
python3 -m venv ~/.clara-venv && ~/.clara-venv/bin/pip install .
~/.clara-venv/bin/clara --version
```
Installs the `clara` command into `~/.clara-venv/bin` (add it to PATH or call it with the full path). Zero runtime dependencies: the install downloads nothing but this package.

## Check the model server

```bash
clara doctor
```
Reads `~/.clara/config.toml` (created on first run; default server `http://127.0.0.1:8000/v1`, first model from `/v1/models`), reports Landlock support, server kind, model list, context size, sleep state and busy slots. It does not wake a sleeping llama-server. Add `--probe` for one real request that verifies tool calling.

## Work in the terminal

```bash
cd ~/some-project
clara
```
Interactive session like Claude Code: streaming answers, tool cards, a prompt before every file write and shell command (`y` once, `s` this session, `p` save as rule, `n` deny). `/help` lists commands, Ctrl+C interrupts, Ctrl+D leaves. Sessions are recorded in `~/.clara/sessions` (`--no-record` to keep nothing, `--resume last` to continue).

```bash
clara -p "run the tests and fix the first failure"
```
One-shot: answer on stdout, progress on stderr, exit code 0 on success. Writes and commands are refused without a user unless you pass `--mode accept-edits` or `--yolo`.

```bash
clara --yolo
```
No questions. The Landlock sandbox is applied automatically: writes only inside the project, `/tmp` and Clara's session folders; TCP only to the model server port; Clara's own config, skills and rules stay read-only; folders listed under `read_deny` in `config.toml` cannot be read at all, not even by `cat` through a symlink. Every child process inherits that. `clara sandbox-check` proves it on your kernel (22 checks).

## Phone app

```bash
clara pair            # prints the WLAN address and the token link
clara serve --lan     # starts the API and the app
```
Open the printed link on the phone (same WLAN), then "Add to Home screen". Approvals and questions from the agent appear on the phone as a sheet; the agent waits until you answer. For HTTPS (needed for a standalone home-screen app on Android), use `clara pair --tls` once, install the printed CA certificate on the phone, and start with `clara serve --lan --tls`.

## Verify that nothing leaves

```bash
clara audit                                   # every connection attempt of Clara itself, allowed or blocked
scripts/netaudit.sh -- clara -p "list the files"  # strace of the whole process tree, no root: verdict PASS or FAIL
sudo scripts/netaudit.sh --pcap -- clara      # same, plus a packet capture for Wireshark (filters in scripts/)
```
Details, threat model and the Wireshark filter: [docs/PRIVACY.md](docs/PRIVACY.md).

## Approval gate and socket seal (0.1.1)

`[permissions] approver_url` lets a local approval service decide before you are asked (APPROVE runs, ESCALATE asks you, DENY refuses). Under the sandbox a seccomp filter leaves only TCP sockets plus Unix SEQPACKET (`sandbox.unix_sockets = "allow"` for training jobs whose data loaders need Unix stream sockets): DNS queries, ICMP and `systemd-run` fail. See CHANGELOG.md for what was measured.

## Test status

2026-10-25, Ubuntu-based container, kernel 6.18 (Landlock ABI 7), Python 3.13, 2 CPU cores: `python -m pytest -q` 84 passed, 4 skipped (the real-model tests, run separately); `clara sandbox-check` 22 of 22; real model Qwen3-1.7B Q8_0 on CPU: 4 of 4 end-to-end tests passed (file read through a tool call in both tool modes, file written in accept-edits mode, write refused headless); fresh-clone install test passed. The table of what each test proves, and what is still open, is in [docs/DETAILS.md](docs/DETAILS.md).

## Requirements

Linux with Landlock (kernel 5.13+, TCP lock from 6.7 / ABI 4) for the sandbox; the agent itself runs on any OS with Python 3.11+. A model server that speaks the OpenAI chat API on localhost: llama-server (`--jinja` for native tool calls), Ollama, LM Studio, vLLM. Tested here with Qwen3-1.7B on CPU and with a simulated llama-server; see the test table for what each claim rests on.
