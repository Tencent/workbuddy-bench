// WorkBuddy Bench leaderboard renderer v2 — vanilla JS, no build step.
// vals.ai-style layout (sticky left leaderboard) + WB DS skin.
//   - scaffold tabs (CodeBuddy Code = cbc scores; Claude Code = cc scores)
//   - ranked summary table (models × in-house tracks + Average), sortable
//   - model search, expandable row details (all evaluated models shown inline)
//   - per-track breakdown rendered into #detail (right article column)
// data.js (window.LEADERBOARD_DATA) is the untouched source of truth.

// ---- scaffolds (evaluation harness front-ends) ----
const SCAFFOLDS = [
  { key: "cbc",    label: "CodeBuddy Code", hasData: true },
  { key: "claude", label: "Claude Code",    hasData: true },
];
// Which score map on a track row to read for a given scaffold. The Claude-Code
// (cc) scaffold reads the row's `scores_cc` set on every track; a track with no
// cc data for a given row simply yields an empty map.
function scoreMapFor(scaffold, track, row) {
  if (scaffold === "claude") {
    return row.scores_cc || null;
  }
  return row.scores || null;
}

// ---- public model roster + tracks (single source: roster.js) ----
const PUBLIC_MODELS = (window.WB_ROSTER || []).filter(m => m.home !== false);
const TRACKS = window.WB_TRACKS;

const PLACEHOLDER = "—";

