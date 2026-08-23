# Ticket-only Discord bot

This is a standalone ticket-only Discord bot: persistent ticket panels, private ticket channels, requester controls, private staff controls, assignment, priorities, notes, transcripts, inactivity reminders, and automatic panel recovery.

Administrative configuration is Discord-command based. The bootstrap user `606751416261935123` is the initial non-removable administrator. Users listed in `TICKET_ADMIN_USER_IDS`, members holding a role listed in `TICKET_ADMIN_ROLE_IDS`, and per-server users added with `/ticket admin-add` may use the `/ticket` administration commands. There is no dashboard and no non-ticket cog.

## Setup

1. Fill in `.env` with the bot token, Turso URL/token, and selected admin IDs/roles.
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. The bot initializes the SQLite/libSQL schema automatically on startup.
4. Invite the bot with permissions to view/send messages, embed links, manage channels, manage threads, create private threads, and send messages in threads. Enable the Members intent when staff-role synchronization is required; role changes automatically sync existing private ticket-control threads.
5. Start it with `python bot.py`.

Use `/ticket panel-create` to create the first panel; it immediately opens the visual editor and a safe preview. Use `/ticket panels` as the single panel-management entry point: select a panel, then edit its appearance, preview, categories, messages, routes, limits, staff roles, publish state, or deletion from the editor. Unicode and server emojis are supported in panel and category emoji fields; server emojis can be pasted as `<:name:id>` or entered as `:name:`. New ticket channels use a three-digit number prefix by default, such as `001-tier-support-royal`; channel templates support `{number}`, `{category}`, `{username}`, and `{id}`. Use `/ticket history` for ticket history and `/ticket transcript` for retained transcripts. The bootstrap administrator can grant or revoke per-server access with `/ticket admin-add` and `/ticket admin-remove`.

## Turso setup

Create a database and token with the Turso CLI:

```powershell
turso db create bhbot-tickets
turso db show bhbot-tickets --url
turso db tokens create bhbot-tickets
```

Put the resulting `libsql://...` URL in `TURSO_DATABASE_URL` and the database token in `TURSO_AUTH_TOKEN`. The official Python client supports remote libSQL connections with these two values; the bot creates its tables automatically when it starts.

## Storage efficiency

The schema uses SQLite/libSQL indexes, so PostgreSQL-only GIN/GiST and table-level autovacuum settings are not used. On the first startup after the storage optimization migration, the bot compactly rebuilds the `audit_events` indexes; local SQLite also refreshes planner statistics and runs `PRAGMA optimize`. Turso's remote Hrana protocol does not allow those planner-maintenance statements, so they are skipped there. The migration is recorded and is not repeated on every restart. If a `member_activity_daily` table exists in the same database, it is maintained the same way. No audit rows are deleted.

Ticket activity is also cached in memory and persisted at most once per 15 seconds per active ticket channel. This prevents a busy ticket or a large server's public chat from producing a Turso query/write for every message. The `ix_tickets_status_activity` index supports the inactivity checker efficiently.

Staff-thread synchronization shares one guild member snapshot across ticket threads for five minutes and skips unchanged thread syncs during that window. It only refreshes threads affected by a changed configured staff role. Staff control member lists are derived from ticket-channel role/member overwrites and cached briefly instead of scanning the entire guild for every interaction.
