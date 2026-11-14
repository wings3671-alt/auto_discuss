// 讨论群.app 的外壳:一个原生窗口,里面显示本机网页界面。
//
// 启动时在后台拉起 Python 服务(python3 -m discuss),等它报告端口后载入界面;
// 退出时把服务一起关掉。关窗口只是隐藏,AI 可以继续把话说完。

import Cocoa
import UniformTypeIdentifiers
import UserNotifications
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var web: WKWebView!
    private var server: Process?
    private var serverErrors = ""
    private var runningTurns = 0          // 正在发言的讨论数,由界面告知

    // ── 启动 ────────────────────────────────────────────────────────────────
    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        showMessage("正在启动…")
        startServer()
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "app")
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1320, height: 840),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "讨论群"
        window.minSize = NSSize(width: 760, height: 520)
        window.contentView = web
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.center()
        window.setFrameAutosaveName("讨论群主窗口")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func startServer() {
        guard let root = Bundle.main.object(forInfoDictionaryKey: "DiscussRoot") as? String,
              FileManager.default.fileExists(atPath: root + "/discuss/__main__.py") else {
            return showMessage("找不到程序文件。请在项目文件夹里重新运行 macos/build.sh。")
        }
        let candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        guard let python = candidates.first(where: FileManager.default.isExecutableFile) else {
            return showMessage("找不到 python3,无法启动。")
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-m", "discuss", "--follow-parent"]
        process.currentDirectoryURL = URL(fileURLWithPath: root)
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUTF8"] = "1"
        environment["LANG"] = "en_US.UTF-8"
        process.environment = environment

        let output = Pipe(), errors = Pipe()
        process.standardOutput = output
        process.standardError = errors
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let text = String(data: handle.availableData, encoding: .utf8) ?? ""
            for line in text.split(separator: "\n") where line.hasPrefix("READY ") {
                if let port = Int(line.dropFirst(6)) {
                    DispatchQueue.main.async {
                        self?.web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!))
                    }
                }
            }
        }
        errors.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let text = String(data: handle.availableData, encoding: .utf8) ?? ""
            DispatchQueue.main.async { self?.serverErrors += text }
        }
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self = self, self.server != nil else { return }      // 正常退出时不提示
                let detail = String(self.serverErrors.suffix(600))
                self.showMessage("后台程序意外停止了(退出码 \(finished.terminationStatus))。\n\n\(detail)\n\n请退出后重新打开。")
            }
        }
        do {
            try process.run()
            server = process
        } catch {
            showMessage("启动后台程序失败:\(error.localizedDescription)")
        }
    }

    private func showMessage(_ text: String) {
        let escaped = text.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
        web.loadHTMLString("""
            <meta charset="utf-8"><style>
            body { font: 14px -apple-system, "PingFang SC"; color: #77736a; background: #f3f1ec;
                   display: grid; place-items: center; height: 100vh; margin: 0; white-space: pre-wrap;
                   text-align: center; padding: 40px; box-sizing: border-box; }
            @media (prefers-color-scheme: dark) { body { background: #17181a; color: #9c9a94; } }
            </style>\(escaped)
            """, baseURL: nil)
    }

    // ── 窗口与退出 ──────────────────────────────────────────────────────────
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)               // 只隐藏:后台继续,点程序坞图标再回来
        return false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        NSApp.dockTile.badgeLabel = nil
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard runningTurns > 0 else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "有 AI 正在发言"
        alert.informativeText = "现在退出会中断这一棒。已经说完的内容不受影响,下次打开可以点「重试」。"
        alert.addButton(withTitle: "退出")
        alert.addButton(withTitle: "取消")
        return alert.runModal() == .alertFirstButtonReturn ? .terminateNow : .terminateCancel
    }

    func applicationWillTerminate(_ notification: Notification) {
        guard let process = server else { return }
        server = nil
        process.terminate()                // 后台收到信号后会先叫停所有 AI 再退出
        let deadline = Date().addingTimeInterval(10)
        while process.isRunning && Date() < deadline { usleep(100_000) }
    }

    // ── 界面发来的消息 ──────────────────────────────────────────────────────
    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        if type == "state" {
            runningTurns = body["running"] as? Int ?? 0
        } else if type == "attention", !NSApp.isActive {
            NSApp.requestUserAttention(.informationalRequest)
            NSApp.dockTile.badgeLabel = "!"
            let content = UNMutableNotificationContent()
            content.title = body["title"] as? String ?? "讨论群"
            content.body = body["body"] as? String ?? ""
            content.sound = .default
            UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }

    // ── 网页行为 ────────────────────────────────────────────────────────────
    /// 讨论里的网页链接交给默认浏览器打开,窗口里只留界面本身。
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, action.navigationType == .linkActivated,
           url.host != "127.0.0.1" {
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    /// 「＋ 图片」按钮:弹出系统的选文件窗口,只让选 PNG / JPG。
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.allowedContentTypes = [.png, .jpeg]
        panel.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    // ── 菜单(没有「编辑」菜单的话,⌘C / ⌘V 在网页里不起作用)───────────────────
    private func buildMenu() {
        let main = NSMenu()
        func menu(_ title: String, _ items: [NSMenuItem]) {
            let holder = NSMenuItem()
            holder.submenu = NSMenu(title: title)
            items.forEach { holder.submenu!.addItem($0) }
            main.addItem(holder)
        }
        func item(_ title: String, _ action: Selector?, _ key: String = "",
                  _ modifiers: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let entry = NSMenuItem(title: title, action: action, keyEquivalent: key)
            entry.keyEquivalentModifierMask = modifiers
            return entry
        }
        menu("讨论群", [
            item("关于讨论群", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("隐藏讨论群", #selector(NSApplication.hide(_:)), "h"),
            item("退出讨论群", #selector(NSApplication.terminate(_:)), "q"),
        ])
        menu("编辑", [
            item("撤销", Selector(("undo:")), "z"),
            item("重做", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("剪切", #selector(NSText.cut(_:)), "x"),
            item("拷贝", #selector(NSText.copy(_:)), "c"),
            item("粘贴", #selector(NSText.paste(_:)), "v"),
            item("全选", #selector(NSText.selectAll(_:)), "a"),
        ])
        menu("窗口", [
            item("显示主窗口", #selector(showMainWindow), "0"),
            item("最小化", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("重新载入界面", #selector(reloadPage), "r"),
        ])
        NSApp.mainMenu = main
    }

    @objc private func showMainWindow() { window.makeKeyAndOrderFront(nil) }
    @objc private func reloadPage() { web.reload() }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
