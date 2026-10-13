import { apiGet, apiPost } from "./api.js";
import { mountLoginCube } from "./cube.js";

const SVG = "http://www.w3.org/2000/svg";
const PAGES = {
  overview: "Overview",
  transfers: "Transfers",
  risk: "Risk",
  feedback: "Feedback",
  statistics: "Statistics",
  logs: "Logs",
  users: "Users",
  chats: "Chats",
  settings: "Settings",
};
const CLASS_ORDER = ["CANONICAL", "VARIANT", "KNOWN_FRAUD", "UNKNOWN", "ERROR"];
const CLASS_LABELS = {
  CANONICAL: "Resmî token",
  VARIANT: "Token varyantı",
  KNOWN_FRAUD: "Bilinen sahte",
  UNKNOWN: "Bilinmeyen token",
  ERROR: "Hata",
};
const CLASS_TONE = {
  CANONICAL: "good",
  VARIANT: "warn",
  KNOWN_FRAUD: "bad",
  UNKNOWN: "info",
  ERROR: "muted",
};
const CLASS_COLOR = {
  CANONICAL: "#2583FF",
  VARIANT: "#7957FF",
  KNOWN_FRAUD: "#FF526B",
  UNKNOWN: "#20D4E7",
  ERROR: "#8EA4C2",
};
const RISK_ORDER = ["DÜŞÜK", "ORTA", "YÜKSEK", "KRİTİK"];
const RISK_LABELS = { "DÜŞÜK": "Low", ORTA: "Medium", "YÜKSEK": "High", "KRİTİK": "Critical" };
const RISK_TONE = { "DÜŞÜK": "good", ORTA: "warn", "YÜKSEK": "high", "KRİTİK": "bad" };
const RISK_COLOR = { "DÜŞÜK": "#27D6A0", ORTA: "#FFBE55", "YÜKSEK": "#F09A4A", "KRİTİK": "#FF526B" };
const KIND_LABELS = {
  address_poisoning: "Address poisoning",
  approval: "Approval",
  transfer: "Transfer",
};
const SIGNAL_LABELS = {
  known_fraud_token: "Bilinen sahte token",
  variant_token: "Token varyantı",
  unknown_token: "Kayıtsız token",
  address_lookalike: "Adres benzerliği",
  lookalike_first_contact: "İlk temas",
  lookalike_dust_amount: "Düşük tutar",
  unlimited_allowance: "Sınırsız yetki",
  large_allowance: "Büyük yetki",
  allowance_increase: "Yetki artışı",
  new_allowance: "Yeni yetki",
  known_fraud_spender: "Bilinen sahte harcayıcı",
  listed_risky_spender: "Listelenmiş riskli harcayıcı",
};
const METRIC_LABELS = {
  transfer_total: "Transfer",
  notifications_sent: "Bildirim",
  check_usage: "/check",
  errors: "Hata",
  suspicious_total: "Şüpheli",
};
const WITHHELD = new Set(["N/A", "Yeterli etiketli veri yok", ""]);

const state = {
  page: "overview",
  range: rollingRange("7d"),
  filters: { network: "", classification: "", detection: "", risk: "", q: "", page: 1 },
  feedbackClass: "",
  riskFocus: "all",
  listPages: { poisoning: 1, approvals: 1, signals: 1 },
  ops: { file: "app.log", page: 1, q: "" },
  chat: { page: 1, q: "" },
  userQuery: "",
  userTab: "wallets",
};

let requestId = 0;
let searchTimer = 0;
let panelStarted = false;

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".nav-item[data-page]").forEach((button) => {
    button.addEventListener("click", () => {
      const page = button.getAttribute("data-page");
      closeSidebar();
      if (pageFromHash() === page) {
        loadCurrent();
        return;
      }
      location.hash = page;
    });
  });
  document.getElementById("menu").addEventListener("click", () => openSidebar(true));
  document.getElementById("backdrop").addEventListener("click", closeSidebar);
  document.getElementById("gate-form").addEventListener("submit", onLogin);
  document.getElementById("gate-reveal").addEventListener("click", onRevealPassword);
  document.getElementById("gate-pass").addEventListener("keyup", onCapsLock);
  document.getElementById("gate-pass").addEventListener("keydown", onCapsLock);
  const card = document.getElementById("gate-form");
  if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    card.addEventListener("pointermove", onCardTilt);
    card.addEventListener("pointerleave", () => {
      card.style.transform = "";
    });
    card.addEventListener("animationend", (event) => {
      if (event.target !== card) return;
      if (event.animationName === "gate-shake") card.classList.remove("is-shaking");
      if (event.animationName === "gate-up") card.classList.add("is-settled");
    });
  }
  document.getElementById("logout").addEventListener("click", onLogout);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSidebar();
  });
  document.addEventListener("aft-unauthorized", () => showGate());
  window.addEventListener("hashchange", () => {
    if (document.getElementById("shell").hidden) return;
    state.filters.page = 1;
    state.listPages = { poisoning: 1, approvals: 1, signals: 1 };
    loadCurrent();
  });
  mountTicker();
  startGateNotes();
  scrambleGuard();
  loginCube = mountLoginCube(document.getElementById("gate-cube"));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopGateNotes();
    else if (!document.getElementById("gate").hidden) startGateNotes();
  });
  openSession();
});

async function openSession() {
  try {
    const session = await apiGet("/api/session");
    if (session.required && !session.authenticated) {
      showGate();
      return;
    }
    showApp(Boolean(session.required));
  } catch (error) {
    showGate(error.message || "Panele ulaşılamadı.");
  }
}

let loginCube = null;
let orcaScene = null;
let orcaTicket = 0;
let tickerStarted = false;

async function mountTicker() {
  if (tickerStarted) return;
  tickerStarted = true;
  try {
    const payload = await apiGet("/api/ticker");
    const coins = tickerNames(payload.coins);
    const networks = tickerNames(payload.networks);
    const seen = new Set(coins.map((name) => name.toLocaleLowerCase("tr")));
    const items = coins.concat(networks.filter((name) => !seen.has(name.toLocaleLowerCase("tr"))));
    renderTicker(items);
  } catch {
    /* The strip stays hidden when the names cannot be read. */
  }
}

function tickerNames(value) {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => typeof item === "string" && item.trim());
}

function renderTicker(items) {
  const root = document.getElementById("gate-ticker");
  const track = document.getElementById("gate-ticker-track");
  const label = document.getElementById("gate-ticker-label");
  if (!root || !track || !items.length) return;
  if (label) label.textContent = items.join(", ");
  clear(track);
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const group = tickerGroup(items);
  track.append(group);
  root.hidden = false;
  if (reduced) return;
  const viewport = track.parentElement;
  const min = viewport ? viewport.clientWidth : 0;
  let guard = 0;
  while (group.scrollWidth < min && guard < 12) {
    appendTickerItems(group, items);
    guard += 1;
  }
  const copy = group.cloneNode(true);
  track.append(copy);
}

function tickerGroup(items) {
  const group = el("div", { class: "gate-ticker-group" });
  appendTickerItems(group, items);
  return group;
}

const GUARD_SYMBOLS = "∆ΣΩ#%&*+=?/<>ΞΨΦΛ";

