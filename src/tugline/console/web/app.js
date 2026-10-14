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
 * The page is a static shell: it carries no data and opens without a
 * token. When the server has a token configured, every API and SSE
 * call needs it. The token comes from the ?token= in the page URL
 * (the phone path: browsers cannot set headers on EventSource) or
 * from the paste-token bar that appears as soon as any call fails
 * with 403. It is held in memory only and never rendered.
 *
 * Delivery chips follow core.policy: userpromptsubmit and sessionstart
 * records never notify (a resume sessionstart can, but the store row
 * carries no source field, so the console hides the chip for all of
 * them rather than ever showing a false "pending"). Everything else
 * shows pending (delivered 0) or delivered (delivered 1), where
 * delivered means the daemon handled the record, published or not.
 *
 * Session identity is operator-first: each row carries a rename
 * affordance backed by PUT /api/sessions/<id>/label, and when a label
 * is set it is the primary name (the id drops to a small secondary
 * line). Sessions also carry tags: small chips with a remove x plus
 * a tag link that opens an inline add editor, backed by PUT and
 * DELETE /api/sessions/<id>/tag/<tag>. Tags are lowercase letters,
 * digits, and hyphens (the server normalizes; anything else in the
 * input is dropped). Ids shown anywhere are display_id values
 * computed per response by the server: suffixes lengthened until
 * unique, so two sessions sharing a tail never render the same short
 * id. State chips read display_state (active now, running, waiting
 * on me, idle, done): the server derives it from recency, so a
 * session whose last event is hours old never shows running. The
 * panel sorts active-now rows first, then by recency.
 */

const EVENTS_LIMIT = 50;
const RECENT_REPLIES_SHOWN = 5;
const STATE_REFRESH_MS = 30000;
const STATE_ONLY_EVENTS = new Set(["userpromptsubmit", "sessionstart"]);
const STATE_LABELS = {
  active: "active now",
  running: "running",
  waiting: "waiting on me",
  idle: "idle",
  done: "done",
};
const STATE_TONES = {
  active: "green",
  running: "green",
  waiting: "amber",
  idle: "gray",
  done: "gray",
};

// phones (< 720px) trade the stacked panels for a bottom tab bar: one
// panel visible at a time, the whole viewport above it scrolls. The
// active tab lives here in memory, so repaints (which only refill the
// lists inside panels) never move the user to another panel.
const MOBILE_QUERY = window.matchMedia("(max-width: 719px)");
const TABS = ["sessions", "stream", "asks", "reply"];
const MOBILE_STREAM_STEP = 30; // the mobile render cap starts at 30 rows

const state = {
  sessions: [],
  events: [],
  threads: [],
  replies: [],
  health: { daemon: "unknown", ntfy: "unreachable" },
  spoolDepth: 0,
  lastIngestMs: null,
  selected: null, // {kind: "session"|"thread", id: "..."} or null
  activeTab: "sessions",
};

let stream = null; // the EventSource, or null before the first open

let mobileStreamRows = MOBILE_STREAM_STEP; // grows by 30 per "show older"

let apiToken = ""; // from ?token= in the page URL, or pasted into the bar

const $ = (id) => document.getElementById(id);

// -- small helpers --

function withToken(path) {
  // every call carries the token when one is known; EventSource
  // cannot set headers, so the query parameter is the shared path
  if (!apiToken) return path;
  return (
    path +
    (path.includes("?") ? "&" : "?") +
    "token=" +
    encodeURIComponent(apiToken)
  );
}

function short8(value) {
  const text = String(value || "");
  return text.slice(0, 8) || "-";
}

// per-response display names, rebuilt on every sessions repaint: the
// server computed each display_id to be unique within the response,
// and labels ride on the same rows
const displayIds = new Map();
const labels = new Map();

function sid(sessionId) {
  return displayIds.get(sessionId) || short8(sessionId);
}

