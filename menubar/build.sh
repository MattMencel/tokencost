#!/bin/bash
set -e
cd "$(dirname "$0")"

APP="TokenCostBar.app"
BUNDLE="$APP/Contents"

echo "Building TokenCostBar..."
swift build -c release 2>&1

echo "Packaging $APP..."
rm -rf "$APP"
mkdir -p "$BUNDLE/MacOS"

cp .build/release/TokenCostBar "$BUNDLE/MacOS/"

cat > "$BUNDLE/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>TokenCostBar</string>
    <key>CFBundleIdentifier</key>
    <string>dev.local.tokencostbar</string>
    <key>CFBundleName</key>
    <string>TokenCostBar</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key>
        <true/>
    </dict>
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
</dict>
</plist>
EOF

echo ""
echo "Done!  $APP is ready."
echo ""
echo "To install:"
echo "  mv $APP ~/Applications/"
echo "  open ~/Applications/$APP"
echo ""
echo "To run from here:"
echo "  open $APP"