function scrambleGuard() {
  const line = document.querySelectorAll(".gate-line")[1];
  const accent = line && line.querySelector(".gate-accent");
  if (!line || !accent) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const word = [...accent.textContent];
  const tail = [..."Tut"];
  clear(accent);
  const glyphs = word.map((ch, index) => {
    const node = el("span", { class: "gate-glyph", text: spinningSymbol(0, index) });
    accent.append(node);
    return { node, ch };
  });
  while (accent.nextSibling) accent.nextSibling.remove();
  line.append(document.createTextNode(" "));
  tail.forEach((ch, index) => {
    const node = el("span", { class: "gate-glyph", text: spinningSymbol(0, word.length + index) });
    line.append(node);
    glyphs.push({ node, ch });
  });
  line.append(document.createTextNode("."));
  const firstLock = 1550;
  const stagger = 140;
  function frame(now) {
    let pending = false;
    glyphs.forEach((glyph, index) => {
      if (now >= firstLock + index * stagger) {
        glyph.node.textContent = glyph.ch;
        return;
      }
      pending = true;
      glyph.node.textContent = spinningSymbol(now, index);
    });
    if (pending) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

function spinningSymbol(now, index) {
  const step = Math.floor(now / 42) + index;
  return GUARD_SYMBOLS[step % GUARD_SYMBOLS.length];
}

const GATE_NOTES = [
  ["Ethereum ağında Tether USD geldi", "güvenli coin"],
  ["TRON ağında TRX izlendi", "kayıtlı coin"],
  ["Ethereum ağında USD Coin doğrulandı", "güvenli"],
  ["Ethereum ağında Ether geldi", "yerel varlık"],
  ["TRON ağında Tether USD eşleşti", "bilinen coin"],
  ["Ethereum ağında Euro Coin görüldü", "kayıtlı"],
  ["Bitcoin ağı Bitcoin doğruladı", "bilinen coin"],
  ["TRON ağında transfer geçti", "TRX güvenli"],
];
let gateNoteTimer = 0;
let gateNoteIndex = 0;

function startGateNotes() {
  const slot = document.getElementById("gate-live-slot");
  if (!slot || gateNoteTimer) return;
  if (!slot.querySelector(".gate-note")) showGateNote(slot);
  gateNoteTimer = window.setInterval(() => showGateNote(slot), 4000);
}

function stopGateNotes() {
  if (!gateNoteTimer) return;
  window.clearInterval(gateNoteTimer);
  gateNoteTimer = 0;
}

function showGateNote(slot) {
  const [text, tag] = GATE_NOTES[gateNoteIndex % GATE_NOTES.length];
  gateNoteIndex += 1;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  slot.querySelectorAll(".gate-note.is-leaving").forEach((node) => node.remove());
  const current = slot.querySelector(".gate-note");
  if (current) {
    if (reduced) current.remove();
    else {
      current.classList.add("is-leaving");
      const drop = () => current.remove();
      current.addEventListener("animationend", drop, { once: true });
      window.setTimeout(drop, 450);
    }
  }
  slot.append(el("p", { class: "gate-note" }, [
    el("span", { class: "gate-note-dot" }),
    el("span", { class: "gate-note-text", text }),
    el("span", { class: "gate-note-sep", text: "·" }),
    el("span", { class: "gate-note-tag", text: tag }),
  ]));
}

function appendTickerItems(group, items) {
  for (const name of items) {
    group.append(
      el("span", { class: "gate-ticker-item", text: name }),
      el("span", { class: "gate-ticker-sep", text: "•" }),
    );
  }
}

async function mountOrca() {
  const stage = document.getElementById("gate-stage");
  if (!stage || orcaScene) return;
  const ticket = ++orcaTicket;
  try {
    const module = await import("./orca.js");
    if (ticket !== orcaTicket || document.getElementById("gate").hidden || orcaScene) return;
    orcaScene = module.mountOrca(stage, document.getElementById("gate"));
    if (!orcaScene) stage.classList.add("is-flat");
  } catch {
    stage.classList.add("is-flat");
  }
}

function unmountOrca() {
  orcaTicket += 1;
  if (!orcaScene) return;
  orcaScene.dispose();
  orcaScene = null;
}

function onRevealPassword() {
  const input = document.getElementById("gate-pass");
  const button = document.getElementById("gate-reveal");
  const shown = input.type === "text";
  input.type = shown ? "password" : "text";
  button.setAttribute("aria-pressed", String(!shown));
  button.setAttribute("aria-label", shown ? "Şifreyi göster" : "Şifreyi gizle");
}

function onCapsLock(event) {
  const caps = document.getElementById("gate-caps");
  if (!caps || typeof event.getModifierState !== "function") return;
  caps.hidden = !event.getModifierState("CapsLock");
}

function onCardTilt(event) {
  const card = event.currentTarget;
  const rect = card.getBoundingClientRect();
  const px = (event.clientX - rect.left) / rect.width - 0.5;
  const py = (event.clientY - rect.top) / rect.height - 0.5;
  card.style.transform = `perspective(900px) rotateX(${(-py * 3).toFixed(2)}deg) rotateY(${(px * 3).toFixed(2)}deg)`;
}

async function onLogin(event) {
  event.preventDefault();
  const button = document.getElementById("gate-submit");
  const label = document.getElementById("gate-submit-label");
  const error = document.getElementById("gate-error");
  const card = document.getElementById("gate-form");
  if (button.disabled) return;
  if (loginCube) loginCube.solve();
  error.textContent = "";
  button.disabled = true;
  button.classList.add("is-busy");
  button.setAttribute("aria-busy", "true");
  label.textContent = "Giriş yapılıyor…";
  let entered = false;
  try {
    await apiPost("/api/login", {
      username: document.getElementById("gate-user").value,
      password: document.getElementById("gate-pass").value,
    });
    document.getElementById("gate-pass").value = "";
    entered = true;
    if (loginCube && loginCube.complete) {
      await Promise.race([
        loginCube.complete(),
        new Promise((resolve) => setTimeout(resolve, 3200)),
      ]);
    }
    document.getElementById("gate").classList.add("is-leaving");
    if (orcaScene && orcaScene.dive) await orcaScene.dive();
    showApp(true);
  } catch (failure) {
    error.textContent = failure.message || "Kullanıcı adı veya şifre hatalı.";
    card.classList.remove("is-shaking");
    void card.offsetWidth;
    card.classList.add("is-shaking");
    if (loginCube) loginCube.reject();
    if (orcaScene && orcaScene.flinch) orcaScene.flinch();
  } finally {
    if (!entered) {
      button.disabled = false;
      button.classList.remove("is-busy");
      button.removeAttribute("aria-busy");
      label.textContent = "Giriş";
    }
  }
}

async function onLogout() {
  closeSidebar();
  try {
    await apiPost("/api/logout", {});
  } catch {
    /* The cookie is cleared when the server accepts the request. */
  }
  showGate();
}

function showGate(message) {
  document.title = "Anti-Flash Token";
  document.getElementById("shell").hidden = true;
  document.getElementById("gate").hidden = false;
  document.getElementById("logout").hidden = true;
  document.getElementById("gate-pass").value = "";
  document.getElementById("gate").classList.remove("is-leaving");
  const caps = document.getElementById("gate-caps");
  if (caps) caps.hidden = true;
  const error = document.getElementById("gate-error");
  error.textContent = message || "";
  const user = document.getElementById("gate-user");
  if (user) user.focus();
  mountOrca();
  startGateNotes();
  if (!loginCube) loginCube = mountLoginCube(document.getElementById("gate-cube"));
  else loginCube.sync();
}

function showApp(loginRequired) {
  stopGateNotes();
  unmountOrca();
  if (loginCube) {
    loginCube.dispose();
    loginCube = null;
  }
  document.getElementById("gate").hidden = true;
  document.getElementById("shell").hidden = false;
  document.getElementById("logout").hidden = !loginRequired;
  if (!panelStarted) {
    panelStarted = true;
    startSidebarSnow();
  }
  loadCurrent();
}

function pageFromHash() {
  const name = location.hash.replace("#", "");
  return Object.prototype.hasOwnProperty.call(PAGES, name) ? name : "overview";
}

function rollingRange(preset) {
  const end = new Date();
  const hours = preset === "24h" ? 24 : preset === "30d" ? 720 : 168;
  const start = new Date(end.getTime() - hours * 60 * 60 * 1000);
  return { start: start.toISOString(), end: end.toISOString(), preset };
}

async function loadCurrent() {
  const id = ++requestId;
  state.page = pageFromHash();
  paintChrome();
  const view = document.getElementById("view");
  const focusId = document.activeElement && document.activeElement.id;
  const keepField = focusId === "q" || focusId === "ops-q" || focusId === "chat-q" || focusId === "user-q";
  const cursor = keepField ? document.activeElement.selectionStart : null;
  if (!document.getElementById("results") || !keepField) showSkeleton();
  try {
    const payload = await apiGet(endpointFor(state.page));
    if (id !== requestId) return;
    renderPage(payload);
    if (keepField) {
      const box = document.getElementById(focusId);
      if (box) {
        box.focus();
        if (cursor != null) box.setSelectionRange(cursor, cursor);
      }
    }
  } catch (error) {
    if (id !== requestId) return;
    clear(view);
    const box = el("div", { class: "error", role: "alert" });
    box.append(el("p", { text: error.message || "Veri alınamadı." }));
    const retry = el("button", { class: "button", type: "button", text: "Yeniden dene" });
    retry.addEventListener("click", loadCurrent);
    box.append(retry);
    view.append(box);
  }
}

function endpointFor(page) {
  if (page === "overview") return "/api/overview";
  if (page === "users") return "/api/users";
  if (page === "settings") return "/api/settings";
  if (page === "chats") {
    const params = new URLSearchParams();
    if (state.selectedChat) params.set("chat", state.selectedChat);
    const query = params.toString();
    return query ? "/api/chats?" + query : "/api/chats";
  }
  if (page === "logs") {
    const params = new URLSearchParams();
    params.set("page", String(state.ops.page));
    params.set("page_size", "40");
    if (state.ops.q) params.set("q", state.ops.q);
    params.set("file", state.ops.file);
    return "/api/logs?" + params.toString();
  }
  const params = new URLSearchParams();
  params.set("start", state.range.start);
  params.set("end", state.range.end);
  if (page === "transfers") {
    for (const key of ["network", "classification", "detection", "risk", "q"]) {
      if (state.filters[key]) params.set(key, state.filters[key]);
    }
    params.set("page", String(state.filters.page));
    params.set("page_size", "20");
  }
  if (page === "feedback" && state.feedbackClass) params.set("classification", state.feedbackClass);
  const path = { transfers: "/api/transfers", risk: "/api/risk", feedback: "/api/feedback", statistics: "/api/statistics" }[page];
  return path + "?" + params.toString();
}

function paintChrome() {
  const title = PAGES[state.page];
  document.getElementById("page-title").textContent = title;
  document.title = title + " · Anti-Flash Token";
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.getAttribute("data-page") === state.page;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

function startSidebarSnow() {
  const sidebar = document.getElementById("sidebar");
  const canvas = document.getElementById("sidebar-snow");
  if (!sidebar || !canvas || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const context = canvas.getContext("2d");
  if (!context) return;
  const flakes = [];
  const pointer = { x: -1000, y: -1000 };
  let width = 0;
  let height = 0;
  let running = true;

  function makeFlake(anywhere) {
    return {
      x: Math.random() * width,
      y: anywhere ? Math.random() * height : -8,
      radius: 0.7 + Math.random() * 1.7,
      speed: 0.12 + Math.random() * 0.34,
      drift: (Math.random() - 0.5) * 0.16,
      phase: Math.random() * Math.PI * 2,
      alpha: 0.22 + Math.random() * 0.5,
    };
  }

  function resize() {
    const rect = sidebar.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    width = Math.max(1, rect.width);
    height = Math.max(1, rect.height);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    const count = Math.max(18, Math.round((width * height) / 7800));
    while (flakes.length < count) flakes.push(makeFlake(true));
    flakes.length = count;
  }

  function frame(time) {
    if (!running) return;
    context.clearRect(0, 0, width, height);
    for (const flake of flakes) {
      flake.y += flake.speed;
      flake.x += flake.drift + Math.sin(time / 1400 + flake.phase) * 0.08;
      if (flake.y > height + 6) {
        flake.y = -8;
        flake.x = Math.random() * width;
      }
      if (flake.x < -8) flake.x = width + 4;
      if (flake.x > width + 8) flake.x = -4;
      const dx = flake.x - pointer.x;
      const dy = flake.y - pointer.y;
      if (dx * dx + dy * dy < 48 * 48) continue;
      context.beginPath();
      context.fillStyle = "rgba(226, 236, 255," + flake.alpha.toFixed(3) + ")";
      context.arc(flake.x, flake.y, flake.radius, 0, Math.PI * 2);
      context.fill();
    }
    requestAnimationFrame(frame);
  }

  document.addEventListener("pointermove", (event) => {
    const rect = sidebar.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    if (x < 0 || y < 0 || x > rect.width || y > rect.height) {
      pointer.x = -1000;
      pointer.y = -1000;
      return;
    }
    pointer.x = x;
    pointer.y = y;
  });
  new ResizeObserver(resize).observe(sidebar);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      running = false;
      return;
    }
    if (running) return;
    running = true;
    requestAnimationFrame(frame);
  });
  resize();
  requestAnimationFrame(frame);
}

function renderPage(payload) {
  const view = document.getElementById("view");
  clear(view);
  if (state.page === "overview") view.append(...overviewNodes(payload));
  else if (state.page === "transfers") view.append(...transferNodes(payload));
  else if (state.page === "risk") view.append(...riskNodes(payload));
  else if (state.page === "feedback") view.append(...feedbackNodes(payload));
  else if (state.page === "logs") view.append(...logNodes(payload));
  else if (state.page === "users") view.append(...userNodes(payload));
  else if (state.page === "chats") view.append(...chatNodes(payload));
  else if (state.page === "settings") view.append(...settingsNodes(payload));
  else view.append(...statisticsNodes(payload));
}

function overviewNodes(data) {
  const nodes = [];
  const notes = notesBanner(data.notes);
  if (notes) nodes.push(notes);
  const strip = el("section", { class: "metric-strip", "aria-label": "Özet" });
  strip.append(
    metric("İzlenen cüzdan", data.wallet_count, "Benzersiz izlenen adres", "blue", "wallet"),
    metric("Kayıtlı transfer", data.transfer_count, "Tüm kayıt", "purple", "swap"),
    metric("Son 24 saat", data.transfers_24h, "Bu sürede kayda giren transfer", "cyan", "clock"),
    metric("Son 7 gün", data.transfers_7d, "Bu sürede kayda giren transfer", "amber", "calendar"),
  );
  nodes.push(strip);
  const grid = el("div", { class: "analysis-grid" });
  grid.append(
    distributionPanel("Token sınıflandırma", "Token sınıfı, risk seviyesi değildir.", data.by_classification, CLASS_ORDER, CLASS_COLOR, CLASS_LABELS, true),
    distributionPanel("Risk seviyesi", "Kayıtlı risk tespitleri.", data.by_risk_level, RISK_ORDER, RISK_COLOR, RISK_LABELS, true),
    systemCard(data.system || {}),
  );
  nodes.push(grid);
  const tables = el("div", { class: "table-stack" });
  tables.append(section("Son transferler", data.recent_transfers, transferColumns(), "Kayıtlı transfer yok.", "transfers", "rows"));
  tables.append(section("Son tespitler", data.recent_detections, detectionColumns(), "Kayıtlı tespit yok.", "risk", "shield"));
  nodes.push(tables);
  return nodes;
}

function transferNodes(data) {
  const nodes = [rangeBar(), transferFilters()];
  if (data.truncated) {
    nodes.push(el("p", { class: "notice", text: "Sonuçlar sınırlandı. Aralığı daraltırsanız kalan kayıtlar da sorgulanır." }));
  }
  const results = el("div", { id: "results" });
  results.append(section("Transferler", data.rows, transferDetailColumns(), "Bu süzgeçte kayıt yok."));
  results.append(pager(data.page, data.page_size, data.total, (next) => {
    state.filters.page = next;
    loadCurrent();
  }));
  nodes.push(results);
  return nodes;
}

function riskNodes(data) {
  const nodes = [rangeBar(), riskFocus()];
  if (!data.available) {
    nodes.push(el("p", { class: "notice", text: "Tespit tablosu yok. Address poisoning ve approval verisi gösterilmiyor." }));
    return nodes;
  }
  const grid = el("div", { class: "analysis-grid" });
  grid.append(distributionPanel("Risk seviyesi", "Bu aralıktaki risk kayıtları.", data.by_level, RISK_ORDER, RISK_COLOR, RISK_LABELS, true));
  const dayPanel = el("section", { class: "panel" });
  dayPanel.append(panelHead("Güne göre risk", "Seviye ve gün birlikte sayılır."));
  dayPanel.append(data.by_day && data.by_day.length
    ? dataTable([
      { label: "Gün", render: (row) => row.day },
      { label: "Seviye", render: (row) => riskBadge(row.level) },
      { label: "Sayı", align: "end", render: (row) => formatCount(row.count) },
    ], data.by_day)
    : el("p", { class: "empty", text: "Bu aralıkta risk kaydı yok." }));
  grid.append(dayPanel);
  nodes.push(grid);
  const focus = state.riskFocus;
  if (focus === "all" || focus === "poisoning") {
    nodes.push(pagedSection("Adres zehirlenmesi", data.poisoning, poisoningColumns(), "Bu aralıkta address poisoning kaydı yok.", "poisoning"));
  }
  if (focus === "all" || focus === "approval") {
    nodes.push(pagedSection("Approval", data.approvals, poisoningColumns(), "Bu aralıkta approval kaydı yok.", "approvals"));
  }
  if (focus === "all" || focus === "signals") {
    nodes.push(pagedSection("Sinyaller", data.signals, signalColumns(), "Bu aralıkta sinyal kaydı yok.", "signals"));
  }
  return nodes;
}

function feedbackNodes(data) {
  const nodes = [rangeBar(), feedbackFilter()];
  if (!data.available) {
    nodes.push(el("p", { class: "notice", text: "Geri bildirim tablosu yok." }));
    return nodes;
  }
  const strip = el("section", { class: "metric-strip", "aria-label": "Geri bildirim" });
  const counts = data.counts || {};
  strip.append(
    metric("CORRECT", counts.CORRECT || 0, "Doğru kabul", "cyan", "check"),
    metric("FALSE_POSITIVE", counts.FALSE_POSITIVE || 0, "Yanlış alarm", "amber", "alert"),
    metric("UNSURE", counts.UNSURE || 0, "Paydaya girmez", "purple", "help"),
    metric("Etiketli örnek", data.labeled, "CORRECT + FALSE_POSITIVE", "blue", "rows"),
  );
  nodes.push(strip);
  nodes.push(falseAlarmBlock(data.false_alarm, data.labeled));
  if (state.feedbackClass) {
    nodes.push(el("p", { class: "notice", text: "Üst sayılar aralığın tamamıdır. Tablo seçilen sınıflandırmayı gösterir." }));
  }
  nodes.push(section("Sınıflandırma ve geri bildirim", data.by_classification, [
    { label: "Sınıflandırma", render: (row) => classificationBadge(row.classification) },
    { label: "Geri bildirim", render: (row) => row.feedback || "—" },
    { label: "Sayı", align: "end", render: (row) => formatCount(row.count) },
  ], "Bu aralıkta geri bildirim yok."));
  return nodes;
}

function statisticsNodes(data) {
  const nodes = [rangeBar()];
  const notes = notesBanner(data.notes);
  if (notes) nodes.push(notes);
  const charts = el("div", { class: "split" });
  charts.append(
    chartPanel("Günlük transfer", data.daily_transfers, "#2583FF", "Bu aralıkta transfer yok."),
    chartPanel("Günlük tespit", data.daily_detections, "#20D4E7", "Bu aralıkta tespit yok."),
  );
  nodes.push(charts);
  const grid = el("div", { class: "analysis-grid" });
  grid.append(
    distributionPanel("Token sınıflandırma", "Seçilen aralıktaki transferler.", data.by_classification, CLASS_ORDER, CLASS_COLOR, CLASS_LABELS, true),
    distributionPanel("Risk seviyesi", "Seçilen aralıktaki risk kayıtları.", data.by_risk_level, RISK_ORDER, RISK_COLOR, RISK_LABELS, true),
  );
  nodes.push(grid);
  const feedback = data.feedback || {};
  if (feedback.available) nodes.push(falseAlarmBlock(feedback.false_alarm, feedback.labeled));
  nodes.push(section("Haftalık gönderimler", data.weekly, [
    { label: "Hafta", render: (row) => row.week_start },
    { label: "Durum", render: (row) => row.status },
    { label: "Kayıt", align: "end", render: (row) => formatCount(row.count) },
  ], "Haftalık gönderim kaydı yok."));
  const globalSection = section("Genel sayaçlar", data.global_metrics, [
    { label: "Gün", render: (row) => row.day },
    { label: "Sayaç", render: (row) => METRIC_LABELS[row.metric] || row.metric },
    { label: "Değer", align: "end", render: (row) => formatCount(row.value) },
  ], "Bu aralıkta stats_daily kaydı yok.");
  const caption = el("p", { class: "summary", text: "stats_daily tüm kurulum içindir. Bu sayılar cüzdan bazında ayrılmaz; hata ve /check sayaçları transfer sayısı değildir." });
  globalSection.insertBefore(caption, globalSection.children[1] || null);
  nodes.push(globalSection);
  return nodes;
}

function metric(label, value, note, tone, icon) {
  const item = el("article", { class: "metric metric-" + (tone || "blue") });
  const top = el("div", { class: "metric-top" });
  const holder = el("span", { class: "metric-icon", "aria-hidden": "true" });
  holder.append(uiIcon(icon || "rows"));
  top.append(holder);
  item.append(top);
  item.append(el("p", { class: "metric-label", text: label }));
  item.append(el("p", { class: "metric-value", text: formatCount(value) }));
  item.append(el("p", { class: "metric-note", text: value == null ? "Kayıt yok" : note }));
  return item;
}

function distributionPanel(title, caption, rows, order, colors, labels, donutChart) {
  const panel = el("section", { class: donutChart ? "panel donut-panel" : "panel" });
  panel.append(panelHead(title, caption));
  if (rows == null) {
    panel.append(el("p", { class: "empty", text: "Bu dağılım bu şemada yok." }));
    return panel;
  }
  if (!rows.length) {
    panel.append(el("p", { class: "empty", text: "Kayıt yok." }));
    return panel;
  }
  const ordered = sortPairs(rows, order);
  if (donutChart) panel.append(donut(ordered, colors, labels));
  else panel.append(bars(ordered, colors, labels));
  return panel;
}

function bars(rows, colors, labels) {
  const max = Math.max(...rows.map((row) => row.count), 1);
  const list = el("div", { class: "bars" });
  for (const row of rows) {
    const line = el("div", { class: "bar-row" });
    const name = el("span", { class: "bar-label", title: labels && labels[row.name] ? labels[row.name] : row.name });
    name.append(el("bdi", { text: row.name }));
    if (labels && labels[row.name]) name.append(el("small", { text: labels[row.name] }));
    line.append(name);
    const track = el("span", { class: "bar-track" });
    const fill = el("span", { class: "bar-fill" });
    fill.style.width = Math.max(2, Math.round((row.count / max) * 100)) + "%";
    fill.style.background = colors[row.name] || "#5B8CFF";
    track.append(fill);
    line.append(track);
    line.append(el("span", { class: "bar-count", text: formatCount(row.count) }));
    list.append(line);
  }
  return list;
}

function donut(rows, colors, labels) {
  const wrap = el("div", { class: "donut-wrap" });
  const total = rows.reduce((sum, row) => sum + row.count, 0);
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 120 120");
  svg.setAttribute("class", "donut");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Dağılım, toplam " + total);
  const radius = 38;
  const circ = 2 * Math.PI * radius;
  const track = document.createElementNS(SVG, "circle");
  track.setAttribute("cx", "60");
  track.setAttribute("cy", "60");
  track.setAttribute("r", String(radius));
  track.setAttribute("fill", "none");
  track.setAttribute("stroke", "#1B3150");
  track.setAttribute("stroke-width", "12");
  svg.append(track);
  const parts = rows.filter((row) => row.count > 0);
  const gap = parts.length > 1 ? 2 : 0;
  let offset = 0;
  for (const row of parts) {
    const length = Math.max(0, (row.count / total) * circ - gap);
    const circle = document.createElementNS(SVG, "circle");
    circle.setAttribute("cx", "60");
    circle.setAttribute("cy", "60");
    circle.setAttribute("r", String(radius));
    circle.setAttribute("fill", "none");
    circle.setAttribute("stroke-width", "12");
    circle.setAttribute("stroke-linecap", "butt");
    circle.setAttribute("stroke", colors[row.name] || "#2583FF");
    circle.setAttribute("stroke-dasharray", length + " " + (circ - length));
    circle.setAttribute("stroke-dashoffset", String(-offset));
    circle.setAttribute("transform", "rotate(-90 60 60)");
    const title = document.createElementNS(SVG, "title");
    title.textContent = (labels && labels[row.name] ? labels[row.name] : row.name) + ": " + row.count;
    circle.append(title);
    svg.append(circle);
    offset += length + gap;
  }
  const caption = document.createElementNS(SVG, "text");
  caption.setAttribute("x", "60");
  caption.setAttribute("y", "56");
  caption.setAttribute("text-anchor", "middle");
  caption.setAttribute("class", "donut-kicker");
  caption.textContent = "Toplam";
  const value = document.createElementNS(SVG, "text");
  value.setAttribute("x", "60");
  value.setAttribute("y", "74");
  value.setAttribute("text-anchor", "middle");
  value.setAttribute("class", "donut-total");
  value.textContent = formatCount(total);
  svg.append(caption, value);
  const stage = el("div", { class: "donut-stage" });
  stage.append(svg);
  wrap.append(stage);
  const legend = el("ul", { class: "legend" });
  for (const row of rows) {
    const item = el("li");
    const swatch = el("span", { class: "swatch" });
    swatch.style.background = colors[row.name] || "#2583FF";
    const coded = CLASS_ORDER.includes(row.name);
    const friendly = labels && labels[row.name] ? labels[row.name] : row.name;
    const name = el("span", { class: "legend-name", title: coded ? friendly : row.name });
    name.append(el("bdi", { text: coded ? row.name : friendly }));
    if (coded && friendly !== row.name) name.append(el("small", { text: friendly }));
    const share = total ? ((row.count / total) * 100).toFixed(1) + "%" : "—";
    item.append(swatch, name, el("b", { text: formatCount(row.count) }), el("span", { class: "legend-share", text: share }));
    legend.append(item);
  }
  wrap.append(legend);
  return wrap;
}

function chartPanel(title, series, color, emptyText) {
  const panel = el("section", { class: "panel" });
  panel.append(panelHead(title, "UTC günü"));
  if (!series || !series.length) {
    panel.append(el("p", { class: "empty", text: emptyText }));
    return panel;
  }
  panel.append(columnChart(series, color));
  return panel;
}

function columnChart(series, color) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 640 196");
  svg.setAttribute("class", "chart");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Günlük sayılar");
  const max = Math.max(...series.map((item) => item.count), 1);
  const width = 640;
  const height = 150;
  const gap = series.length > 20 ? 2 : 8;
  const barW = Math.max(2, (width - gap * (series.length + 1)) / series.length);
  series.forEach((item, index) => {
    const barH = Math.max((item.count / max) * (height - 8), item.count ? 2 : 0);
    const rect = document.createElementNS(SVG, "rect");
    rect.setAttribute("x", String(gap + index * (barW + gap)));
    rect.setAttribute("y", String(height - barH));
    rect.setAttribute("width", String(barW));
    rect.setAttribute("height", String(barH));
    rect.setAttribute("rx", "2");
    rect.setAttribute("fill", color);
    const title = document.createElementNS(SVG, "title");
    title.textContent = item.day + ": " + item.count;
    rect.append(title);
    svg.append(rect);
    if (series.length <= 12) {
      const text = document.createElementNS(SVG, "text");
      text.setAttribute("x", String(gap + index * (barW + gap) + barW / 2));
      text.setAttribute("y", "176");
      text.setAttribute("text-anchor", "middle");
      text.textContent = String(item.day).slice(5);
      svg.append(text);
    }
  });
  return svg;
}

