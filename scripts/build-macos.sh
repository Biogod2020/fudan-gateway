#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_root=${1:-"$project_root/dist"}
app="$build_root/FudanGateway.app"
mkdir -p "$app/Contents/MacOS"
arch=$(uname -m)
xcrun swiftc -O -target "$arch-apple-macos13.0" \
    -framework AppKit "$project_root/macos/FudanGateway.swift" \
    -o "$app/Contents/MacOS/FudanGateway"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>FudanGateway</string>
<key>CFBundleIdentifier</key><string>com.biogod.fudan-gateway.menu</string>
<key>CFBundleName</key><string>FudanGateway</string>
<key>CFBundleDisplayName</key><string>复旦校园访问</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLIST
plutil -lint "$app/Contents/Info.plist"
codesign --force --deep --sign - "$app"
codesign --verify --deep "$app"
printf '%s\n' "$app"