// ---- score helpers ----
// Score strings may carry multiple reasoning-mode runs, e.g.
// "92.51%(think) / 91.05%(nothink)". Policy: think is the default assumption —
// when a think value exists, use it (unmarked); only fall back to nothink when
// no think value is present for that cell.
const SCORE_PART_RE = /(\d+(?:\.\d+)?)\s*%\s*(?:\((think|nothink)\))?/g;
function parseScoreParts(v) {
  if (typeof v !== "string") return [];
  const out = [];
  let m;
  SCORE_PART_RE.lastIndex = 0;
  while ((m = SCORE_PART_RE.exec(v))) out.push({ v: parseFloat(m[1]), mode: m[2] || "" });
  return out;
}
// The value to display/sort/rank on: think value if present, else nothink,
// else the lone unmarked value.
function pctValue(v) {
  const parts = parseScoreParts(v);
  if (!parts.length) return null;
  const think = parts.find(p => p.mode === "think");
  if (think) return think.v;
  const nothink = parts.find(p => p.mode === "nothink");
  if (nothink) return nothink.v;
  return parts[0].v;
}
// Always render exactly one decimal place so e.g. "90.0%" doesn't collapse to
// "90%" next to "67.2%" in the same column (parseFloat drops trailing zeros).
function cleanScore(v) { const n = pctValue(v); return n == null ? null : `${n.toFixed(1)}%`; }
// Column headers are uppercase mono text in a fixed-width cell; long track
// names (e.g. "Security") clip at desktop widths. Abbreviate anything long
// and surface the full name via the header's tooltip instead.
function trackHeaderLabel(t) { return t.short.length > 6 ? t.short.slice(0, 3).toUpperCase() : t.short; }
function isHighConfidence(row) {
  return typeof row.notes === "string" && row.notes.trim().startsWith("HIGH confidence");
}
function primaryRow(bench) {
  if (!bench) return null;
  // Rows are chronological; the last HIGH-confidence row is the current scored snapshot.
  const hi = (bench.rows || []).filter(isHighConfidence);
  return hi.length ? hi[hi.length - 1] : null;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

const VENDOR_COLOR = {
  Anthropic: "#d97757", OpenAI: "#0A0A0A", Zhipu: "#3859FF", DeepSeek: "#4d6bfe",
  Tencent: "#0052d9", Alibaba: "#ff6a00", MiniMax: "#e0405e", Moonshot: "#16191E",
  Hunyuan: "#0055e9", Google: "#4285F4",
};
// Official brand marks — real paths fetched from simple-icons (MIT) / official brand SVGs.
// Rendered white on the vendor-color badge. {d, vb} — vb is the source viewBox.
const VENDOR_ICON = {
  Anthropic: { vb: "0 0 24 24", d: "M17.3041 3.541h-3.6718l6.696 16.918H24Zm-10.6082 0L0 20.459h3.7442l1.3693-3.5527h7.0052l1.3693 3.5528h3.7442L10.5363 3.5409Zm-.3712 10.2232 2.2914-5.9456 2.2914 5.9456Z" },
  DeepSeek: { vb: "0 0 24 24", d: "M23.748 4.651c-.254-.124-.364.113-.512.233-.051.04-.094.09-.137.137-.372.397-.806.657-1.373.626-.829-.046-1.537.214-2.163.848-.133-.782-.575-1.248-1.247-1.548-.352-.155-.708-.311-.955-.65-.172-.24-.219-.509-.305-.774-.055-.16-.11-.323-.293-.35-.2-.031-.278.136-.356.276-.313.572-.434 1.202-.422 1.84.027 1.436.633 2.58 1.838 3.393.137.094.172.187.129.323-.082.28-.18.553-.266.833-.055.179-.137.218-.328.14a5.5 5.5 0 0 1-1.737-1.179c-.857-.828-1.631-1.743-2.597-2.46a12 12 0 0 0-.689-.47c-.985-.957.13-1.743.387-1.836.27-.098.094-.433-.778-.428-.872.003-1.67.295-2.687.685a3 3 0 0 1-.465.136 9.6 9.6 0 0 0-2.883-.101c-1.885.21-3.39 1.1-4.497 2.622C.082 8.776-.231 10.854.152 13.02c.403 2.284 1.568 4.175 3.36 5.653 1.857 1.533 3.997 2.284 6.438 2.14 1.482-.085 3.132-.284 4.994-1.86.47.234.962.328 1.78.398.629.058 1.235-.031 1.705-.129.735-.155.684-.836.418-.961-2.155-1.004-1.682-.595-2.112-.926 1.095-1.295 2.768-3.598 3.284-6.733.05-.346.115-.834.108-1.114-.004-.171.035-.238.23-.257a4.2 4.2 0 0 0 1.545-.475c1.397-.763 1.96-2.016 2.093-3.517.02-.23-.004-.467-.247-.588M11.58 18.168c-2.088-1.642-3.101-2.183-3.52-2.16-.39.024-.32.472-.234.763.09.288.207.487.371.74.114.167.192.416-.113.603-.673.416-1.842-.14-1.897-.168-1.361-.801-2.5-1.86-3.301-3.306-.775-1.393-1.225-2.888-1.299-4.482-.02-.385.094-.522.477-.592a4.7 4.7 0 0 1 1.53-.038c2.131.311 3.946 1.264 5.467 2.774.868.86 1.525 1.887 2.202 2.89.72 1.066 1.494 2.082 2.48 2.915.348.291.626.513.892.677-.802.09-2.14.109-3.055-.615zm1.001-6.44a.306.306 0 0 1 .415-.287.3.3 0 0 1 .113.074.3.3 0 0 1 .086.214c0 .17-.136.307-.308.307a.303.303 0 0 1-.306-.307m3.11 1.596c-.2.081-.4.151-.591.16a1.25 1.25 0 0 1-.798-.254c-.274-.23-.47-.358-.551-.758a1.7 1.7 0 0 1 .015-.588c.07-.327-.007-.537-.238-.727-.188-.156-.426-.199-.689-.199a.6.6 0 0 1-.254-.078.253.253 0 0 1-.114-.358 1 1 0 0 1 .192-.21c.356-.202.767-.136 1.146.016.352.144.618.408 1.001.782.392.451.462.576.685.915.176.264.336.536.446.848.066.194-.02.353-.25.45" },
  Moonshot: { vb: "0 0 24 24", d: "m1.053 16.91 9.538 2.55a21 20.981 0 0 0 .06 2.031l5.956 1.592a12 11.99 0 0 1-15.554-6.172m-1.02-5.79 11.352 3.035a21 20.981 0 0 0-.469 2.01l10.817 2.89a12 11.99 0 0 1-1.845 2.004L.658 15.918a12 11.99 0 0 1-.625-4.796m1.593-5.146L13.573 9.17a21 20.981 0 0 0-1.01 1.874l11.297 3.02a21 20.981 0 0 1-.67 2.362l-11.55-3.087L.125 10.26a12 11.99 0 0 1 1.499-4.285ZM6.067 1.58l11.285 3.016a21 20.981 0 0 0-1.688 1.719l7.824 2.091a21 20.981 0 0 1 .513 2.664L2.107 5.218a12 11.99 0 0 1 3.96-3.638M21.68 4.866 7.222 1.003A12 11.99 0 0 1 21.68 4.866" },
  MiniMax: { vb: "0 0 24 24", d: "M11.43 3.92a.86.86 0 1 0-1.718 0v14.236a1.999 1.999 0 0 1-3.997 0V9.022a.86.86 0 1 0-1.718 0v3.87a1.999 1.999 0 0 1-3.997 0V11.49a.57.57 0 0 1 1.139 0v1.404a.86.86 0 0 0 1.719 0V9.022a1.999 1.999 0 0 1 3.997 0v9.134a.86.86 0 0 0 1.719 0V3.92a1.998 1.998 0 1 1 3.996 0v11.788a.57.57 0 1 1-1.139 0zm10.572 3.105a2 2 0 0 0-1.999 1.997v7.63a.86.86 0 0 1-1.718 0V3.923a1.999 1.999 0 0 0-3.997 0v16.16a.86.86 0 0 1-1.719 0V18.08a.57.57 0 1 0-1.138 0v2a1.998 1.998 0 0 0 3.996 0V3.92a.86.86 0 0 1 1.719 0v12.73a1.999 1.999 0 0 0 3.996 0V9.023a.86.86 0 1 1 1.72 0v6.686a.57.57 0 0 0 1.138 0V9.022a2 2 0 0 0-1.998-1.997" },
  OpenAI: { vb: "0 0 24 24", d: "M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z" },
  Tencent: { vb: "0 0 24 24", d: "M21.395 15.035a40 40 0 0 0-.803-2.264l-1.079-2.695c.001-.032.014-.562.014-.836C19.526 4.632 17.351 0 12 0S4.474 4.632 4.474 9.241c0 .274.013.804.014.836l-1.08 2.695a39 39 0 0 0-.802 2.264c-1.021 3.283-.69 4.643-.438 4.673.54.065 2.103-2.472 2.103-2.472 0 1.469.756 3.387 2.394 4.771-.612.188-1.363.479-1.845.835-.434.32-.379.646-.301.778.343.578 5.883.369 7.482.189 1.6.18 7.14.389 7.483-.189.078-.132.132-.458-.301-.778-.483-.356-1.233-.646-1.846-.836 1.637-1.384 2.393-3.302 2.393-4.771 0 0 1.563 2.537 2.103 2.472.251-.03.581-1.39-.438-4.673" },
  Zhipu: { vb: "0 0 30 30", d: "M15.47 7.1l-1.3 1.85c-0.2 0.29-0.54 0.47-0.9 0.47h-7.1V7.09zM24.3 7.1L13.14 22.91H5.7L16.86 7.1zM14.53 22.91l1.31-1.86c0.2-0.29 0.54-0.47 0.9-0.47h7.09v2.33z" },
  Google: { vb: "0 0 24 24", d: "M12.48 10.92v3.28h7.84c-.24 1.84-.853 3.187-1.787 4.133-1.147 1.147-2.933 2.4-6.053 2.4-4.827 0-8.6-3.893-8.6-8.72s3.773-8.72 8.6-8.72c2.6 0 4.507 1.027 5.907 2.347l2.307-2.307C18.747 1.44 16.133 0 12.48 0 5.867 0 .307 5.387.307 12s5.56 12 12.173 12c3.573 0 6.267-1.173 8.373-3.36 2.16-2.16 2.84-5.213 2.84-7.667 0-.76-.053-1.467-.173-2.053H12.48z" },
  // Hunyuan (腾讯混元) has its own brand mark, distinct from the Tencent QQ
  // penguin — used for models tagged with brand:"Hunyuan" (see roster.js).
  Hunyuan: { vb: "0 0 24 24", d: "M12 0c6.627 0 12 5.373 12 12s-5.373 12-12 12S0 18.627 0 12 5.373 0 12 0zm1.652 1.123l-.01-.001c.533.097 1.023.233 1.41.404 6.084 2.683 7.396 9.214 1.601 14.338a3.781 3.781 0 01-5.337-.328 3.654 3.654 0 01-.884-3.044c-1.934.6-3.295 2.305-3.524 4.45-.204 1.912.324 4.044 2.056 5.634l.245.067C10.1 22.876 11.036 23 12 23c6.075 0 11-4.925 11-11 0-5.513-4.056-10.08-9.348-10.877zM2.748 6.21c-.178.269-.348.536-.51.803l-.235.394.078-.167A10.957 10.957 0 001 12c0 4.919 3.228 9.083 7.682 10.49l.214.065C3.523 18.528 2.84 14.149 6.47 8.68A2.234 2.234 0 102.748 6.21zm10.157-5.172c4.408 1.33 3.61 5.41 2.447 6.924-.86 1.117-2.922 1.46-3.708 2.238-.666.657-1.077 1.462-1.212 2.291A5.303 5.303 0 0112 12.258a5.672 5.672 0 001.404-11.169 10.51 10.51 0 00-.5-.052z" },
};
// brand overrides vendor for icon+color lookup (e.g. HY-3 is vended by
// Tencent but has its own logo/mark, distinct from the Tencent QQ penguin).
function brandKey(m) { return m.brand || m.vendor; }
function orgBadge(m) {
  const k = brandKey(m);
  const c = VENDOR_COLOR[k] || "#9aa1ab";
  const ic = VENDOR_ICON[k];
  const inner = ic
    ? `<svg viewBox="${ic.vb}" width="16" height="16" fill="#fff" aria-hidden="true"><path d="${ic.d}"/></svg>`
    : escapeHtml((m.vendor || "?")[0]);
  return `<span class="org" style="background:${c}" title="${escapeHtml(m.vendor)}">${inner}</span>`;
}
function modelCell(m) {
  const tg = m.type === "open" ? "open" : "prop";
  return `<div class="mc">${orgBadge(m)}<div class="mc-text">` +
    `<div class="mc-top"><span class="m-name">${escapeHtml(m.label)}</span></div>` +
    `<span class="m-meta"><span class="tg ${tg}">${tg}</span><span class="m-vendor">${escapeHtml(m.vendor)}</span></span></div></div>`;
}

// ---- state ----
let BENCH = {}, GENERATED = "", SCAFFOLD = "cbc";
let SUMMARY = null, MAXIMA = null, SORT = { key: "__avg", dir: -1 };
let QUERY = "";
const OPEN_ROWS = new Set(); // expanded model keys

// ---- build summary for a given scaffold ----
function buildSummary(scaffold) {
  const sc = SCAFFOLDS.find(s => s.key === scaffold);
  const cols = TRACKS.map(t => ({ track: t, row: primaryRow(BENCH[t.name]) })).filter(c => c.row);
  const rows = PUBLIC_MODELS.map(m => {
    const live = sc.hasData && m.real; // real data only for models with scores
    const cells = {};
    let sum = 0, n = 0, anyCell = false;
    cols.forEach(c => {
      let val = null, raw = null;
      if (live) {
        const map = scoreMapFor(scaffold, c.track, c.row);
        raw = map ? map[m.key] : null;
        val = cleanScore(raw);
        const pv = pctValue(raw);
        if (pv != null) anyCell = true;
        if (pv != null && !c.track.preview) { sum += pv; n++; }
      }
      cells[c.track.name] = val;
    });
    return { model: m, cells, avg: n ? sum / n : null, cov: n, placeholder: !anyCell };
  })
  // Models with zero scores in the active harness view are dropped from the
  // board entirely (no all-"—" placeholder rows). Partial coverage still
  // shows, with "—" only for the missing tracks.
  .filter(r => !r.placeholder);
  const coreN = cols.filter(c => !c.track.preview).length;
  return { cols, rows, coreN };
}
function columnMaxima(summary) {
  const max = {};
  summary.cols.forEach(c => {
    let mx = -Infinity;
    summary.rows.forEach(r => { const pv = pctValue(r.cells[c.track.name]); if (pv != null && pv > mx) mx = pv; });
    max[c.track.name] = mx;
  });
  let mxAvg = -Infinity, mxAvgKey = null;
  summary.rows.forEach(r => { if (r.avg != null && r.avg > mxAvg) { mxAvg = r.avg; mxAvgKey = r.model.key; } });
  max.__avg = mxAvg; max.__avgKey = mxAvgKey;
  return max;
}

// ---- expanded row detail (per-model history across tracks) ----
function detailRow(r, colSpan) {
  const m = r.model;
  const kv = [
    { k: "Vendor", v: m.vendor },
    { k: "Type", v: m.type === "open" ? "Open-weights" : "Proprietary" },
    { k: "Harnesses", v: m.access },
    { k: "Scaffold", v: SCAFFOLDS.find(s => s.key === SCAFFOLD).label },
  ];
  // Only the current scored snapshot is public (site is de-versioned) — show one
  // row per track for the model's latest score, never the internal dataset_version
  // history rows/labels.
  const hist = TRACKS.map(t => {
    const bench = BENCH[t.name];
    if (!bench) return "";
    const row = primaryRow(bench);
    if (!row) return "";
    const map = scoreMapFor(SCAFFOLD, t, row);
    const raw = map ? map[m.key] : null;
    const clean = cleanScore(raw);
    if (clean == null) return "";
    return `<tr><td class="hv">${escapeHtml(t.short)}</td><td>${escapeHtml(clean)}</td></tr>`;
  }).join("");
  const histHtml = hist
    ? `<div class="xhist"><div class="xh-title"><span class="en">Scores</span><span class="zh">各项成绩</span></div><table><tbody>${hist}</tbody></table></div>`
    : `<div class="xhist"><div class="xh-title"><span class="en">Evaluation pending</span><span class="zh">评测排队中</span></div></div>`;
  return `
    <tr class="xrow" data-xrow="${escapeHtml(m.key)}"><td colspan="${colSpan}">
      <div class="xgrid">${kv.map(e => `<div class="xkv"><div class="k">${escapeHtml(e.k)}</div><div class="v">${escapeHtml(e.v)}</div></div>`).join("")}</div>
      ${histHtml}
    </td></tr>`;
}

// ---- renderers ----
function renderToolbar() {
  return `
    <div class="lb-toolbar">
      <div class="seg lb-scaffold">
        ${SCAFFOLDS.map(s => `
          <button data-scaffold="${s.key}" class="${s.key === SCAFFOLD ? "active" : ""}">${escapeHtml(s.label)}${s.hasData ? "" : `<span class="soon">soon</span>`}</button>`).join("")}
      </div>
      <label class="lb-search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
        <input id="lbSearch" type="search" value="${escapeHtml(QUERY)}" placeholder="Search models…" aria-label="Search models">
      </label>
    </div>`;
}

function renderSummary() {
  const { cols, rows, coreN } = SUMMARY;

  const q = QUERY.trim().toLowerCase();
  const filtered = q
    ? rows.filter(r => (r.model.label + " " + r.model.vendor).toLowerCase().includes(q))
    : rows;

  const sorted = [...filtered].sort((a, b) => {
    // placeholder rows always sink to the bottom
    if (a.placeholder !== b.placeholder) return a.placeholder ? 1 : -1;
    let av, bv;
    if (SORT.key === "__model") {
      av = a.model.label.toLowerCase(); bv = b.model.label.toLowerCase();
      return av < bv ? SORT.dir : av > bv ? -SORT.dir : 0;
    }
    if (SORT.key === "__avg") { av = a.avg; bv = b.avg; }
    else { av = pctValue(a.cells[SORT.key]); bv = pctValue(b.cells[SORT.key]); }
    av = av == null ? -Infinity : av; bv = bv == null ? -Infinity : bv;
    return (av - bv) * SORT.dir;
  });

  // Show every evaluated model inline (no top-N collapse) — the page scrolls
  // naturally instead of the leaderboard living in its own tall inner scroll box.
  const visible = sorted;
  const colSpan = 2 + cols.length; // model + tracks + avg

  const arrow = key => SORT.key === key ? (SORT.dir === -1 ? " ▾" : " ▴") : "";
  const sortedCls = key => SORT.key === key ? " is-sorted" : "";
  const head = `
    <tr>
      <th class="col-model sortable${sortedCls("__model")}" data-sort="__model">Models (${rows.length})${arrow("__model")}</th>
      ${cols.map(c => {
        const label = trackHeaderLabel(c.track);
        const abbreviated = label !== c.track.short;
        const title = abbreviated ? `${c.track.short} — ${c.track.metric}` : c.track.metric;
        return `<th class="c-track sortable${sortedCls(c.track.name)}" data-sort="${escapeHtml(c.track.name)}" title="${escapeHtml(title)}">${escapeHtml(label)}${arrow(c.track.name)}</th>`;
      }).join("")}
      <th class="col-avg sortable${sortedCls("__avg")}" data-sort="__avg">Avg${arrow("__avg")}</th>
    </tr>`;

  const body = visible.map(r => {
    const cells = cols.map(c => {
      const v = r.cells[c.track.name];
      const pv = pctValue(v);
      const best = pv != null && pv === MAXIMA[c.track.name];
      if (v == null) return `<td class="c-track cell-empty">${PLACEHOLDER}</td>`;
      return `<td class="c-track cell-score${best ? " cell-best" : ""}">${escapeHtml(v)}</td>`;
    }).join("");
    const avgBest = r.avg != null && r.avg === MAXIMA.__avg;
    const partial = r.avg != null && r.cov < coreN;
    const avgCell = r.avg == null
      ? `<td class="col-avg"><span class="cell-empty">${PLACEHOLDER}</span></td>`
      : `<td class="col-avg" title="mean of ${r.cov}/${coreN} core tracks${partial ? " (partial coverage)" : ""}"><span class="avgnum${avgBest ? " best" : ""}${partial ? " avg-partial" : ""}">${r.avg.toFixed(1)}%${partial ? "<sup>*</sup>" : ""}</span></td>`;
    const open = OPEN_ROWS.has(r.model.key);
    return `
      <tr class="mrow${r.placeholder ? " row-placeholder" : ""}" data-model="${escapeHtml(r.model.key)}" title="Click for details">
        <td class="col-model">${modelCell(r.model)}</td>
        ${cells}
        ${avgCell}
      </tr>
      ${open ? detailRow(r, colSpan) : ""}`;
  }).join("");

  const noteLines = [
    { en: `Code uses hidden-test scores; Web uses rubric scoring with rule, LLM/VLM and agent judges; Office blends rule checks and an LLM judge with per-task weights.`,
      zh: `Code 采用隐藏测试得分；Web 采用 rule、LLM/VLM 和 agent judge 的 rubric 评分；Office 采用 Rule 检查与 LLM Judge 的按任务加权融合评分。` },
    { en: `Avg is the unweighted mean over evaluated core tracks; * marks partial coverage.`,
      zh: `Avg 为已评测核心赛道的无加权平均；* 表示该模型未覆盖全部核心赛道。` },
    { en: `Security is scored on both harnesses like the other tracks; it stays out of Avg because its scoring instrument differs.`,
      zh: `Security 与其他赛道一样在两个 harness 上评测；因其评分方式不同，不并入 Avg。` },
    { en: `Scores are 3-run averages in think mode.`,
      zh: `当前分数为 3 次运行平均，均为 think 模式。` },
    { en: `Model settings: reasoning effort defaults to high, and each model runs with its provider-default inference hyperparameters.`,
      zh: `模型设置：reasoning effort 默认为 high，各模型使用其默认推理超参。` },
    { en: `Harness settings: the context window is unified at 200k with a unified auto-compaction threshold; the WebSearch and AskUserQuestion tools are disabled.`,
      zh: `harness 设置：上下文窗口统一为 200k，自动压缩阈值统一；禁用 WebSearch 与 AskUserQuestion 工具。` },
    { en: `Claude Opus 4.8's Code score under Claude Code comes from a modified-instruction run: on top of the disabled AskUserQuestion tool, an explicit do-not-ask / complete-in-one-pass instruction was added, so its setup differs slightly from the other runs.`,
      zh: `Claude Opus 4.8 在 Claude Code 下的 Code 分数来自一次调整指令的运行：在禁用 AskUserQuestion 工具的基础上，额外加入了不要询问、一次性完成的指令，因此其设置与其他运行略有不同。` },
    { en: `Evaluation is pinned to specific harness builds — codebuddy-code:2.109.3 and claude-code:2.1.187; metrics may shift as harness versions evolve.`,
      zh: `评测固定在特定 harness 构建上 —— codebuddy-code:2.109.3 与 claude-code:2.1.187；随 harness 版本演进，指标可能变化。` },
    { en: `The HY (Hunyuan) endpoint in this evaluation is served first-party by its provider; the other models are accessed through third-party serving endpoints, whose parameter configuration and request handling may affect metrics.`,
      zh: `本次评测中 HY（混元）端点由其提供方第一方部署；其余模型通过第三方推理端点接入，第三方的参数配置与请求处理可能影响指标。` },
    { en: `This is a research preview leaderboard intended solely for research purposes. The metrics were last updated on July 10, and we will continue to update the leaderboard as models and harnesses evolve.`,
      zh: `本榜单为 research preview，仅供研究用途；指标最后更新于 7 月 10 日，后续将随模型与评测框架的演进持续更新。` },
  ];
  const note = `<ul class="lb-note-list">${noteLines.map(l => `<li class="lb-note-line"><span class="en">${l.en}</span><span class="zh">${l.zh}</span></li>`).join("")}</ul>`;

  return `
    ${renderToolbar()}
    <div class="lb-card">
      <table class="lb-table"><thead>${head}</thead><tbody>${body}</tbody></table>
    </div>
    <div class="lb-note">${note}</div>`;
}

function renderDetails() {
  return TRACKS.map(t => {
    const bench = BENCH[t.name];
    if (!bench) return "";
    const row = primaryRow(bench);            // current scored snapshot (last HIGH-confidence row)
    const map = row ? scoreMapFor(SCAFFOLD, t, row) : null;

    let listHtml;
    if (map) {
      const items = PUBLIC_MODELS.filter(m => m.real)
        .map(m => ({ m, pv: pctValue(map[m.key]), raw: map[m.key] }))
        .filter(x => x.pv != null)
        .sort((a, b) => b.pv - a.pv);
      if (!items.length) return "";
      const mx = items[0].pv;                 // sorted desc → first is the leader
      listHtml = items.map((x, i) => `
        <div class="brk-row${x.pv === mx ? " is-best" : ""}">
          <span class="brk-rank">${i + 1}</span>
          <span class="brk-name" title="${escapeHtml(x.m.vendor)}">${escapeHtml(x.m.label)}</span>
          <span class="brk-bar"><i style="width:${Math.max(2, x.pv).toFixed(1)}%"></i></span>
          <span class="brk-score">${escapeHtml(cleanScore(x.raw))}</span>
        </div>`).join("");
    } else {
      // no data map for this track under the current scaffold (e.g. Claude Code on non-Code tracks)
      listHtml = `<div class="brk-empty"><span class="en">No <b>${escapeHtml(SCAFFOLDS.find(s=>s.key===SCAFFOLD).label)}</b> evaluation for this track yet.</span><span class="zh">该 track 暂无 <b>${escapeHtml(SCAFFOLDS.find(s=>s.key===SCAFFOLD).label)}</b> 评测数据。</span></div>`;
    }

    return `
      <div class="bench-card">
        <div class="bench-head">
          <h3>${escapeHtml(bench.name)}</h3>
          <span class="bd-badge mint">${escapeHtml(t.metric)}</span>
          <span class="bd-badge">${bench.open_source === "开源" ? "public set" : "in-house set"}</span>
        </div>
        <div class="brk-list">${listHtml}</div>
      </div>`;
  }).join("");
}

function repaintSummary() {
  document.getElementById("app").innerHTML = renderSummary();
  wire();
}
function repaint() {
  SUMMARY = buildSummary(SCAFFOLD);
  MAXIMA = columnMaxima(SUMMARY);
  repaintSummary();
  const d = document.getElementById("detail");
  if (d) d.innerHTML = renderDetails();
}

function wire() {
  document.querySelectorAll(".lb-scaffold button[data-scaffold]").forEach(b => {
    b.addEventListener("click", () => {
      SCAFFOLD = b.dataset.scaffold;
      SORT = { key: "__avg", dir: -1 };
      OPEN_ROWS.clear();
      repaint();
    });
  });
  document.querySelectorAll(".lb-table th.sortable").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (SORT.key === key) SORT.dir = -SORT.dir;
      else SORT = { key, dir: key === "__model" ? 1 : -1 };
      repaintSummary();
    });
  });
  document.querySelectorAll(".lb-table tr.mrow").forEach(tr => {
    tr.addEventListener("click", () => {
      const k = tr.dataset.model;
      if (OPEN_ROWS.has(k)) OPEN_ROWS.delete(k); else OPEN_ROWS.add(k);
      repaintSummary();
    });
  });
  const search = document.getElementById("lbSearch");
  if (search) {
    search.addEventListener("input", () => {
      QUERY = search.value;
      repaintSummary();
      const s2 = document.getElementById("lbSearch");
      s2.focus();
      s2.setSelectionRange(s2.value.length, s2.value.length);
    });
  }
}

function render(data) {
  BENCH = {};
  (data.benchmarks || []).forEach(b => { BENCH[b.name] = b; });
  GENERATED = (data._meta && data._meta.generated) || "";
  repaint();
}

// Data is inlined via data.js (window.LEADERBOARD_DATA) — no fetch, works from file://.
if (window.LEADERBOARD_DATA) {
  render(window.LEADERBOARD_DATA);
} else {
  document.getElementById("app").innerHTML =
    `<p style="text-align:center;color:var(--wb-text-tertiary)">Could not find <code>data.js</code> (window.LEADERBOARD_DATA). ` +
    `Make sure <code>data.js</code> is loaded before <code>app_v2.js</code>.</p>`;
}