function section(title, rows, columns, emptyText, more, icon) {
  const block = el("section", { class: "section panel" });
  const head = el("div", { class: "section-head" });
  head.append(heading(title, icon || "rows"));
  if (more) {
    const link = el("button", { class: "see-all", type: "button", text: "View all" });
    link.addEventListener("click", () => {
      location.hash = more;
    });
    head.append(link);
  }
  block.append(head);
  block.append(rows && rows.length ? dataTable(columns, rows) : el("p", { class: "empty", text: emptyText }));
  return block;
}

function pagedSection(title, rows, columns, emptyText, pageKey) {
  const list = rows || [];
  const size = 20;
  const pages = Math.max(1, Math.ceil(list.length / size));
  const page = Math.min(state.listPages[pageKey] || 1, pages);
  const slice = list.slice((page - 1) * size, page * size);
  const block = section(title, slice, columns, emptyText);
  if (list.length > size) {
    block.append(pager(page, size, list.length, (next) => {
      state.listPages[pageKey] = next;
      loadCurrent();
    }));
  }
  return block;
}

function dataTable(columns, rows) {
  const wrap = el("div", { class: "table-wrap" });
  const table = el("table");
  const head = el("thead");
  const headRow = el("tr");
  for (const column of columns) {
    headRow.append(el("th", { scope: "col", class: column.align === "end" ? "num" : "", text: column.label }));
  }
  head.append(headRow);
  const body = el("tbody");
  for (const row of rows) {
    const line = el("tr");
    for (const column of columns) {
      const cell = el("td", { class: column.align === "end" ? "num" : "" });
      const content = column.render(row);
      if (content instanceof Node) cell.append(content);
      else cell.textContent = content == null || content === "" ? "—" : String(content);
      line.append(cell);
    }
    body.append(line);
  }
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

function transferColumns() {
  return [
    { label: "Zaman (UTC)", render: (row) => formatUtc(row.time_utc || row.block_time) },
    { label: "Ağ", render: (row) => networkCell(row.network) },
    { label: "İşlem", render: (row) => hashCell(row.tx_hash) },
    { label: "Token", render: (row) => tokenCell(row.token || row.token_symbol) },
    { label: "Tutar", align: "end", render: (row) => amountCell(row.amount) },
    { label: "Sınıflandırma", render: (row) => classificationBadge(row.classification) },
  ];
}

function transferDetailColumns() {
  return transferColumns().concat([
    { label: "Tespit", render: (row) => kindBadge(row.detection_kind) },
    { label: "Skor", align: "end", render: (row) => row.risk_score == null || row.risk_score === "" ? "—" : String(row.risk_score) },
    { label: "Risk", render: (row) => riskBadge(row.risk_level) },
    { label: "Geri bildirim", render: (row) => row.feedback || "—" },
  ]);
}

function detectionColumns() {
  return [
    { label: "Zaman (UTC)", render: (row) => formatUtc(row.time_utc) },
    { label: "Ağ", render: (row) => networkCell(row.network) },
    { label: "İşlem", render: (row) => hashCell(row.tx_hash) },
    { label: "Tespit türü", render: (row) => kindBadge(row.detection_kind) },
    { label: "Risk skoru", align: "end", render: (row) => row.risk_score == null ? "—" : String(row.risk_score) },
    { label: "Risk seviyesi", render: (row) => riskBadge(row.risk_level) },
  ];
}

function poisoningColumns() {
  return detectionColumns().concat([
    { label: "Sinyaller", render: (row) => signalText(row.signals) },
  ]);
}

function signalColumns() {
  return [
    { label: "Zaman", render: (row) => formatUtc(row.time_utc) },
    { label: "Ağ", render: (row) => networkCell(row.network) },
    { label: "İşlem", render: (row) => hashCell(row.tx_hash) },
    { label: "Tespit", render: (row) => kindBadge(row.detection_kind) },
    { label: "Sinyal", render: (row) => signalText(row.signal) },
    { label: "Ağırlık", align: "end", render: (row) => row.weight == null ? "—" : String(row.weight) },
    { label: "Doğrulama", render: (row) => row.verified == null ? "—" : row.verified ? "Doğrulandı" : "Sezgisel" },
    { label: "Kanıt", render: (row) => isolated(row.evidence) },
  ];
}

function rangeBar() {
  const bar = el("div", { class: "toolbar" });
  const presets = el("div", { class: "segment", role: "group", "aria-label": "Hazır aralık" });
  for (const [preset, label] of [["24h", "24 saat"], ["7d", "7 gün"], ["30d", "30 gün"]]) {
    const button = el("button", { type: "button", text: label });
    if (state.range.preset === preset) button.classList.add("is-active");
    button.addEventListener("click", () => {
      state.range = rollingRange(preset);
      state.filters.page = 1;
      state.listPages = { poisoning: 1, approvals: 1, signals: 1 };
      loadCurrent();
    });
    presets.append(button);
  }
  const start = dateField("Başlangıç", "range-start", utcDay(state.range.start));
  const end = dateField("Bitiş", "range-end", inclusiveEndDay(state.range.end));
  const apply = el("button", { class: "button", type: "button", text: "Aralığı uygula" });
  apply.addEventListener("click", () => {
    const startDay = document.getElementById("range-start").value;
    const endDay = document.getElementById("range-end").value;
    if (!startDay || !endDay) return;
    const endDate = new Date(endDay + "T00:00:00Z");
    endDate.setUTCDate(endDate.getUTCDate() + 1);
    state.range = { start: startDay + "T00:00:00+00:00", end: endDate.toISOString(), preset: "custom" };
    state.filters.page = 1;
    state.listPages = { poisoning: 1, approvals: 1, signals: 1 };
    loadCurrent();
  });
  const windowText = el("p", { class: "summary", text: formatUtc(state.range.start) + " → " + formatUtc(state.range.end) + " · bitiş hariç" });
  bar.append(presets, start, end, apply);
  const wrap = el("div", { class: "section panel toolbar-panel" });
  wrap.append(bar, windowText);
  return wrap;
}

function dateField(label, id, value) {
  const field = el("label", { class: "field" });
  field.append(el("span", { text: label }));
  const input = el("input", { id, type: "date", value });
  field.append(input);
  return field;
}

function transferFilters() {
  const bar = el("div", { class: "filters" });
  bar.append(selectField("Ağ", "network", [["", "Tümü"], ["ethereum", "ethereum"], ["tron", "tron"]], state.filters.network));
  bar.append(selectField("Sınıflandırma", "classification", [["", "Tümü"]].concat(CLASS_ORDER.map((name) => [name, name])), state.filters.classification));
  bar.append(selectField("Tespit", "detection", [["", "Tümü"], ["transfer", "Transfer"], ["poisoning", "Adres zehirlenmesi"], ["approval", "Approval"]], state.filters.detection));
  bar.append(selectField("Risk", "risk", [["", "Tümü"]].concat(RISK_ORDER.map((name) => [name, name])), state.filters.risk));
  const search = el("label", { class: "field" });
  search.append(el("span", { text: "Arama" }));
  const input = el("input", { id: "q", type: "search", value: state.filters.q, placeholder: "İşlem, token, sınıf" });
  input.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.filters.q = input.value.trim();
      state.filters.page = 1;
      loadCurrent();
    }, 250);
  });
  search.append(input);
  bar.append(search);
  return bar;
}

