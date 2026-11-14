// 画出 App 图标(1024×1024 PNG):米色底板上三个对话气泡,颜色对应 Claude、Codex 和用户。
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

// 底板:macOS 图标的标准留边与圆角
let plate = NSBezierPath(roundedRect: NSRect(x: 100, y: 100, width: 824, height: 824),
                         xRadius: 186, yRadius: 186)
let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.28)
shadow.shadowOffset = NSSize(width: 0, height: -12)
shadow.shadowBlurRadius = 28
NSGraphicsContext.saveGraphicsState()
shadow.set()
color(0xf6f3ec).setFill()
plate.fill()
NSGraphicsContext.restoreGraphicsState()
NSGradient(starting: color(0xfdfcf9), ending: color(0xebe6da))!.draw(in: plate, angle: -90)

/// 一个对话气泡:圆角矩形 + 一个小尾巴 + 两行"文字"。
func bubble(x: CGFloat, y: CGFloat, width: CGFloat, fill: Int, tailOnRight: Bool) {
    let height: CGFloat = 158
    let body = NSBezierPath(roundedRect: NSRect(x: x, y: y, width: width, height: height),
                            xRadius: 56, yRadius: 56)
    let tail = NSBezierPath()
    let edge = tailOnRight ? x + width - 60 : x + 60
    let tip = tailOnRight ? x + width + 8 : x - 8
    let lift: (CGFloat, CGFloat) = tailOnRight ? (14, 34) : (34, 14)   // 两侧的尾巴互为镜像
    tail.move(to: NSPoint(x: edge - 34, y: y + lift.0))
    tail.line(to: NSPoint(x: tip, y: y - 22))
    tail.line(to: NSPoint(x: edge + 34, y: y + lift.1))
    tail.close()
    color(fill).setFill()
    body.fill()
    tail.fill()
    color(0xffffff, 0.86).setFill()
    for (offset, length) in [(CGFloat(92), width - 124), (CGFloat(46), width - 210)] {
        NSBezierPath(roundedRect: NSRect(x: x + 62, y: y + offset, width: length, height: 22),
                     xRadius: 11, yRadius: 11).fill()
    }
}

bubble(x: 204, y: 634, width: 430, fill: 0xc9822b, tailOnRight: false)
bubble(x: 204, y: 436, width: 372, fill: 0x2f8f6b, tailOnRight: false)
bubble(x: 432, y: 238, width: 388, fill: 0x3b5b8c, tailOnRight: true)

let output = URL(fileURLWithPath: CommandLine.arguments[1])
try! bitmap.representation(using: .png, properties: [:])!.write(to: output)
