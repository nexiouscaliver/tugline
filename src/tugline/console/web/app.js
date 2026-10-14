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
 * Session identity is operator-first, and every control lives on the
 * session detail page. Tapping a card (phone) or a table row (desktop)
 * opens the detail: header line (label or id, display id, state chip,
 * adapter and project badges), meta grid (first seen, last seen, cwd,
 * tags), big-target actions (reply, rename through PUT
 * /api/sessions/<id>/label, tags through PUT and DELETE
 * /api/sessions/<id>/tag/<tag>), the last prompt body, the last stop
 * response preview, and the session's newest events. The list cards
 * themselves are read-only: name/id line, state chip, adapter,
 * relative time, last event text, nothing else. Tags are lowercase
 * letters, digits, and hyphens (the server normalizes; anything else
 * in the input is dropped). Ids shown anywhere are display_id values
 * computed per response by the server: suffixes lengthened until
 * unique, so two sessions sharing a tail never render the same short
 * id. State chips read display_state (active now, running, waiting
 * on me, idle, done): the server derives it from recency, so a
 * session whose last event is hours old never shows running. The
 * panel sorts active-now rows first, then by recency.
 *
 * The detail prompt and response blocks render through an escape-first
 * markdown renderer (see its rule comment above renderMarkdown); every
 * other surface stays plain text. The sessions panel carries an inline
 * filter bar: a search input (identity fields plus last-event text)
 * and state chips over the same display_state vocabulary, applied
 * client side to the rows in memory, with the state kept in memory so
 * repaints and SSE deltas keep honoring it.
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

// Sessions filter bar. Chips read the display_state vocabulary the
// console already renders; Running covers running AND active (both are
// live work). The filter lives in memory only and survives repaints,
// so SSE delta refreshes keep honoring it.
const FILTER_CHIPS = [
  { key: "all", label: "All", states: null },
  { key: "running", label: "Running", states: ["running", "active"] },
  { key: "waiting", label: "Waiting on me", states: ["waiting"] },
  { key: "idle", label: "Idle", states: ["idle"] },
  { key: "done", label: "Done", states: ["done"] },
];
const sessionFilter = { text: "", chip: "all" };