function selectField(label, key, options, current) {
  const field = el("label", { class: "field" });
  field.append(el("span", { text: label }));
  const select = el("select");
  for (const [value, text] of options) {
    const option = el("option", { value, text });
    select.append(option);
  }
  select.value = current;
  select.addEventListener("change", () => {
    state.filters[key] = select.value;
    state.filters.page = 1;
    loadCurrent();
  });
  field.append(select);
  return field;
}

function riskFocus() {
  const bar = el("div", { class: "segment", role: "group", "aria-label": "Tespit türü" });
  for (const [value, label] of [["all", "Tümü"], ["poisoning", "Adres zehirlenmesi"], ["approval", "Approval"], ["signals", "Sinyaller"]]) {
    const button = el("button", { type: "button", text: label });
    if (state.riskFocus === value) button.classList.add("is-active");
    button.addEventListener("click", () => {
      state.riskFocus = value;
      state.listPages = { poisoning: 1, approvals: 1, signals: 1 };
      loadCurrent();
    });
    bar.append(button);
  }
  return bar;
}

function feedbackFilter() {
  const field = el("label", { class: "field" });
  field.append(el("span", { text: "Sınıflandırma" }));
  const select = el("select", { "aria-label": "Geri bildirim sınıflandırması" });
  for (const [value, text] of [["", "Tümü"]].concat(CLASS_ORDER.map((name) => [name, name]))) {
    select.append(el("option", { value, text }));
  }
  select.value = state.feedbackClass;
  select.addEventListener("change", () => {
    state.feedbackClass = select.value;
    loadCurrent();
  });
  field.append(select);
  return field;
}

