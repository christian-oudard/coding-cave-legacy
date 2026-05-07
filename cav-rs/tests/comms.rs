/// Integration tests for the broker binary: routing, ack, and registration.
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};
use std::{fs, thread};

use serde_json::{json, Value};

const BROKER: &str = env!("CARGO_BIN_EXE_cav-broker");

static COUNTER: AtomicU32 = AtomicU32::new(0);

fn unique_sock() -> PathBuf {
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir()
        .join(format!("cav-broker-test-{}-{}.sock", std::process::id(), n))
}

// ── Helpers ──────────────────────────────────────────────────────────────

struct BrokerProcess {
    proc: Child,
    sock: PathBuf,
}

impl BrokerProcess {
    fn start() -> Self {
        let sock = unique_sock();
        let proc = Command::new(BROKER).arg(&sock).spawn().unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if sock.exists() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert!(sock.exists(), "broker socket never appeared");
        BrokerProcess { proc, sock }
    }

    fn connect(&self) -> (UnixStream, BufReader<UnixStream>) {
        let w = UnixStream::connect(&self.sock).unwrap();
        // Cap blocking reads so a deadlocked test fails in seconds rather
        // than hanging cargo forever. The broker replies in microseconds
        // under normal conditions; 5s is an order of magnitude over that
        // even on a heavily loaded host.
        let reader_stream = w.try_clone().unwrap();
        reader_stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let r = BufReader::new(reader_stream);
        (w, r)
    }
}

impl Drop for BrokerProcess {
    fn drop(&mut self) {
        self.proc.kill().ok();
        self.proc.wait().ok();
        let _ = fs::remove_file(&self.sock);
    }
}

fn send(stream: &UnixStream, msg: &Value) {
    let mut data = serde_json::to_vec(msg).unwrap();
    data.push(b'\n');
    (&*stream).write_all(&data).unwrap();
}

fn recv(reader: &mut BufReader<UnixStream>) -> Value {
    let mut line = String::new();
    reader
        .read_line(&mut line)
        .expect("recv timed out or socket closed; expected broker reply");
    serde_json::from_str(line.trim()).unwrap()
}

fn register(stream: &UnixStream, id: &str) {
    send(stream, &json!({"register": id}));
}

// ── Tests ─────────────────────────────────────────────────────────────────

#[test]
fn broker_delivers_message_and_acks() {
    let broker = BrokerProcess::start();

    let (wa, mut ra) = broker.connect();
    let (wb, mut rb) = broker.connect();

    register(&wa, "1");
    register(&wb, "2");
    // Synchronize: wb probes a dummy agent so we know the broker has processed
    // wb's registration before wa tries to deliver to it.
    send(&wb, &json!({"to": "999", "body": "probe"}));
    recv(&mut rb); // ack=false, no_listener — discarded

    send(&wa, &json!({"to": "2", "body": "hello"}));

    let ack = recv(&mut ra);
    assert_eq!(ack["ack"], true, "sender should get delivered ack");

    let msg = recv(&mut rb);
    assert_eq!(msg["from"], "1");
    assert_eq!(msg["body"], "hello");
}

#[test]
fn broker_acks_no_listener() {
    let broker = BrokerProcess::start();

    let (wa, mut ra) = broker.connect();
    register(&wa, "1");

    send(&wa, &json!({"to": "99", "body": "nobody home"}));

    let ack = recv(&mut ra);
    assert_eq!(ack["ack"], false);
    assert_eq!(ack["reason"], "no_listener");
}

#[test]
fn broker_reregister_replaces_writer() {
    let broker = BrokerProcess::start();

    let (wa1, mut ra1) = broker.connect(); // first connection for agent "1"
    let (wa2, mut ra2) = broker.connect(); // second connection, will replace
    let (ws, mut rs) = broker.connect();

    // The broker handles each connection in its own thread, so registrations
    // from wa1 and wa2 are racy in arrival order. Force a happens-before by
    // probing through each connection after its register: when the probe
    // ack returns, that connection's prior register has been processed.
    register(&wa1, "1");
    send(&wa1, &json!({"to": "999", "body": "probe"}));
    recv(&mut ra1); // no_listener ack, discarded

    register(&wa2, "1"); // replaces wa1
    send(&wa2, &json!({"to": "999", "body": "probe"}));
    recv(&mut ra2); // no_listener ack, discarded

    register(&ws, "2");
    send(&ws, &json!({"to": "1", "body": "new only"}));

    let ack = recv(&mut rs);
    assert_eq!(ack["ack"], true);

    let msg = recv(&mut ra2);
    assert_eq!(msg["body"], "new only");
}

#[test]
fn broker_cleans_up_disconnected_agent() {
    let broker = BrokerProcess::start();

    {
        let (wa, _) = broker.connect();
        register(&wa, "1");
        // wa drops here, disconnecting
    }
    // give the broker time to process the EOF
    thread::sleep(Duration::from_millis(100));

    let (ws, mut rs) = broker.connect();
    register(&ws, "2");
    send(&ws, &json!({"to": "1", "body": "gone"}));

    let ack = recv(&mut rs);
    assert_eq!(ack["ack"], false);
    assert_eq!(ack["reason"], "no_listener");
}
