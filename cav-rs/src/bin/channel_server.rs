// MCP channel server for inter-agent messaging.
//
// Pure-stdlib implementation of MCP's stdio transport (JSON-RPC framed
// as newline-delimited JSON). Bridges between Claude Code's channel
// notification system and the cave message broker (JSON lines over
// Unix socket).
//
// Env:
//   CAVE_MSG_SOCK   - path to the broker Unix socket
//   CAVE_AGENT_ID   - this agent's numeric ID

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::{json, Value};

fn log(msg: &str) {
    if std::env::var_os("CAVE_DEBUG").is_some() {
        eprintln!("[channel] {msg}");
    }
}

fn write_message(stdout: &Mutex<std::io::Stdout>, obj: &Value) {
    let mut data = serde_json::to_vec(obj).unwrap();
    data.push(b'\n');
    let mut out = stdout.lock().unwrap();
    let _ = out.write_all(&data);
    let _ = out.flush();
}

fn read_message<R: BufRead>(reader: &mut R) -> Option<Value> {
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).ok()? == 0 {
            return None;
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if let Ok(v) = serde_json::from_str(trimmed) {
            return Some(v);
        }
    }
}

// -- Broker connection --

enum Ack {
    Delivered,
    Dropped(String),
}

struct Broker {
    sock_path: String,
    agent_id: String,
    stream: Mutex<Option<UnixStream>>,
    pending_ack: Mutex<Option<mpsc::Sender<Ack>>>,
    send_lock: Mutex<()>,
}

impl Broker {
    fn new(sock_path: String, agent_id: String) -> Self {
        Self {
            sock_path,
            agent_id,
            stream: Mutex::new(None),
            pending_ack: Mutex::new(None),
            send_lock: Mutex::new(()),
        }
    }

    fn connect(&self) -> Option<UnixStream> {
        let s = UnixStream::connect(&self.sock_path).ok()?;
        let reg = format!(r#"{{"register":"{}"}}"#, self.agent_id) + "\n";
        let mut w = s.try_clone().ok()?;
        w.write_all(reg.as_bytes()).ok()?;
        log(&format!("connected to broker as agent {}", self.agent_id));
        Some(s)
    }

    fn writer(&self) -> Option<UnixStream> {
        let mut slot = self.stream.lock().unwrap();
        if slot.is_none() {
            *slot = self.connect();
        }
        slot.as_ref().and_then(|s| s.try_clone().ok())
    }

    fn send(&self, to: &str, message: &str) -> String {
        // Serialize sends so there's at most one outstanding ack to match.
        let _guard = self.send_lock.lock().unwrap();

        let (tx, rx) = mpsc::channel::<Ack>();
        *self.pending_ack.lock().unwrap() = Some(tx);

        let line = serde_json::to_string(&json!({"to": to, "body": message})).unwrap() + "\n";
        let Some(mut w) = self.writer() else {
            *self.pending_ack.lock().unwrap() = None;
            return "Not connected to message broker".to_string();
        };
        if let Err(e) = w.write_all(line.as_bytes()) {
            *self.stream.lock().unwrap() = None;
            *self.pending_ack.lock().unwrap() = None;
            return format!("Send failed: {e}");
        }

        let result = match rx.recv_timeout(Duration::from_secs(5)) {
            Ok(Ack::Delivered) => format!("Delivered to agent {to}"),
            Ok(Ack::Dropped(reason)) if reason == "no_listener" => {
                format!("Agent {to} is not online; message not delivered")
            }
            Ok(Ack::Dropped(reason)) => format!("Not delivered: {reason}"),
            Err(_) => "Broker did not acknowledge within 5s".to_string(),
        };
        *self.pending_ack.lock().unwrap() = None;
        result
    }

    fn take_reader(&self) -> Option<UnixStream> {
        let mut slot = self.stream.lock().unwrap();
        if slot.is_none() {
            *slot = self.connect();
        }
        slot.as_ref().and_then(|s| s.try_clone().ok())
    }

    fn drop_stream(&self) {
        *self.stream.lock().unwrap() = None;
    }

    fn dispatch_ack(&self, ack: Ack) {
        if let Some(tx) = self.pending_ack.lock().unwrap().take() {
            let _ = tx.send(ack);
        }
    }
}

fn broker_listener(broker: Arc<Broker>, stdout: Arc<Mutex<std::io::Stdout>>) {
    loop {
        let Some(reader) = broker.take_reader() else {
            thread::sleep(Duration::from_secs(2));
            continue;
        };
        let mut reader = BufReader::new(reader);
        loop {
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) => {
                    log("broker disconnected, reconnecting...");
                    broker.drop_stream();
                    thread::sleep(Duration::from_secs(2));
                    break;
                }
                Ok(_) => {}
                Err(_) => {
                    broker.drop_stream();
                    thread::sleep(Duration::from_secs(2));
                    break;
                }
            }
            let Ok(msg) = serde_json::from_str::<Value>(line.trim()) else {
                continue;
            };

            if let Some(ack) = msg.get("ack") {
                let delivered = ack.as_bool().unwrap_or(false);
                let reason = msg
                    .get("reason")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                broker.dispatch_ack(if delivered { Ack::Delivered } else { Ack::Dropped(reason) });
                continue;
            }

            let from = msg.get("from").and_then(|v| v.as_str()).unwrap_or("?");
            let body = msg.get("body").and_then(|v| v.as_str()).unwrap_or("");
            log(&format!("from {from}: {}", &body.chars().take(80).collect::<String>()));
            write_message(&stdout, &json!({
                "jsonrpc": "2.0",
                "method": "notifications/claude/channel",
                "params": {
                    "content": body,
                    "meta": {"from": from},
                },
            }));
        }
    }
}

