package control

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"time"
)

// Client is the host's side. Dial is how a connection to the guest is made,
// which is the only place the transport appears: a vsock connection today, a
// TLS one for a cave that is not on this machine.
type Client struct {
	Dial  func(context.Context) (net.Conn, error)
	Token string

	http *http.Client
}

// NewClient builds a client over a dialer. Connections are not pooled: the
// plane is cheap enough that a fresh one costs under 2ms, and a pooled
// connection to a VM that has since been restarted is a confusing failure
// rather than a saved millisecond.
func NewClient(dial func(context.Context) (net.Conn, error), token string) *Client {
	c := &Client{Dial: dial, Token: token}
	c.http = &http.Client{
		Timeout: 15 * time.Second,
		Transport: &http.Transport{
			DisableKeepAlives: true,
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return c.Dial(ctx)
			},
		},
	}
	return c
}

// request builds an authenticated request. The host in the URL is ignored; the
// transport always dials the guest.
func (c *Client) request(method, path string, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequest(method, "http://cave-guest"+path, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	return req, nil
}

func (c *Client) do(method, path string, body, out any) error {
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rdr = bytes.NewReader(b)
	}
	req, err := c.request(method, path, rdr)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("%s %s: %s: %s", method, path, resp.Status,
			bytes.TrimSpace(msg))
	}
	if out == nil {
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func (c *Client) ListSessions() ([]SessionRecord, error) {
	var body struct {
		Sessions []SessionRecord `json:"sessions"`
	}
	err := c.do("GET", "/sessions", nil, &body)
	return body.Sessions, err
}

// ReadSession streams one transcript. The caller closes it, and closing before
// the end is allowed: the guest is writing into a socket, not a file, so an
// abandoned read costs it a broken pipe rather than a stuck process.
func (c *Client) ReadSession(id string) (io.ReadCloser, error) {
	req, err := c.request("GET", "/sessions/"+url.PathEscape(id), nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode/100 != 2 {
		defer resp.Body.Close()
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("GET /sessions/%s: %s: %s", id, resp.Status,
			bytes.TrimSpace(msg))
	}
	return resp.Body, nil
}

func (c *Client) WriteSession(id string, r io.Reader, shared bool) error {
	path := "/sessions/" + url.PathEscape(id)
	if shared {
		path += "?dest=shared"
	}
	req, err := c.request("POST", path, r)
	if err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("POST %s: %s: %s", path, resp.Status, bytes.TrimSpace(msg))
	}
	return nil
}

func (c *Client) Health() (Health, error) {
	var h Health
	err := c.do("GET", "/health", nil, &h)
	return h, err
}

func (c *Client) Agents() ([]Agent, error) {
	var body struct {
		Agents []Agent `json:"agents"`
	}
	err := c.do("GET", "/agents", nil, &body)
	return body.Agents, err
}

func (c *Client) StopAgent(id string) error {
	return c.do("POST", "/agents/"+id+"/stop", nil, nil)
}

// SetTime hands the guest the host's clock. Sent as a wall-clock instant at
// the moment of the call: the plane's round trip is tens of microseconds, so
// nothing here compensates for flight time, and anything that cared about
// milliseconds would need a protocol that measures it rather than a correction
// term that guesses.
func (c *Client) SetTime(t time.Time) error {
	return c.do("POST", "/time", struct {
		UnixNano int64 `json:"unixNano"`
	}{t.UnixNano()}, nil)
}
