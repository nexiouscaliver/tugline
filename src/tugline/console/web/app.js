/**
 * tugline console application.
 *
 * One module, no dependencies. /api/state paints the first frame, then
 * one EventSource at /api/events/stream keeps the page live: its
 * "snapshot" event repaints everything, and each "delta" triggers
 * cheap refetches (events always; sessions and threads only when the
 * delta counts say those tables changed). A slow /api/state poll
 * (every 30 seconds) keeps the header health honest, because daemon,
 * ntfy, and spool depth otherwise only ride on snapshots.
 *
 * Delivery chips follow core.policy: userpromptsubmit and sessionstart
 * records never notify (a resume sessionstart can, but the store row
 * carries no source field, so the console hides the chip for all of
 * them rather than ever showing a false "pending"). Everything else
 * shows pending (delivered 0) or delivered (delivered 1), where
 * delivered means the daemon handled the record, published or not.
 */

const EVENTS_LIMIT = 50;
const RECENT_REPLIES_SHOWN = 5;
const STATE_REFRESH_MS = 30000;
const STATE_ONLY_EVENTS = new Set(["userpromptsubmit", "sessionstart"]);

const state = {
  sessions: [],
  events: [],
  threads: [],
  replies: [],
  health: { daemon: "unknown", ntfy: "unreachable" },
  spoolDepth: 0,
  lastIngestMs: null,
  selected: null, // {kind: "session"|"thread", id: "..."} or null
};

let stream = null; // the EventSource, or null before the first open

const $ = (id) => document.getElementById(id);

// -- small helpers --

function short8(value) {
  const text = String(value || "");
  return text.slice(0, 8) || "-";
}

function parseTs(iso) {
  if (!iso) return null;
  const ms = Date.parse(String(iso));
  return Number.isNaN(ms) ? null : ms;
}

function relTime(ms) {
  if (ms === null) return "-";
  const seconds = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (seconds < 60) return seconds + "s ago";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes + "m ago";
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + "h ago";
  return Math.floor(hours / 24) + "d ago";
}

function clock(ms) {
  if (ms === null) return "--:--:--";
  const when = new Date(ms);
  const two = (n) => String(n).padStart(2, "0");
  return (
    two(when.getHours()) + ":" + two(when.getMinutes()) + ":" + two(when.getSeconds())
  );
}