// -- MCP request handling --

fn handle(msg: &Value, broker: &Broker, agent_id: &str) -> Option<Value> {
    let method = msg.get("method").and_then(|v| v.as_str()).unwrap_or("");
    let rid = msg.get("id").cloned();
    let params = msg.get("params").cloned().unwrap_or_else(|| json!({}));

    match method {
        "initialize" => Some(json!({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "cave-messages", "version": "0.1.0"},
                "capabilities": {
                    "experimental": {"claude/channel": {}},
                    "tools": {},
                },
                "instructions": format!(
                    "You are agent {agent_id}. \
                     You receive messages from other agents as <channel> events. \
                     Each event has a \"from\" attribute with the sender agent ID. \
                     Use the send_message tool to message other agents."
                ),
            },
        })),

        "tools/list" => Some(json!({
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"tools": [{
                "name": "send_message",
                "description": "Send a message to another coding cave agent",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Target agent ID"},
                        "message": {"type": "string", "description": "Message text"},
                    },
                    "required": ["to", "message"],
                },
            }]},
        })),

        "tools/call" => {
            let name = params.get("name").and_then(|v| v.as_str()).unwrap_or("");
            let args = params.get("arguments").cloned().unwrap_or_else(|| json!({}));
            if name == "send_message" {
                let to = args.get("to").and_then(|v| v.as_str()).unwrap_or("");
                let message = args.get("message").and_then(|v| v.as_str()).unwrap_or("");
                let text = broker.send(to, message);
                Some(json!({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"content": [{"type": "text", "text": text}]},
                }))
            } else {
                Some(json!({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": format!("unknown tool: {name}")},
                }))
            }
        }

        "ping" => Some(json!({"jsonrpc": "2.0", "id": rid, "result": {}})),

        _ => {
            if rid.is_none() {
                None
            } else {
                Some(json!({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": format!("method not found: {method}")},
                }))
            }
        }
    }
}

fn main() {
    let sock_path = std::env::var("CAVE_MSG_SOCK").unwrap_or_default();
    let agent_id = std::env::var("CAVE_AGENT_ID").unwrap_or_else(|_| "0".to_string());

    if sock_path.is_empty() {
        log("CAVE_MSG_SOCK not set");
        std::process::exit(1);
    }

    let broker = Arc::new(Broker::new(sock_path, agent_id.clone()));
    let stdout = Arc::new(Mutex::new(std::io::stdout()));

    // Proactively connect so the background listener starts immediately.
    let _ = broker.writer();

    {
        let b = broker.clone();
        let o = stdout.clone();
        thread::spawn(move || broker_listener(b, o));
    }

    let stdin = std::io::stdin();
    let mut reader = BufReader::new(stdin.lock());
    while let Some(msg) = read_message(&mut reader) {
        if let Some(resp) = handle(&msg, &broker, &agent_id) {
            write_message(&stdout, &resp);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn parse(input: &str) -> Option<Value> {
        read_message(&mut Cursor::new(input))
    }

    #[test]
    fn read_message_parses_json() {
        let v = parse("{\"a\":1}\n").unwrap();
        assert_eq!(v["a"], 1);
    }

    #[test]
    fn read_message_skips_blank_lines() {
        let v = parse("\n\n{\"x\":2}\n").unwrap();
        assert_eq!(v["x"], 2);
    }

    #[test]
    fn read_message_skips_invalid_json() {
        // invalid line followed by valid — skips invalid, returns valid
        let v = parse("not-json\n{\"ok\":true}\n").unwrap();
        assert_eq!(v["ok"], true);
    }

    #[test]
    fn read_message_eof_returns_none() {
        assert!(parse("").is_none());
    }
}
