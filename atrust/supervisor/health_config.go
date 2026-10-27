package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// A private optional fallback avoids publishing a user's campus server address.
type healthTargets struct {
	HTTP string `json:"http_target"`
	SSH  string `json:"ssh_target"`
}

func defaultHealthTargets() healthTargets {
	return healthTargets{HTTP: "pai-console.cfff.fudan.edu.cn:32080"}
}

func loadHealthTargets(root string) (healthTargets, error) {
	targets := defaultHealthTargets()
	data, err := os.ReadFile(filepath.Join(root, "health.json"))
	if os.IsNotExist(err) {
		return targets, nil
	}
	if err != nil {
		return targets, err
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err = decoder.Decode(&targets); err != nil {
		return targets, err
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return targets, fmt.Errorf("trailing JSON")
	}
	if targets.HTTP == "" && targets.SSH == "" {
		return targets, fmt.Errorf("at least one health target is required")
	}
	for _, address := range []string{targets.HTTP, targets.SSH} {
		if address == "" {
			continue
		}
		host, portText, err := net.SplitHostPort(address)
		if err != nil || host == "" || strings.ContainsAny(host, "\r\n\t /\\?#@") {
			return targets, fmt.Errorf("invalid health target")
		}
		port, err := strconv.Atoi(portText)
		if err != nil || port < 1 || port > 65535 {
			return targets, fmt.Errorf("invalid health port")
		}
	}
	return targets, nil
}
