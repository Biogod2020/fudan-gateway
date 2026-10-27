#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
out=${1:-"$root/../dist/atrust"}
mkdir -p "$out"
out=$(CDPATH= cd -- "$out" && pwd)
go_bin=${GO_BIN:-go}
"$go_bin" -C "$root/supervisor" build -trimpath -o "$out/fudan-supervisor" .
source_dir=${ZJU_CONNECT_SOURCE:-"$("$root/scripts/fetch-client.sh")"}
test "$(git -C "$source_dir" rev-parse HEAD)" = 8de443f582d962554f8d86d53cdddf6a11ec82a6
test -z "$(git -C "$source_dir" status --porcelain)"
"$go_bin" -C "$source_dir" build -mod=readonly -trimpath -o "$out/zju-connect" .
arch=$(uname -m)
xcrun swiftc -O -target "$arch-apple-macos13.0" -framework AppKit -framework WebKit \
    "$root/macos/Login.swift" -o "$out/fudan-login"
app="$out/FudanGateway aTrust.app"
mkdir -p "$app/Contents/MacOS"
xcrun swiftc -O -target "$arch-apple-macos13.0" -framework AppKit -framework Network \
    "$root/macos/MenuBar.swift" -o "$app/Contents/MacOS/FudanGateway"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>FudanGateway</string>
<key>CFBundleIdentifier</key><string>com.biogod.fudan-gateway.atrust.menu</string>
<key>CFBundleName</key><string>FudanGateway aTrust</string>
<key>CFBundleDisplayName</key><string>复旦 aTrust 代理</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.2</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLIST
plutil -lint "$app/Contents/Info.plist"
codesign --force --deep --sign - "$app"
codesign --verify --deep "$app"
codesign --force --sign - "$out/fudan-login"
codesign --verify "$out/fudan-login"
printf '%s\n' "$out"
