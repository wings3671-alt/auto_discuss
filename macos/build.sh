#!/bin/bash
# 生成「胡说八道群.app」。改了 macos/ 里的文件、或把项目文件夹搬了位置之后,重新运行一次。
#
#   bash macos/build.sh
#
# 生成的 App 只是个外壳,记着项目文件夹的位置;真正的程序是 discuss/ 里的 Python 代码,
# 改 Python 或界面不需要重新生成。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
APP="$ROOT/胡说八道群.app"
WORK="$(mktemp -d)"
MIN_MACOS="13.0"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/discuss/__init__.py")"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "① 编译窗口外壳…"
# 明确写出最低系统版本:编译器的默认值可能比本机系统还新,那样系统会拒绝启动这个 App
swiftc -O -target "$(uname -m)-apple-macos${MIN_MACOS}" "$HERE/main.swift" \
  -o "$APP/Contents/MacOS/discuss-app" \
  -framework Cocoa -framework WebKit -framework UserNotifications

echo "② 画图标…"
swiftc "$HERE/icon.swift" -o "$WORK/icon" -framework Cocoa
"$WORK/icon" "$WORK/icon.png"
mkdir "$WORK/AppIcon.iconset"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$WORK/icon.png" --out "$WORK/AppIcon.iconset/icon_${size}x${size}.png" >/dev/null
  sips -z "$((size * 2))" "$((size * 2))" "$WORK/icon.png" --out "$WORK/AppIcon.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$WORK/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"

echo "③ 写入 App 信息…"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>胡说八道群</string>
  <key>CFBundleDisplayName</key><string>胡说八道群</string>
  <key>CFBundleIdentifier</key><string>local.autodiscuss.app</string>
  <key>CFBundleExecutable</key><string>discuss-app</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key><string>${MIN_MACOS}</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>DiscussRoot</key><string>${ROOT}</string>
</dict>
</plist>
EOF

echo "④ 签名(仅本机使用的临时签名)…"
codesign --force --sign - "$APP" >/dev/null 2>&1
touch "$APP"

echo "✓ 已生成:$APP"
