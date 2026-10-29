# Proposed entry for blackbeard_wikki

`clara-agent-offline-harness-2026-10.md` follows `templates/knowledge-entry.md` of the wiki and belongs in
`knowledge/local_llm_operation/findings/`. It was not pushed (no write access, and the wiki's rule is: show the
list, push after the owner's yes). If you take it:

1. copy the file into `knowledge/local_llm_operation/findings/`,
2. add a row to `knowledge/local_llm_operation/INDEX.md` and a paragraph to its `SUMMARY.md`,
3. add a line to `updates/LOG.md`:
   `2026-10-25 | Claude (claude.ai chat) | knowledge/local_llm_operation/findings/clara-agent-offline-harness-2026-10.md, {INDEX,SUMMARY}.md | Clara harness: three-level network lock (guard, Landlock, strace), Qwen3-1.7B CPU tool-call times, open: 27B on llama-server, phone browser`
4. run the wiki maintenance check and rebuild `docs/` as the README says.
