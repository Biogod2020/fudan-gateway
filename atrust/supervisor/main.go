// Low-power supervision: event-driven lifecycle, bounded retries and a real
// campus probe every five minutes. Never change v2rayN, Tailscale or system routes.
package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func defaultBase() string {
	if configured := os.Getenv("FUDANGATEWAY_ATRUST_HOME"); configured != "" {
		return configured
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Library", "Application Support", "FudanGateway", "atrust")
}

const supervisorVersion = "1.2"

var queryPattern = regexp.MustCompile(`(https?://[^\s?]+)\?[^\s]+`)
var privatePattern = regexp.MustCompile(`(?i)(cookie|password|ticket|token|sid:|signkey|deviceid)`)

func safeLine(line string) string {
	if privatePattern.MatchString(line) || strings.Contains(line, "Visit ") {
		return "[authentication detail withheld]"
	}
	return queryPattern.ReplaceAllString(line, "$1?[redacted]")
}

func requiresLogin(line string) bool {
	return strings.Contains(line, " to login, and catch the callback url") ||
		strings.Contains(line, "aTrust session is invalid; reauthentication required") ||
		strings.Contains(line, "Please enter the callback URL")
}

type healthResult struct {
	ok     bool
	target string
}

type options struct {
	root                                       string
	healthInterval, confirmDelay, probeTimeout time.Duration
	startupTimeout, stableAfter, stopTimeout   time.Duration
	retryDelays                                []time.Duration
	command                                    func() *exec.Cmd
	probe                                      func(context.Context) healthResult
	notify                                     func()
}

func defaults(root string) options {
	o := options{
		root: root, healthInterval: 5 * time.Minute, confirmDelay: 30 * time.Second,
		probeTimeout: 8 * time.Second, startupTimeout: 120 * time.Second,
		stableAfter: 5 * time.Minute, stopTimeout: 8 * time.Second,
		retryDelays: []time.Duration{10 * time.Second, 20 * time.Second, 40 * time.Second,
			80 * time.Second, 160 * time.Second, 5 * time.Minute},
	}
	o.command = func() *exec.Cmd {
		cmd := exec.Command(filepath.Join(root, "bin", "zju-connect"), "-config", filepath.Join(root, "config.toml"))
		cmd.Dir = root
		home, _ := os.UserHomeDir()
		cmd.Env = []string{"HOME=" + home, "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "LANG=en_US.UTF-8"}
		// No stdin: interactive authorization is never attempted unattended.
		return cmd
	}
	o.probe = func(ctx context.Context) healthResult {
		return campusHealth(ctx, "127.0.0.1:1080", defaultHealthTargets(), o.probeTimeout)
	}
	o.notify = func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = exec.CommandContext(ctx, "/usr/bin/osascript", "-e", `display notification "UIS 会话明确失效，请打开 FudanGateway 重新登录。其他网络服务未受影响。" with title "复旦校园代理需要登录"`).Run()
	}
	return o
}

type supervisor struct {
	opts         options
	log          *os.File
	lastChecked  string
	lastOK       string
	lastTarget   string
	healthySince time.Time
	retries      int
}

func (s *supervisor) writeState(state, detail, retryAt string) {
	data, _ := json.MarshalIndent(map[string]string{
		"state": state, "updated_at": time.Now().Format(time.RFC3339), "detail": detail,
		"last_checked_at": s.lastChecked, "last_ok_at": s.lastOK, "health_target": s.lastTarget,
		"retry_at": retryAt, "retry_count": strconv.Itoa(s.retries), "version": supervisorVersion,
		"note": "Health means a recent PAI HTTP response or campus SSH banner, not every service or entitlement.",
	}, "", "  ")
	path := filepath.Join(s.opts.root, "status.json")
	if os.WriteFile(path+".new", append(data, '\n'), 0600) == nil {
		_ = os.Rename(path+".new", path)
	}
}

func (s *supervisor) logLine(line string) {
	path := filepath.Join(s.opts.root, "logs", "gateway.log")
	if s.log != nil {
		if info, err := s.log.Stat(); err == nil && info.Size() > 2<<20 {
			_ = s.log.Close()
			_ = os.Rename(path, path+".1")
			s.log = nil
		}
	}
	if s.log == nil {
		var err error
		s.log, err = os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
		if err != nil {
			fmt.Println("Fudan Gateway: unable to open private runtime log")
			return
		}
	}
	_, _ = fmt.Fprintln(s.log, safeLine(line))
}

func (s *supervisor) event(message string) {
	s.logLine(time.Now().Format("2006/01/02 15:04:05") + " Supervisor: " + message)
}

type outcome int

const (
	retryChild outcome = iota
	stopSupervisor
	loginRequired
)

// Only the owning event loop writes state. Probes are asynchronous so a manual
// stop cancels an unresponsive network immediately, including during backoff.
func (s *supervisor) attempt(ctx context.Context, wake <-chan struct{}) outcome {
	attemptCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	cmd := s.opts.command()
	reader, writer, err := os.Pipe()
	if err != nil {
		s.event("could not create client output pipe; will retry")
		return retryChild
	}
	defer reader.Close()
	cmd.Stdout, cmd.Stderr = writer, writer
	if err = cmd.Start(); err != nil {
		_ = writer.Close()
		s.event("could not start VPN client; will retry")
		return retryChild
	}
	_ = writer.Close()
	done := make(chan struct{})
	go func() { _ = cmd.Wait(); close(done) }()
	defer func() {
		cancel()
		_ = cmd.Process.Signal(syscall.SIGTERM)
		timer := time.NewTimer(s.opts.stopTimeout)
		defer timer.Stop()
		select {
		case <-done:
		case <-timer.C:
			_ = cmd.Process.Kill()
			<-done
		}
	}()
	lines := make(chan string, 16)
	go func() {
		defer close(lines)
		scanner := bufio.NewScanner(reader)
		scanner.Buffer(make([]byte, 4096), 1<<20)
		for scanner.Scan() {
			select {
			case lines <- scanner.Text():
			case <-attemptCtx.Done():
				return
			}
		}
	}()
	s.healthySince = time.Time{}
	s.writeState("starting", "正在复用保存的会话建立校园隧道。", "")
	startup := time.NewTimer(s.opts.startupTimeout)
	defer startup.Stop()
	probeTimer := time.NewTimer(time.Hour)
	probeTimer.Stop()
	defer probeTimer.Stop()
	results := make(chan healthResult, 1)
	ready, probing, failures := false, false, 0
	var confirmAt time.Time
	startProbe := func() {
		if !ready || probing {
			return
		}
		if failures == 1 && time.Now().Before(confirmAt) {
			probeTimer.Reset(time.Until(confirmAt))
			return
		}
		probing = true
		go func() {
			result := s.opts.probe(attemptCtx)
			select {
			case results <- result:
			case <-attemptCtx.Done():
			}
		}()
	}
	for {
		select {
		case <-ctx.Done():
			return stopSupervisor
		case <-wake:
			// Probe first: never blindly break a healthy tunnel after waking.
			s.event("wake/manual check requested")
			startProbe()
		case <-startup.C:
			s.event("startup deadline exceeded; will retry")
			return retryChild
		case line, ok := <-lines:
			if !ok {
				s.event("VPN client exited; saved session retained for retry")
				return retryChild
			}
			s.logLine(line)
			if requiresLogin(line) {
				s.event("explicit session invalidation; automatic retry stopped")
				return loginRequired
			}
			if !ready && strings.Contains(line, "SOCKS5 server listening on 127.0.0.1:1080") {
				ready = true
				startup.Stop()
				s.writeState("checking", "本地代理已启动，正在验证校园访问。", "")
				startProbe()
			}
		case <-probeTimer.C:
			startProbe()
		case result := <-results:
			probing = false
			s.lastChecked = time.Now().Format(time.RFC3339)
			if result.ok {
				failures = 0
				s.lastOK, s.lastTarget = s.lastChecked, result.target
				if s.healthySince.IsZero() {
					s.healthySince = time.Now()
				}
				if time.Since(s.healthySince) >= s.opts.stableAfter {
					s.retries = 0
				}
				s.writeState("running", "校园访问检查通过；每 5 分钟低频检查一次。", "")
				s.event("campus probe passed: " + result.target)
				probeTimer.Reset(s.opts.healthInterval)
			} else {
				s.healthySince = time.Time{}
				failures++
				if failures >= 2 {
					s.event("two independent-campus probe rounds failed; rebuilding VPN client")
					return retryChild
				}
				s.writeState("degraded", "校园检查未通过，30 秒后再次确认；不会因单个服务器故障立即重启。", "")
				s.event("campus probe failed; awaiting confirmation, not reauthentication")
				confirmAt = time.Now().Add(s.opts.confirmDelay)
				probeTimer.Reset(s.opts.confirmDelay)
			}
		}
	}
}

func (s *supervisor) run(ctx context.Context, wake <-chan struct{}) int {
	lock, err := os.OpenFile(filepath.Join(s.opts.root, "runtime.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return 1
	}
	defer lock.Close()
	if syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		fmt.Println("Another gateway or interactive login owns the runtime lock.")
		return 0 // Never fight an interactive login in a launchd crash loop.
	}
	defer func() {
		if s.log != nil {
			_ = s.log.Close()
		}
	}()
	if _, err := os.Stat(filepath.Join(s.opts.root, "client-data.json")); err != nil {
		s.writeState("needs_login", "尚无保存的 UIS 会话，需要首次登录。", "")
		s.opts.notify()
		return 0
	}
	s.event("supervisor " + supervisorVersion + " started; bounded recovery enabled")
	for {
		if ctx.Err() != nil {
			s.writeState("stopped", "已按请求停止；不会自动重连。", "")
			return 0
		}
		result := s.attempt(ctx, wake)
		if result == stopSupervisor || ctx.Err() != nil {
			s.writeState("stopped", "已按请求停止；不会自动重连。", "")
			return 0
		}
		if result == loginRequired {
			s.writeState("needs_login", "UIS 会话明确失效；已停止重试，请手动登录。", "")
			s.opts.notify()
			return 0
		}
		delay := s.retryDelay()
		s.retries++
		retryAt := time.Now().Add(delay).Format(time.RFC3339)
		s.writeState("recovering", "校园连接中断，将自动复用会话重连，无需重复 UIS 授权。", retryAt)
		s.event(fmt.Sprintf("retry %d scheduled in %s", s.retries, delay))
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			s.writeState("stopped", "已按请求停止；不会自动重连。", "")
			return 0
		case <-wake:
			timer.Stop()
			s.event("wake/manual request shortened retry wait")
		case <-timer.C:
		}
	}
}

