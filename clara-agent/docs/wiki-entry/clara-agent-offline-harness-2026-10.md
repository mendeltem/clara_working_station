---
id: clara-agent-offline-harness-2026-10
typ: befund
status: vorschlag
thema: knowledge/local_llm_operation
stand: 2026-10-25
herkunft: Claude (claude.ai chat), build and tests of github.com/mendeltem/clara-agent in an Ubuntu-based container, 2026-10-25
verweist_auf: [landlock-write-lock-for-a-yolo-agent, minimal-harness-prompt-size, rule-lists-make-small-models-refuse, prefix-cache-single-slot, ollama-silently-truncates-context, hermes-keeps-every-conversation-in-state-db]
---

# Clara: an offline agent harness with a kernel-level network lock, measured on a small Qwen3

## Aussage
A Claude-Code-like harness in pure Python (no dependencies) can be held to "no network except the model server" at three levels at once, and a 1.7B Qwen3 on two CPU cores drives it through real tool calls:

1. In-process guard: every `connect()`, UDP `sendto()` and name resolution of the harness process is checked against a loopback allowlist and refused before a packet leaves; every attempt is one JSON line in an audit log. Measured by opening sockets to 1.1.1.1 and 8.8.8.8 in tests: refused, logged.
2. Landlock (ABI 7 in the container, ABI 8 on Alita): writes only beneath the project, `/tmp` and Clara's session folders (config, skills, rules and token stay read-only); TCP connect only to the model port; optional kernel read denial for listed folders (Landlock cannot carve out, so reading is granted to everything that is not on the way to a denied folder); inherited by every child. 22 of 22 checks in a child process, including `bash -c 'exec 3<>/dev/tcp/127.0.0.1/<other port>'` and `cat` of a read-denied file through a symlink: denied. Landlock does not cover UDP or ICMP; an nftables rule per user closes that.
3. Independent recorder: `strace -f -e trace=connect,sendto,sendmsg` of the whole process tree gives a per-destination list; a deliberate `curl 1.1.1.1` in the same tree is reported as FAIL with the address, so the detector can fail.

Real model, Qwen3-1.7B Q8_0 on 2 CPU cores behind llama-cpp-python 0.3.36 (OpenAI-compatible server, thinking on, sampling left to the server): read a file through a tool call and answered correctly in 106 s (tools described in the prompt, Qwen wording, `<tool_call>` parsed by the harness) and 130 s (tools sent in the request); created a file with exact content in accept-edits mode in 153 s; in headless default mode the write was refused by the harness and the file stayed absent. 4 of 4, first attempt each.

Harness size: system prompt under 200 words, static, no timestamps (date and project path travel in the first user message), ten tools with short descriptions. Character estimate for the prompt-mode system prompt is about 1 200 tokens; the exact number on llama-server comes from `clara doctor --probe` via `/tokenize` and is not measured yet on the 27B.

## Trägt
Running the local profile without a browser-side guard: the harness itself cannot reach anything but the model, and the model's commands cannot open TCP connections. A record-less mode (`--no-record`) exists, unlike Hermes.

## Trägt nicht
- Numbers from a 1.7B model on CPU say nothing about the 27B on the A5000; the wire format is the same, the quality is not.
- The phone app page was tested in jsdom and through its API, not in a phone browser.
- Reads are open unless folders are listed in `read_deny`; the prompt-level deny rule alone is a rule, not a kernel lock.
- llama-server itself was simulated in the tests (its SSE format with reasoning_content and tool_calls deltas); the real binary ran only as llama-cpp-python.

## Bruchindikator
A destination outside loopback in `~/.clara/netaudit.log` or in the strace report of `scripts/netaudit.sh`, or a write outside the allowed folders under `--yolo`.

## Ablesung
Own build and measurements 2026-10-25: `tests/` of the repository (84 passed, 4 real-model tests separately), `clara sandbox-check` output, `scripts/netaudit.sh` runs, `scripts/fresh_install_test.sh`. A second model instance reviewed the code read-only and found five ways the model could have widened its own permissions (writable rules and memory files, loose saved bash rules, deny rules that symlinks and shell commands got around, token in the stream URL); fixed and tested the same day. Tier 1a (own measurement), container not identical to Alita.

## Verknüpft
- [landlock-write-lock-for-a-yolo-agent](landlock-write-lock-for-a-yolo-agent.md)
- [minimal-harness-prompt-size](../open_checks/minimal-harness-prompt-size.md)
- [rule-lists-make-small-models-refuse](rule-lists-make-small-models-refuse.md)
- [prefix-cache-single-slot](../mechanisms/prefix-cache-single-slot.md)
- [ollama-silently-truncates-context](../risks/ollama-silently-truncates-context.md)
- [hermes-keeps-every-conversation-in-state-db](hermes-keeps-every-conversation-in-state-db.md)