function targetName(sessionId) {
  const label = labels.get(sessionId);
  return label ? label + " (" + sid(sessionId) + ")" : sid(sessionId);
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

// -- mobile tabs --

function isMobile() {
  return MOBILE_QUERY.matches;
}

// Exactly one panel carries "active"; the mobile stylesheet hides the
// rest. Switching is instant (no animation) and starts each panel at
// the top of the page. On desktop the class has no styling, so all
// four panels keep their grid seats.
function showTab(name) {
  state.activeTab = name;
  for (const tab of TABS) {
    const on = tab === name;
    $("tab-" + tab).classList.toggle("active", on);
    $("tab-" + tab).setAttribute("aria-current", on ? "true" : "false");
    $(tab + "-panel").classList.toggle("active", on);
  }
  if (isMobile()) window.scrollTo(0, 0);
}

async function api(path, options) {
  const response = await fetch(withToken(path), options);
  let payload = {};
  try {
    payload = await response.json();
  } catch (err) {
    // an empty or non-json body is fine to read as {}
  }
  if (!response.ok) {
    if (response.status === 403) showTokenBar(); // the paste fallback
    const error = new Error(payload.error || "HTTP " + response.status);
    error.payload = payload; // carries e.g. the ambiguous session list
    throw error;
  }
  return payload;
}

// -- token bar: the paste fallback when auth fails --

function showTokenBar() {
  const bar = $("token-bar");
  if (!bar.hidden) return;
  bar.hidden = false;
  $("token-input").focus();
}

async function applyToken() {
  const input = $("token-input");
  const value = input.value.trim();
  if (!value) return;
  apiToken = value;
  input.value = ""; // held in memory only, never rendered back
  $("token-bar").hidden = true;
  try {
    applyState(await api("/api/state"));
  } catch (err) {
    showTokenBar(); // still denied: let the user paste again
    return;
  }
  connectStream(); // a new EventSource picks up the token
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

function stateCell(session) {
  const name = String(
    session.display_state || session.state || "running"
  );
  const label = STATE_LABELS[name] || name;
  const tone = STATE_TONES[name] || "gray";
  const cell = el("td", "cell-state");
  cell.append(el("span", "chip chip-state chip-" + tone, label));
  return cell;
}

// The newest spooled event matching the session's last_event name; its
// title rides the desktop cell and its body fills the mobile card.
function lastEventFor(session) {
  const name = String(session.last_event || "");
  if (!name) return null;
  return (
    state.events.find(
      (event) => event.session_id === session.session_id && event.event === name
    ) || null
  );
}

// Mobile: tapping a card picks that session and jumps straight to the
// reply tab. Pick who, then write. There is no toggle-off here; the
// reply box explains itself when no target is set.
function pickSession(sessionId) {
  state.selected = { kind: "session", id: sessionId };
  paintSessions();
  paintStream(); // the loaded window is filtered client side right away
  paintTarget();
  showTab("reply");
  refreshEvents(); // then a session-scoped window replaces it
}

function idCellContent(session) {
  // the label is the primary name when set; the collision-proof short
  // id is always visible beside or beneath it; tags render as chips
  // with a remove x, plus a link that adds one more
  const wrap = el("div", "idwrap");
  const shown = String(session.display_id || short8(session.session_id));
  const button = el(
    "button",
    "idlink session-name",
    String(session.label || shown)
  );
  button.type = "button";
  button.title = session.session_id;
  button.setAttribute(
    "aria-pressed",
    String(isSelected("session", session.session_id))
  );
  button.addEventListener("click", () => {
    if (isMobile()) return; // the card handler catches the tap above
    toggleSession(session.session_id);
  });
  wrap.append(button);
  if (session.label) wrap.append(el("div", "idsub", shown));
  wrap.append(tagChips(session));
  const rename = el("button", "linkish rename", "rename");
  rename.type = "button";
  rename.title = "set a label for this session";
  rename.addEventListener("click", (event) => {
    // stop the bubble so the phone card tap never selects the session
    event.stopPropagation();
    startRename(session, wrap);
  });
  wrap.append(rename);
  return wrap;
}

// -- tags: chips with remove, an inline add editor --

function tagChips(session) {
  const line = el("div", "tagline");
  for (const tag of session.tags || []) {
    const chip = el("span", "tagchip", String(tag));
    const remove = el("button", "tagx", "x");
    remove.type = "button";
    remove.title = "remove tag " + tag;
    remove.setAttribute("aria-label", "remove tag " + tag);
    remove.addEventListener("click", () => removeTag(session, tag));
    chip.append(remove);
    line.append(chip);
  }
  const add = el("button", "linkish tagadd", "tag");
  add.type = "button";
  add.title = "add a tag to this session";
  add.addEventListener("click", () => startAddTag(session, line));
  line.append(add);
  return line;
}

function startAddTag(session, line) {
  // one inline editor in place of the chips: enter adds (the server
  // normalizes to lowercase letters, digits, hyphens), escape or blur
  // cancels
  const input = document.createElement("input");
  input.type = "text";
  input.className = "rename-input tag-input";
  input.maxLength = 24;
  input.placeholder = "tag (a-z, 0-9, -)";
  const hint = el("span", "taghint", "enter adds, esc cancels");
  line.replaceChildren(input, hint);
  input.focus();
  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    if (!save) {
      settled = true;
      paintSessions();
      return;
    }
    const value = input.value.trim();
    if (!value) {
      settled = true;
      paintSessions();
      return;
    }
    try {
      await addOrRemoveTag(session, value, "PUT");
      settled = true;
      await refreshSessions();
    } catch (err) {
      hint.textContent = "add failed: " + err.message;
      input.focus();
    }
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      finish(true);
    } else if (event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener("blur", () => finish(false));
}

async function addOrRemoveTag(session, tag, method) {
  await api(
    "/api/sessions/" +
      encodeURIComponent(session.session_id) +
      "/tag/" +
      encodeURIComponent(tag),
    { method: method }
  );
}

async function removeTag(session, tag) {
  try {
    await addOrRemoveTag(session, tag, "DELETE");
    await refreshSessions();
  } catch (err) {
    // removal cannot fail for a missing tag; a network or auth error
    // surfaces on the next repaint anyway
  }
}

function startRename(session, wrap) {
  // one inline editor in place of the name: enter saves, escape or
  // blur cancels; an empty value clears the label
  const input = document.createElement("input");
  input.type = "text";
  input.className = "rename-input";
  input.value = String(session.label || "");
  input.maxLength = 60;
  input.placeholder = "label (empty clears)";
  // taps inside the editor must not bubble into the phone card
  // handler, which would repaint the table and close this editor
  input.addEventListener("click", (event) => event.stopPropagation());
  const hint = el("div", "idsub", "enter saves, esc cancels");
  hint.addEventListener("click", (event) => event.stopPropagation());
  wrap.replaceChildren(input, hint);
  input.focus();
  input.select();
  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    if (!save) {
      settled = true;
      paintSessions();
      return;
    }
    try {
      await api(
        "/api/sessions/" +
          encodeURIComponent(session.session_id) +
          "/label",
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label: input.value.trim() }),
        }
      );
      settled = true;
      await refreshSessions();
    } catch (err) {
      hint.textContent = "save failed: " + err.message;
      input.focus();
    }
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      finish(true);
    } else if (event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener("blur", () => finish(false));
}

