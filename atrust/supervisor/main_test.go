package main

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func TestAuthenticationStop(t *testing.T) {
	for _, line := range []string{
		"Visit https://id.fudan.edu.cn/login to login, and catch the callback url",
		"aTrust session maintenance stopped: aTrust session is invalid; reauthentication required",
		"Please enter the callback URL:",
	} {
		if !requiresLogin(line) {
			t.Fatalf("missed authentication condition: %s", line)
		}
	}
	if requiresLogin("aTrust authConfig refresh failed: network timeout") {
		t.Fatal("temporary network failure must not be mistaken for expired authentication")
	}
}

// Real child processes exercise stdout ordering, graceful stop and process exit.
// Their runtime and saved-session placeholder are isolated from the installation.
func TestSupervisorChildHelper(t *testing.T) {
	mode := os.Getenv("FUDAN_TEST_CHILD")
	if mode == "" {
		return
	}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM)
	switch mode {
	case "network":
		fmt.Println("aTrust authConfig refresh failed: network timeout")
		fmt.Println("VPN client setup error: no reachable node for ip request")
		os.Exit(7)
	case "auth":
		fmt.Println("Visit https://id.fudan.edu.cn/login?code=test-secret to login, and catch the callback url")
		os.Exit(1)
	case "ready":
		fmt.Println("SOCKS5 server listening on 127.0.0.1:1080")
	case "hang":
	default:
		os.Exit(8)
	}
	<-signals
	os.Exit(0)
}

func childCommand(mode string) *exec.Cmd {
	cmd := exec.Command(os.Args[0], "-test.run=^TestSupervisorChildHelper$")
	cmd.Env = []string{"FUDAN_TEST_CHILD=" + mode}
	return cmd
}

func testOptions(t *testing.T) options {
	t.Helper()
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "logs"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "client-data.json"), []byte("saved-session-placeholder"), 0600); err != nil {
		t.Fatal(err)
	}
	o := defaults(root)
	o.healthInterval = time.Hour
	o.confirmDelay = 80 * time.Millisecond
	o.startupTimeout = 2 * time.Second
	o.stopTimeout = 300 * time.Millisecond
	o.retryDelays = []time.Duration{20 * time.Millisecond, 40 * time.Millisecond, 80 * time.Millisecond}
	o.probe = func(context.Context) healthResult { return healthResult{true, "isolated campus"} }
	o.command = func() *exec.Cmd { return childCommand("ready") }
	o.notify = func() { t.Error("unexpected reauthentication request") }
	return o
}

func readTestState(root string) map[string]string {
	var state map[string]string
	data, err := os.ReadFile(filepath.Join(root, "status.json"))
	if err == nil {
		_ = json.Unmarshal(data, &state)
	}
	return state
}

func waitTestState(t *testing.T, root, wanted string) map[string]string {
	t.Helper()
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		state := readTestState(root)
		if state["state"] == wanted {
			return state
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("state never became %s; got %v", wanted, readTestState(root))
	return nil
}

func startTestSupervisor(t *testing.T, o options) (context.CancelFunc, chan struct{}, <-chan int) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	wake := make(chan struct{}, 1)
	done := make(chan int, 1)
	go func() { s := supervisor{opts: o}; done <- s.run(ctx, wake) }()
	t.Cleanup(cancel)
	return cancel, wake, done
}

