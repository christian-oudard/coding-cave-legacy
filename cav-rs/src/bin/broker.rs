// Message broker for inter-agent channel communication.
//
// Listens on a single Unix domain socket. Agents connect and register
// with their ID, then send/receive JSON-line messages. The broker
// routes messages by the "to" field and stamps "from" based on the
// sender's registration.
//
// One writer per agent id: re-registering the same id replaces the
// previous writer (but the old connection is free to keep sending).
//
// Protocol (newline-delimited JSON):
//   Client -> Broker:  {"register": "<agent_id>"}
//   Client -> Broker:  {"to": "<agent_id>", "body": "<text>"}
//   Broker -> Client:  {"from": "<agent_id>", "body": "<text>"}
//   Broker -> Sender:  {"ack": true}                       (delivered)
//   Broker -> Sender:  {"ack": false, "reason": "<why>"}   (dropped)

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::thread;

use serde_json::json;

type Writer = Arc<Mutex<UnixStream>>;
type Agents = Arc<Mutex<HashMap<String, Writer>>>;

fn log(msg: &str) {
    if std::env::var_os("CAVE_DEBUG").is_some() {
        eprintln!("[broker] {msg}");
    }
}

fn write_line(writer: &Writer, value: &serde_json::Value) {
    let mut data = serde_json::to_vec(value).unwrap();
    data.push(b'\n');
    let mut w = writer.lock().unwrap();
    let _ = w.write_all(&data);
    let _ = w.flush();
}

fn deliver(agents: &Agents, to: &str, envelope: &serde_json::Value) -> Result<(), &'static str> {
    let writer = agents.lock().unwrap().get(to).cloned();
    let Some(writer) = writer else {
        return Err("no_listener");
    };
    write_line(&writer, envelope);
    Ok(())
}

fn handle(stream: UnixStream, agents: Agents) {
    let writer: Writer = match stream.try_clone() {
        Ok(w) => Arc::new(Mutex::new(w)),
        Err(_) => return,
    };
    let reader = BufReader::new(stream);
    let mut agent_id: Option<String> = None;

    for line in reader.lines() {
        let Ok(line) = line else { break };
        let Ok(msg) = serde_json::from_str::<serde_json::Value>(&line) else {
            continue;
        };

        if let Some(reg) = msg.get("register") {
            let id = match reg {
                serde_json::Value::String(s) => s.clone(),
                v => v.to_string(),
            };
            agent_id = Some(id.clone());
            let replaced = agents
                .lock()
                .unwrap()
                .insert(id.clone(), writer.clone())
                .is_some();
            log(&format!(
                "agent {id} registered{}",
                if replaced { " (replaced prior writer)" } else { "" }
            ));
            continue;
        }

        let Some(sender) = agent_id.as_ref() else { continue };
        let Some(to) = msg.get("to").and_then(|v| v.as_str()) else { continue };
        let body = msg.get("body").and_then(|v| v.as_str()).unwrap_or("");

        let envelope = json!({"from": sender, "body": body});
        let ack = match deliver(&agents, to, &envelope) {
            Ok(()) => json!({"ack": true}),
            Err(reason) => {
                log(&format!("no connection for agent {to}, dropping message"));
                json!({"ack": false, "reason": reason})
            }
        };
        write_line(&writer, &ack);
    }

    if let Some(id) = agent_id {
        // Only clear the map entry if it still points at our writer;
        // a later registration under the same id may have replaced it.
        let mut map = agents.lock().unwrap();
        if let Some(current) = map.get(&id) {
            if Arc::ptr_eq(current, &writer) {
                map.remove(&id);
            }
        }
        log(&format!("agent {id} disconnected"));
    }
}

fn main() -> std::io::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 2 {
        eprintln!("usage: {} <socket_path>", args[0]);
        std::process::exit(1);
    }
    let sock_path = PathBuf::from(&args[1]);
    if let Some(parent) = sock_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let _ = std::fs::remove_file(&sock_path);

    let listener = UnixListener::bind(&sock_path)?;
    std::fs::set_permissions(&sock_path, std::fs::Permissions::from_mode(0o600))?;
    log(&format!("listening on {}", sock_path.display()));

    let agents: Agents = Arc::new(Mutex::new(HashMap::new()));

    for conn in listener.incoming() {
        match conn {
            Ok(stream) => {
                let agents = agents.clone();
                thread::spawn(move || handle(stream, agents));
            }
            Err(e) => log(&format!("accept failed: {e}")),
        }
    }

    Ok(())
}