function sessionRow(session) {
  const row = document.createElement("tr");
  if (isSelected("session", session.session_id)) row.classList.add("selected");

  // the whole card is the tap target on phones; on desktop the row
  // stays inert and only the name link selects
  row.addEventListener("click", () => {
    if (isMobile()) pickSession(session.session_id);
  });

  row.append(stateCell(session));

  const idCell = el("td", "cell-id");
  idCell.append(idCellContent(session));
  row.append(idCell);

  row.append(el("td", "cell-badge", String(session.adapter || "")));
  row.append(el("td", "cell-project", String(session.project || "")));

  const latest = lastEventFor(session);
  const name = String(session.last_event || "");
  const eventCell = el("td", "cell-event");
  eventCell.append(
    el("span", "evname", name + (latest && latest.title ? " " + latest.title : ""))
  );
  const bodyText = latest ? String(latest.body || "") : "";
  if (bodyText) eventCell.append(el("span", "evbody", bodyText));
  row.append(eventCell);

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
  displayIds.clear();
  labels.clear();
  for (const session of state.sessions) {
    displayIds.set(
      session.session_id,
      String(session.display_id || short8(session.session_id))
    );
    if (session.label) labels.set(session.session_id, String(session.label));
  }
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

  // the head group is layout-transparent on desktop (display:
  // contents) and becomes the phone's first line; the delivery chip
  // gets its own grid seat on phones so it never leaves the screen
  const head = el("span", "srow");
  head.append(el("span", "t", clock(parseTs(event.ts))));
  head.append(el("span", "ev", String(event.event || "")));
  head.append(el("span", "sid", sid(event.session_id)));
  row.append(head);

  const title = String(event.title || "");
  const body = String(event.body || "");
  row.append(el("span", "tx", title || body));
  // when both exist the desktop cell keeps title-or-body as before,
  // and phones wrap the full body under the title
  if (title && body) row.append(el("span", "tx-extra", body));

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
  // phones render the newest rows first and extend on demand; the cap
  // lives only here, so desktop always paints the full window
  const capped = isMobile() ? events.slice(0, mobileStreamRows) : events;
  $("stream").replaceChildren(...capped.map(streamRow));
  $("stream-more").hidden = !(isMobile() && events.length > capped.length);

  const filtered = state.selected !== null && state.selected.kind === "session";
  $("stream-title").textContent = filtered
    ? "live stream: " + targetName(state.selected.id)
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
  line.append(el("span", "sid", sid(thread.session_id)));
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
  reply.addEventListener("click", () => {
    if (isMobile()) {
      // same flow as picking a session card: set the thread target
      // and land on the reply box, ready to write
      state.selected = { kind: "thread", id: thread.nonce };
      paintAsks();
      paintTarget();
      showTab("reply");
      return;
    }
    toggleThread(thread.nonce);
  });

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
  item.append(el("span", "sid", sid(thread.session_id)));
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
    // the resolved target, label first, id second: the operator
    // always sees exactly where the reply goes
    text = "reply to session " + targetName(state.selected.id);
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
        : "session " + sid(reply.session_id);
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
    let text = "send failed: " + err.message;
    // an ambiguous name answer lists the matching sessions so the
    // operator can see the choice and pick a row instead
    const matches = err.payload && err.payload.sessions;
    if (Array.isArray(matches) && matches.length > 1) {
      text += " (" + matches.map((one) => sid(one)).join(", ") + ")";
    }
    errorNode.textContent = text;
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
  // phones: a dropped stream also draws the thin amber line that sits
  // just above the tab bar, so the state survives the header scrolling
  // off screen; the tab bar itself hides this on desktop
  $("drop-line").hidden = open;
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
  stream = new EventSource(withToken("/api/events/stream"));
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
  $("token-save").addEventListener("click", applyToken);
  $("token-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") applyToken();
  });

  for (const tab of TABS) {
    $("tab-" + tab).addEventListener("click", () => showTab(tab));
  }
  $("stream-more").addEventListener("click", () => {
    mobileStreamRows += MOBILE_STREAM_STEP;
    paintStream();
  });
  showTab(state.activeTab); // one panel active from the first frame
  MOBILE_QUERY.addEventListener("change", () => {
    // crossing the 720px line: restore the one-visible-panel invariant
    // and reapply (or lift) the mobile stream cap
    if (isMobile()) showTab(state.activeTab);
    paintStream();
  });

  // capture ?token= from the page URL: the shell opened without
  // data, and this one value unlocks every call that follows
  apiToken = (new URLSearchParams(window.location.search).get("token") || "").trim();

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