function falseAlarmBlock(text, labeled) {
  const rate = text || "N/A";
  if (WITHHELD.has(rate)) {
    return el("p", { class: "notice", text: "Yanlış alarm oranı: " + rate + ". UNSURE ve etiketsiz tespitler paydaya girmez." });
  }
  const panel = el("section", { class: "panel" });
  panel.append(metric("Yanlış alarm oranı", rate, "Etiketli örnek " + formatCount(labeled)));
  return panel;
}

function pager(page, pageSize, total, onPage) {
  const bar = el("div", { class: "pager" });
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const prev = el("button", { class: "ghost", type: "button", text: "Önceki" });
  const next = el("button", { class: "ghost", type: "button", text: "Sonraki" });
  prev.disabled = page <= 1;
  next.disabled = page >= pages || total === 0;
  prev.addEventListener("click", () => onPage(page - 1));
  next.addEventListener("click", () => onPage(page + 1));
  bar.append(prev, el("span", { text: page + " / " + pages + " · " + formatCount(total) + " kayıt" }), next);
  return bar;
}

function hashCell(hash) {
  if (!hash) return document.createTextNode("—");
  const wrap = el("div", { class: "hash" });
  wrap.append(el("code", { class: "hash-text", title: hash, text: shortHash(hash) }));
  const button = el("button", {
    class: "copy",
    type: "button",
    "data-tip": "Kopyala",
    "aria-label": "İşlem kimliğini kopyala",
  });
  button.append(copyMark());
  button.addEventListener("click", () => copyHash(hash, button));
  wrap.append(button);
  return wrap;
}

function amountCell(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "—";
  return el("span", { class: "amount", title: raw, text: formatAmount(raw) });
}