func awaitTestExit(t *testing.T, done <-chan int) {
	t.Helper()
	select {
	case code := <-done:
		if code != 0 {
			t.Fatalf("supervisor exited with code %d", code)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("supervisor did not stop promptly")
	}
}

func TestTransientStartupRecoversWithoutLogin(t *testing.T) {
	o := testOptions(t)
	var starts atomic.Int32
	o.command = func() *exec.Cmd {
		if starts.Add(1) == 1 {
			return childCommand("network")
		}
		return childCommand("ready")
	}
	cancel, _, done := startTestSupervisor(t, o)
	state := waitTestState(t, o.root, "running")
	if starts.Load() != 2 || state["retry_count"] != "1" {
		t.Fatalf("did not retry: %v", state)
	}
	cancel()
	awaitTestExit(t, done)
	data, _ := os.ReadFile(filepath.Join(o.root, "client-data.json"))
	if string(data) != "saved-session-placeholder" {
		t.Fatal("saved session changed")
	}
}

func TestExplicitAuthenticationStopsRetries(t *testing.T) {
	o := testOptions(t)
	var starts, notifications atomic.Int32
	o.command = func() *exec.Cmd { starts.Add(1); return childCommand("auth") }
	o.notify = func() { notifications.Add(1) }
	_, _, done := startTestSupervisor(t, o)
	awaitTestExit(t, done)
	if readTestState(o.root)["state"] != "needs_login" || starts.Load() != 1 || notifications.Load() != 1 {
		t.Fatal("authentication did not stop retries and notify once")
	}
	log, _ := os.ReadFile(filepath.Join(o.root, "logs", "gateway.log"))
	if strings.Contains(string(log), "test-secret") {
		t.Fatal("callback leaked")
	}
}

func TestManualStopDuringBackoffIsFinal(t *testing.T) {
	o := testOptions(t)
	o.retryDelays = []time.Duration{time.Hour}
	var starts atomic.Int32
	o.command = func() *exec.Cmd { starts.Add(1); return childCommand("network") }
	cancel, _, done := startTestSupervisor(t, o)
	waitTestState(t, o.root, "recovering")
	cancel()
	awaitTestExit(t, done)
	if starts.Load() != 1 || readTestState(o.root)["state"] != "stopped" {
		t.Fatal("manual stop retried")
	}
}

func TestStartupWatchdogRecoversHungClient(t *testing.T) {
	o := testOptions(t)
	o.startupTimeout = 150 * time.Millisecond
	var starts atomic.Int32
	o.command = func() *exec.Cmd {
		if starts.Add(1) == 1 {
			return childCommand("hang")
		}
		return childCommand("ready")
	}
	cancel, _, done := startTestSupervisor(t, o)
	waitTestState(t, o.root, "running")
	if starts.Load() != 2 {
		t.Fatal("hung client was not rebuilt")
	}
	cancel()
	awaitTestExit(t, done)
}

func TestLiveButBrokenTunnelRequiresConfirmation(t *testing.T) {
	o := testOptions(t)
	var probes, starts atomic.Int32
	o.command = func() *exec.Cmd { starts.Add(1); return childCommand("ready") }
	o.probe = func(context.Context) healthResult {
		if probes.Add(1) <= 2 {
			return healthResult{}
		}
		return healthResult{true, "recovered campus"}
	}
	cancel, wake, done := startTestSupervisor(t, o)
	waitTestState(t, o.root, "degraded")
	// A wake event must not bypass the failure-confirmation grace interval.
	wake <- struct{}{}
	time.Sleep(25 * time.Millisecond)
	if starts.Load() != 1 || probes.Load() != 1 {
		t.Fatal("restarted before confirmation")
	}
	waitTestState(t, o.root, "running")
	if starts.Load() != 2 || probes.Load() != 3 {
		t.Fatal("broken live tunnel did not recover")
	}
	cancel()
	awaitTestExit(t, done)
}

func TestSingleFailedRoundDoesNotRestart(t *testing.T) {
	o := testOptions(t)
	var probes, starts atomic.Int32
	o.command = func() *exec.Cmd { starts.Add(1); return childCommand("ready") }
	o.probe = func(context.Context) healthResult {
		if probes.Add(1) == 1 {
			return healthResult{}
		}
		return healthResult{true, "healthy fallback"}
	}
	cancel, _, done := startTestSupervisor(t, o)
	waitTestState(t, o.root, "running")
	if starts.Load() != 1 {
		t.Fatal("single failed round broke a healthy client")
	}
	cancel()
	awaitTestExit(t, done)
}

func TestWakeChecksWithoutBlindRestart(t *testing.T) {
	o := testOptions(t)
	var probes, starts atomic.Int32
	o.command = func() *exec.Cmd { starts.Add(1); return childCommand("ready") }
	o.probe = func(context.Context) healthResult { probes.Add(1); return healthResult{true, "campus"} }
	cancel, wake, done := startTestSupervisor(t, o)
	waitTestState(t, o.root, "running")
	wake <- struct{}{}
	deadline := time.Now().Add(time.Second)
	for probes.Load() < 2 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if probes.Load() != 2 || starts.Load() != 1 {
		t.Fatal("wake did not probe the existing client")
	}
	cancel()
	awaitTestExit(t, done)
}

func TestStopCancelsInFlightProbe(t *testing.T) {
	o := testOptions(t)
	started := make(chan struct{})
	o.probe = func(ctx context.Context) healthResult { close(started); <-ctx.Done(); return healthResult{} }
	cancel, _, done := startTestSupervisor(t, o)
	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("probe not started")
	}
	cancel()
	awaitTestExit(t, done)
	if readTestState(o.root)["state"] != "stopped" {
		t.Fatal("probe revived stopped service")
	}
}

