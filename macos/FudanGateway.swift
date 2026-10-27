import AppKit
import Foundation

final class GatewayMenu: NSObject, NSApplicationDelegate {
    let root = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/FudanGateway/webvpn")
    let executable = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/bin/fudangateway")
    var item: NSStatusItem!
    var stateItem = NSMenuItem(title: "WebVPN：尚未验证", action: nil, keyEquivalent: "")
    var checkedItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    var watcher: DispatchSourceFileSystemObject?
    var directoryFD: Int32 = -1

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "复旦"
        item.button?.image = NSImage(systemSymbolName: "network", accessibilityDescription: "FudanGateway")
        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "FudanGateway · WebVPN 模式", action: nil, keyEquivalent: ""))
        menu.addItem(stateItem)
        menu.addItem(checkedItem)
        menu.addItem(.separator())
        add(menu, "登录 UIS…", #selector(login))
        add(menu, "打开校园资源", #selector(openResources))
        add(menu, "验证原生 SSH", #selector(doctor))
        add(menu, "查看使用说明", #selector(showHelp))
        menu.addItem(.separator())
        add(menu, "停止登录助手", #selector(stopBroker))
        add(menu, "仅退出界面", #selector(quit))
        item.menu = menu
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        directoryFD = open(root.path, O_EVTONLY)
        if directoryFD >= 0 {
            watcher = DispatchSource.makeFileSystemObjectSource(fileDescriptor: directoryFD,
                        eventMask: [.write, .rename], queue: .main)
            watcher?.setEventHandler { [weak self] in self?.refresh() }
            watcher?.resume()
        }
        refresh()
    }

    func add(_ menu: NSMenu, _ title: String, _ action: Selector) {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
        entry.target = self
        menu.addItem(entry)
    }

    func refresh() {
        guard let data = try? Data(contentsOf: root.appendingPathComponent("status.json")),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }
        let state = value["state"] as? String ?? "unverified"
        switch state {
        case "ready":
            stateItem.title = value["native_ssh_verified"] == nil
                ? "WebVPN：会话已验证" : "WebVPN：原生 SSH 已验证"
        case "needs_login": stateItem.title = "WebVPN：需要 UIS 登录"
        case "stopped": stateItem.title = "WebVPN：登录助手已停止"
        default: stateItem.title = "WebVPN：等待验证"
        }
        if let timestamp = value["checked_at"] as? Double {
            let formatter = DateFormatter()
            formatter.dateFormat = "MM-dd HH:mm:ss"
            checkedItem.title = "上次验证：" + formatter.string(from: Date(timeIntervalSince1970: timestamp))
        } else { checkedItem.title = "" }
    }

    func run(_ arguments: [String]) {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let process = Process()
            process.executableURL = self.executable
            process.arguments = arguments
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            do { try process.run() }
            catch {
                DispatchQueue.main.async { self.stateItem.title = "请先安装 FudanGateway 命令行组件" }
                return
            }
            process.waitUntilExit()
            DispatchQueue.main.async { self.refresh() }
        }
    }

    @objc func login() { run(["login"]) }
    @objc func openResources() { run(["open"]) }
    @objc func stopBroker() { run(["stop"]) }
    @objc func doctor() {
        var target = "headcw"
        if let data = try? Data(contentsOf: root.appendingPathComponent("config.json")),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let configured = config["default_ssh_target"] as? String { target = configured }
        run(["doctor", "--native-ssh", target])
    }
    @objc func showHelp() {
        NSWorkspace.shared.open(URL(string: "https://github.com/Biogod2020/fudan-gateway")!)
    }
    @objc func quit() { NSApp.terminate(nil) }
    func applicationWillTerminate(_ notification: Notification) {
        watcher?.cancel()
        if directoryFD >= 0 { close(directoryFD) }
    }
}

let app = NSApplication.shared
let delegate = GatewayMenu()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