function notifiable(eventName) {
  return !STATE_ONLY_EVENTS.has(String(eventName || "").toLowerCase());
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function isSelected(kind, id) {
  return state.selected !== null && state.selected.kind === kind && state.selected.id === id;
}

async function api(path, options) {
  const response = await fetch(path, options);
  let payload = {};
  try {
    payload = await response.json();
  } catch (err) {
    // an empty or non-json body is fine to read as {}
  }
  if (!response.ok) {
    throw new Error(payload.error || "HTTP " + response.status);
  }
  return payload;
}

// -- header --

function setDot(which, color) {
  $(which + "-dot").className = "dot dot-" + color;
}

function paintHeader() {
  const daemon = String(state.health.daemon || "unknown");
  setDot("daemon", daemon === "running" ? "green" : daemon === "stopped" ? "red" : "gray");
  $("daemon-word").textContent = daemon;

  const ntfy = String(state.health.ntfy || "unreachable");
  setDot("ntfy", ntfy === "ok" ? "green" : "red");
  $("ntfy-word").textContent = ntfy;

  paintSpool();
  paintFreshness();
}

function paintSpool() {
  const node = $("spool-stat");
  const depth = state.spoolDepth || 0;
  node.textContent = depth > 0 ? depth + " pending" : "nothing pending";
  node.classList.toggle("warn", depth > 0);
}

function paintFreshness() {
  const text =
    state.lastIngestMs === null
      ? "never updated"
      : "updated " + relTime(state.lastIngestMs);
  $("fresh-stat").textContent = text;
}

// -- sessions panel --

function stateCell(value) {
  const name = String(value || "running");
  const label = name === "waiting" ? "waiting on me" : name;
  const tone = name === "running" ? "green" : name === "waiting" ? "amber" : "gray";
  const cell = el("td", "cell-state");
  cell.append(el("span", "chip chip-state chip-" + tone, label));
  return cell;
}

function lastEventText(session) {
  const name = String(session.last_event || "");
  const latest = state.events.find(
    (event) => event.session_id === session.session_id && event.event === name
  );
  const title = latest && latest.title ? " " + latest.title : "";
  return name + title;
}

function sessionRow(session) {
  const row = document.createElement("tr");
  if (isSelected("session", session.session_id)) row.classList.add("selected");

  row.append(stateCell(session.state));

  const idCell = el("td", "cell-id");
  const button = el("button", "idlink", short8(session.session_id));
  button.type = "button";
  button.title = session.session_id;
  button.setAttribute("aria-pressed", String(isSelected("session", session.session_id)));
  button.addEventListener("click", () => toggleSession(session.session_id));
  idCell.append(button);
  row.append(idCell);

  row.append(el("td", "cell-badge", String(session.adapter || "")));
  row.append(el("td", "cell-project", String(session.project || "")));
  row.append(el("td", "cell-event", lastEventText(session)));

  const seenMs = parseTs(session.last_seen);
  const seen = el("td", "cell-time", relTime(seenMs));
  seen.dataset.rel = seenMs === null ? "" : String(seenMs);
  row.append(seen);

  return row;
}

// Adapter and project are empty until field enrichment lands, so the
// two columns render only when the current data carries at least one
// non-empty value; the classes are re-derived on every repaint and
// the columns come back on their own once enriched data arrives.
function paintSessions() {
  const hasAdapter = state.sessions.some(
    (session) => String(session.adapter || "").trim() !== ""
  );
  const hasProject = state.sessions.some(
    (session) => String(session.project || "").trim() !== ""
  );
  const table = $("sessions-table");
  table.classList.toggle("no-adapter", !hasAdapter);
  table.classList.toggle("no-project", !hasProject);
  $("sessions-body").replaceChildren(...state.sessions.map(sessionRow));
  $("sessions-empty").hidden = state.sessions.length > 0;
}

// -- live stream --

function visibleEvents() {
  if (!state.selected || state.selected.kind !== "session") return state.events;
  return state.events.filter((event) => event.session_id === state.selected.id);
}

function streamRow(event) {
  const row = document.createElement("li");
  row.append(el("span", "t", clock(parseTs(event.ts))));
  row.append(el("span", "ev", String(event.event || "")));
  row.append(el("span", "sid", short8(event.session_id)));
  row.append(el("span", "tx", String(event.title || event.body || "")));
  if (notifiable(event.event)) {
    const delivered = Number(event.delivered) === 1;
    row.append(
      el(
        "span",
        "chip " + (delivered ? "chip-green" : "chip-amber"),
        delivered ? "delivered" : "pending"
      )
    );
  } else {
    row.append(el("span", "nochip"));
  }
  return row;
}

function paintStream() {
  const events = visibleEvents();
  $("stream").replaceChildren(...events.map(streamRow));

  const filtered = state.selected !== null && state.selected.kind === "session";
  $("stream-title").textContent = filtered
    ? "live stream: session " + short8(state.selected.id)
    : "live stream";

  const empty = $("stream-empty");
  empty.hidden = events.length > 0;
  empty.textContent = filtered ? "no events for this session yet" : "no events yet";
}

// -- asks panel --

function openAskRow(thread) {
  const item = document.createElement("li");
  if (isSelected("thread", thread.nonce)) item.classList.add("selected");

  const line = el("div", "ask-line");
  line.append(el("span", "sid", short8(thread.session_id)));
  line.append(el("span", "tx", String(thread.body || thread.title || "")));

  const createdMs = parseTs(thread.created_ts);
  const age = el("span", "age", relTime(createdMs));
  age.dataset.rel = createdMs === null ? "" : String(createdMs);
  line.append(age);

  const approve = el("button", "btn btn-approve", "Approve");
  const deny = el("button", "btn btn-deny", "Deny");
  const reply = el("button", "linkish", "reply");
  approve.type = "button";
  deny.type = "button";
  reply.type = "button";
  if (isSelected("thread", thread.nonce)) reply.classList.add("on");

  const error = el("p", "error");
  error.hidden = true;

  approve.addEventListener("click", () =>
    decide(thread.nonce, "allow", [approve, deny], error)
  );
  deny.addEventListener("click", () =>
    decide(thread.nonce, "deny", [approve, deny], error)
  );
  reply.addEventListener("click", () => toggleThread(thread.nonce));

  line.append(approve, deny, reply);
  item.append(line, error);
  return item;
}

function settledAskRow(thread) {
  const item = document.createElement("li");
  const expired = thread.status === "expired";
  const outcome = expired ? "expired" : String(thread.answer || "unknown");
  const tone = expired ? "gray" : outcome === "allow" ? "green" : "red";
  item.append(el("span", "out out-" + tone, outcome));
  item.append(el("span", "sid", short8(thread.session_id)));
  item.append(el("span", "tx", String(thread.body || thread.title || "")));
  return item;
}

function paintAsks() {
  const open = state.threads.filter((thread) => thread.status === "open");
  const settled = state.threads.filter((thread) => thread.status !== "open");

  $("asks-open").replaceChildren(...open.map(openAskRow));
  $("asks-empty").hidden = open.length > 0;

  $("asks-settled").replaceChildren(...settled.map(settledAskRow));
  $("asks-settled-heading").hidden = settled.length === 0;

  // a targeted thread that is no longer open stops being a target
  if (
    state.selected !== null &&
    state.selected.kind === "thread" &&
    !open.some((thread) => thread.nonce === state.selected.id)
  ) {
    state.selected = null;
    paintTarget();
  }
}

// -- reply box --

function paintTarget() {
  const line = $("reply-target");
  const active = state.selected !== null;
  let text = "no target selected";
  if (active && state.selected.kind === "thread") {
    text = "reply via thread " + short8(state.selected.id);
  } else if (active && state.selected.kind === "session") {
    text = "reply to session " + short8(state.selected.id);
  }
  line.textContent = text;
  line.classList.toggle("active", active);
  $("reply-body").disabled = !active;
  $("reply-send").disabled = !active;
}

function paintReplies() {
  const recent = state.replies.slice(0, RECENT_REPLIES_SHOWN);
  $("recent-replies").replaceChildren(
    ...recent.map((reply) => {
      const item = document.createElement("li");
      item.append(el("span", "tx", String(reply.body || "")));
      const target = reply.thread_nonce
        ? "thread " + short8(reply.thread_nonce)
        : "session " + short8(reply.session_id);
      item.append(el("span", "tgt", target));
      return item;
    })
  );
  $("recent-empty").hidden = recent.length > 0;
}

// -- selection --

function toggleSession(sessionId) {
  state.selected = isSelected("session", sessionId)
    ? null
    : { kind: "session", id: sessionId };
  paintSessions();
  paintStream(); // the loaded window is filtered client side right away
  paintTarget();
  refreshEvents(); // then a session-scoped window replaces it
}

function toggleThread(nonce) {
  state.selected = isSelected("thread", nonce) ? null : { kind: "thread", id: nonce };
  paintAsks();
  paintTarget();
}

// -- actions --

async function decide(nonce, decision, buttons, errorNode) {
  for (const button of buttons) {
    button.disabled = true;
  }
  errorNode.hidden = true;
  errorNode.textContent = "";
  try {
    await api("/api/approvals/" + encodeURIComponent(nonce) + "?decision=" + decision, {
      method: "POST",
    });
    await refreshThreads(); // the repaint moves the row into the settled list
  } catch (err) {
    errorNode.textContent = "decision failed: " + err.message;
    errorNode.hidden = false;
    for (const button of buttons) {
      button.disabled = false;
    }
  }
}

async function sendReply() {
  if (state.selected === null) return;
  const box = $("reply-body");
  const errorNode = $("reply-error");
  const text = box.value.trim();
  errorNode.hidden = true;
  if (!text) {
    errorNode.textContent = "reply body is empty";
    errorNode.hidden = false;
    return;
  }
  const payload =
    state.selected.kind === "thread"
      ? { thread_nonce: state.selected.id, body: text }
      : { session_id: state.selected.id, body: text };
  $("reply-send").disabled = true;
  try {
    const data = await api("/api/replies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    box.value = "";
    if (data.reply) {
      state.replies.unshift(data.reply);
      paintReplies();
    }
  } catch (err) {
    errorNode.textContent = "send failed: " + err.message;
    errorNode.hidden = false;
  } finally {
    paintTarget(); // restores the send button for the current target
  }
}

// -- data refresh --

async function refreshEvents() {
  try {
    const session =
      state.selected !== null && state.selected.kind === "session"
        ? state.selected.id
        : null;
    const path =
      "/api/events?limit=" +
      EVENTS_LIMIT +
      (session ? "&session=" + encodeURIComponent(session) : "");
    const data = await api(path);
    state.events = data.events || [];
    paintStream();
  } catch (err) {
    // the stream stays as it is; the next delta retries
  }
}

async function refreshSessions() {
  try {
    const data = await api("/api/sessions");
    state.sessions = data.sessions || [];
    paintSessions();
  } catch (err) {
    // keep the last painted table
  }
}

async function refreshThreads() {
  try {
    const data = await api("/api/threads");
    state.threads = data.threads || [];
    paintAsks();
  } catch (err) {
    // keep the last painted list
  }
}

function applyState(payload) {
  state.sessions = payload.sessions || [];
  state.events = payload.events || [];
  state.threads = payload.threads || [];
  state.replies = payload.replies || [];
  state.health = payload.health || state.health;
  state.spoolDepth = payload.spool_depth || 0;
  state.lastIngestMs = parseTs(payload.last_ingest_ts);
  paintHeader();
  paintSessions();
  paintStream();
  paintAsks();
  paintReplies();
  paintTarget();
}

// -- live stream connection --

function paintLiveChip() {
  const chip = $("live-chip");
  const open = stream !== null && stream.readyState === 1;
  chip.textContent = open ? "live" : "reconnecting";
  chip.className = "chip " + (open ? "chip-green" : "chip-amber");
}

function onDelta(delta) {
  const ts = parseTs(delta.ts);
  if (ts !== null) {
    state.lastIngestMs = ts;
    paintFreshness();
  }
  const counts = delta.counts || {};
  refreshEvents(); // always: repaint the stream from a cheap refetch
  if (counts.sessions) refreshSessions();
  if (counts.threads || counts.answered || counts.open || counts.expired) {
    refreshThreads();
  }
}

function connectStream() {
  if (stream !== null) stream.close();
  stream = new EventSource("/api/events/stream");
  stream.onopen = paintLiveChip;
  stream.onerror = () => {
    paintLiveChip();
    if (stream.readyState === 2) {
      // the browser stopped reconnecting on its own; try again soon
      setTimeout(connectStream, 5000);
    }
  };
  stream.addEventListener("snapshot", (event) => {
    try {
      applyState(JSON.parse(event.data));
    } catch (err) {
      // a bad frame never kills the stream
    }
    paintLiveChip();
  });
  stream.addEventListener("delta", (event) => {
    try {
      onDelta(JSON.parse(event.data));
    } catch (err) {
      // same: skip the frame, keep the connection
    }
  });
  paintLiveChip();
}

// -- clock tick: relative times and the connection chip stay honest --

function tick() {
  paintFreshness();
  paintLiveChip();
  for (const node of document.querySelectorAll("[data-rel]")) {
    const ms = Number(node.dataset.rel);
    if (ms) node.textContent = relTime(ms);
  }
}

async function pollState() {
  try {
    applyState(await api("/api/state"));
  } catch (err) {
    // header health goes stale until the next pass or snapshot
  }
}

async function init() {
  $("reply-send").addEventListener("click", sendReply);
  $("reply-body").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      sendReply();
    }
  });

  // paint from the state snapshot before the stream opens
  try {
    applyState(await api("/api/state"));
  } catch (err) {
    // the stream snapshot will paint the page instead
  }

  connectStream();
  setInterval(tick, 1000);
  setInterval(pollState, STATE_REFRESH_MS);
}

init();
