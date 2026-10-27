import AppKit
import WebKit

// One-shot UIS login window. Passwords stay in the university's web page.
// Capture the OAuth callback BEFORE WebKit consumes its single-use code.
// Pass it directly to zju-connect's stdin; never save or print the callback.
final class LoginApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    let base = ProcessInfo.processInfo.environment["FUDANGATEWAY_ATRUST_HOME"] ?? (NSHomeDirectory() + "/Library/Application Support/FudanGateway/atrust")
    let child = Process()
    let input = Pipe()
    let output = Pipe()
    var buffer = Data()
    var window: NSWindow!
    var web: WKWebView!
    var status: NSTextField!
    var callbackSent = false
    var succeeded = false
    var cancelled = false
    var log: FileHandle!
    var lockFD: Int32 = -1

    func applicationDidFinishLaunching(_ notification: Notification) {
        umask(0o077)
        lockFD = open(base + "/runtime.lock", O_CREAT | O_RDWR, 0o600)
        guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
            print("LOGIN_NOT_STARTED: gateway already running; stop it before reauthentication")
            exit(1)
        }
        let logPath = base + "/logs/login.log"
        FileManager.default.createFile(atPath: logPath, contents: nil,
                                      attributes: [.posixPermissions: 0o600])
        log = FileHandle(forWritingAtPath: logPath)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 980, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "复旦 aTrust 代理：首次 UIS 授权"
        window.delegate = self
        let content = window.contentView!
        status = NSTextField(labelWithString: "正在连接 vpn.fudan.edu.cn；不会修改系统路由或 DNS。")
        status.frame = NSRect(x: 16, y: 725, width: 948, height: 24)
        status.autoresizingMask = [.width, .minYMargin]
        content.addSubview(status)
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 980, height: 715), configuration: configuration)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.uiDelegate = self
        content.addSubview(web)
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        child.executableURL = URL(fileURLWithPath: base + "/bin/zju-connect")
        child.arguments = ["-config", base + "/config.toml"]
        child.currentDirectoryURL = URL(fileURLWithPath: base)
        child.environment = ["HOME": NSHomeDirectory(), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
        child.standardInput = input
        child.standardOutput = output
        child.standardError = output
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async { self?.consume(data) }
        }
        child.terminationHandler = { [weak self] process in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                guard let self = self else { return }
                print(self.succeeded ? "AUTH_SAVED: ready for separate connection verification" : "LOGIN_STOPPED: see private login log; no routing changes applied")
                self.log?.closeFile()
                exit(self.succeeded ? 0 : 1)
            }
        }
        do { try child.run() }
        catch { print("LOGIN_START_FAILED"); exit(1) }
    }

    func consume(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 10) {
            let line = String(decoding: buffer[..<newline], as: UTF8.self)
            buffer.removeSubrange(...newline)
            if let start = line.range(of: "Visit "), let end = line.range(of: " to login, and catch the callback url") {
                let raw = String(line[start.upperBound..<end.lowerBound])
                guard let url = URL(string: raw), url.scheme == "https", url.host == "id.fudan.edu.cn" else {
                    status.stringValue = "登录地址不符合预期；已停止。"
                    child.terminate()
                    return
                }
                status.stringValue = "请在复旦官方 id.fudan.edu.cn 页面完成 UIS 登录；无需向聊天提供密码或验证码。"
                web.load(URLRequest(url: url))
                log.write(Data("UIS login requested; URL withheld.\n".utf8))
                print("UIS_WINDOW_READY: waiting for user authentication")
                continue
            }
            // Normal diagnostics only; strip query strings and sensitive values.
            var safe = line.replacingOccurrences(of: #"(https?://[^\s?]+)\?[^\s]+"#, with: "$1?[redacted]", options: .regularExpression)
            if safe.range(of: #"(?i)(cookie|password|ticket|token|sid:|signkey|deviceid)"#, options: .regularExpression) != nil {
                safe = "[authentication detail withheld]"
            }
            log.write(Data((safe + "\n").utf8))
            if line.contains("SOCKS5 server listening on 127.0.0.1:1080") {
                succeeded = true
                status.stringValue = "登录状态已保存。正在关闭临时登录进程，随后验证后台连接。"
                if child.isRunning { child.terminate() }
            }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "https", url.host == "vpn.fudan.edu.cn", url.path == "/passport/v1/auth/httpsOauth2" {
            decisionHandler(.cancel)
            guard !callbackSent,
                  let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
                  parts.queryItems?.contains(where: { $0.name == "code" && !($0.value ?? "").isEmpty }) == true else { return }
            callbackSent = true
            status.stringValue = "UIS 已授权，正在建立校园会话……"
            input.fileHandleForWriting.write(Data((url.absoluteString + "\n").utf8))
            return
        }
        if action.targetFrame?.isMainFrame != false && url.scheme != "https" && url.scheme != "about" {
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if action.targetFrame == nil { webView.load(action.request) }
        return nil
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        cancelled = true
        if child.isRunning { child.terminate() } else { NSApp.terminate(nil) }
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        if child.isRunning { child.terminate() }
    }
}

let app = NSApplication.shared
let delegate = LoginApp()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
