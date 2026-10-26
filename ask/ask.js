(() => {
  const params = new URLSearchParams(location.search);
  const API = (params.get("api") || window.KF_API || "").replace(/\/$/, "");
  const SAMPLES = [
    "My TPU VM has no external IP address. How does it reach a package index on the internet?",
    "My TPU VMs use Cloud NAT and run out of ports. How do I raise the minimum ports per VM?",
    "What is the default TCP TIME_WAIT timeout for a new Cloud NAT gateway?",
    "Which software version do I pass to create a v6e TPU VM?",
    "Can I create TPU7x capacity with the Cloud TPU API?",
  ];
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const md = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");
  const setName = { tpu: "Cloud TPU · DITA", nat: "Cloud NAT · MyST" };
  const edgeName = { entry: "entry topic", about: "shared subject", related: "reltable row", "links-to": "link",
    contains: "map hierarchy", reuses: "conref", states: "key" };

  // samples
  SAMPLES.forEach((q, i) => {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = q; if (i === 0) b.className = "primary";
    b.onclick = () => { $("q").value = q; ask(q); };
    $("samples").appendChild(b);
  });
  const qp = params.get("q");
  $("q").value = qp || SAMPLES[0];
  $("q").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("ask").requestSubmit(); } });
  $("ask").addEventListener("submit", (e) => { e.preventDefault(); ask($("q").value); });
  $("loadgaps").onclick = loadGaps;
  if (qp) ask(qp);

  let busy = false;
  async function ask(q) {
    q = (q || "").trim();
    if (q.length < 3 || busy) return;
    busy = true; $("go").disabled = true;
    const st = $("status"); st.className = "status";
    st.innerHTML = '<span class="spinner"></span>Finding entry topics, walking the graph, composing and checking each step…';
    const slow = setTimeout(() => { st.innerHTML += " The service may be starting up; the first question can take about 20 seconds."; }, 6000);
    try {
      const r = await fetch(API + "/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }) });
      if (!r.ok) throw new Error(r.status === 429 ? "Too many questions this minute. Try again shortly." : `The service answered ${r.status}.`);
      const data = await r.json();
      render(data);
      st.textContent = "";
      const u = new URL(location.href); u.searchParams.set("q", q); history.replaceState(null, "", u);
    } catch (e) {
      st.className = "status err";
      st.textContent = e instanceof TypeError ? "The service couldn't be reached. Try again in a moment." : e.message;
    } finally { clearTimeout(slow); busy = false; $("go").disabled = false; }
  }

  function render(d) {
    $("result").hidden = false;
    $("summary").innerHTML = md(d.summary || (d.steps.length ? "" : "No step passed the checks, so there is no answer to show."));
    $("steps").innerHTML = d.steps.map(stepHtml).join("");
    const nc = $("notcovered");
    nc.hidden = !d.not_covered; nc.innerHTML = d.not_covered ? "<b>Not in these topics:</b> " + md(d.not_covered) : "";
    const dr = $("dropped");
    dr.hidden = !d.dropped.length;
    dr.innerHTML = d.dropped.length ? `<b>${d.dropped.length} step${d.dropped.length > 1 ? "s" : ""} dropped.</b> They failed the checks after ${Math.max(...d.dropped.map((x) => x.revisions))} revision(s), so they aren't shown as part of the answer and each is recorded as a gap.` +
      d.dropped.map((x) => `<p><s>${md(x.text)}</s><br><span class="mono">${esc(x.topic)}</span> — ${esc(x.reason)}</p>`).join("") : "";
    $("graph").innerHTML = graphHtml(d);
    $("gaps").innerHTML = d.gaps.length ? d.gaps.map(gapHtml).join("") : '<p class="empty">No gaps: every step passed and every join is an authored link.</p>';
    const m = d.model || {};
    $("meta").textContent = `model ${m.name}${m.id && m.id !== "none" ? " (" + m.id + ")" : ""}${m.error ? " · model error, fell back to quoting: " + m.error : ""} · ${d.ms} ms · corpus ${String(d.corpus_commit || "").slice(0, 7)} · BM25 hits ${d.path.hits.slice(0, 4).map((h) => h[0] + " " + h[1]).join(", ")}`;
    $("result").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function stepHtml(s) {
    const t = s.topic, r = s.reached;
    const via = r.type === "entry" ? `<span class="chip entry">entry topic · BM25</span>`
      : `<span class="chip ${r.type === "about" ? "about" : "edge"}">${esc(edgeName[r.type] || r.type)}${r.type === "about" ? ": " + esc(r.detail.replace("shared subject: ", "")) : ""}</span>` +
        r.edges.map((e) => `<a href="${esc(e.link)}" target="_blank" rel="noopener" class="mono">${esc(e.file)}:${e.line}</a>`).join(" ");
    const checks = s.checks.map((c) => `<span title="${esc(c.detail)}">${esc(c.name)}</span>`).join("") +
      (s.revisions ? `<span class="rev" title="The first draft failed a check">passed after ${s.revisions} revision${s.revisions > 1 ? "s" : ""}</span>` : "");
    return `<li class="step"><p class="t">${md(s.text)}</p>
      <blockquote>“${esc(s.quote)}”</blockquote>
      <div class="cite"><span class="chip ${t.set}">${esc(setName[t.set] || t.set)}</span>
        <a href="${esc(t.source)}" target="_blank" rel="noopener">${esc(t.title)}${t.section_title ? " › " + esc(t.section_title) : ""}</a>
        ${t.url ? `<a href="${esc(t.url)}" target="_blank" rel="noopener" title="The published page this topic was adapted from">published page ↗</a>` : ""}</div>
      <div class="cite" style="margin-top:6px"><span class="lbl">Reached by</span> ${via}</div>
      <div class="checks">${checks}</div></li>`;
  }

  function gapHtml(g) {
    const kind = g.kind === "missing-link" ? "Missing link" : "Dropped step";
    const body = g.kind === "missing-link"
      ? `<p>${esc(g.reason)} (joined only by the subject <b>${esc(g.subject)}</b>).</p>`
      : `<p><span class="mono">${esc(g.topic)}</span>: ${esc(g.reason)}</p>${g.text ? `<p><s>${md(g.text)}</s></p>` : ""}`;
    const cnt = g.count ? `<p class="cnt">Seen ${g.count} time${g.count > 1 ? "s" : ""}${g.last_seen ? " · last " + esc(g.last_seen.slice(0, 16).replace("T", " ")) + " UTC" : ""}</p>` : "";
    return `<div class="gap ${esc(g.kind)}"><span class="k">${kind}</span>${body}<p class="act">${esc(g.action || "")}</p>${cnt}</div>`;
  }

  async function loadGaps() {
    const box = $("allgaps");
    box.innerHTML = '<p class="empty"><span class="spinner"></span>Loading…</p>';
    try {
      const r = await fetch(API + "/api/gaps");
      const d = await r.json();
      box.innerHTML = d.gaps.length ? d.gaps.map(gapHtml).join("") : '<p class="empty">No gaps recorded yet.</p>';
      $("loadgaps").textContent = "Refresh";
    } catch { box.innerHTML = '<p class="empty">The service couldn\'t be reached.</p>'; }
  }

  // Vertical path graph: nodes in reading order; edges drawn as arcs on the left, labelled with their type.
  function graphHtml(d) {
    const g = d.graph; if (!g.nodes.length) return '<p class="empty">No path.</p>';
    const byId = Object.fromEntries(g.nodes.map((n) => [n.id, n]));
    const order = [];
    const cited = new Set(d.steps.map((s) => s.topic.id));
    d.path.topics.forEach((tid) => {
      const viaEdge = g.edges.find((e) => (e.src === tid || e.dst === tid) && byId[e.src === tid ? e.dst : e.src]?.kind === "subject" && !order.includes(e.src === tid ? e.dst : e.src));
      if (viaEdge && order.length) order.push(viaEdge.src === tid ? viaEdge.dst : viaEdge.src);
      order.push(tid);
    });
    g.nodes.forEach((n) => { if (!order.includes(n.id)) order.push(n.id); });
    const rowH = 58, x0 = 168, w = 420, top = 12;
    const y = Object.fromEntries(order.map((id, i) => [id, top + i * rowH + 20]));
    const H = top + order.length * rowH;
    const col = (t) => t === "about" ? "var(--amber)" : "var(--blue)";
    let edges = "";
    g.edges.forEach((e) => {
      const ya = y[e.src], yb = y[e.dst]; if (ya == null || yb == null) return;
      const span = Math.abs(order.indexOf(e.src) - order.indexOf(e.dst));
      const dx = 22 + span * 26;
      const p = `M${x0},${ya} C${x0 - dx},${ya} ${x0 - dx},${yb} ${x0},${yb}`;
      const ly = (ya + yb) / 2 + 4, lx = x0 - dx * 0.75 - 6;
      edges += `<a href="${esc(e.link)}" target="_blank" rel="noopener" class="e"><title>${esc(e.type)} · ${esc(e.where)}</title>
        <path d="${p}" stroke="${col(e.type)}" ${e.type === "about" ? 'stroke-dasharray="4 3"' : ""}/>
        <text x="${lx}" y="${ly}" text-anchor="end" fill="${col(e.type)}">${esc(e.type)}</text></a>`;
    });
    const nodes = order.map((id) => {
      const n = byId[id]; const yy = y[id];
      if (n.kind === "subject") {
        return `<g class="node"><rect x="${x0}" y="${yy - 14}" width="${w * 0.7}" height="28" rx="14" fill="var(--amber-bg)" stroke="var(--amber-border)"/>
          <text x="${x0 + 14}" y="${yy + 4}">subject: ${esc(n.label)}</text></g>`;
      }
      const c = n.set === "nat" ? "teal" : "purple";
      const label = n.label.length > 52 ? n.label.slice(0, 50) + "…" : n.label;
      return `<g class="node"><rect x="${x0}" y="${yy - 18}" width="${w}" height="36" rx="6" fill="var(--${c}-bg)" stroke="var(--${c}-border)" ${cited.has(id) ? 'stroke-width="2"' : ""}/>
        <text x="${x0 + 12}" y="${yy + 4}">${esc(label)}</text>
        <text class="set-l" x="${x0 + w - 10}" y="${yy + 4}" text-anchor="end">${n.set === "nat" ? "NAT · MyST" : "TPU · DITA"}</text></g>`;
    }).join("");
    const list = g.edges.map((e) => `<div><span class="chip ${e.type === "about" ? "about" : "edge"}">${esc(e.type)}</span> <a class="mono" href="${esc(e.link)}" target="_blank" rel="noopener">${esc(e.where)}</a></div>`).join("");
    return `<svg viewBox="0 0 ${x0 + w + 6} ${H}" role="img" aria-label="Cited path graph">${edges}${nodes}</svg>
      <div class="cite" style="flex-direction:column;align-items:flex-start;margin-top:10px">${list}</div>`;
  }
})();