function filterActive() {
  return sessionFilter.text.trim() !== "" || sessionFilter.chip !== "all";
}

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
  detailId: null, // the session whose detail page is open, or null
  detail: null, // the last /api/sessions/<id> payload for that page
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
// four panels keep their grid seats. Picking a tab also closes the
// session detail: tabs and the detail page share the panel area.
function showTab(name) {
  state.activeTab = name;
  closeDetail();
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

// Tapping a card (phone) or row (desktop) opens the session detail
// page. Pick who, then act: reply, rename, and tags all live there.
function openDetail(sessionId) {
  state.detailId = sessionId;
  state.detail = null;
  document.body.classList.add("detail-open");
  // phones: the detail takes the panel area above the tab bar, the
  // same seat any tab panel occupies; the tab bar itself stays put
  for (const tab of TABS) $(tab + "-panel").classList.remove("active");
  $("detail-panel").classList.add("active");
  renderDetail();
  if (isMobile()) window.scrollTo(0, 0);
  refreshDetail();
}

function closeDetail() {
  if (state.detailId === null) return;
  state.detailId = null;
  state.detail = null;
  document.body.classList.remove("detail-open");
  $("detail-panel").classList.remove("active");
}

function goBackFromDetail() {
  // back returns to the previous tab/panel state, unchanged
  closeDetail();
  if (isMobile()) showTab(state.activeTab);
}

async function refreshDetail() {
  if (state.detailId === null) return;
  try {
    state.detail = await api(
      "/api/sessions/" + encodeURIComponent(state.detailId)
    );
    renderDetail();
  } catch (err) {
    // keep the last painted detail; the next delta retries
  }
}

function idCellContent(session) {
  // the label is the primary name when set; the collision-proof short
  // id is always visible beside or beneath it. Cards carry no
  // controls: rename and tags live on the detail page only
  const wrap = el("div", "idwrap");
  const shown = String(session.display_id || short8(session.session_id));
  const button = el(
    "button",
    "idlink session-name",
    String(session.label || shown)
  );
  button.type = "button";
  button.title = session.session_id;
  button.addEventListener("click", (event) => {
    // the row handler opens the detail for every tap; the button only
    // keeps a keyboard path to the same place (stopPropagation stops
    // the row handler from opening it twice)
    event.stopPropagation();
    openDetail(session.session_id);
  });
  wrap.append(button);
  if (session.label) wrap.append(el("div", "idsub", shown));
  return wrap;
}

// -- markdown rendering (detail prompt and response blocks) --

/**
 * ESCAPE-FIRST RULE: every character of payload text reaches the DOM
 * through textContent (the DOM-safe equivalent of html-escaping before
 * any transformation), never through innerHTML, so no text can inject
 * markup. The only tags ever created are the whitelisted ones below;
 * fenced code is appended as one verbatim text node with nothing
 * inside it transformed. No other guarantees are made.
 *
 * Supported: ``` fenced code blocks, `inline code`, **bold**, *italic*,
 * [label](url) links (only http:// and https:// urls become anchors;
 * anything else renders the label with the url in parentheses),
 * #..#### headings, "- " / "* " unordered lists, "1. " ordered lists,
 * "> " blockquotes, --- thematic breaks, paragraphs. Text carrying no
 * construct at all takes the plain path and renders exactly as the old
 * single <p> did.
 */

const MD_HAS_CONSTRUCT = new RegExp(
  "```" +
    "|^[ \\t]*#{1,4}\\s+" +
    "|^[ \\t]*-{3,}$" +
    "|^[ \\t]*[-*]\\s+" +
    "|^[ \\t]*\\d+\\.\\s+" +
    "|^[ \\t]*>" +
    "|`[^`\\n]+`" +
    "|\\*\\*[^*\\n]+\\*\\*" +
    "|\\*[^*\\n]+\\*" +
    "|\\[[^\\]\\n]+\\]\\([^)\\s]+\\)",
  "m"
);

// ordered: code before bold, bold before italic, so ** never reads as
// two italics; link content cannot span lines
const MD_INLINE_RULES = [
  { re: /^`([^`\n]+)`/, make: (m) => el("code", null, m[1]) },
  { re: /^\*\*([^*\n]+)\*\*/, make: (m) => el("strong", null, m[1]) },
  { re: /^\*([^*\n]+)\*/, make: (m) => el("em", null, m[1]) },
  {
    re: /^\[([^\]\n]+)\]\(([^)\s]+)\)/,
    make: (m) => {
      const url = m[2];
      if (/^https?:\/\//i.test(url)) {
        const link = el("a", null, m[1]);
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        return link;
      }
      return document.createTextNode(m[1] + " (" + url + ")");
    },
  },
];

function mdInline(parent, text) {
  let rest = String(text);
  while (rest) {
    let node = null;
    for (const rule of MD_INLINE_RULES) {
      const match = rule.re.exec(rest);
      if (match) {
        node = rule.make(match);
        rest = rest.slice(match[0].length);
        break;
      }
    }
    if (node) {
      parent.append(node);
      continue;
    }
    // plain run up to the next possible opener; an unmatched opener at
    // the scan position is one literal character, so this always ends
    const next = /[`*[]/.exec(rest);
    const take = next ? next.index : rest.length;
    const chunk = take === 0 ? rest.slice(0, 1) : rest.slice(0, take);
    parent.append(chunk);
    rest = rest.slice(chunk.length);
  }
}

function mdStartsBlock(line) {
  const t = line.trim();
  return (
    t === "" ||
    t.startsWith("```") ||
    t.startsWith(">") ||
    /^#{1,4}\s+/.test(t) ||
    /^[-*]\s+/.test(t) ||
    /^\d+\.\s+/.test(t) ||
    /^-{3,}$/.test(t)
  );
}

