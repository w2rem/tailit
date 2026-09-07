package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// tailit/app — sidecar service.
//
// - Listens on TS_PORT+1 (GO_PORT overrides).
// - Round-robin pings TS_PEERS every TS_PEER_INTERVAL: one peer per tick,
//   single peer just gets pinged every tick.
// - Each check reads the FULL response body (waits for the server to finish),
//   classifies state: awake | starting | asleep | down.
// - /ready returns 200 only after the first full sweep (peers see a peer
//   that finished its own startup).
// - DATABASE_URL / VALKEY_URL are read from env (Secrets) for future checks.

type Config struct {
	Port        int
	Peers       []string
	Interval    time.Duration
	PostgresDSN string
	ValkeyAddr  string
}

type PeerResult struct {
	URL       string `json:"url"`
	Status    int    `json:"status"`
	LatencyMs int64  `json:"latency_ms"`
	OK        bool   `json:"ok"`
	State     string `json:"state"`
	At        string `json:"at"`
	Error     string `json:"error,omitempty"`
}

var (
	mu      sync.RWMutex
	results []PeerResult
	ready   bool
)

func loadConfig() Config {
	base := 8501
	if v := strings.TrimSpace(os.Getenv("TS_PORT")); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n < 65534 {
			base = n
		}
	}
	port := base + 1
	if v := strings.TrimSpace(os.Getenv("GO_PORT")); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n < 65535 {
			port = n
		}
	}
	var peers []string
	if raw := os.Getenv("TS_PEERS"); raw != "" {
		for _, p := range strings.FieldsFunc(raw, func(r rune) bool { return r == ',' || r == ' ' || r == '\n' || r == '\t' }) {
			p = strings.TrimSuffix(strings.TrimSpace(p), "/")
			if p != "" {
				peers = append(peers, p)
			}
		}
	}
	iv := 300
	if v := strings.TrimSpace(os.Getenv("TS_PEER_INTERVAL")); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			iv = n
		}
	}
	if iv < 60 {
		iv = 60
	}
	return Config{
		Port:        port,
		Peers:       peers,
		Interval:    time.Duration(iv) * time.Second,
		PostgresDSN: os.Getenv("DATABASE_URL"),
		ValkeyAddr:  os.Getenv("VALKEY_URL"),
	}
}

// checkPeer performs a deep check: full body read (waits for the server),
// manual redirect handling (303 from private Streamlit apps = alive).
func checkPeer(client *http.Client, base string) PeerResult {
	base = strings.TrimSuffix(strings.TrimSpace(base), "/")
	res := PeerResult{URL: base, At: time.Now().UTC().Format("15:04:05")}
	candidates := []string{base + "/_stcore/health", base + "/_stcore/host-config", base + "/"}

	for _, url := range candidates {
		req, err := http.NewRequest("GET", url, nil)
		if err != nil {
			continue
		}
		req.Header.Set("User-Agent", "tailit-go/1.0")
		t0 := time.Now()
		resp, err := client.Do(req)
		ms := time.Since(t0).Milliseconds()
		if err != nil {
			res.Error = trunc(err.Error(), 120)
			continue // try next candidate
		}
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
		resp.Body.Close()

		res.Status = resp.StatusCode
		res.LatencyMs = ms
		text := string(body)

		switch {
		case resp.StatusCode >= 200 && resp.StatusCode < 300:
			res.OK = true
			switch {
			case strings.Contains(text, "get this app back up"):
				res.State = "asleep" // needs manual "Yes, get this app back up!"
				res.OK = false
			case strings.Contains(text, "Waking up") || strings.Contains(text, "in the oven") || strings.Contains(text, "Please wait"):
				res.State = "starting"
			default:
				res.State = "awake"
			}
			return res
		case resp.StatusCode >= 300 && resp.StatusCode < 400:
			// Private Streamlit apps redirect to /-/auth — front proxy alive.
			res.OK = true
			res.State = "awake"
			return res
		default:
			res.Error = fmt.Sprintf("HTTP %d", resp.StatusCode)
			res.State = "down"
			// try next candidate before giving up
		}
	}
	if res.State == "" {
		res.State = "down"
	}
	return res
}

func trunc(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}

func pinger(cfg Config) {
	if len(cfg.Peers) == 0 {
		mu.Lock()
		ready = true
		mu.Unlock()
		log.Println("no TS_PEERS — pinger idle, service ready")
		return
	}
	// Don't follow redirects: we want the raw 303 from private apps.
	client := &http.Client{
		Timeout: 45 * time.Second,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	idx := 0
	checked := 0
	ticker := time.NewTicker(cfg.Interval)
	defer ticker.Stop()
	log.Printf("pinger: %d peer(s), every %s, round-robin", len(cfg.Peers), cfg.Interval)
	for range ticker.C {
		peer := cfg.Peers[idx%len(cfg.Peers)]
		idx++
		r := checkPeer(client, peer)
		mu.Lock()
		// Replace previous result for this peer.
		kept := results[:0]
		for _, old := range results {
			if old.URL != r.URL {
				kept = append(kept, old)
			}
		}
		results = append(kept, r)
		checked++
		if checked >= len(cfg.Peers) {
			ready = true // first full sweep done
		}
		mu.Unlock()
		log.Printf("peer %s -> %d %s %dms ok=%v", r.URL, r.Status, r.State, r.LatencyMs, r.OK)
	}
}

func writeJSON(w http.ResponseWriter, v any, code int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func main() {
	cfg := loadConfig()
	log.Printf("tailit/app on :%d (postgres=%v valkey=%v peers=%d interval=%s)",
		cfg.Port, cfg.PostgresDSN != "", cfg.ValkeyAddr != "", len(cfg.Peers), cfg.Interval)

	mux := http.NewServeMux()

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"status": "ok", "time": time.Now().UTC().Format(time.RFC3339)}, 200)
	})

	mux.HandleFunc("/ready", func(w http.ResponseWriter, r *http.Request) {
		mu.RLock()
		ok := ready
		mu.RUnlock()
		if ok {
			writeJSON(w, map[string]any{"ready": true}, 200)
			return
		}
		writeJSON(w, map[string]any{"ready": false, "note": "first peer sweep not done"}, 503)
	})

	mux.HandleFunc("/peers", func(w http.ResponseWriter, r *http.Request) {
		mu.RLock()
		out := append([]PeerResult{}, results...)
		mu.RUnlock()
		if out == nil {
			out = []PeerResult{}
		}
		writeJSON(w, map[string]any{"peers": out, "interval_sec": int(cfg.Interval / time.Second)}, 200)
	})

	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			writeJSON(w, map[string]any{"error": "not found"}, 404)
			return
		}
		writeJSON(w, map[string]any{
			"service":   "tailit/app",
			"port":      cfg.Port,
			"endpoints": []string{"/health", "/ready", "/peers"},
		}, 200)
	})

	go pinger(cfg)

	srv := &http.Server{
		Addr:         ":" + strconv.Itoa(cfg.Port),
		Handler:      mux,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
	}
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("listen: %v", err)
	}
}
