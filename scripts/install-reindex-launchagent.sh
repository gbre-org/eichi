#!/usr/bin/env bash
# install-reindex-launchagent.sh — macOS launchd installer for the recurring
# eichi reindex job (scripts/eichi-reindex). Renders
# scripts/com.claude-watch.eichi-reindex.plist.template with this machine's
# $HOME + repo path, writes it to ~/Library/LaunchAgents/, and bootstraps it
# (loads it into launchd so it starts firing on the StartInterval cadence
# immediately — no reboot/relogin needed).
#
# Idempotent: re-running bootouts any existing instance of the label first,
# so editing the template and re-running picks up the change cleanly.
#
# Dismantle:
#   launchctl bootout gui/$(id -u)/com.claude-watch.eichi-reindex 2>/dev/null || true
#   rm ~/Library/LaunchAgents/com.claude-watch.eichi-reindex.plist

set -euo pipefail

script_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -P "$script_dir/.." && pwd)"
template="$script_dir/com.claude-watch.eichi-reindex.plist.template"
label="com.claude-watch.eichi-reindex"
dest="$HOME/Library/LaunchAgents/$label.plist"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "install-reindex-launchagent.sh: this installs a macOS launchd agent;" >&2
  echo "host is $(uname -s), not Darwin. See scripts/crontab.eichi-reindex" >&2
  echo "(cron) or scripts/eichi-reindex.timer (systemd user timer) instead." >&2
  exit 1
fi

if [ ! -r "$template" ]; then
  echo "install-reindex-launchagent.sh: template not found at $template" >&2
  exit 1
fi

if [ ! -x "$repo_root/scripts/eichi-reindex" ]; then
  echo "install-reindex-launchagent.sh: $repo_root/scripts/eichi-reindex is not executable" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s#__HOME__#$HOME#g" -e "s#__REPO_ROOT__#$repo_root#g" "$template" > "$dest"
echo "wrote $dest"

# Idempotent reload: bootout a prior instance (ignore "no such process"),
# then bootstrap fresh.
launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$dest"
echo "bootstrapped gui/$(id -u)/$label (fires every 900s / 15min)"

echo
echo "Verify:   launchctl list | grep eichi-reindex"
echo "Run now:  launchctl kickstart -k gui/\$(id -u)/$label"
echo "Logs:     \$HOME/.local/state/eichi-reindex.log"
echo "Dismantle:"
echo "  launchctl bootout gui/\$(id -u)/$label 2>/dev/null || true"
echo "  rm $dest"
