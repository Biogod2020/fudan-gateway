import AppKit
import Darwin
import Network

final class MenuBarApp: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    let base = ProcessInfo.processInfo.environment["FUDANGATEWAY_ATRUST_HOME"] ?? (NSHomeDirectory() + "/Library/Application Support/FudanGateway/atrust")
    let service = "gui/\(getuid())/com.biogod.fudan-gateway.atrust"
    var item: NSStatusItem!
    let popover = NSPopover()
    let stateLabel = NSTextField(labelWithString: "正在读取状态")
    let detailLabel = NSTextField(wrappingLabelWithString: "")
    let checkLabel = NSTextField(wrappingLabelWithString: "每 5 分钟轻量检查校园连接；失败后再次确认。")
    var toggleButton: NSButton!
    var loginButton: NSButton!
    var checkButton: NSButton!
    var quitUIOnlyButton: NSButton!
    var quitGatewayButton: NSButton!
    let statusMenu = NSMenu()
    let panelSize = NSSize(width: 352, height: 400)
    var watcher: DispatchSourceFileSystemObject?
    var state = "unknown"
    var busy = false
    var pendingLogin = false
    var loginProcess: Process?
    var overview: NSWindow?
    let pathMonitor = NWPathMonitor()
    var lastNetworkPath: String?
    var wakeCheck: DispatchWorkItem?
    var recoveryEnabled = false
    var active: Bool { ["running", "starting", "checking", "degraded", "recovering"].contains(state) }

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.target = self
        item.button?.action = #selector(togglePopover)
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        item.button?.imagePosition = .imageLeading
        item.button?.font = .systemFont(ofSize: 12, weight: .medium)
        buildMenus()
        buildPopover()
        // Watch atomic status-file replacement, not a timer or a network poll.
        let fd = open(base, O_EVTONLY)
        if fd >= 0 {
            let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd,
                         eventMask: [.write, .rename], queue: .main)
            source.setEventHandler { [weak self] in self?.refresh() }
            source.setCancelHandler { close(fd) }
            source.resume()
            watcher = source
        }
        refresh()
        NSWorkspace.shared.notificationCenter.addObserver(self, selector: #selector(didWake),
            name: NSWorkspace.didWakeNotification, object: nil)
        // Path changes are notifications, not a repeating poll. The first
        // callback is only a baseline, so opening the UI never starts a service.
        pathMonitor.pathUpdateHandler = { [weak self] path in
            let signature = "\(path.status)-" + path.availableInterfaces.map { $0.name }.sorted().joined(separator: ",")
            DispatchQueue.main.async {
                guard let self = self else { return }
                let changed = self.lastNetworkPath != nil && self.lastNetworkPath != signature
                self.lastNetworkPath = signature
                if changed && path.status == .satisfied { self.scheduleWakeCheck() }
            }
        }
        pathMonitor.start(queue: DispatchQueue(label: "com.biogod.fudan-gateway.atrust.network", qos: .utility))
        if !UserDefaults.standard.bool(forKey: "overviewShown") {
            showOverview()
            UserDefaults.standard.set(true, forKey: "overviewShown")
        }
    }

    @objc func didWake(_ notification: Notification) { scheduleWakeCheck() }

    func scheduleWakeCheck() {
        wakeCheck?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            self.refresh()
            // Signal only an already-enabled supervisor. A user's stop/quit
            // remains a hard stop, even after wake or a Wi-Fi change.
            if self.active && self.recoveryEnabled && !self.busy { self.command(["kill", "SIGUSR1", self.service]) }
        }
        wakeCheck = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 10, execute: work)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showOverview()
        return true
    }

    func showOverview() {
        popover.performClose(nil)
        if overview == nil {
            overview = NSWindow(contentRect: NSRect(origin: .zero, size: panelSize),
                                styleMask: [.titled, .closable], backing: .buffered, defer: false)
            overview?.title = "复旦 aTrust 代理"
            overview?.isReleasedWhenClosed = false
            overview?.center()
        }
        overview?.contentView = popover.contentViewController?.view
        overview?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func button(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        b.controlSize = .regular
        b.heightAnchor.constraint(equalToConstant: 30).isActive = true
        return b
    }

    func buildMenus() {
        let showItem = NSMenuItem(title: "打开复旦 aTrust 代理", action: #selector(openOverview), keyEquivalent: "")
        showItem.target = self
        statusMenu.addItem(showItem)
        statusMenu.addItem(.separator())
        for (title, action) in [("仅退出界面（代理继续运行）", #selector(quitUI)),
                                ("退出并停止代理", #selector(quitGateway))] {
            let menuItem = NSMenuItem(title: title, action: action, keyEquivalent: "")
            menuItem.target = self
            statusMenu.addItem(menuItem)
        }
        let mainMenu = NSMenu()
        let applicationItem = NSMenuItem()
        let applicationMenu = NSMenu(title: "FudanGateway")
        let quitItem = NSMenuItem(title: "退出并停止代理", action: #selector(quitGateway), keyEquivalent: "q")
        quitItem.target = self
        applicationMenu.addItem(quitItem)
        applicationItem.submenu = applicationMenu
        mainMenu.addItem(applicationItem)
        NSApp.mainMenu = mainMenu
    }

    @objc func openOverview() { showOverview() }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        return !busy
    }

    func buildPopover() {
        let controller = NSViewController()
        controller.view = NSView(frame: NSRect(origin: .zero, size: panelSize))
        let title = NSTextField(labelWithString: "复旦 aTrust 代理")
        title.font = .systemFont(ofSize: 19, weight: .semibold)
        let subtitle = NSTextField(labelWithString: "本地 SOCKS · 127.0.0.1:1080")
        subtitle.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        subtitle.textColor = .secondaryLabelColor
        stateLabel.font = .systemFont(ofSize: 14, weight: .semibold)
        detailLabel.font = .systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.maximumNumberOfLines = 3
        checkLabel.font = .systemFont(ofSize: 11)
        checkLabel.textColor = .secondaryLabelColor
        checkLabel.maximumNumberOfLines = 3
        let separator = NSBox()
        separator.boxType = .separator
        toggleButton = button("启动代理", #selector(toggleGateway))
        loginButton = button("重新登录 UIS", #selector(login))
        checkButton = button("检查校园连接", #selector(checkConnection))
        let row1 = NSStackView(views: [toggleButton, loginButton])
        let row2 = NSStackView(views: [checkButton, button("查看日志", #selector(showLogs))])
        for row in [row1, row2] { row.distribution = .fillEqually; row.spacing = 8 }
        let footnote = NSTextField(labelWithString: "按需传输 · 正常休眠 · 不接管系统路由")
        footnote.font = .systemFont(ofSize: 10)
        footnote.textColor = .tertiaryLabelColor
        let exitSeparator = NSBox()
        exitSeparator.boxType = .separator
        quitUIOnlyButton = button("仅退出界面", #selector(quitUI))
        quitUIOnlyButton.toolTip = "关闭窗口和菜单栏图标，校园代理继续运行。"
        quitGatewayButton = button("退出并停止代理", #selector(quitGateway))
        let exitRow = NSStackView(views: [quitUIOnlyButton, quitGatewayButton])
        exitRow.distribution = .fillEqually
        exitRow.spacing = 8
        let exitNote = NSTextField(labelWithString: "仅退出界面时，代理继续运行。")
        exitNote.font = .systemFont(ofSize: 11)
        exitNote.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [title, subtitle, separator, stateLabel, detailLabel,
                                      row1, row2, checkLabel, footnote, exitSeparator, exitRow, exitNote])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false
        controller.view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: controller.view.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: controller.view.trailingAnchor, constant: -18),
            stack.topAnchor.constraint(equalTo: controller.view.topAnchor, constant: 18),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: controller.view.bottomAnchor, constant: -18),
            row1.widthAnchor.constraint(equalTo: stack.widthAnchor),
            row2.widthAnchor.constraint(equalTo: stack.widthAnchor),
            exitRow.widthAnchor.constraint(equalTo: stack.widthAnchor),
            exitSeparator.widthAnchor.constraint(equalTo: stack.widthAnchor),
            separator.widthAnchor.constraint(equalTo: stack.widthAnchor),
            detailLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
            checkLabel.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
        popover.contentViewController = controller
        popover.contentSize = panelSize
        popover.behavior = .transient
    }

    func refresh() {
        var metadata: [String: String] = [:]
        if let data = try? Data(contentsOf: URL(fileURLWithPath: base + "/status.json")),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: String] {
            state = object["state"] ?? "unknown"
            metadata = object
        }
        recoveryEnabled = metadata["version"] == "1.2"
        let display: (String, String, NSColor, String)
        switch state {
        case "running":
            if recoveryEnabled {
                display = ("校园连接可用", "checkmark.circle.fill", .systemGreen, "最近一次校园访问检查通过；不代表所有服务器或期刊权限均可用。")
            } else {
                display = ("运行中 · 优化待启用", "circle", .systemOrange, "当前连接保持不变；新的恢复逻辑将在下次启动后台时生效。")
            }
        case "starting": display = ("正在连接", "arrow.triangle.2.circlepath", .systemOrange, "正在复用保存的会话，建立本地校园代理。")
        case "checking": display = ("正在检查校园连接", "arrow.triangle.2.circlepath", .systemOrange, "代理已启动，正在验证真正的校内访问。")
        case "degraded": display = ("连接待确认", "exclamationmark.circle", .systemOrange, "首次检查未通过，30 秒后再次确认；暂不要求重新登录。")
        case "recovering": display = ("自动重连中", "arrow.triangle.2.circlepath", .systemOrange, "后台将复用保存的会话重连；也可以立即重试。")
        case "needs_login": display = ("需要登录", "exclamationmark.circle.fill", .systemOrange, "UIS 会话需要重新认证。已停止自动重试，其他网络服务不受影响。")
        case "stopped": display = ("已停止", "pause.circle", .secondaryLabelColor, "校园代理已停止；v2rayN 和 Tailscale 不受影响。")
        case "exited_retry_pending", "start_failed": display = ("等待恢复", "exclamationmark.circle", .systemOrange, "进程异常退出；后台重启限速为 5 分钟，也可手动启动。")
        default: display = ("尚未启动", "circle", .secondaryLabelColor, "点击启动代理；首次使用需要完成 UIS 登录。")
        }
        item.button?.title = " 复旦"
        let image = NSImage(systemSymbolName: display.1, accessibilityDescription: display.0)
        image?.isTemplate = true
        image?.size = NSSize(width: 16, height: 16)
        item.button?.image = image
        // Template images and the default tint follow the menu bar's appearance,
        // including wallpaper contrast, dark mode and the highlighted state.
        item.button?.contentTintColor = nil
        item.button?.toolTip = "复旦 aTrust 代理 · " + display.0
        item.button?.setAccessibilityLabel("复旦 aTrust 代理 · " + display.0)
        item.button?.setAccessibilityIdentifier("fudan-gateway-status")
        stateLabel.stringValue = display.0
        stateLabel.textColor = display.2
        detailLabel.stringValue = metadata["detail"] ?? display.3
        if state == "recovering", let raw = metadata["retry_at"],
           let retry = ISO8601DateFormatter().date(from: raw) {
            let formatter = DateFormatter()
            formatter.dateFormat = "HH:mm:ss"
            detailLabel.stringValue = "将于 \(formatter.string(from: retry)) 自动重试；复用保存的会话，无需反复授权。"
        }
        if let raw = metadata["last_checked_at"], let checked = ISO8601DateFormatter().date(from: raw) {
            let formatter = DateFormatter()
            formatter.dateFormat = "HH:mm:ss"
            let target = metadata["health_target"] ?? "校园访问"
            checkLabel.stringValue = "最近检查：\(formatter.string(from: checked)) · \(target)\n正常每 5 分钟检查；失败后再次确认。"
        }
        toggleButton.title = active ? "停止代理" : "启动代理"
        toggleButton.isEnabled = !busy
        loginButton.isEnabled = !busy
        checkButton.title = state == "recovering" ? "立即重试" : "检查校园连接"
        checkButton.isEnabled = active && !busy
        quitUIOnlyButton.isEnabled = !busy
        quitGatewayButton.isEnabled = !busy
        if pendingLogin && !active {
            pendingLogin = false
            // Give the stopped supervisor time to release its runtime lock.
            // One-shot UI transition, not an idle/background polling timer.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.beginLogin() }
        }
    }

    @objc func togglePopover() {
        if NSApp.currentEvent?.type == .rightMouseUp, let button = item.button {
            popover.performClose(nil)
            statusMenu.popUp(positioning: nil, at: NSPoint(x: 0, y: button.bounds.minY), in: button)
            return
        }
        if popover.isShown { popover.performClose(nil); return }
        if overview?.isVisible == true { overview?.orderOut(nil) }
        if let view = overview?.contentView { popover.contentViewController?.view = view }
        refresh()
        if let button = item.button { popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY) }
        NSApp.activate(ignoringOtherApps: true)
    }

    func confirmInterruption(_ message: String) -> Bool {
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = "现有校园 SSH 连接可能中断。其他 v2rayN 和 Tailscale 流量不会切换。"
        alert.addButton(withTitle: "继续")
        alert.addButton(withTitle: "取消")
        return alert.runModal() == .alertFirstButtonReturn
    }

    func command(_ args: [String], completion: @escaping (Int32) -> Void = { _ in }) {
        DispatchQueue.global(qos: .utility).async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            p.arguments = args
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            do { try p.run(); p.waitUntilExit() }
            catch { DispatchQueue.main.async { completion(-1) }; return }
            DispatchQueue.main.async { completion(p.terminationStatus); self.refresh() }
        }
    }

    func startGateway() {
        command(["kickstart", service]) { result in
            guard result != 0 else { return }
            // A full quit unloads this service. Restore its existing LaunchAgent
            // on the next explicit start, including its saved-session behavior.
            self.command(["bootstrap", "gui/\(getuid())", NSHomeDirectory() + "/Library/LaunchAgents/com.biogod.fudan-gateway.atrust.plist"])
        }
    }

    @objc func toggleGateway() {
        if state == "needs_login" { beginLogin(); return }
        if active {
            guard confirmInterruption("停止校园代理？") else { return }
            command(["kill", "SIGTERM", service])
        } else { startGateway() }
    }

    @objc func login() {
        if active {
            guard confirmInterruption("停止当前校园连接并重新登录 UIS？") else { return }
            pendingLogin = true
            command(["kill", "SIGTERM", service]) { result in
                if result != 0 { self.pendingLogin = false }
            }
        } else { beginLogin() }
    }

    func beginLogin() {
        guard !busy else { return }
        busy = true
        refresh()
        popover.performClose(nil)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: base + "/bin/fudan-login")
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.busy = false
                if process.terminationStatus == 0 { self.startGateway() }
                self.refresh()
                self.loginProcess = nil
            }
        }
        do { try p.run(); loginProcess = p }
        catch { busy = false; refresh() }
    }

    @objc func checkConnection() {
        if state == "recovering" {
            command(["kill", "SIGUSR1", service])
            checkLabel.stringValue = "已请求立即重试；不会启动已被手动停止的代理。"
            return
        }
        if recoveryEnabled { command(["kill", "SIGUSR1", service]) }
        checkButton.isEnabled = false
        checkLabel.stringValue = "正在通过校园代理请求 PAI 校内网站……"
        DispatchQueue.global(qos: .utility).async {
            let p = Process(), pipe = Pipe()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
            p.arguments = ["--socks5-hostname", "127.0.0.1:1080", "--noproxy", "", "--connect-timeout", "8", "--max-time", "15", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "http://pai-console.cfff.fudan.edu.cn:32080/"]
            p.standardOutput = pipe
            p.standardError = FileHandle.nullDevice
            var code = ""
            do {
                try p.run()
                code = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                p.waitUntilExit()
            } catch { }
            DispatchQueue.main.async {
                let time = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .short)
                let status = Int(code) ?? 0
                self.checkLabel.stringValue = (200...599).contains(status) ? "\(time) PAI 已响应 · HTTP \(status)\n不代表已登录网站或所有校园服务可用。" : "\(time) PAI 暂未响应，后台会参考备用校内目标复核；不必先重登 UIS。"
                self.checkButton.isEnabled = self.active && !self.busy
            }
        }
    }

    @objc func showLogs() { NSWorkspace.shared.open(URL(fileURLWithPath: base + "/logs")) }
    @objc func quitUI() {
        guard !busy else { return }
        NSApp.terminate(nil)
    }

    @objc func quitGateway() {
        guard !busy else { return }
        refresh()
        if active {
            guard confirmInterruption("退出并停止校园代理？") else { return }
        }
        busy = true
        refresh()
        // Unload the job as well as stopping it, so a pending launchd crash
        // restart cannot bring the gateway back after the user has quit.
        command(["bootout", service]) { result in
            if result == 0 {
                NSApp.terminate(nil)
                return
            }
            self.command(["print", self.service]) { status in
                if status != 0 { NSApp.terminate(nil) }
                else { self.quitFailed() }
            }
        }
    }

    func quitFailed() {
        busy = false
        refresh()
        let alert = NSAlert()
        alert.messageText = "尚未确认代理已停止"
        alert.informativeText = "界面保持打开。请查看日志，或稍后再次点击退出并停止代理。"
        alert.addButton(withTitle: "好")
        alert.runModal()
    }
}

let app = NSApplication.shared
let delegate = MenuBarApp()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
