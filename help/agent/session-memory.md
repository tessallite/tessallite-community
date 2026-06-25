---
title: "Agent Session Memory"
audience: tenant-admin
area: agent
updated: 2026-05-01
---

# Agent Session Memory

## What this covers

The conversational agent maintains a history of turns (question-answer pairs) within each conversation. This page explains how session memory works, how the history depth setting affects prompt construction, and how to manage conversation data through stats and purge controls.

---

## How session memory works

Each conversation between a user and the agent is stored as a sequence of turns. A turn consists of the user's question and the agent's response (answer text, citations, thought process, and metadata).

When a new question arrives, the agent includes prior turns in the LLM prompt so the model has conversational context. This allows the agent to handle follow-up questions ("what about last quarter?"), resolve pronouns ("show me that by region"), and maintain coherence across a multi-turn conversation.

The trade-off is that more history means a larger prompt, which consumes more LLM tokens and increases latency. The session history depth setting controls this trade-off.

---

## Session history depth

The `session_history_depth` setting (default: 20) controls how many recent turns include the full agent response in the prompt. Older turns are still included but in a compressed form: only the user's question appears, prefixed with `[earlier question]`. The agent's response is omitted.

This design preserves the thread of the conversation (the user can still see what they asked earlier) while keeping the prompt size bounded.

### How it works

For a conversation with 30 turns and a depth of 20:

- Turns 1--10 appear as `[earlier question] (turn N): <user's question>` — no agent response.
- Turns 11--30 appear as `User (turn N): <user's question>` followed by `Assistant: <agent's response>`.

The boundary moves as the conversation grows. A conversation with fewer turns than the depth setting includes all turns in full.

### Choosing a depth

| Scenario | Suggested depth |
|---|---|
| Short, focused Q&A sessions (most use cases) | 10--20 |
| Long analytical conversations with many follow-ups | 30--50 |
| High-throughput API usage where token cost matters | 5--10 |
| Models with very long agent responses | 5--15 |

Configure the depth in the project agent settings:

1. Open *Tenant Administration > Projects*.
2. Click the gear icon on the project row.
3. Navigate to the **Advanced** tab under the agent settings.
4. Set the **Session history depth** value (range: 1--100).
5. Click **Save**.

The change takes effect on the next agent turn — it does not retroactively alter stored turns.

---

## Conversation stats

The Advanced tab displays two statistics for the project:

- **Total conversations** — the number of conversation threads stored for this project.
- **Oldest conversation** — the timestamp of the oldest conversation. Useful for understanding how much history has accumulated.

These statistics are fetched from `GET /api/v1/admin/agent/conversations/stats?project_id=<id>`.

---

## Purging conversations

Conversation data accumulates over time. The Advanced tab provides two purge controls:

### Purge by age

Enter a number of days and click **Purge**. Every conversation whose last activity is older than the specified number of days is permanently deleted, along with all its turns and trace data.

### Delete all

Click **Delete All Conversations** and type `DELETE ALL` in the confirmation field. Every conversation for the project is permanently removed.

Both actions are hard deletes — the data cannot be recovered. The purge operates at the conversation level: individual turns cannot be selectively deleted.

### When to purge

- **Compliance windows.** If your organisation requires conversation data to be retained for no more than N days, set the agent's `conversation_retention_days` setting (which auto-purges via a scheduled job) OR use the manual purge for immediate cleanup.
- **After testing.** When the agent has been used for extensive testing during setup, purge the test conversations before going live.
- **Storage management.** Projects with high-volume agent usage accumulate significant conversation data. Periodic purges keep storage manageable.

---

## API reference

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/v1/admin/agent/conversations/stats` | Conversation count and oldest date (requires `project_id` query param) |
| DELETE | `/api/v1/admin/agent/conversations/purge` | Delete conversations (requires `project_id` and `older_than_days` query params; `older_than_days=0` deletes all) |

Both require `tenant_admin` role.

---

## Relationship to retention

The `conversation_retention_days` setting on the Retention tab controls automatic cleanup via a scheduled job. The manual purge on the Advanced tab is an immediate, ad-hoc operation. They are independent — you can use one, both, or neither.

| Mechanism | Trigger | Granularity |
|---|---|---|
| Retention setting | Automatic (daily scheduler job) | Conversations older than the configured number of days |
| Manual purge | On-demand (admin clicks button) | By age threshold or all |

---

## Best practices

- **Set retention for ongoing cleanup.** Manual purges are for one-off situations. For steady-state operations, configure `conversation_retention_days` on the Retention tab.
- **Start with default depth.** 20 turns covers most conversational patterns. Adjust only after observing that conversations are either losing context (increase) or consuming excessive tokens (decrease).
- **Purge test data before production launch.** Test conversations contain synthetic or exploratory data that skews stats and wastes storage.

---

## Related

- [Configure your project agent](configure-agent.md)
- [Agent log screen](agent-log-screen.md)

---

← [Agent log screen](agent-log-screen.md) | [Home](../index.md) | [Agent Conversation Export →](agent-export.md)
