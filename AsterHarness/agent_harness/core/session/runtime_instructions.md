### Persistence and resume

This Session runs a persistent Thread. For persistence details, this contract supersedes older statements that all generators disappear on Session close.

On orderly suspension, the harness saves pending events and generator registrations. Resume restores their IDs, queue order, sequence numbers and timer progress. Restored pending events and future generated events can start steps without another human message. A queued tool result is observed in a later step; its tool is not executed again.

Timers keep a wall-clock deadline across downtime. An overdue timer emits once on restart. Repeating timers then wait their interval after that emission, without a catch-up burst. Cancelling a generator is persistent; suspending the Session is not cancelling it. Nothing fires while the process is offline.

Command processes and their handles do not survive restart. An active command-completion registration cannot resume watching its old process: it emits generator_unavailable once and becomes failed. This is NOT command completion and does not supply an exit code. Do not reuse the old command handle or automatically repeat a potentially side-effecting command. Reassess the task and inspect durable results first.

Use list_event_generators to inspect current registrations. Historical registration receipts alone are not proof that a source is still active. Orderly suspension is supported; automatic crash recovery is not.

Active program generators restart a new Python process from their registered script
on resume, with their existing generator_id and private state directory. This is
different from restoring an exec_command process, which is not supported. Programs
must implement their own business cursors and tolerate possible duplicate events.
Paused/failed/cancelled/completed programs are not automatically restarted.
