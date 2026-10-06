# Recurring reindex schedule

`eichi index` is delta-only and idempotent (it re-embeds only changed/new
files, by mtime+hash), so running it every 15 minutes is cheap: a tick with
nothing changed just walks the configured corpora and skips. This doc covers
wiring that up as a recurring host-side job.

The job definition is [`scripts/eichi-reindex`](../scripts/eichi-reindex) — a
small shell script that:

1. Reindexes every filesystem corpus declared in your local `eichi.toml`
   (`[[corpus]]` blocks — see [`eichi.toml.example`](../eichi.toml.example)).
   This file is host-local and **not** committed; it's where your personal
   corpus paths live.
2. Runs every connector shipped with eichi (`eichi index --corpus <name>`:
   currently `claude-jsonl`, `claude-watch-queue`, `botchat`). Each no-ops
   gracefully when its underlying path or API is absent. `botchat` is
   incremental: it fetches only messages with an id above the stored
   `max_id` cursor.

It logs to `~/.local/state/eichi-reindex.log` (truncated each run — the log
always reflects the latest tick only). Override with `EICHI_REINDEX_LOG`.

## macOS — launchd (recommended on macOS)

```sh
./scripts/install-reindex-launchagent.sh
```

Renders [`scripts/com.claude-watch.eichi-reindex.plist.template`](../scripts/com.claude-watch.eichi-reindex.plist.template)
with your `$HOME` and repo path, installs it to `~/Library/LaunchAgents/`,
and bootstraps it (`launchctl bootstrap`) so it starts firing immediately
on a 900-second (`StartInterval`) cadence — no reboot or relogin required.
Re-running the installer is safe (it `bootout`s any existing instance
first).

```sh
# Verify it's loaded:
launchctl list | grep eichi-reindex

# Fire one tick right now (don't wait for the interval):
launchctl kickstart -k gui/$(id -u)/com.claude-watch.eichi-reindex

# Dismantle:
launchctl bootout gui/$(id -u)/com.claude-watch.eichi-reindex 2>/dev/null || true
rm ~/Library/LaunchAgents/com.claude-watch.eichi-reindex.plist
```

## Linux — cron

```sh
( crontab -l 2>/dev/null | grep -v scripts/eichi-reindex ; \
  sed "s#__REPO_ROOT__#$HOME/repos/eichi#g" scripts/crontab.eichi-reindex ) \
  | crontab -
```

See [`scripts/crontab.eichi-reindex`](../scripts/crontab.eichi-reindex) for
the fragment (`*/15 * * * *`). Verify with `crontab -l | grep eichi-reindex`;
remove with `crontab -l | grep -v scripts/eichi-reindex | crontab -`.

## Linux — systemd user timer (alternative to cron)

```sh
mkdir -p ~/.config/systemd/user
cp scripts/eichi-reindex.service scripts/eichi-reindex.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now eichi-reindex.timer
```

Verify: `systemctl --user list-timers | grep eichi-reindex`. Fire one tick
now: `systemctl --user start eichi-reindex.service`. Dismantle:

```sh
systemctl --user disable --now eichi-reindex.timer
rm ~/.config/systemd/user/eichi-reindex.{service,timer}
systemctl --user daemon-reload
```

## Why not bundle this into `eichi-backup`'s launchd agent?

Backup (daily, `com.claude-watch.eichi-backup`) and reindex (every 15 min)
have very different cadences and failure modes — a reindex failure is
harmless (the next idempotent tick retries), a backup failure risks losing a
restore point. Keeping them as separate scheduled units means either can be
paused/dismantled independently without touching the other.
