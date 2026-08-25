#!/bin/sh
set -eu

arch="${1:?architecture is required: x64 or arm64}"
version="${2:?version is required}"
output="${3:?output path is required}"

case "$arch" in
  x64) goarch=amd64 ;;
  arm64) goarch=arm64 ;;
  *) echo "unsupported architecture: $arch" >&2; exit 2 ;;
esac

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT HUP INT TERM

app="$stage/Applications/1C-Consultant.app/Contents"
mkdir -p "$app/MacOS" "$app/Resources" "$(dirname -- "$output")"

(
  cd "$project_root/deployment/installer"
  go test ./...
  CGO_ENABLED=0 GOOS=darwin GOARCH="$goarch" \
    go build -trimpath -ldflags "-s -w" \
    -o "$app/Resources/1c-consultant-installer" ./cmd/installer
)

cp "$project_root/deployment/macos/Info.plist" "$app/Info.plist"
sed -i '' "s/__VERSION__/$version/g" "$app/Info.plist"
cp "$project_root/deployment/macos/launcher.sh" "$app/MacOS/1C-Consultant"
cp "$project_root/assets/1c-consultant.icns" "$app/Resources/1C-Consultant.icns"
chmod 755 "$app/MacOS/1C-Consultant" "$app/Resources/1c-consultant-installer"

sh -n "$app/MacOS/1C-Consultant"
pkgbuild \
  --root "$stage" \
  --install-location / \
  --identifier com.ilnurcode.1c-consultant \
  --version "$version" \
  "$output"

pkgutil --payload-files "$output" | grep -Fq 'Applications/1C-Consultant.app/Contents/MacOS/1C-Consultant'
pkgutil --payload-files "$output" | grep -Fq 'Applications/1C-Consultant.app/Contents/Resources/1c-consultant-installer'
pkgutil --payload-files "$output" | grep -Fq 'Applications/1C-Consultant.app/Contents/Resources/1C-Consultant.icns'
echo "$output"
