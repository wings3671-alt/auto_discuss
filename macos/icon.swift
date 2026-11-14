// 画出 App 图标(1024×1024 PNG):墨色底板上一枚朱红印章,印文"胡说八道"。
// 印章左下角带一个小尾巴,暗示"说话"。与界面左上角的标志是同一枚。
// 用法: icon <输出.png>

import Cocoa

let size = 1024
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                              bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                              colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)

func color(_ hex: Int, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

func withShadow(_ blur: CGFloat, _ offset: CGFloat, _ alpha: CGFloat, _ draw: () -> Void) {
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(alpha)
    shadow.shadowOffset = NSSize(width: 0, height: -offset)
    shadow.shadowBlurRadius = blur
    shadow.set()
    draw()
    NSGraphicsContext.restoreGraphicsState()
}

// ── 底板:墨色,macOS 图标的标准留边与圆角 ─────────────────────────────────────
let plate = NSBezierPath(roundedRect: NSRect(x: 100, y: 100, width: 824, height: 824),
                         xRadius: 186, yRadius: 186)
withShadow(28, 12, 0.32) { color(0x1c212b).setFill(); plate.fill() }
NSGradient(colors: [color(0x343c4e), color(0x1f2430), color(0x12151c)])!.draw(in: plate, angle: -90)
// 顶部一道很淡的高光,让底板有厚度
NSGraphicsContext.saveGraphicsState()
plate.addClip()
NSGradient(starting: color(0xffffff, 0.10), ending: color(0xffffff, 0))!
    .draw(in: NSRect(x: 100, y: 620, width: 824, height: 304), angle: -90)
NSGraphicsContext.restoreGraphicsState()

// ── 印章:略微歪一点,像手盖上去的 ─────────────────────────────────────────────
let seal = NSRect(x: 252, y: 262, width: 520, height: 520)
let turn = NSAffineTransform()
turn.translateX(by: 512, yBy: 522)
turn.rotate(byDegrees: 4)
turn.translateX(by: -512, yBy: -522)
NSGraphicsContext.saveGraphicsState()
turn.concat()

let body = NSBezierPath(roundedRect: seal, xRadius: 64, yRadius: 64)
let tail = NSBezierPath()                                   // 左下角的小尾巴
tail.move(to: NSPoint(x: seal.minX + 44, y: seal.minY + 96))
tail.line(to: NSPoint(x: seal.minX - 38, y: seal.minY - 46))
tail.line(to: NSPoint(x: seal.minX + 150, y: seal.minY + 30))
tail.close()
body.append(tail)
body.windingRule = .nonZero
withShadow(26, 14, 0.45) { color(0xa9322a).setFill(); body.fill() }
NSGradient(colors: [color(0xd24a38), color(0xb3372a), color(0x8f261f)])!.draw(in: body, angle: -90)

// 印章内侧的一圈细边
let frame = NSBezierPath(roundedRect: seal.insetBy(dx: 30, dy: 30), xRadius: 40, yRadius: 40)
frame.lineWidth = 9
color(0xfbf3e4, 0.82).setStroke()
frame.stroke()

// 印文:两行,上"胡说"下"八道",与界面左上角的标志一致
let fontNames = ["STLibianSC-Regular", "STKaitiSC-Black", "STKaitiSC-Bold", "STSongti-SC-Black"]
let font = fontNames.lazy.compactMap { NSFont(name: $0, size: 196) }.first
    ?? NSFont.systemFont(ofSize: 180, weight: .heavy)
let attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color(0xfbf3e4)]
let cell = (seal.width - 96) / 2
for (index, character) in ["胡", "说", "八", "道"].enumerated() {
    let text = NSAttributedString(string: character, attributes: attributes)
    let box = text.size()
    let column = CGFloat(index % 2), row = CGFloat(index / 2)
    let centerX = seal.minX + 48 + cell * (column + 0.5)
    let centerY = seal.maxY - 48 - cell * (row + 0.5)
    text.draw(at: NSPoint(x: centerX - box.width / 2, y: centerY - box.height / 2))
}
NSGraphicsContext.restoreGraphicsState()

FileHandle.standardError.write("印文字体:\(font.fontName)\n".data(using: .utf8)!)
let output = URL(fileURLWithPath: CommandLine.arguments[1])
try! bitmap.representation(using: .png, properties: [:])!.write(to: output)
