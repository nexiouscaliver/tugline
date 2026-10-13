# Comparison

Plain facts, one row per tool, sources linked. Cells say unknown where the
public material does not settle the question. Nothing here is a verdict;
the tools solve different slices of the same problem.

| | self-hosted | two-way replies | in-notification approve | harnesses | needs its own app | license |
| --- | --- | --- | --- | --- | --- | --- |
| tugline | yes, your ntfy server | yes, into a local inbox file (no auto prompt injection in 0.1.0) | toggle exists, off by default, receiving side not shipped in 0.1.0 | zcode, Claude Code, any CLI via notify | no, stock ntfy app | MIT |
| raw curl to ntfy | yes | no | no | any (you write the glue) | no | not applicable |
| claude-remote-approver | unknown (examples target ntfy.sh) | no, approve and deny only | yes, core feature | Claude Code | no, ntfy app | MIT |
| Pulse | no, hosted platform (SDK and plugins run on your infra) | yes, agent sessions with follow-up prompts | unknown | Claude Managed Agents, Codex via plugins | yes | SDK MIT, platform not open source |
| Omnara | yes, Apache 2.0, plus a hosted cloud | yes, streaming sessions | permissions are built in; notification buttons specifically: unknown | any agent through its API | yes | Apache 2.0 |
| Happy | yes, runs on your own hardware | yes, session chat synced to phone | unknown | Claude Code, Codex, Grok | yes | MIT |

Sources, checked 2026-10-13:

- tugline: this repository.
- claude-remote-approver: https://github.com/yuuichieguchi/claude-remote-approver
- Pulse: https://github.com/try-pulse (SDK docs at https://trypulse.tech/docs/developers/agents)
- Omnara: https://github.com/omnara-ai/omnara
- Happy: https://github.com/slopus/happy

Where the rows differ most: tugline and raw curl ride an existing ntfy
server and the stock ntfy app, and add no server or app of their own. The
other four each bring a client app (Happy, Pulse, Omnara) or a dedicated
flow (claude-remote-approver), and in exchange carry the session further
than a notification: full chat, or a wired approve path. Which slice you
need is the whole decision.
