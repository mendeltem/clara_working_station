# Privacy: what Clara can reach, and how you check it

Clara's purpose is to work with data that must not leave your computer. This page says exactly which network paths exist, which layers close the others, and how to verify each claim yourself rather than trust this text.

## The only two sockets

| Who | Destination | Why | Switch |
|---|---|---|---|
| Clara (any mode) | `127.0.0.1:8000` by default, or the `base_url` in `~/.clara/config.toml` | the model server | `[model].base_url` |
| `clara serve` | listens on `127.0.0.1:8765`, or on all interfaces with `--lan` | the phone app | `clara serve --lan` |

There is no update check, no telemetry, no crash reporting, no model catalogue download, no web tool. The package has zero dependencies, so the complete set of code that can open a socket is in this repository: `clara/providers/openai_local.py` (model server) and `clara/server.py` (app). Both go through `clara/netguard.py`. The WLAN address shown by `clara pair` and `clara serve --lan` is read from `/proc/net/route` and an interface ioctl, not by connecting anywhere. Child processes (`clara sandbox-check`, scheduled runs) are Clara itself again, under the same guard.

## Four layers, each verifiable on its own

1. **In-process allowlist** (`clara/netguard.py`). Every `connect()`, UDP `sendto()` and name resolution of Clara's own process is checked against `[network].allowed_hosts` (loopback only, by default) and refused before a packet leaves. Every attempt, allowed or refused, is one JSON line in `~/.clara/netaudit.log` with the code location that made it. `clara audit` shows it. The model has no tool that reaches the guard.
   Verify: `pytest tests/test_netguard.py` opens a socket to 1.1.1.1 and asserts that the guard refuses it; `clara audit` shows the refused line.

2. **Landlock sandbox** (`clara/sandbox.py`, Linux, no root). With `--yolo`, or `--sandbox on`, the kernel limits the whole process tree: writes only beneath the project, `/tmp`, and the parts of `~/.clara` that hold session output (sessions, backups, state, memory notes, audit log); TCP `connect()` only to the model server port, TCP `bind()` only to the app port. Clara's own configuration (`config.toml`), the skills folder, the permission rules, the token and the certificates stay read-only for the locked process, so a model running under yolo cannot change the allowlist, plant a skill or grant itself rules for the next session. A `curl`, `pip install` or `git push` that the model starts through the bash tool fails with "Permission denied". Children cannot lift the lock. Landlock does not cover UDP or ICMP; see layer 4.
   With `[sandbox].read_deny = ["/media/patients"]` in `config.toml`, reading inside those folders is refused by the kernel too, for every child process, through symlinks and relative paths alike (Landlock cannot carve a folder out, so Clara grants reading to everything that is not on the way to a denied folder; see `read_grant_rules` in `clara/sandbox.py`). Files added later directly inside an ancestor of a denied folder stay unreadable until the next start.
   Verify: `clara sandbox-check` runs 22 checks in a child process on your kernel (19 on a kernel without Landlock TCP rules), including a shell that tries `/dev/tcp` to a forbidden port, writes to `config.toml`, the skills folder, the rules folder and the token, and `cat` of a read-denied file through a symlink. `pytest tests/test_sandbox.py` runs the same checks.

3. **Permission prompts.** Every write and every shell command asks you first (`default` mode). Commands that normally use the network (`curl`, `wget`, `pip`, `git push`, `ssh`, `scp`, `rsync`, `apt`, `docker`, `ollama`, …) are flagged in the prompt. Writes to Clara's own files (`CLARA.md`, anything under `~/.clara` or `.clara/`, the `remember` tool) are never auto-allowed below yolo, so the model cannot quietly edit the instructions or rules it will read back next session. A saved rule such as `bash(pytest*)` covers only simple commands: anything with `;`, `&&`, `||`, `|`, redirection or substitution asks again. Deny rules in `~/.clara/projects/<project>.json` win over everything, including yolo: `"deny": ["read_file(/media/**)"]` is matched against the literal path, its resolved real path (symlinks, `..`) and any shell command, search or listing that names the folder. This is a prompt-level barrier; the kernel-level one is `read_deny` in layer 2, use both for data that must not be read.
   Verify: `pytest tests/test_permissions.py` (compound commands, protected files, symlink and shell bypass attempts).

4. **Your own observation, outside Clara.** `scripts/netaudit.sh -- clara …` records every address the process tree connects or sends to, with `strace` (no root), and with `--pcap` also a packet capture for Wireshark. `scripts/wireshark-filter.txt` has the display filters. `scripts/nft-clara-egress.sh` installs an nftables rule that drops and logs everything a dedicated user sends outside loopback (and your WLAN, if you use the phone app): that covers UDP and ICMP too and needs root once.
   Verify: run `scripts/netaudit.sh -- bash -c 'clara -p "hello"; curl 1.1.1.1'` and watch it report FAIL with `1.1.1.1:80`. A detector that cannot fail proves nothing.

## What is written to disk, and where

| Path | Content | Switch |
|---|---|---|
| `~/.clara/sessions/*.jsonl` | every message of a session, including tool results (file contents you let it read) | `--no-record` writes nothing |
| `~/.clara/backups/<session>/` | copy of every file before Clara's first change to it in that session | delete when done |
| `~/.clara/memory.md`, `<project>/.clara/memory.md` | notes the model saved with `remember` | edit or delete freely |
| `~/.clara/netaudit.log` | connection attempts (host, port, decision, code location) | `clara audit --clear` |
| `~/.clara/serve-token`, `~/.clara/tls/` | token and local certificate for the phone app | `clara pair --new-token` |
| `~/.clara/projects/<project>.json` | your allow/deny rules for that project | edit |
| `~/.clara/state/history` | your terminal input history | delete |

Nothing is written outside `~/.clara`, the project folder and `/tmp` unless you approve a tool call that does it. Under the sandbox even that set shrinks to the rows marked as session output above.

## Phone app over WLAN

`clara serve --lan` opens one port on the workstation. Every request needs the 128-bit token from `clara pair` in the `Authorization` header; without it the API answers 401 and only the static page is served. The token never travels in a URL: the event stream uses a single-use ticket that expires after two minutes, and the pairing link carries the token in the URL fragment, which browsers do not send to the server. State-changing requests are refused when their `Origin` is another site. Request bodies above 4 MB are refused. The page loads no fonts, scripts or images from anywhere but the workstation (check the browser's network tab: one origin); model output and file contents are rendered as text, never as HTML. The service worker caches only the app shell, never data. With `--tls`, the connection is encrypted with a certificate from a local CA that you install on the phone once; the private key never leaves `~/.clara/tls`.

Risks that remain: anyone on the same WLAN who gets the token can use Clara with your permissions on the workstation. Rotate the token (`clara pair --new-token`) if a phone is lost, keep `--lan` off when you do not need it, and use `--tls` on networks you do not control.

## What Clara does not protect against

- Reading, unless you list the folders. Without `read_deny`, the model can read any file you can; a deny rule in the permission layer catches the tool calls and shell commands that name the folder, not a script that finds the path on its own. For patient data, set `read_deny` (kernel) and the deny rule (prompt) and keep the local model.
- Prompt injection through files. Text inside a file the model reads can ask it to do things. The permission prompts and the sandbox exist for exactly that case; read what the prompt shows before pressing `y`.
- The model server itself. llama-server and Ollama are separate programs with their own network behaviour (Ollama checks for updates; llama-server does not). Watch them with the same Wireshark filter.
- Other processes on the machine. A machine-wide packet capture shows them too; the per-process strace report in `netaudit.sh` is what attributes traffic to Clara.