func TestRuntimeLockDoesNotFightInteractiveLogin(t *testing.T) {
	o := testOptions(t)
	lock, err := os.OpenFile(filepath.Join(o.root, "runtime.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	o.command = func() *exec.Cmd { t.Fatal("client started over interactive login"); return nil }
	s := supervisor{opts: o}
	if s.run(context.Background(), make(chan struct{})) != 0 {
		t.Fatal("lock contention became a crash loop")
	}
}

func TestRetryBackoffCapsAtFiveMinutes(t *testing.T) {
	s := supervisor{opts: defaults(t.TempDir())}
	expected := []time.Duration{10 * time.Second, 20 * time.Second, 40 * time.Second, 80 * time.Second, 160 * time.Second, 5 * time.Minute}
	for i := 0; i < 20; i++ {
		s.retries = i
		if s.retryDelay() != expected[min(i, len(expected)-1)] {
			t.Fatal("retry cap changed")
		}
	}
}

func TestMissingSessionDoesNotRetryOrStartClient(t *testing.T) {
	o := testOptions(t)
	if err := os.Remove(filepath.Join(o.root, "client-data.json")); err != nil {
		t.Fatal(err)
	}
	var notifications atomic.Int32
	o.command = func() *exec.Cmd { t.Fatal("started client without saved session"); return nil }
	o.notify = func() { notifications.Add(1) }
	s := supervisor{opts: o}
	if s.run(context.Background(), make(chan struct{})) != 0 || notifications.Load() != 1 || readTestState(o.root)["state"] != "needs_login" {
		t.Fatal("missing session was not handled once")
	}
}

func TestHealthyPeriodResetsRetryBackoff(t *testing.T) {
	o := testOptions(t)
	o.healthInterval = 30 * time.Millisecond
	o.stableAfter = 50 * time.Millisecond
	var starts atomic.Int32
	o.command = func() *exec.Cmd {
		if starts.Add(1) == 1 {
			return childCommand("network")
		}
		return childCommand("ready")
	}
	cancel, _, done := startTestSupervisor(t, o)
	state := waitTestState(t, o.root, "running")
	if state["retry_count"] != "1" {
		t.Fatal("backoff was reset before stable operation")
	}
	deadline := time.Now().Add(2 * time.Second)
	for readTestState(o.root)["retry_count"] != "0" && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if readTestState(o.root)["retry_count"] != "0" || starts.Load() != 2 {
		t.Fatal("stable healthy client did not reset backoff")
	}
	cancel()
	awaitTestExit(t, done)
}

// Minimal local SOCKS server: these tests distinguish a real protocol response
// from a listening proxy or a successful CONNECT with a broken downstream.
func fakeSOCKS(t *testing.T, mode string) (string, *atomic.Int32) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var sshProbes atomic.Int32
	var workers sync.WaitGroup
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			workers.Add(1)
			go func() {
				defer workers.Done()
				defer conn.Close()
				_ = conn.SetDeadline(time.Now().Add(time.Second))
				var greeting [3]byte
				if _, err := io.ReadFull(conn, greeting[:]); err != nil {
					return
				}
				_, _ = conn.Write([]byte{5, 0})
				var request [5]byte
				if _, err := io.ReadFull(conn, request[:]); err != nil {
					return
				}
				if request[3] != 3 {
					return
				}
				address := make([]byte, int(request[4])+2)
				if _, err := io.ReadFull(conn, address); err != nil {
					return
				}
				port := binary.BigEndian.Uint16(address[len(address)-2:])
				_, _ = conn.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 0})
				if mode == "connect-only" {
					return
				}
				if port == 32080 {
					if mode == "fallback" {
						return
					}
					_, _ = http.ReadRequest(bufio.NewReader(conn))
					_, _ = io.WriteString(conn, "HTTP/1.1 302 Found\r\nContent-Length: 0\r\nLocation: /login\r\n\r\n")
				} else {
					sshProbes.Add(1)
					_, _ = io.WriteString(conn, "SSH-2.0-OpenSSH-test\r\n")
				}
			}()
		}
	}()
	t.Cleanup(func() { _ = listener.Close(); <-done; workers.Wait() })
	return listener.Addr().String(), &sshProbes
}

func TestCampusProbeRequiresRealDownstreamResponse(t *testing.T) {
	proxy, _ := fakeSOCKS(t, "connect-only")
	if campusHealth(context.Background(), proxy, healthTargets{HTTP: "pai-console.cfff.fudan.edu.cn:32080", SSH: "ssh.campus.example:22"}, time.Second).ok {
		t.Fatal("CONNECT alone marked campus healthy")
	}
}

func TestCampusProbeUsesHTTPWithoutUnnecessarySSH(t *testing.T) {
	proxy, sshProbes := fakeSOCKS(t, "http")
	result := campusHealth(context.Background(), proxy, healthTargets{HTTP: "pai-console.cfff.fudan.edu.cn:32080", SSH: "ssh.campus.example:22"}, time.Second)
	if !result.ok || result.target != "PAI HTTP" || sshProbes.Load() != 0 {
		t.Fatal("primary probe failed or wasted a fallback")
	}
}

func TestCampusProbeFallsBackWhenOneServerFails(t *testing.T) {
	proxy, sshProbes := fakeSOCKS(t, "fallback")
	result := campusHealth(context.Background(), proxy, healthTargets{HTTP: "pai-console.cfff.fudan.edu.cn:32080", SSH: "ssh.campus.example:22"}, time.Second)
	if !result.ok || result.target != "campus SSH banner" || sshProbes.Load() != 1 {
		t.Fatal("single website failure was treated as a gateway failure")
	}
}

func TestLogRedaction(t *testing.T) {
	for _, line := range []string{
		"Visit https://id.fudan.edu.cn/login?code=secret to login, and catch the callback url",
		"request failed: https://vpn.fudan.edu.cn/callback?code=secret&state=private",
		"SID: secret", "Cookie: secret", "password=secret", "SignKey: secret",
	} {
		if strings.Contains(safeLine(line), "secret") {
			t.Fatal("credential leaked")
		}
	}
	if safeLine("aTrust session refreshed (SID changed: true)") != "aTrust session refreshed (SID changed: true)" {
		t.Fatal("safe refresh event should remain visible")
	}
}