function formatAmount(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return value;
  const abs = Math.abs(n);
  if (n !== 0 && abs < 0.01) {
    return n.toLocaleString("en-US", {
      useGrouping: false,
      maximumSignificantDigits: 4,
      maximumFractionDigits: 8,
    });
  }
  return n.toLocaleString("en-US", {
    useGrouping: false,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function tokenCell(value) {
  const wrap = el("div", { class: "token-cell" });
  const line = el("span", { class: "token-line" });
  const symbol = tokenSymbol(value);
  line.append(tokenMark(symbol, value));
  line.append(el("bdi", { class: "token-text", text: value || "—" }));
  wrap.append(line);
  return wrap;
}

function tokenSymbol(value) {
  return String(value || "").split(" · ")[0].trim();
}

function networkCell(name) {
  if (!name) return document.createTextNode("—");
  const wrap = el("div", { class: "net-cell" });
  wrap.append(networkMark(name));
  wrap.append(el("span", { text: name === "ethereum" ? "Ethereum" : name === "tron" ? "Tron" : name }));
  return wrap;
}

function isolated(value) {
  if (value == null || value === "") return document.createTextNode("—");
  return el("bdi", { text: String(value) });
}

function classificationBadge(name) {
  if (!name) return document.createTextNode("—");
  return el("span", {
    class: "badge badge-" + (CLASS_TONE[name] || "muted"),
    text: name,
    title: CLASS_LABELS[name] || name,
  });
}

function riskBadge(level) {
  if (!level) return document.createTextNode("—");
  return el("span", { class: "badge badge-" + (RISK_TONE[level] || "muted"), text: RISK_LABELS[level] || level, title: level });
}

function kindBadge(kind) {
  if (!kind) return document.createTextNode("—");
  const tone = kind === "address_poisoning" ? "warn" : kind === "approval" ? "info" : "muted";
  return el("span", { class: "badge badge-" + tone, text: KIND_LABELS[kind] || kind, title: kind });
}

function signalText(value) {
  if (!value) return "—";
  return String(value).split(", ").map((name) => SIGNAL_LABELS[name] || name).join(", ");
}

function systemCard(status) {
  const panel = el("section", { class: "panel system-card" });
  const head = el("div", { class: "panel-head" });
  head.append(heading("Sistem Özeti", "pulse"));
  panel.append(head);
  const list = el("ul", { class: "system-list" });
  const active = Number(status.api_keys_active || 0);
  const total = Number(status.api_keys_total || 0);
  const keyValue = el("span", { class: "system-value" });
  keyValue.append(document.createTextNode(active + " / " + total));
  if (status.api_keys_ready) keyValue.append(el("span", { class: "live-dot", "aria-hidden": "true" }));
  list.append(
    systemRow("cloud", "Aktif API Key", keyValue),
    systemRow("clock", "Son API çağrısı", status.last_api || "—"),
    systemRow("telegram", "Son Telegram gönderimi", status.last_telegram || "—"),
    systemRow("database", "Log dizini boyutu", status.log_size || "—"),
    systemRow("disk", "Boş disk", status.disk_free || "—"),
  );
  panel.append(list);
  return panel;
}

function systemRow(icon, label, value) {
  const row = el("li", { class: "system-row" });
  const mark = el("span", { class: "system-ico", "aria-hidden": "true" });
  mark.append(uiIcon(icon));
  row.append(mark, el("span", { text: label }));
  if (value instanceof Node) row.append(value);
  else row.append(el("span", { class: "system-value", text: String(value) }));
  return row;
}

function panelHead(title, caption) {
  const head = el("div", { class: "panel-head" });
  const titles = el("div", { class: "panel-titles" });
  titles.append(heading(title, "chart"));
  titles.append(el("p", { text: caption }));
  head.append(titles);
  return head;
}

function heading(title, icon) {
  const node = el("h2", { class: "with-icon" });
  const holder = el("span", { class: "panel-icon", "aria-hidden": "true" });
  holder.append(uiIcon(icon || "rows"));
  node.append(holder, document.createTextNode(title));
  return node;
}

function notesBanner(notes) {
  if (!notes || !notes.length) return null;
  const stack = el("div", { class: "section" });
  for (const note of notes) stack.append(el("p", { class: "notice", text: note }));
  return stack;
}

function showSkeleton() {
  const view = document.getElementById("view");
  clear(view);
  for (let index = 0; index < 3; index += 1) view.append(el("div", { class: "skeleton" }));
}

function openSidebar(open) {
  document.getElementById("sidebar").classList.toggle("is-open", open);
  document.getElementById("backdrop").hidden = !open;
  document.getElementById("menu").setAttribute("aria-expanded", open ? "true" : "false");
}

function closeSidebar() {
  openSidebar(false);
}

function sortPairs(rows, order) {
  return rows.slice().sort((left, right) => {
    const a = order.indexOf(left.name);
    const b = order.indexOf(right.name);
    return (a === -1 ? 99 : a) - (b === -1 ? 99 : b);
  });
}

function formatCount(value) {
  if (value == null) return "yok";
  if (typeof value === "string" && Number.isNaN(Number(value))) return value;
  return new Intl.NumberFormat("tr-TR").format(value);
}

function formatUtc(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  const y = date.getUTCFullYear();
  const m = String(date.getUTCMonth() + 1).padStart(2, "0");
  const d = String(date.getUTCDate()).padStart(2, "0");
  const hh = String(date.getUTCHours()).padStart(2, "0");
  const mm = String(date.getUTCMinutes()).padStart(2, "0");
  return y + "-" + m + "-" + d + " " + hh + ":" + mm + " UTC";
}

function utcDay(iso) {
  return formatUtc(iso).slice(0, 10);
}

function inclusiveEndDay(iso) {
  const date = new Date(new Date(iso).getTime() - 1);
  return utcDay(date.toISOString());
}

function shortHash(hash) {
  if (hash.length <= 18) return hash;
  return hash.slice(0, 8) + "…" + hash.slice(-6);
}

function hasLookalike(value) {
  const [symbol, ...rest] = String(value || "").split(" · ");
  return oddCodePoints(symbol).length > 0 || oddCodePoints(rest.join(" · ")).length > 0;
}

function oddCodePoints(value) {
  const notes = [];
  for (const char of String(value || "")) {
    const code = char.codePointAt(0);
    if (code < 32 || code > 126) notes.push("U+" + code.toString(16).toUpperCase().padStart(4, "0"));
  }
  return notes;
}

async function copyHash(hash, button, label) {
  let copied = false;
  try {
    await navigator.clipboard.writeText(hash);
    copied = true;
  } catch {
    const area = document.createElement("textarea");
    area.value = hash;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-999px";
    document.body.append(area);
    area.select();
    copied = document.execCommand("copy");
    area.remove();
  }
  const mark = button.querySelector(".copy-mark");
  button.classList.toggle("is-copied", copied);
  button.setAttribute("data-tip", copied ? "Kopyalandı" : "Kopyalanamadı");
  button.setAttribute("aria-label", copied ? "Kopyalandı" : "Kopyalanamadı");
  if (mark) mark.textContent = copied ? "✅" : "📋";
  window.setTimeout(() => {
    button.classList.remove("is-copied");
    button.setAttribute("data-tip", "Kopyala");
    button.setAttribute("aria-label", label || "İşlem kimliğini kopyala");
    if (mark) mark.textContent = "📋";
  }, 1600);
}

function svgRoot(viewBox, className) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", viewBox);
  svg.setAttribute("aria-hidden", "true");
  if (className) svg.setAttribute("class", className);
  return svg;
}

function shape(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function uiIcon(name) {
  const paths = {
    wallet: ["M3.2 6.8h9.6v6.4H3.2z", "M3.2 6.8 6 4.2h4.2l2.6 2.6", "M9.2 9.6h2.2"],
    swap: ["M3.2 5.4h8.2", "M9.2 3.4 11.6 5.4 9.2 7.4", "M12.8 10.6H4.6", "M6.2 8.6 3.8 10.6 6.2 12.6"],
    clock: ["M8 2.6a5.4 5.4 0 1 0 0 10.8A5.4 5.4 0 0 0 8 2.6Z", "M8 5v3.2l2 1.2"],
    pulse: ["M2 8h2.2l1.6-3.2 2.4 6.4L10 8h4"],
    cloud: ["M5 11.2h6.2a2.4 2.4 0 0 0 .4-4.8 3.2 3.2 0 0 0-6.1-1 2.2 2.2 0 0 0-.5 5.8Z"],
    telegram: ["M3 7.6 13.2 3.2 10.6 13.2 8.2 9.4 3 7.6Z", "M8.2 9.4 13.2 3.2"],
    database: ["M4 4.2c0-1 1.8-1.8 4-1.8s4 .8 4 1.8-1.8 1.8-4 1.8-4-.8-4-1.8Z", "M4 4.2v7.6c0 1 1.8 1.8 4 1.8s4-.8 4-1.8V4.2", "M4 8c0 1 1.8 1.8 4 1.8s4-.8 4-1.8"],
    disk: ["M3.2 4.2h9.6v7.6H3.2z", "M3.2 6.6h9.6", "M5.2 9.4h2.4"],
    calendar: ["M3.2 4.8h9.6v8H3.2z", "M3.2 7.2h9.6", "M5.6 3.2v2.6", "M10.4 3.2v2.6"],
    rows: ["M3 4.2h10", "M3 8h10", "M3 11.8h7"],
    shield: ["M8 1.8 13.2 4v3.6c0 3.2-2.1 5.4-5.2 6.6C4.9 13 2.8 10.8 2.8 7.6V4L8 1.8Z"],
    chart: ["M3.2 12.6V8", "M8 12.6V3.4", "M12.8 12.6V6.2"],
    check: ["M3.2 8.2 6.4 11.2 12.8 4.4"],
    alert: ["M8 2.2 14.2 13H1.8L8 2.2Z", "M8 6.4v3", "M8 11.2h.01"],
    help: ["M8 12.8h.01", "M6.3 6.1a1.8 1.8 0 1 1 2.5 1.7c-.6.3-.8.7-.8 1.4"],
  };
  const svg = svgRoot("0 0 16 16", "icon");
  for (const d of paths[name] || paths.rows) svg.append(shape("path", { d: d }));
  return svg;
}

function tokenMark(symbol, raw) {
  const key = String(symbol || "").toUpperCase();
  if (hasLookalike(raw || symbol)) return fallbackMark("U");
  if (!/^[A-Z0-9]{2,12}$/.test(key)) return fallbackMark("");
  if (key === "USDT") return tetherMark();
  if (key === "USDC") return logoMark("/img/usdc.png");
  if (key === "EURC") return logoMark("/img/eurc.png");
  if (key === "ETH" || key === "WETH") return etherMark();
  if (key === "TRX") return tronMark();
  return fallbackMark(key);
}

function networkMark(name) {
  if (name === "ethereum") return etherMark();
  if (name === "tron") return tronMark();
  return fallbackMark("");
}

function logoMark(src) {
  return el("img", { class: "token-mark", alt: "", src: src });
}

function etherMark() {
  const svg = svgRoot("0 0 32 32", "token-mark");
  svg.append(
    shape("path", { d: "M16 3 7 16.2 16 20.4 25 16.2Z", fill: "#627EEA" }),
    shape("path", { d: "M16 3v17.4l9-4.2Z", fill: "#C1CCF7" }),
    shape("path", { d: "M16 22 7 17.8 16 29.2 25 17.8Z", fill: "#627EEA" }),
    shape("path", { d: "M16 29.2V22l9-4.2Z", fill: "#C1CCF7" }),
  );
  return svg;
}

function tetherMark() {
  const svg = svgRoot("0 0 32 32", "token-mark");
  svg.append(shape("circle", { cx: "16", cy: "16", r: "16", fill: "#26A17B" }));
  svg.append(shape("path", {
    d: "M17.922 17.383v-.002c-.11.008-.677.042-1.942.042-1.01 0-1.721-.03-1.971-.042v.003c-3.888-.171-6.79-.848-6.79-1.658 0-.809 2.902-1.486 6.79-1.66v2.644c.254.018.982.061 1.988.061 1.207 0 1.812-.05 1.925-.06v-2.643c3.88.173 6.775.85 6.775 1.658 0 .81-2.895 1.485-6.775 1.657m0-3.59v-2.366h5.414V8.819H8.595v2.608h5.414v2.365c-4.4.202-7.709 1.074-7.709 2.118 0 1.044 3.309 1.915 7.709 2.118v7.582h3.913v-7.584c4.393-.202 7.694-1.073 7.694-2.116 0-1.043-3.301-1.914-7.694-2.117",
    fill: "#fff",
  }));
  return svg;
}

function tronMark() {
  const svg = svgRoot("0 0 32 32", "token-mark");
  svg.append(shape("path", {
    d: "M16.2 3.2 28 11.4 16.6 28.8 4.2 11.4 16.2 3.2Zm.2 6.2.4 12.8 6.2-8.6-6.6-4.2Zm-1.5.6L8.6 14.2l6.3 8.4V9.9Z",
    fill: "#EF0027",
  }));
  return svg;
}

function fallbackMark(symbol) {
  const svg = svgRoot("0 0 32 32", "token-mark token-fallback");
  svg.append(shape("circle", { cx: "16", cy: "16", r: "14.2", fill: "#0E1C31", stroke: "#1B3150", "stroke-width": "1.5" }));
  const letter = (String(symbol || "").match(/[A-Z0-9]/) || [])[0];
  if (!letter) {
    svg.append(shape("circle", { cx: "16", cy: "16", r: "4", fill: "none", stroke: "#8EA4C2", "stroke-width": "1.6" }));
    return svg;
  }
  const text = document.createElementNS(SVG, "text");
  text.setAttribute("x", "16");
  text.setAttribute("y", "21");
  text.setAttribute("text-anchor", "middle");
  text.setAttribute("class", "token-letter");
  text.textContent = letter;
  svg.append(text);
  return svg;
}

function copyMark() {
  return el("span", { class: "copy-mark", "aria-hidden": "true", text: "📋" });
}

function logNodes(data) {
  return recordNodes(data, "logs");
}

function chatNodes(data) {
  const chats = data.chats || [];
  if (!state.selectedChat || !chats.some((chat) => chat.chat_id === state.selectedChat)) {
    state.selectedChat = data.chat_id || (chats[0] && chats[0].chat_id) || "";
  }
  const layout = el("div", { class: "people" });
  const aside = el("aside", { class: "people-list" });
  const list = el("div", { class: "person-list" });
  if (!chats.length) list.append(el("p", { class: "empty", text: "No messages" }));
  for (const chat of chats) list.append(chatButton(chat));
  aside.append(list);
  const detail = el("section", { class: "person-detail thread-panel", id: "thread" });
  layout.append(aside, detail);
  fillThread(detail, data);
  return [layout];
}

function chatButton(chat) {
  const button = el("button", { type: "button", class: "person", "data-id": chat.chat_id });
  const selected = chat.chat_id === state.selectedChat;
  button.classList.toggle("is-selected", selected);
  button.setAttribute("aria-pressed", selected ? "true" : "false");
  const profile = { name: chat.title, has_photo: false };
  button.append(avatar({ profile: profile, user_id: chat.user_id }, false));
  const copy = el("span", { class: "person-copy" });
  copy.append(el("strong", { text: chat.title || chat.chat_id }));
  const handle = chat.username ? "@" + chat.username : chat.chat_id;
  copy.append(el("small", { text: handle }));
  button.append(copy);
  button.addEventListener("click", () => {
    if (state.selectedChat === chat.chat_id) return;
    state.selectedChat = chat.chat_id;
    loadCurrent();
  });
  return button;
}

function fillThread(detail, data) {
  const messages = data.messages || [];
  if (!messages.length) {
    detail.append(el("p", { class: "empty", text: "No messages" }));
    return;
  }
  const thread = el("div", { class: "thread" });
  for (const message of messages) thread.append(bubble(message));
  detail.append(thread);
  thread.scrollTop = thread.scrollHeight;
}

function bubble(message) {
  const item = el("article", { class: "bubble " + (message.direction === "out" ? "out" : "in") });
  const name = message.sender_name || (message.direction === "out" ? "Bot" : message.user_id || "User");
  item.append(el("p", { class: "bubble-name", text: name }));
  item.append(el("p", { class: "bubble-text" }, [isolated(message.body || "")]));
  item.append(el("time", { class: "bubble-time", text: formatUtc(message.sent_at) }));
  return item;
}

function recordNodes(data, kind) {
  const source = kind === "logs" ? state.ops : state.chat;
  const nodes = [];
  const bar = el("div", { class: "filters" });
  if (kind === "logs") {
    const files = el("div", { class: "segment", role: "group", "aria-label": "Günlük dosyası" });
    const choices = data.files && data.files.length ? data.files : [{ name: state.ops.file, bytes: 0 }];
    for (const file of choices) {
      const button = el("button", { type: "button", text: file.name });
      if (file.name === data.file) button.classList.add("is-active");
      button.addEventListener("click", () => {
        state.ops.file = file.name;
        state.ops.page = 1;
        loadCurrent();
      });
      files.append(button);
    }
    bar.append(files);
  }
  const search = el("label", { class: "field" });
  search.append(el("span", { text: "Arama" }));
  const input = el("input", {
    id: kind === "logs" ? "ops-q" : "chat-q",
    type: "search",
    value: source.q,
    placeholder: kind === "logs" ? "Metin, sınıf, işlem" : "Günlük metni",
  });
  input.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      source.q = input.value.trim();
      source.page = 1;
      loadCurrent();
    }, 250);
  });
  search.append(input);
  bar.append(search);
  nodes.push(bar);
  const list = el("div", { class: "log-list", id: "results" });
  if (!data.entries || !data.entries.length) {
    list.append(el("p", { class: "empty", text: "Bu süzgeçte kayıt yok." }));
  } else {
    for (const entry of data.entries) list.append(logCard(entry));
  }
  nodes.push(list);
  nodes.push(pager(data.page, data.page_size, data.total, (next) => {
    source.page = next;
    loadCurrent();
  }));
  return nodes;
}