function renderMarkdown(text) {
  const root = el("div", "md");
  const value = String(text || "");
  if (!value) return root;
  if (!MD_HAS_CONSTRUCT.test(value)) {
    // plain path: byte for byte the old single-paragraph block
    root.append(el("p", null, value));
    return root;
  }
  const lines = value.split("\n");
  let i = 0;
  while (i < lines.length) {
    const t = lines[i].trim();
    if (t === "") {
      i += 1;
    } else if (t.startsWith("```")) {
      // fence: everything to the closing fence (or the end) is one
      // verbatim code node; the language tag is ignored
      i += 1;
      const buf = [];
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        buf.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      const code = el("code", null, buf.join("\n"));
      const pre = el("pre");
      pre.append(code);
      root.append(pre);
    } else if (/^-{3,}$/.test(t)) {
      root.append(el("hr"));
      i += 1;
    } else if (/^#{1,4}\s+/.test(t)) {
      const level = t.match(/^#+/)[0].length;
      const heading = el("h" + level);
      mdInline(heading, t.replace(/^#{1,4}\s+/, ""));
      root.append(heading);
      i += 1;
    } else if (t.startsWith(">")) {
      const quote = el("blockquote");
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        const stripped = lines[i].trim().replace(/^>\s?/, "");
        mdInline(quote, stripped);
        i += 1;
        if (i < lines.length && lines[i].trim().startsWith(">")) {
          quote.append(el("br"));
        }
      }
      root.append(quote);
    } else if (/^[-*]\s+/.test(t) || /^\d+\.\s+/.test(t)) {
      const ordered = /^\d+\.\s+/.test(t);
      const list = el(ordered ? "ol" : "ul");
      const item = ordered ? /^\d+\.\s+/ : /^[-*]\s+/;
      while (i < lines.length && item.test(lines[i].trim())) {
        const li = el("li");
        mdInline(li, lines[i].trim().replace(item, ""));
        list.append(li);
        i += 1;
      }
      root.append(list);
    } else {
      // paragraph: consecutive plain lines keep their line breaks
      const buf = [lines[i]];
      i += 1;
      while (i < lines.length && !mdStartsBlock(lines[i])) {
        buf.push(lines[i]);
        i += 1;
      }
      const p = el("p");
      mdInline(p, buf.join("\n"));
      root.append(p);
    }
  }
  return root;
}

// -- the session detail page --

function detailHead(session) {
  const head = el("div", "detail-head");
  const nameBlock = el("div", "detail-nameblock");
  const shown = String(session.display_id || short8(session.session_id));
  const idLine = el("div", "detail-idmono", shown);
  // hover or long-press reveals the full id when the short one is not
  // enough (the events rows and the store agree on this display id)
  idLine.title = String(session.session_id);
  nameBlock.append(
    el("div", "detail-title", String(session.label || shown)),
    idLine
  );
  const badges = el("div", "detail-badges");
  const stateName = String(session.display_state || session.state || "running");
  badges.append(
    el(
      "span",
      "chip chip-state chip-" + (STATE_TONES[stateName] || "gray"),
      STATE_LABELS[stateName] || stateName
    )
  );
  if (session.adapter) {
    badges.append(el("span", "chip chip-gray", String(session.adapter)));
  }
  if (session.project) {
    badges.append(el("span", "chip chip-gray", String(session.project)));
  }
  head.append(nameBlock, badges);
  return head;
}

function detailMeta(session) {
  const grid = el("dl", "detail-meta");
  const add = (label, value, valueClass) => {
    const dd = el("dd", valueClass || null);
    if (value instanceof Node) dd.append(value);
    else dd.textContent = String(value);
    grid.append(el("dt", null, label), dd);
  };
  const when = (iso) => {
    const ms = parseTs(iso);
    const node = el("span", "mono-time", relTime(ms));
    node.dataset.rel = ms === null ? "" : String(ms);
    node.title = String(iso || "");
    return node;
  };
  add("first seen", when(session.first_seen));
  add("last seen", when(session.last_seen));
  add("cwd", String(session.cwd || "-"), "mono-wrap");
  const tags = session.tags || [];
  add("tags", tags.join(", ") || "none", tags.length ? "mono-wrap" : "muted");
  return grid;
}

// chips with a real 44px x each; they live here, never on the cards
function detailTagChips(session) {
  const line = el("div", "tagline detail-tagline");
  for (const tag of session.tags || []) {
    const chip = el("span", "tagchip", String(tag));
    const remove = el("button", "tagx", "x");
    remove.type = "button";
    remove.title = "remove tag " + tag;
    remove.setAttribute("aria-label", "remove tag " + tag);
    remove.addEventListener("click", async () => {
      try {
        await addOrRemoveTag(session, tag, "DELETE");
        await Promise.all([refreshDetail(), refreshSessions()]);
      } catch (err) {
        // removal cannot fail for a missing tag; the repaint shows truth
      }
    });
    chip.append(remove);
    line.append(chip);
  }
  if (!(session.tags || []).length) {
    line.append(el("span", "muted", "no tags yet"));
  }
  return line;
}

function detailActions(session) {
  const wrap = el("div", "detail-actions");

  const reply = el(
    "button",
    "btn btn-primary detail-replybtn",
    "Reply to this session"
  );
  reply.type = "button";
  reply.addEventListener("click", () => {
    state.selected = { kind: "session", id: session.session_id };
    paintTarget();
    if (isMobile()) {
      showTab("reply"); // closes the detail, lands on the reply box
    } else {
      $("reply-body").focus();
    }
  });
  wrap.append(reply);

  // rename: input + save, both 44px; an empty value clears the label
  const renameLine = el("div", "detail-control");
  renameLine.append(el("span", "ctl-label", "rename"));
  const renameInput = document.createElement("input");
  renameInput.type = "text";
  renameInput.className = "detail-input";
  renameInput.id = "detail-rename-input";
  renameInput.maxLength = 60;
  renameInput.value = String(session.label || "");
  renameInput.placeholder = "label (empty clears)";
  renameInput.setAttribute("aria-label", "rename this session");
  const renameSave = el("button", "btn", "save");
  renameSave.type = "button";
  const renameError = el("p", "error");
  renameError.hidden = true;
  const saveRename = async () => {
    renameError.hidden = true;
    try {
      await api(
        "/api/sessions/" +
          encodeURIComponent(session.session_id) +
          "/label",
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label: renameInput.value.trim() }),
        }
      );
      await Promise.all([refreshDetail(), refreshSessions()]);
    } catch (err) {
      renameError.textContent = "save failed: " + err.message;
      renameError.hidden = false;
    }
  };
  renameSave.addEventListener("click", saveRename);
  renameInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveRename();
    }
  });
  renameLine.append(renameInput, renameSave);
  wrap.append(renameLine, renameError);

  // add tag: input + add button, chips with x beneath, all 44px
  const tagLine = el("div", "detail-control");
  tagLine.append(el("span", "ctl-label", "add tag"));
  const tagInput = document.createElement("input");
  tagInput.type = "text";
  tagInput.className = "detail-input";
  tagInput.id = "detail-tag-input";
  tagInput.maxLength = 24;
  tagInput.placeholder = "tag (a-z, 0-9, -)";
  tagInput.setAttribute("aria-label", "add a tag to this session");
  const tagAdd = el("button", "btn", "add");
  tagAdd.type = "button";
  const tagError = el("p", "error");
  tagError.hidden = true;
  const saveTag = async () => {
    tagError.hidden = true;
    const value = tagInput.value.trim();
    if (!value) return;
    try {
      await addOrRemoveTag(session, value, "PUT");
      tagInput.value = "";
      await Promise.all([refreshDetail(), refreshSessions()]);
    } catch (err) {
      tagError.textContent = "add failed: " + err.message;
      tagError.hidden = false;
    }
  };
  tagAdd.addEventListener("click", saveTag);
  tagInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveTag();
    }
  });
  tagLine.append(tagInput, tagAdd);
  wrap.append(tagLine, detailTagChips(session), tagError);
  return wrap;
}

