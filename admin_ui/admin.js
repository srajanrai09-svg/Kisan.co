"use strict";
const PAGES = [["dashboard", "Dashboard"], ["users", "Users"], ["products", "Products"], ["orders", "Orders"],
  ["feedback", "Feedback & complaints"], ["images", "Images"], ["categories", "Categories"], ["system", "System & AI"], ["activity", "Activity log"]];
const page = document.body.dataset.page;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const money = v => "₹" + Number(v || 0).toLocaleString("en-IN", {maximumFractionDigits: 2});
const when = iso => iso ? new Date(iso).toLocaleString() : "";
const kb = n => n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";

function say(text, ok) {
  const m = $("msg");
  m.textContent = text;
  m.classList.toggle("ok", !!ok);
  m.classList.remove("hidden");
  if (ok) setTimeout(() => m.classList.add("hidden"), 3500);
}

async function api(path, opts = {}) {
  const o = {method: opts.method || "GET", credentials: "same-origin", headers: {"X-Admin-Panel": "1"}};
  if (opts.body !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(opts.body); }
  const r = await fetch("/admin-api" + path, o);
  if (r.status === 401) { location.href = "/admin/login.html"; throw new Error("Logged out"); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Request failed (" + r.status + ")");
  return d;
}

// run an action, show errors, then re-render the page
async function act(fn, okText) {
  try { await fn(); if (okText) say(okText, true); await RENDER[page](); }
  catch (e) { say(e.message); }
}

$("nav").innerHTML = PAGES.map(([k, t]) => `<a href="/admin/${k}.html"${k === page ? ' class="on"' : ""}>${t}</a>`).join("");
$("logout").addEventListener("click", async () => {
  try { await api("/logout", {method: "POST"}); } catch (e) {}
  location.href = "/admin/login.html";
});

// buttons carry data-act and data-id/data-name; one listener per page handles them
const ACTIONS = {};
document.addEventListener("click", e => {
  const b = e.target.closest("[data-act]");
  if (b && ACTIONS[b.dataset.act]) ACTIONS[b.dataset.act](b.dataset);
});

function filterable(inputId, rowsSel) {
  $(inputId).addEventListener("input", e => {
    const q = e.target.value.toLowerCase();
    document.querySelectorAll(rowsSel).forEach(tr => tr.classList.toggle("hidden", !!q && !tr.textContent.toLowerCase().includes(q)));
  });
}
const tag = (t, cls) => `<span class="tag ${cls || ""}">${esc(t)}</span>`;
const STATUS = {placed: ["Placed", "amber"], accepted: ["Accepted", "amber"], out_for_delivery: ["Out for delivery", "amber"],
  delivered: ["Delivered", "green"], cancelled: ["Cancelled", "red"]};
const statusTag = s => tag(...(STATUS[s] || [s]));
const itemsLine = o => (o.items || []).map(i => `${esc(i.name)} × ${esc(i.qty)} ${esc(i.unit)}`).join("<br>");
const logTable = rows => `<div class="wrap"><table><thead><tr><th>Time</th><th>Action</th><th>Detail</th><th>IP</th></tr></thead><tbody>` +
  rows.map(r => `<tr><td>${when(r.ts)}</td><td>${esc(r.action)}</td><td>${esc(r.detail)}</td><td>${esc(r.ip)}</td></tr>`).join("") +
  `</tbody></table></div>`;

const RENDER = {};

RENDER.feedback = async () => {
  const d = await api("/feedback");
  const box = (kind, x, owner) => `<article class="panel"><strong>${esc(owner)} · ${when(x.created_at)}</strong><p>${kind === "feedback" ? `★ ${x.rating}/5 · Purchase #${x.order_id}<br>` : `${esc(x.subject)}<br>`}${esc(x.comment ?? x.message)}</p>${x.admin_reply ? `<div class="status">Your response: ${esc(x.admin_reply)} · ${when(x.replied_at)}</div>` : `<form class="reply-form" data-kind="${kind}" data-id="${x.id}"><label>Admin response</label><textarea name="reply" required minlength="2" maxlength="2000" rows="3" style="width:100%"></textarea><button class="btn small" type="submit">Send response</button></form>`}</article>`;
  $("view").innerHTML = `<section class="panel"><h2>Feedback summary</h2><button class="btn small" id="feedbackSummary">Summarize feedback</button><p class="muted">Summary uses customer comments currently on file. Read the original feedback before making changes.</p><div id="feedbackSummaryResult"></div></section><h2>Verified purchase feedback</h2>${d.feedback.length ? d.feedback.map(x=>box("feedback",x,`${x.customer_name} → ${x.farm_name || x.farmer_name}`)).join("") : '<p class="muted">No purchase feedback has been submitted.</p>'}<h2>Farmer complaints</h2>${d.complaints.length ? d.complaints.map(x=>box("complaint",x,x.farm_name || x.farmer_name)).join("") : '<p class="muted">No farmer complaints have been submitted.</p>'}`;
  $("feedbackSummary").addEventListener("click", async () => { const b=$("feedbackSummary");b.disabled=true;b.textContent="Summarizing…";try{const s=await api("/feedback/summary",{method:"POST"});$("feedbackSummaryResult").innerHTML=`<p>${esc(s.summary)}</p><p class="muted">${s.rating_count} ratings · ${s.comment_count} comments · 1★ ${s.ratings[1]} · 2★ ${s.ratings[2]} · 3★ ${s.ratings[3]} · 4★ ${s.ratings[4]} · 5★ ${s.ratings[5]} · ${s.mode === "ollama" ? "Local Ollama summary" : "Record-based summary"}</p>`;}catch(e){say(e.message);}finally{b.disabled=false;b.textContent="Summarize feedback";}});
};
document.addEventListener("submit", async e => {
  const form = e.target.closest(".reply-form"); if (!form) return; e.preventDefault();
  const path = form.dataset.kind === "feedback" ? `/feedback/${form.dataset.id}/reply` : `/complaints/${form.dataset.id}/reply`;
  try { await api(path, {method:"POST", body:{reply:new FormData(form).get("reply")}}); say("Response sent.", true); await RENDER.feedback(); }
  catch (err) { say(err.message); }
});

RENDER.dashboard = async () => {
  const d = await api("/stats"), s = d.stats;
  const cards = [["Farmers", s.farmers], ["Customers", s.customers], ["Blocked users", s.blocked], ["Products", s.products],
    ["Orders", s.orders], ["Open orders", s.open_orders], ["Delivered", s.delivered], ["Sales (delivered)", money(s.sales)],
    ["Assistant questions", s.chats]];
  $("view").innerHTML = `<div class="stats">` + cards.map(([k, v]) => `<div><b>${esc(v)}</b><span class="muted">${k}</span></div>`).join("") +
    `</div><h2>Recent admin activity</h2>` + logTable(d.activity);
};

RENDER.users = async () => {
  const r = (await api("/users")).users;
  $("view").innerHTML = `<div class="bar"><input id="q" type="search" placeholder="Search name, phone, area…"></div><div class="wrap"><table><thead>
    <tr><th>ID</th><th>Name</th><th>Role</th><th>Phone</th><th>Area</th><th>Joined</th><th></th></tr></thead><tbody>` +
    r.map(u => `<tr class="row"><td>${u.id}</td><td>${esc(u.farm_name || u.name)} ${u.blocked ? tag("blocked", "red") : ""}</td><td>${esc(u.role)}</td>
      <td>${esc(u.phone)}</td><td>${esc(u.area)} ${esc(u.pincode)}</td><td>${when(u.created_at)}</td>
      <td><button class="btn small ${u.blocked ? "ghost" : "danger"}" data-act="${u.blocked ? "unblock" : "block"}" data-id="${u.id}">${u.blocked ? "Unblock" : "Block"}</button></td></tr>`).join("") +
    `</tbody></table></div>`;
  filterable("q", "tr.row");
};
ACTIONS.block = d => confirm("Block this user? They are logged out and a farmer's products are hidden.") &&
  act(() => api(`/users/${d.id}/block`, {method: "POST"}), "User blocked.");
ACTIONS.unblock = d => act(() => api(`/users/${d.id}/unblock`, {method: "POST"}), "User unblocked. A farmer must switch products back on.");

RENDER.products = async () => {
  const r = (await api("/products")).products;
  $("view").innerHTML = `<div class="bar"><input id="q" type="search" placeholder="Search product, farm, category…"></div><div class="wrap"><table><thead>
    <tr><th></th><th>Product</th><th>Farm</th><th>Price</th><th>Stock</th><th>Status</th><th></th></tr></thead><tbody>` +
    r.map(p => `<tr class="row"><td>${p.image ? `<img class="thumb" loading="lazy" src="/${esc(p.image)}" alt="">` : ""}</td>
      <td>${esc(p.name)}<br><span class="muted">${esc(p.category)}</span></td><td>${esc(p.farm)}<br><span class="muted">${esc(p.farmer_phone)}</span></td>
      <td>${money(p.price)} / ${esc(p.unit)}</td><td>${esc(p.qty)}</td>
      <td>${p.active ? tag("Live", "green") : tag("Hidden", "amber")} ${p.farmer_blocked ? tag("farmer blocked", "red") : ""}</td>
      <td>${p.image ? `<button class="btn small ghost" data-act="verifyimage" data-id="${p.id}">AI image check</button> ` : ""}<button class="btn small danger" data-act="delprod" data-id="${p.id}">Remove</button></td></tr>`).join("") + `</tbody></table></div>`;
  filterable("q", "tr.row");
};
ACTIONS.delprod = d => confirm("Remove this product permanently?") && act(() => api(`/products/${d.id}`, {method: "DELETE"}), "Product removed.");
ACTIONS.verifyimage = async d => {
  try {
    const r = await api(`/products/${d.id}/verify-image`, {method:"POST"});
    const verdict = {consistent:"looks consistent", possible_mismatch:"possible mismatch", uncertain:"uncertain"}[r.verdict] || "uncertain";
    say(`AI suggestion: ${verdict}. ${r.reason} Human review required; no product or account action was taken.`, r.verdict === "consistent");
  } catch (e) { say(e.message); }
};

let orderFilter = "";
RENDER.orders = async () => {
  const r = (await api("/orders" + (orderFilter ? "?status=" + orderFilter : ""))).orders;
  $("view").innerHTML = `<div class="bar"><select id="st"><option value="">All statuses</option>` +
    Object.entries(STATUS).map(([k, v]) => `<option value="${k}"${k === orderFilter ? " selected" : ""}>${v[0]}</option>`).join("") +
    `</select><input id="q" type="search" placeholder="Search bill, customer, farm…"></div><div class="wrap"><table><thead>
    <tr><th>Bill</th><th>Date</th><th>Customer</th><th>Farmer</th><th>Items</th><th>Total</th><th>Status</th><th></th></tr></thead><tbody>` +
    r.map(o => `<tr class="row"><td>${esc(o.bill_no)}</td><td>${when(o.created_at)}</td><td>${esc(o.customer)}<br><span class="muted">${esc(o.customer_phone)}</span></td>
      <td>${esc(o.farmer)}<br><span class="muted">${esc(o.farmer_phone)}</span></td><td>${itemsLine(o)}</td><td>${money(o.total)}</td>
      <td>${statusTag(o.status)}${o.cancelled_by ? `<br><span class="muted">by ${esc(o.cancelled_by)}</span>` : ""}</td>
      <td>${["placed", "accepted", "out_for_delivery"].includes(o.status) ? `<button class="btn small danger" data-act="cancelorder" data-id="${o.id}">Cancel</button>` : ""}</td></tr>`).join("") +
    `</tbody></table></div>`;
  $("st").addEventListener("change", e => { orderFilter = e.target.value; RENDER.orders().catch(x => say(x.message)); });
  filterable("q", "tr.row");
};
ACTIONS.cancelorder = d => confirm("Cancel this order and return the stock?") && act(() => api(`/orders/${d.id}/cancel`, {method: "POST"}), "Order cancelled.");

RENDER.images = async () => {
  const d = await api("/images");
  $("view").innerHTML = `
    <div class="card"><h2>Home page banner</h2>
      <img class="hero" src="/img/hero.jpg?t=${Date.now()}" alt="Current banner"><p class="muted">Current size: ${kb(d.hero_bytes)}. JPEG only, up to 1.5 MB.</p>
      <div class="bar"><input id="heroFile" type="file" accept="image/jpeg"><button class="btn" data-act="hero">Replace banner</button></div></div>
    <div class="card"><h2>Product photos (${d.products.length})</h2><p class="muted">Total stored: ${kb(d.total_bytes)}. Remove a photo that is inappropriate; the product stays and shows its emoji.</p>
      <div class="wrap"><table><thead><tr><th></th><th>Product</th><th>Farm</th><th>Size</th><th></th></tr></thead><tbody>` +
    d.products.map(p => `<tr><td><img class="thumb" loading="lazy" src="/${esc(p.image)}" alt=""></td><td>${esc(p.name)}</td><td>${esc(p.farm)}</td><td>${kb(p.size)}</td>
      <td><button class="btn small danger" data-act="delimg" data-id="${p.id}">Remove photo</button></td></tr>`).join("") +
    `</tbody></table></div></div>
    <div class="card"><h2>Unused files (${d.orphans.length})</h2><p class="muted">Photos on disk that no product uses (left behind by removed products). Files newer than 10 minutes are skipped.</p>` +
    (d.orphans.length ? `<div class="bar"><button class="btn danger" data-act="cleanup">Delete all unused (${kb(d.orphans.reduce((a, o) => a + o.size, 0))})</button></div>
      <div class="wrap"><table><tbody>` + d.orphans.map(o => `<tr><td><img class="thumb" loading="lazy" src="/uploads/${esc(o.name)}" alt=""></td><td>${esc(o.name)}</td><td>${kb(o.size)}</td>
      <td><button class="btn small danger" data-act="delorphan" data-name="${esc(o.name)}">Delete</button></td></tr>`).join("") + `</tbody></table></div>` : `<p>Nothing to clean up.</p>`) + `</div>`;
};
ACTIONS.delimg = d => confirm("Remove this photo?") && act(() => api(`/images/product/${d.id}`, {method: "DELETE"}), "Photo removed.");
ACTIONS.delorphan = d => act(() => api(`/images/orphan/${encodeURIComponent(d.name)}`, {method: "DELETE"}), "File deleted.");
ACTIONS.cleanup = () => confirm("Delete every unused photo file?") && act(async () => { const r = await api("/images/cleanup", {method: "POST"}); say(r.deleted + " files deleted.", true); });
ACTIONS.hero = () => {
  const f = $("heroFile").files[0];
  if (!f) return say("Choose a JPEG image first.");
  const rd = new FileReader();
  rd.onload = () => act(() => api("/images/hero", {method: "POST", body: {image_data: rd.result}}), "Banner replaced.");
  rd.readAsDataURL(f);
};

RENDER.categories = async () => {
  const r = (await api("/categories")).categories;
  $("view").innerHTML = `<div class="card"><h2>Add a category</h2><div class="bar"><input id="cName" placeholder="Name, e.g. Mushrooms" maxlength="30">
    <input id="cEmoji" placeholder="Emoji (optional)" maxlength="8"><button class="btn" data-act="addcat">Add</button></div>
    <p class="muted">New categories appear for farmers and customers right away.</p></div><div class="wrap"><table><thead>
    <tr><th>Emoji</th><th>Category</th><th>Products</th><th></th></tr></thead><tbody>` +
    r.map(c => `<tr><td>${esc(c.emoji) || "—"}</td><td>${esc(c.name)}</td><td>${c.products}</td>
      <td><button class="btn small ghost" data-act="emoji" data-name="${esc(c.name)}">Change emoji</button>
      <button class="btn small danger" data-act="delcat" data-name="${esc(c.name)}">Delete</button></td></tr>`).join("") + `</tbody></table></div>`;
};
ACTIONS.addcat = () => act(() => api("/categories", {method: "POST", body: {name: $("cName").value.trim(), emoji: $("cEmoji").value.trim()}}), "Category added.");
ACTIONS.emoji = d => { const e = prompt("New emoji for " + d.name + ":", ""); if (e !== null) act(() => api("/categories/" + encodeURIComponent(d.name), {method: "PATCH", body: {emoji: e}}), "Emoji updated."); };
ACTIONS.delcat = d => confirm('Delete category "' + d.name + '"?') && act(() => api("/categories/" + encodeURIComponent(d.name), {method: "DELETE"}), "Category deleted.");

RENDER.system = async () => {
  const s = await api("/system"), ix = s.index || {};
  const yn = (v, label) => `<tr><td>${label}</td><td>${v ? tag("on", "green") : tag("off", "amber")}</td></tr>`;
  $("view").innerHTML = `<div class="card"><h2>Services</h2><table><tbody>` +
    yn(s.sqlite, "SQLite database") + yn(s.ollama, "Ollama (AI answers and embeddings)") + yn(s.faiss, "FAISS vector search (else exact NumPy search)") +
    yn(s.vision?.available, `Product photo model (${esc(s.vision?.model || "not configured")})`) +
    yn(s.cache === "redis", "Redis cache (else in-memory)") + yn(s.postgis, "PostGIS (nearby farms)") + yn(s.osrm, "OSRM (road routes)") +
    `</tbody></table></div>
    <div class="card"><h2>Assistant search index (RAG)</h2><p>Documents: <b>${esc(ix.count)}</b> · Embedder: <b>${esc(ix.embedder)}</b> · Built: ${esc(ix.built_at)}</p>
    <p class="muted">The embedder "hash" means Ollama was not running, so a simple built-in matcher is used. It is rebuilt automatically when products change. Product photo comparison uses a separate local vision model and always needs human review.</p>
    <p class="muted">Vision setup: <code>${esc(s.vision?.setup || "Configure VISION_MODEL")}</code></p>
    <button class="btn" data-act="reindex">Rebuild index now</button>
    <h2>Test what the assistant retrieves</h2><div class="bar"><input id="rq" type="search" placeholder="e.g. cheapest tomatoes, organic honey"><button class="btn" data-act="ragtest">Test</button></div><div id="ragOut"></div></div>
    <div class="card"><h2>Test price reference</h2><p class="muted">Reference rows loaded from data/price_reference.csv: ${esc(s.reference_rows)}</p>
    <div class="bar"><input id="pn" placeholder="Product, e.g. Tomato"><input id="pu" value="kg" placeholder="Unit"><input id="pp" type="number" min="0" step="0.01" placeholder="Your price (optional)">
    <button class="btn" data-act="pricetest">Check</button></div><div id="priceOut"></div></div>`;
};
ACTIONS.reindex = () => act(async () => { const r = await api("/rag/reindex", {method: "POST"}); say("Index rebuilt: " + r.index.count + " documents.", true); });
ACTIONS.ragtest = async () => {
  try {
    const r = await api("/rag/test", {method: "POST", body: {question: $("rq").value}});
    $("ragOut").innerHTML = `<div class="wrap"><table><thead><tr><th>Score</th><th>Type</th><th>Source</th><th>Text</th></tr></thead><tbody>` +
      r.results.map(x => `<tr><td>${x.score}</td><td>${esc(x.type)}</td><td>${esc(x.source || "live catalogue")}</td><td>${esc(x.text)}</td></tr>`).join("") + `</tbody></table></div>`;
  } catch (e) { say(e.message); }
};
ACTIONS.pricetest = async () => {
  try {
    const q = new URLSearchParams({name: $("pn").value, unit: $("pu").value || "kg"});
    if ($("pp").value) q.set("price", $("pp").value);
    const r = await api("/price-test?" + q), h = r.headline;
    $("priceOut").innerHTML = `<p><b>${esc(r.advice)}</b></p><p class="muted">Basis: ${esc(h.basis)}` + (h.min != null ? ` · range ${money(h.min)} to ${money(h.max)}` : "") +
      (r.position ? ` · your price is ${esc(r.position)} the range` : "") + ` · similar listings: ${r.market.count}</p>`;
  } catch (e) { say(e.message); }
};

RENDER.activity = async () => { $("view").innerHTML = logTable((await api("/activity")).activity); };

RENDER[page]().catch(e => { if (e.message !== "Logged out") say(e.message); });