function logCard(entry) {
  const card = el("article", { class: "log-card" });
  const head = el("div", { class: "log-head" });
  head.append(el("span", { class: "badge badge-" + levelTone(entry.level), text: entry.level || "KAYIT" }));
  head.append(el("time", { text: entry.time || "—" }));
  if (entry.logger) head.append(el("span", { class: "log-source", text: entry.logger }));
  card.append(head);
  card.append(el("p", { class: "log-message" }, [isolated(entry.message || "—")]));
  const fields = entry.fields || {};
  const keys = Object.keys(fields);
  if (keys.length) {
    const list = el("dl", { class: "log-fields" });
    for (const key of keys) {
      list.append(el("dt", { text: key }));
      list.append(el("dd", {}, [isolated(fields[key])]));
    }
    card.append(list);
  }
  return card;
}

function userNodes(data) {
  const nodes = [];
  const people = data.users || [];
  const legacy = data.legacy || [];
  const directory = el("div", { class: "people", id: "results" });
  const aside = el("aside", { class: "people-list" });
  const search = el("label", { class: "field people-search" });
  search.append(el("span", { text: "Kişi ara" }));
  const input = el("input", { id: "user-q", type: "search", value: state.userQuery, placeholder: "Ad, kimlik veya cüzdan" });
  search.append(input);
  aside.append(search);
  const list = el("div", { class: "person-list" });
  if (!people.length && !legacy.length) {
    list.append(el("p", { class: "empty", text: "Kullanıcı kaydı yok." }));
  }
  for (const user of people) list.append(personButton(user));
  if (legacy.length) list.append(personButton(legacyPerson(legacy)));
  aside.append(list);
  const detail = el("section", { class: "person-detail", id: "person-detail" });
  directory.append(aside, detail);
  nodes.push(directory);
  input.addEventListener("input", () => {
    state.userQuery = input.value.trim();
    applyUserFilter(people, legacy);
  });
  const visible = visiblePeople(people, legacy);
  if (!visible.some((user) => user.user_id === state.selectedUser)) {
    state.selectedUser = visible.length ? visible[0].user_id : "";
  }
  list.querySelectorAll(".person").forEach((item) => {
    const on = !item.hidden && item.getAttribute("data-id") === state.selectedUser;
    item.classList.toggle("is-selected", on);
    item.setAttribute("aria-pressed", on ? "true" : "false");
  });
  fillPerson(detail, visible.find((user) => user.user_id === state.selectedUser) || null);
  return nodes;
}

function legacyPerson(wallets) {
  return {
    user_id: "legacy",
    legacy: true,
    profile: { available: false, name: "Eski izleme kaydı", username: "", bio: "", has_photo: false },
    wallets: wallets,
    feedback: [],
    transfers: [],
    counterparties: [],
  };
}

function personButton(user) {
  const button = el("button", {
    type: "button",
    class: "person",
    "data-user": userSearchText(user),
    "data-id": user.user_id,
  });
  button.append(avatar(user, false));
  const copy = el("span", { class: "person-copy" });
  copy.append(el("strong", { text: personName(user) }));
  const meta = user.legacy
    ? "Telegram kullanıcısı değil"
    : personMeta(user);
  copy.append(el("small", { text: meta }));
  button.append(copy);
  const needle = state.userQuery.toLocaleLowerCase("tr");
  if (needle && !userSearchText(user).toLocaleLowerCase("tr").includes(needle)) button.hidden = true;
  button.addEventListener("click", () => {
    state.selectedUser = user.user_id;
    state.userTab = "wallets";
    document.querySelectorAll(".person").forEach((item) => {
      const on = item.getAttribute("data-id") === user.user_id;
      item.classList.toggle("is-selected", on);
      item.setAttribute("aria-pressed", on ? "true" : "false");
    });
    showPerson(user);
  });
  return button;
}