// Full-text blocks only: the prompt and response render through the
// escape-first markdown renderer; stream rows and card subtitles stay
// plain single-line text.
function detailTextBlock(label, text, absentLine) {
  const block = el("section", "detail-block");
  block.append(el("h3", null, label));
  const value = String(text || "");
  block.append(value ? renderMarkdown(value) : el("p", "empty", absentLine));
  return block;
}

function renderDetail() {
  const box = $("detail-body");
  if (state.detailId === null) {
    box.replaceChildren();
    return;
  }
  if (state.detail === null) {
    box.replaceChildren(el("p", "empty", "loading session detail"));
    return;
  }
  // a repaint must never yank an input the operator is typing into;
  // the first delta after the blur brings the page current again
  const focused = document.activeElement;
  if (focused && focused.tagName === "INPUT" && box.contains(focused)) {
    return;
  }

  const detail = state.detail;
  const session = detail.session;
  const parts = [
    detailHead(session),
    detailMeta(session),
    detailActions(session),
    detailTextBlock("last prompt", detail.last_prompt, "no prompt recorded yet"),
    detailTextBlock(
      "last response",
      detail.last_response,
      "no response preview recorded yet"
    ),
  ];

  const events = el("section", "detail-block");
  events.append(el("h3", null, "events"));
  const list = el("ol", "stream detail-events");
  list.append(...(detail.events || []).map(streamRow));
  const scroll = el("div", "scroll");
  scroll.append(list);
  events.append(scroll);
  if (!detail.events || detail.events.length === 0) {
    events.append(el("p", "empty", "no events for this session yet"));
  }
  parts.push(events);

  box.replaceChildren(...parts);
}

// -- tags: the one PUT/DELETE path the detail controls share --