func (s *supervisor) retryDelay() time.Duration {
	return s.opts.retryDelays[min(s.retries, len(s.opts.retryDelays)-1)]
}

// These probes terminate inside the campus network. A public homepage or a
// successful SOCKS CONNECT alone cannot prove the aTrust tunnel is working.
// PAI is primary; a configured SSH banner is a fallback, with no login attempt.
func campusHealth(ctx context.Context, proxy string, targets healthTargets, timeout time.Duration) healthResult {
	if targets.HTTP != "" && probeHTTP(ctx, proxy, targets.HTTP, targets.HTTP, timeout) == nil {
		return healthResult{true, "PAI HTTP"}
	}
	if ctx.Err() == nil && targets.SSH != "" && probeSSH(ctx, proxy, targets.SSH, timeout) == nil {
		return healthResult{true, "campus SSH banner"}
	}
	return healthResult{}
}

func socksConnect(ctx context.Context, proxy, target string, timeout time.Duration) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	conn, err := (&net.Dialer{}).DialContext(ctx, "tcp", proxy)
	if err != nil {
		return nil, err
	}
	good := false
	defer func() {
		if !good {
			_ = conn.Close()
		}
	}()
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}
	stopClose := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stopClose()
	if _, err = conn.Write([]byte{5, 1, 0}); err != nil {
		return nil, err
	}
	var method [2]byte
	if _, err = io.ReadFull(conn, method[:]); err != nil {
		return nil, err
	}
	if method != [2]byte{5, 0} {
		return nil, fmt.Errorf("SOCKS authentication unavailable")
	}
	host, portString, err := net.SplitHostPort(target)
	if err != nil {
		return nil, err
	}
	port, err := strconv.ParseUint(portString, 10, 16)
	if err != nil || len(host) > 255 {
		return nil, fmt.Errorf("invalid probe target")
	}
	request := []byte{5, 1, 0, 3, byte(len(host))}
	request = append(request, host...)
	request = binary.BigEndian.AppendUint16(request, uint16(port))
	if _, err = conn.Write(request); err != nil {
		return nil, err
	}
	var reply [4]byte
	if _, err = io.ReadFull(conn, reply[:]); err != nil {
		return nil, err
	}
	if reply[0] != 5 || reply[1] != 0 || reply[2] != 0 {
		return nil, fmt.Errorf("SOCKS target unreachable")
	}
	size := 0
	switch reply[3] {
	case 1:
		size = 4
	case 4:
		size = 16
	case 3:
		var length [1]byte
		if _, err = io.ReadFull(conn, length[:]); err != nil {
			return nil, err
		}
		size = int(length[0])
	default:
		return nil, fmt.Errorf("invalid SOCKS reply")
	}
	if _, err = io.ReadFull(conn, make([]byte, size+2)); err != nil {
		return nil, err
	}
	good = true
	return conn, nil
}