function applyUserFilter(people, legacy) {
  const needle = state.userQuery.toLocaleLowerCase("tr");
  document.querySelectorAll(".person").forEach((card) => {
    const haystack = card.getAttribute("data-user") || "";
    card.hidden = needle ? !haystack.toLocaleLowerCase("tr").includes(needle) : false;
  });
  const visible = visiblePeople(people, legacy);
  if (visible.length && !visible.some((user) => user.user_id === state.selectedUser)) {
    state.selectedUser = visible[0].user_id;
    state.userTab = "wallets";
    document.querySelectorAll(".person").forEach((item) => {
      const on = item.getAttribute("data-id") === state.selectedUser;
      item.classList.toggle("is-selected", on);
      item.setAttribute("aria-pressed", on ? "true" : "false");
    });
    showPerson(visible[0]);
  }
  if (!visible.length) showPerson(null);
  document.querySelectorAll(".person").forEach((item) => {
    if (!item.hidden && item.getAttribute("data-id") === state.selectedUser) {
      item.classList.add("is-selected");
      item.setAttribute("aria-pressed", "true");
    }
  });
}

function visiblePeople(people, legacy) {
  const needle = state.userQuery.toLocaleLowerCase("tr");
  const rows = people.slice();
  if (legacy.length) rows.push(legacyPerson(legacy));
  return rows.filter((user) => !needle || userSearchText(user).toLocaleLowerCase("tr").includes(needle));
}

function showPerson(user) {
  fillPerson(document.getElementById("person-detail"), user);
}

function fillPerson(detail, user) {
  if (!detail) return;
  clear(detail);
  if (!user) {
    detail.append(el("p", { class: "empty", text: "Bu aramada kişi yok." }));
    return;
  }
  detail.append(personHeader(user));
  if (user.legacy) {
    detail.append(el("p", { class: "summary", text: "Bu cüzdanlar bir Telegram kullanıcısına bağlı değil." }));
    for (const wallet of user.wallets) detail.append(walletBlock(wallet));
    return;
  }
  detail.append(personTabs(user));
}

function personHeader(user) {
  const head = el("div", { class: "person-hero" });
  head.append(avatar(user, true));
  const copy = el("div", { class: "person-copy" });
  copy.append(el("h2", { text: personName(user) }));
  if (!user.legacy && user.profile && user.profile.username) {
    copy.append(el("p", { class: "person-handle", text: "@" + user.profile.username }));
  }
  if (!user.legacy) {
    const idLine = el("p", { class: "person-id" });
    idLine.append(el("span", { text: user.user_id }));
    const button = el("button", {
      class: "copy",
      type: "button",
      "data-tip": "Kopyala",
      "aria-label": "Kullanıcı kimliğini kopyala",
    });
    button.append(copyMark());
    button.addEventListener("click", () => copyHash(user.user_id, button, "Kullanıcı kimliğini kopyala"));
    idLine.append(button);
    copy.append(idLine);
  }
  if (user.profile && user.profile.bio) copy.append(el("p", { class: "person-bio" }, [isolated(user.profile.bio)]));
  head.append(copy);
  return head;
}

function personTabs(user) {
  const wrap = el("div", { class: "person-tabs" });
  const tabs = [
    ["wallets", "Wallets", user.wallets.length],
    ["feedback", "Feedback", user.feedback.length],
    ["transfers", "Transfers", user.transfers.length],
    ["parties", "Counterparties", user.counterparties.length],
  ];
  const bar = el("div", { class: "segment", role: "tablist", "aria-label": "Kullanıcı ayrıntısı" });
  for (const [key, label, count] of tabs) {
    const button = el("button", { type: "button", text: label + " " + count, role: "tab" });
    if (state.userTab === key) button.classList.add("is-active");
    button.addEventListener("click", () => {
      state.userTab = key;
      showPerson(user);
    });
    bar.append(button);
  }
  wrap.append(bar);
  wrap.append(personPanel(user));
  return wrap;
}

function personPanel(user) {
  if (state.userTab === "feedback") {
    return miniTable("Geri bildirim", user.feedback, [
      { label: "Zaman", render: (row) => formatUtc(row.time_utc) },
      { label: "Sınıf", render: (row) => classificationBadge(row.classification) },
      { label: "Geri bildirim", render: (row) => row.feedback || "—" },
      { label: "Cüzdan", render: (row) => isolated(row.wallet_address) },
    ], "Bu kullanıcıya ait geri bildirim yok.");
  }
  if (state.userTab === "transfers") {
    return miniTable("Son transferler", user.transfers, transferColumns().slice(0, 5), "Bu cüzdanlarda kayıtlı transfer yok.");
  }
  if (state.userTab === "parties") {
    return miniTable("Karşı taraflar", user.counterparties, [
      { label: "Ağ", render: (row) => networkCell(row.network) },
      { label: "Karşı taraf", render: (row) => isolated(row.counterparty) },
      { label: "İlk", render: (row) => formatUtc(row.first_seen) },
      { label: "Son", render: (row) => formatUtc(row.last_seen) },
      { label: "Adet", align: "end", render: (row) => formatCount(row.transfer_count) },
    ], "Karşı taraf geçmişi yok.");
  }
  const stack = el("div");
  if (!user.wallets.length) stack.append(el("p", { class: "empty", text: "Bu kullanıcıya bağlı cüzdan yok." }));
  for (const wallet of user.wallets) stack.append(walletBlock(wallet));
  return stack;
}

function personName(user) {
  const profile = user.profile || {};
  return profile.name || "İsim yok";
}

function personMeta(user) {
  const profile = user.profile || {};
  const handle = profile.username ? "@" + profile.username : "kullanıcı adı yok";
  return handle + " · " + user.wallets.length + " cüzdan";
}

function avatar(user, large) {
  const profile = user.profile || {};
  const wrap = el("span", { class: large ? "avatar avatar-lg" : "avatar", "aria-hidden": "true" });
  const letter = el("span", { class: "avatar-letter", text: initials(profile, user.user_id) });
  if (profile.has_photo && !user.legacy) {
    const img = el("img", { class: "avatar-img", alt: "" });
    img.addEventListener("error", () => img.remove());
    img.src = "/api/users/photo?user_id=" + encodeURIComponent(user.user_id);
    wrap.append(img);
  }
  wrap.append(letter);
  return wrap;
}

function initials(profile, userId) {
  const name = profile && profile.name ? profile.name : "";
  const parts = name.split(/\s+/).filter(Boolean).slice(0, 2);
  const letters = parts.map((part) => Array.from(part)[0] || "").join("");
  if (letters) return letters.toLocaleUpperCase("tr");
  const tail = String(userId || "").replace(/\D/g, "").slice(-2);
  return tail || "?";
}

function walletBlock(wallet) {
  const block = el("div", { class: "wallet-block" });
  block.append(el("h3", { text: (wallet.alias || "Adsız") + " · " + (wallet.network || "ağ yok") }));
  const list = el("dl", { class: "kv" });
  const rows = [
    ["Adres", wallet.wallet_address || "—"],
    ["Minimum tutar", wallet.min_amount || "0"],
    ["Yalnızca şüpheli", wallet.suspicious_only ? "açık" : "kapalı"],
    ["Sessiz saat", wallet.quiet_start && wallet.quiet_end ? wallet.quiet_start + "–" + wallet.quiet_end + " UTC" : "yok"],
    ["Haftalık özet", wallet.weekly_report ? "açık" : "kapalı"],
    ["Fee bildirimi", wallet.fee_alerts ? "açık" : "kapalı"],
    ["Ayar kaydı", wallet.settings_saved ? "var" : "varsayılan"],
  ];
  for (const [label, value] of rows) {
    list.append(el("dt", { text: label }));
    list.append(el("dd", {}, [isolated(value)]));
  }
  block.append(list);
  return block;
}

function miniTable(title, rows, columns, emptyText) {
  const block = el("div", { class: "wallet-block" });
  block.append(el("h3", { text: title }));
  block.append(rows && rows.length ? dataTable(columns, rows) : el("p", { class: "empty", text: emptyText }));
  return block;
}

function settingsNodes(data) {
  const nodes = [];
  const list = el("div", { class: "settings-list" });
  for (const group of data.groups || []) {
    const card = el("section", { class: "settings-card" });
    card.append(el("h2", { text: group.title }));
    const fields = el("dl", { class: "kv" });
    for (const row of group.rows || []) {
      fields.append(el("dt", { text: row.label }));
      fields.append(el("dd", {}, [isolated(row.value)]));
    }
    card.append(fields);
    list.append(card);
  }
  if (!data.groups || !data.groups.length) list.append(el("p", { class: "empty", text: "Ayar okunamadı." }));
  nodes.push(list);
  return nodes;
}

function userSearchText(user) {
  const profile = user.profile || {};
  const wallets = (user.wallets || []).map((wallet) => [wallet.wallet_address, wallet.alias, wallet.network].join(" ")).join(" ");
  return [user.user_id, profile.name, profile.username, profile.first_name, profile.last_name, wallets].join(" ");
}

function levelTone(level) {
  const name = String(level || "").toUpperCase();
  if (name === "ERROR" || name === "CRITICAL" || name === "FAILED" || name === "KNOWN_FRAUD") return "bad";
  if (name === "WARNING" || name === "VARIANT") return "warn";
  if (name === "INFO" || name === "PASSED" || name === "CANONICAL") return "good";
  if (name === "UNKNOWN") return "info";
  return "muted";
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "selected") node.selected = Boolean(value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children) if (child) node.append(child);
  return node;
}