async function addOrRemoveTag(session, tag, method) {
  await api(
    "/api/sessions/" +
      encodeURIComponent(session.session_id) +
      "/tag/" +
      encodeURIComponent(tag),
    { method: method }
  );
}

function sessionRow(session) {
  const row = document.createElement("tr");
  if (isSelected("session", session.session_id)) row.classList.add("selected");

  // the whole row is the detail target, phones and desktop alike
  row.addEventListener("click", () => openDetail(session.session_id));

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

// One session against the active filter: the chip picks display
// states (Running means running or active), and every whitespace
// separated search term must appear somewhere across the identity
// fields and the last-event text. Client side, on the data in memory.
function filterMatches(session) {
  const chip =
    FILTER_CHIPS.find((c) => c.key === sessionFilter.chip) ||
    FILTER_CHIPS[0];
  if (chip.states) {
    const name = String(session.display_state || session.state || "");
    if (!chip.states.includes(name)) return false;
  }
  const terms = sessionFilter.text
    .trim()
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  if (!terms.length) return true;
  const latest = lastEventFor(session);
  const haystack = [
    session.label || "",
    session.display_id || "",
    session.session_id || "",
    session.adapter || "",
    session.project || "",
    session.last_event || "",
    latest ? latest.title || "" : "",
    latest ? latest.body || "" : "",
  ]
    .join(" ")
    .toLowerCase();
  return terms.every((term) => haystack.includes(term));
}

// Adapter and project are empty until field enrichment lands, so the
// two columns render only when the current data carries at least one
// non-empty value; the classes are re-derived on every repaint and
// the columns come back on their own once enriched data arrives. The
// rows come from the filter; the id maps stay built from every
// session so names resolve everywhere else too.
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

  const shown = state.sessions.filter(filterMatches);
  $("sessions-body").replaceChildren(...shown.map(sessionRow));

  const active = filterActive();
  $("filter-clear").hidden = !active;
  const count = $("filter-count");
  count.hidden = !active;
  if (active) {
    count.textContent = shown.length + " of " + state.sessions.length +
      " sessions";
  }
  const empty = $("sessions-empty");
  empty.hidden = shown.length > 0;
  empty.textContent =
    active && state.sessions.length > 0
      ? "no sessions match the filter"
      : "no sessions yet; rows appear when a hook spools an event";
}

function setFilterChip(key) {
  sessionFilter.chip = key;
  for (const chip of FILTER_CHIPS) {
    $("filter-chip-" + chip.key).classList.toggle(
      "active",
      chip.key === key
    );
  }
  paintSessions();
}

function clearFilter() {
  sessionFilter.text = "";
  $("filter-search").value = "";
  setFilterChip("all");
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
      // wake state straight from the dispatcher journal: dispatched,
      // finished, timed_out, unsupported, skipped. Absent (null) means
      // no wake decision was ever recorded for this reply.
      if (reply.wake && reply.wake.state) {
        item.append(el("span", "wake-state wake-" + reply.wake.state, reply.wake.state));
      }
      return item;
    })
  );
  $("recent-empty").hidden = recent.length > 0;
}

// -- selection --

// Session selection (the reply target) is set from the detail page's
// reply button and from ask reply links; thread selection toggles
// from the asks panel. The stream panel filters to the selected
// session client side.

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
  if (state.detailId !== null) refreshDetail(); // the open detail rides along
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
  if (state.detailId !== null) refreshDetail(); // the open detail page rides along
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
  $("filter-search").addEventListener("input", (event) => {
    sessionFilter.text = event.target.value;
    paintSessions(); // instant, client side, on the data in memory
  });
  $("filter-clear").addEventListener("click", clearFilter);
  for (const chip of FILTER_CHIPS) {
    $("filter-chip-" + chip.key).addEventListener("click", () =>
      setFilterChip(chip.key)
    );
  }
  $("detail-back").addEventListener("click", goBackFromDetail);
  $("stream-more").addEventListener("click", () => {
    mobileStreamRows += MOBILE_STREAM_STEP;
    paintStream();
  });
  showTab(state.activeTab); // one panel active from the first frame
  MOBILE_QUERY.addEventListener("change", () => {
    // crossing the 720px line: restore the one-visible-panel invariant
    // and reapply (or lift) the mobile stream cap; an open detail
    // stays the visible panel, whichever side of the line it lands on
    if (isMobile()) {
      if (state.detailId !== null) {
        for (const tab of TABS) $(tab + "-panel").classList.remove("active");
        $("detail-panel").classList.add("active");
      } else {
        showTab(state.activeTab);
      }
    }
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
