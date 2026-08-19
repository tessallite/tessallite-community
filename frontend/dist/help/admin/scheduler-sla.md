---
title: "Refresh SLA Declarations"
audience: tenant_admin
area: admin
updated: 2026-06-12
---

## What this covers

A refresh SLA (Service Level Agreement) is a promise you write down for a model: "the data in this model must be refreshed and ready by this time every day." The SLA monitor checks that promise for you. If the data is not ready on time, it can retry the refresh automatically and send you an alert. This page explains the idea, how to set one up, what the monitor actually checks, and how to read a breach alert.

## Why SLA declarations matter

Refresh jobs run on a schedule and are mostly unattended. Without a declared SLA, a refresh that fails quietly at 3 a.m. surfaces only when someone opens a dashboard and notices yesterday's numbers. By then, decisions may already have been made on stale data.

An SLA turns "the data is usually ready in the morning" into a checked rule: "a successful refresh must have finished by 07:00 UTC; if not, retry it and tell the on-call channel." Think of it like a school bus schedule — the question is not "did the bus drive around?" but "did it arrive at the stop by 8 o'clock?" Only an on-time arrival counts.

## Defining an SLA

SLAs are managed per model in the **Refresh SLA** section (available in the Scheduler panel and in the model's Settings panel).

To create one:

1. Open the model and locate the **Refresh SLA** section.
2. Click **Configure SLA**.
3. Fill in:
   - **Target completion time (HH:MM UTC)** — the time of day by which a successful refresh must have finished. Note this is UTC, not your local time.
   - **Grace period (minutes)** — extra slack added to the target before a breach is declared. Covers normal day-to-day jitter in refresh duration.
   - **Max retries per day** — how many automatic retry attempts the monitor may make per day when a breach is detected.
   - **Send webhook alert on breach** — whether to emit a `refresh.sla_breach` webhook event (and email/Slack notification, if notification routes are configured).
4. Save.

Or via API:

```
POST /api/v1/projects/{project_id}/models/{model_id}/sla
{
  "target_completion_time": "07:00",
  "grace_period_minutes": 15,
  "max_retries": 1,
  "alert_on_breach": true
}
```

One SLA exists per model. Creating a second returns a conflict — use PATCH on the same path to change an existing SLA, and DELETE to remove it.

## How the monitor works

The SLA monitor runs as an hourly sweep job. For each model with an SLA it:

1. Works out today's deadline: target completion time plus the grace period (all in UTC).
2. Does nothing until the deadline has passed — no early alarms.
3. After the deadline, checks **every active aggregate on the model** one by one: did a refresh of that aggregate **finish successfully** between midnight UTC and the deadline? Only aggregates the scheduler actually keeps refreshed are checked — **retired and invalid aggregates are not monitored**. A retired aggregate is no longer kept fresh and nothing reads from it, so it can never breach the SLA. If every aggregate on a model has been retired, the model has nothing to monitor and is always reported as meeting its SLA.
4. The SLA is met only when **all** active aggregates pass that check. If even one active aggregate has no on-time success, the model is in breach — one slow table cannot hide behind six fast ones. A later refresh the same day does not undo an on-time success.
5. A refresh that **failed** before the deadline does not count as meeting the SLA — only a successful one does.

Each aggregate ends up in one of three buckets, and the alert reports all three:

- **on time** — refreshed successfully before the deadline.
- **late** — refreshed successfully today, but after the deadline. The data is fine now; the promise was still missed.
- **stale** — no successful refresh at all today. This is the data that is actually wrong.

## What happens on a breach

A breach opens a **breach episode** for that model and that day. Within one episode:

1. **Automatic retry — stale aggregates only.** If the day's retry budget is not used up, the monitor immediately runs a real full refresh of every **stale** aggregate — the same refresh machinery the scheduler uses. Aggregates whose data already landed (on time or late) are never refreshed again: the monitor does not redo work that is already done. Each retry run appears in the refresh history (triggered by `sla_monitor`) and always finishes in a final state: completed or failed. If some other refresh of an aggregate is still running at that moment, the monitor does not start a second one on top of it — it skips that aggregate and reports the skip honestly. One retry attempt uses one unit of the daily budget.
2. **One alert.** If "Send webhook alert on breach" is on, a single `refresh.sla_breach` webhook event is emitted — once per episode, on the first sweep that detects the breach. Later sweeps of the same day stay quiet, even if the breach persists: they may still retry stale data (budget permitting), but they will not re-alert.

   The email/Slack notification is treated a little differently, because it can fail in a way the webhook queue cannot. If you have notification routes configured for the `sla_breach` event type, the alert is sent to them — and if **none** of them accepted it (the mail server was down, Slack rejected it, or no route matched), the next hourly sweep tries again. It keeps trying, at most once per sweep, until one channel accepts it, and then stops for the rest of the episode. So "one alert" means one alert that actually reached somebody: a breach is never marked as announced when nobody was told.
3. **Recovery.** When every aggregate finally has a successful refresh for the day, the episode is marked recovered and a single `refresh.sla_recovered` webhook event is emitted. After that the monitor is silent for the rest of the day. The next day is a fresh start — a new breach is a new episode and alerts again.

## Breach webhook payload

```json
{
  "model_id": "<uuid>",
  "target_completion_time": "07:00",
  "grace_period_minutes": 15,
  "last_completed_at": "2026-06-12T08:24:59Z",
  "retried": true,
  "retries_today": 0,
  "retry_run_statuses": ["completed", "completed"],
  "sla_scope": "all_aggregates",
  "breach_kind": "partial",
  "aggregates_total": 7,
  "aggregates_on_time": 5,
  "aggregates_late": 0,
  "aggregates_stale": 2
}
```

How to read it:

- **last_completed_at** — when the latest *successful* refresh finished today, or `null` if none succeeded yet. In a breach this is always either missing or later than the deadline.
- **retried** — `true` only when the monitor actually executed a retry refresh and it ran to a final state. If the retry could not run at all, this is `false` — the payload never claims work that did not happen.
- **retries_today** — how many retry attempts were already used before this check.
- **retry_run_statuses** — the outcome for each stale aggregate in this retry attempt: `completed` or `failed` (the refresh really ran), or `skipped_in_flight` (another refresh of that aggregate was already running, so the monitor left it alone).
- **sla_scope** — always `all_aggregates`: the SLA counts only when every aggregate is fresh.
- **breach_kind** — decided purely by the on-time bucket. `full` means **no** aggregate met the deadline (zero on time) — every aggregate was late or stale. `partial` means at least one aggregate refreshed on time while others were late or stale. A partial breach means your dashboards are a mix of fresh and stale numbers — often more dangerous than fully stale data, because it looks plausible. Note: a model where every aggregate eventually refreshed but all after the deadline is a `full` breach (nothing was on time), even though no data is ultimately stale.
- **aggregates_total / on_time / late / stale** — the per-bucket counts behind the verdict.

## Recovery webhook payload

```json
{
  "model_id": "<uuid>",
  "target_completion_time": "07:00",
  "grace_period_minutes": 15,
  "last_completed_at": "2026-06-12T08:03:11Z",
  "resolved_at": "2026-06-12T09:00:02Z",
  "aggregates_total": 7
}
```

Emitted once per episode (event type `refresh.sla_recovered`) when every aggregate has a successful refresh for the breached day. It is your "all clear": the data is whole again, even though the deadline was missed.

## Worked example

Model "Sales" refreshes nightly at 03:00 UTC and takes about 20 minutes. You declare: target 07:00, grace 15 minutes, max retries 1, alerts on.

- **Normal night:** refresh completes 03:21. At the 08:00 sweep the monitor sees every aggregate succeeded before 07:15 — SLA met, no alert, no retry.
- **Bad night:** the refresh fails at 03:15. At the 08:00 sweep no aggregate has a successful run before 07:15 — breach. The monitor reruns the stale aggregates; they complete at 08:03. You receive exactly one alert saying `retried: true` with `retry_run_statuses: ["completed", ...]`. The 09:00 and later sweeps see the data is healed and stay quiet — no duplicate alerts, no pointless re-refreshes. The one alert is your signal to investigate why the 03:00 run failed.
- **Partial night:** five of seven aggregates refresh on time, two fail. That is a breach — `breach_kind: "partial"`, `aggregates_stale: 2`. Only the two stale aggregates are retried; the five healthy ones are left untouched.
- **Very bad night:** the retry fails too. The one alert says `retried: true` but the statuses show `failed`. Later sweeps keep retrying the stale aggregates until the budget (1) is used up, then stop until tomorrow — without sending more alerts. When a later run finally heals the data, you get a single `refresh.sla_recovered` event. Time to look at the refresh error in the run history.

## Best practices and pitfalls

- **Set the target after your scheduled refresh, with room to spare.** If the refresh starts at 03:00 and takes up to an hour, a 07:00 target gives a comfortable buffer without hiding real problems.
- **Mind the timezone.** Targets are UTC. A "07:00" target is 07:00 UTC everywhere, in every season.
- **Use the grace period for jitter, not for problems.** 10–30 minutes of grace absorbs slow nights; hours of grace just delays the alarm.
- **Keep max retries small.** One or two retries heal transient failures; more usually just repeats the same error and delays a human looking at it.
- **Breach alerts are worth reading even when the retry succeeded.** The data is fine, but something made the scheduled run miss its window — that something tends to come back.

## Updating and deleting SLAs

SLAs can be updated via PATCH and deleted via DELETE (or with **Edit** / **Remove** in the Refresh SLA section). Deleting an SLA does not affect in-flight refreshes; breach checking simply stops from the next sweep cycle.

## Limits

- One SLA per model.
- The monitor sweep runs hourly, so a breach is detected on the first sweep after the deadline, not at the deadline itself.
- The retry budget and the breach episode both reset at midnight UTC.
- One breach alert and one recovery event per model per day, at most.
- Only active aggregates are monitored. Retired and invalid aggregates are skipped entirely — they never cause a breach and are never retried by the monitor.
- The recovery event (`refresh.sla_recovered`) is emitted only when the data heals **within the same UTC day** as the breach. If a breached day's data does not heal until after midnight UTC, the old day's episode simply ends with the day — no recovery event is sent for it. The new day is judged on its own deadline.
- Turning alerts off (`alert_on_breach` = false) suppresses the webhook and email/Slack notifications only. The breach episode itself still opens and closes internally, so a healed model is reported as recovered rather than staying in breach for the rest of the day.
- Webhook delivery is best-effort; it is not a guaranteed delivery channel.

---

## Related

- [Manage Aggregate Schedules](../modelling/manage-aggregate-schedules.md)
- [Run a Refresh](../modelling/run-a-refresh.md)
- [Webhooks](webhooks.md)

---

← [Security Audit Trail](security-audit-trail.md) | [Home](../index.md) | [Architecture Overview →](../system-admin/architecture-overview.md)