func probeHTTP(ctx context.Context, proxy, target, host string, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	conn, err := socksConnect(ctx, proxy, target, timeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	stopClose := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stopClose()
	if _, err = fmt.Fprintf(conn, "HEAD / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\nUser-Agent: FudanGateway-health/1.2\r\n\r\n", host); err != nil {
		return err
	}
	reader := bufio.NewReader(io.LimitReader(conn, 16<<10))
	response, err := http.ReadResponse(reader, &http.Request{Method: http.MethodHead})
	if err != nil {
		return err
	}
	defer response.Body.Close()
	// An application error still proves transport, unlike a missing response.
	if response.StatusCode < 200 || response.StatusCode > 599 {
		return fmt.Errorf("invalid HTTP status")
	}
	return nil
}

func probeSSH(ctx context.Context, proxy, target string, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	conn, err := socksConnect(ctx, proxy, target, timeout)
	if err != nil {
		return err
	}
	defer conn.Close()
	stopClose := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stopClose()
	reader := bufio.NewReader(io.LimitReader(conn, 4096))
	for i := 0; i < 8; i++ {
		line, err := reader.ReadString('\n')
		if err != nil {
			return err
		}
		if strings.HasPrefix(line, "SSH-2.0-") {
			return nil
		}
	}
	return fmt.Errorf("no SSH banner")
}

func run() int {
	syscall.Umask(0077)
	root := flag.String("base", defaultBase(), "Private runtime directory (default: installed Fudan Gateway)")
	check := flag.Bool("check", false, "Probe the existing campus proxy once, without starting or changing it")
	flag.Parse()
	targets, err := loadHealthTargets(*root)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Invalid private health.json; expected campus host:port targets.")
		return 2
	}
	if *check {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		result := campusHealth(ctx, "127.0.0.1:1080", targets, 8*time.Second)
		data, _ := json.Marshal(map[string]any{"ok": result.ok, "target": result.target})
		fmt.Println(string(data))
		if result.ok {
			return 0
		}
		return 2
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	wake := make(chan struct{}, 1)
	signals := make(chan os.Signal, 4)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT, syscall.SIGHUP, syscall.SIGUSR1)
	defer signal.Stop(signals)
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case sig := <-signals:
				if sig == syscall.SIGUSR1 {
					select {
					case wake <- struct{}{}:
					default:
					}
				} else {
					cancel()
					return
				}
			}
		}
	}()
	o := defaults(*root)
	o.probe = func(ctx context.Context) healthResult {
		return campusHealth(ctx, "127.0.0.1:1080", targets, o.probeTimeout)
	}
	s := supervisor{opts: o}
	return s.run(ctx, wake)
}

func main() { os.Exit(run()) }
