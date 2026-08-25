#!/bin/sh
set -eu

data_root="$HOME/Library/Application Support/1C-Consultant"
installed="$data_root/1C-Consultant.command"
resources=$(CDPATH= cd -- "$(dirname -- "$0")/../Resources" && pwd)
installer="$resources/1c-consultant-installer"

if [ -x "$installed" ]; then
  target="$installed"
else
  target="$installer"
fi

/usr/bin/osascript - "$target" <<'APPLESCRIPT'
on run argv
  set commandPath to quoted form of (item 1 of argv)
  tell application "Terminal"
    activate
    do script "env CONSULTANT_EXTERNAL_APP=1 " & commandPath
  end tell
end run
APPLESCRIPT
