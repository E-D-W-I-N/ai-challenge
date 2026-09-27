# Reminder MCP service

This service has its own SQLite database and depends only on the official MCP
SDK. It imports no application Agent, Store, configuration, or LLM code and needs
no model API key. Start it manually in a separate terminal:

```sh
python3.11 -m venv /private/tmp/reminder-service-venv
/private/tmp/reminder-service-venv/bin/python -m pip install -r services/reminders/requirements.txt
REMIND_DB_PATH=/absolute/path/reminders.db REMIND_PORT=8001 /private/tmp/reminder-service-venv/bin/python -m services.reminders.server
```

The Streamable HTTP MCP endpoint is `http://127.0.0.1:8001/mcp`. The default host
is loopback (`REMIND_HOST` overrides it); default database is
`services/reminders/data/reminders.db`. The application connects to this URL;
closing its connection does not stop the service. Authentication and remote
deployment are outside this local service's contract. Keep it on loopback.

For the current day-18 checkpoint, a config entry is:

```json
{"servers":{"remind":{"url":"http://127.0.0.1:8001/mcp","timeout_s":10}}}
```

The common day-16/17 change supplies persisted URL editing in Tools and default
manual service ownership before this checkpoint can be merged. Legacy
`app.mcp_servers.remind` remains a stdio entry point for existing configs and
offline fixtures; the implementation and database belong to this service.

`remind(text, in_seconds, every=None)` retains its parameters. The host injects
an opaque `context_id` identifying its database and exact originating chat;
the model cannot choose it. A schedule returns JSON with `scheduled`, `id`,
`due_at`, and a human acknowledgement. `reminders()` reports actual state and
successful execution count, never an arithmetic count of elapsed periods.
`cancel(id)` removes a job; the host scopes it to the originating context.

Private `_reminder_claim` / `_reminder_finish` MCP tools implement the executor
protocol and are not declared to the model or shown in Tools. Claims are atomic,
due-only, exclusive per context, and token checked. Running claims expire after
300 seconds and become failed, with no automatic replay. The executor is
limited to 240 seconds. Legacy rows migrate without loss and remain unbound;
they are never assigned to whichever chat happens to be open.

Periodic jobs run at a fixed cadence starting from the original deadline. After
success, the next slot is the first original-cadence deadline strictly after
completion; missed slots are skipped. One execution is active at a time. Failed
or interrupted runs stop the job rather than silently repeating side effects.
Cancel prevents future work and results; actions already started cannot be
undone. A disconnected host leaves pending jobs durable until it reconnects.

The application supports one process owning a chat's history. Atomic service
claims also prevent duplicate job claims across executors, but they do not
serialize unrelated foreground history writers in multiple application workers.
There is no exactly-once claim for external side effects: a crash between chat
commit and job finish may leave a visible result and an interrupted job. Such
jobs are not automatically retried.
