// Per-subset leaderboard renderer — used by repo/web/wb/security detail pages.
// SINGLE SOURCE: reads window.LEADERBOARD_DATA (data.js) + window.WB_ROSTER (roster.js),
// so every subset leaderboard stays in lock-step with the homepage board.
// Usage:  <div id="lb-code"></div> ... renderSubsetLB("WorkBuddyBench-Code","lb-code")
// Optional: renderSubsetLB("WorkBuddyBench-Web","lb-web",{scoreKey:"scores_cc"})
// Optional (Code only): renderSubsetLB("WorkBuddyBench-Code","lb-code",{
//   extraScore: { key:"scores_judge", label:{en:"LLM Judge",zh:"LLM Judge"} },
//   dimensionTabs: [{ dim:"role", label:{en:"By role",zh:"按角色"}, values:["developer","algo","pm","qa","ops"] }, ...]
// })
// dimensionTabs re-renders the ranking using `row.code_dims[dim][value]` as the
// score map instead of `row[scoreKey]`; extraScore is hidden in that view since
// judge scores are only computed at the overall level, not per metadata slice.
(function () {
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  // parse "92.51%(think) / 91.05%(nothink)" -> [{v:92.51,mode:"think"},{v:91.05,mode:"nothink"}]
  function parseScores(raw) {
    var out = [], re = /(\d+(?:\.\d+)?)\s*%\s*(?:\((think|nothink)\))?/g, m;
    while ((m = re.exec(raw))) out.push({ v: parseFloat(m[1]), mode: m[2] || "" });
    return out;
  }
  // Policy: think is the default assumption. If a think value exists, show it
  // (unmarked). Otherwise fall back to the nothink value, tagged "nothink".
  function primaryScore(parts) {
    if (!parts.length) return null;
    var think = parts.filter(function (p) { return p.mode === "think"; })[0];
    if (think) return { v: think.v, mode: "" };
    var nothink = parts.filter(function (p) { return p.mode === "nothink"; })[0];
    if (nothink) return { v: nothink.v, mode: "nothink" };
    return { v: parts[0].v, mode: "" };
  }
  function scoreOf(raw) { return primaryScore(parseScores(raw || "")); }
  function rosterMap() { var r = {}; (window.WB_ROSTER || []).forEach(function (m) { r[m.key] = m; }); return r; }
  function primaryRow(bench) {
    // Rows are chronological; the last HIGH-confidence row is the current scored snapshot.
    var hi = (bench.rows || []).filter(function (r) { return typeof r.notes === "string" && r.notes.trim().indexOf("HIGH confidence") === 0; });
    return hi.length ? hi[hi.length - 1] : null;
  }
  function zh() { return document.body.classList.contains("lang-zh"); }
  function L(label) { return zh() ? (label.zh || label.en) : (label.en || label.zh); }
  function bilingual(en, zhLabel) {
    return '<span class="en">' + esc(en) + '</span><span class="zh">' + esc(zhLabel) + '</span>';
  }

  function metricLeaderboard(el, row, R) {
    var defs = [
      { key: "overall", en: "Overall", zh: "总分", scores: row.scores },
      { key: "rule_checks", en: "Rule checks", zh: "Rule 检查", scores: row.score_breakdown.rule_checks },
      { key: "llm_judge", en: "LLM Judge", zh: "LLM Judge", scores: row.score_breakdown.llm_judge }
    ];
    // Display is roster-driven: score-map keys absent from the public roster
    // (retired models) are dropped, never rendered with fallback metadata.
    // A rostered model stays on the board as long as its Overall score exists.
    // A null / unparsable breakdown value (pending refresh) renders as an
    // em-dash "—": no bar, excluded from that tab's ranking order (bottom).
    var items = Object.keys(row.scores).filter(function (k) { return R[k]; }).map(function (k) {
      var metrics = {};
      defs.forEach(function (d) { metrics[d.key] = scoreOf(d.scores[k]); });
      return { m: R[k], metrics: metrics };
    }).filter(function (x) { return x.metrics.overall != null; });
    if (!items.length) { el.innerHTML = ""; return; }
    var active = "overall";

    function metricCells(x) {
      return defs.map(function (d) {
        var ps = x.metrics[d.key];
        return '<span class="wb-lb-metric' + (d.key === active ? " is-active" : "") + '" data-metric="' + d.key + '">' +
          '<small>' + bilingual(d.en, d.zh) + "</small>" +
          '<strong>' + (ps != null ? ps.v.toFixed(1) + "%" : "&mdash;") + "</strong>" +
          "</span>";
      }).join("");
    }

    function drawRows() {
      var sorted = items.slice().sort(function (a, b) {
        var av = a.metrics[active], bv = b.metrics[active];
        if (av == null && bv == null) return b.metrics.overall.v - a.metrics.overall.v;
        if (av == null) return 1;
        if (bv == null) return -1;
        return bv.v - av.v;
      });
      var mx = sorted[0].metrics[active] != null ? sorted[0].metrics[active].v : null;
      var rows = sorted.map(function (x, i) {
        var ps = x.metrics[active];
        var tg = x.m.type === "open" ? "open" : "prop";
        var tgTag = x.m.type ? '<span class="wb-lb-tg ' + tg + '">' + tg + "</span>" : "";
        var vendor = x.m.vendor ? '<span class="wb-lb-vendor">' + esc(x.m.vendor) + "</span>" : "";
        var rank = ps != null ? String(i + 1) : "&mdash;";
        var bar = ps != null ? '<i style="width:' + Math.max(2, ps.v).toFixed(1) + '%"></i>' : "";
        return '<div class="wb-lb-row wb-lb-row--metrics' + (ps != null && ps.v === mx ? " is-top" : "") + '" style="--wb-lb-order:' + i + '">' +
          '<span class="wb-lb-rank">' + rank + "</span>" +
          '<span class="wb-lb-model"><b>' + esc(x.m.label) + "</b>" + vendor + tgTag + "</span>" +
          '<span class="wb-lb-bar" aria-hidden="true">' + bar + "</span>" +
          '<span class="wb-lb-metrics">' + metricCells(x) + "</span>" +
          "</div>";
      }).join("");
      el.querySelector(".wb-lb-rows").innerHTML = rows;
      el.querySelector(".wb-lb--metrics").setAttribute("data-active-metric", active);
    }

    var tabs = defs.map(function (d) {
      return '<button type="button" role="tab" aria-selected="' + (d.key === active ? "true" : "false") + '" data-metric="' + d.key + '">' + bilingual(d.en, d.zh) + "</button>";
    }).join("");
    el.innerHTML = '<div class="wb-lb wb-lb--metrics" data-active-metric="overall">' +
      '<div class="wb-lb-tools">' +
        '<div class="wb-lb-tabs" role="tablist" aria-label="Leaderboard metric / 排行指标" data-active-metric="overall">' + tabs + "</div>" +
        '<p class="wb-lb-explain">' +
          '<span class="en">Overall combines Rule checks and LLM Judge.</span>' +
          '<span class="zh">总分综合 Rule 检查与 LLM Judge。</span>' +
        "</p>" +
      "</div>" +
      '<div class="wb-lb-rows" aria-live="polite"></div>' +
    "</div>";
    drawRows();
    Array.prototype.forEach.call(el.querySelectorAll(".wb-lb-tabs button"), function (button) {
      button.addEventListener("click", function () {
        active = button.getAttribute("data-metric");
        el.querySelector(".wb-lb-tabs").setAttribute("data-active-metric", active);
        Array.prototype.forEach.call(el.querySelectorAll(".wb-lb-tabs button"), function (b) {
          b.setAttribute("aria-selected", b === button ? "true" : "false");
        });
        drawRows();
      });
      button.addEventListener("keydown", function (event) {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        var buttons = Array.prototype.slice.call(el.querySelectorAll(".wb-lb-tabs button"));
        var index = buttons.indexOf(button);
        var next = event.key === "ArrowRight" ? (index + 1) % buttons.length : (index - 1 + buttons.length) % buttons.length;
        buttons[next].focus();
        buttons[next].click();
      });
    });
  }

  window.renderSubsetLB = function (trackName, mountId, opts) {
    opts = opts || {};
    var scoreKey = opts.scoreKey || "scores";
    var extraScore = opts.extraScore || null;
    var dimensionTabs = opts.dimensionTabs || null;
    var el = document.getElementById(mountId);
    if (!el) return;
    var data = window.LEADERBOARD_DATA;
    if (!data) { el.innerHTML = '<p class="wb-lb-empty"><span class="en">Leaderboard data unavailable.</span><span class="zh">排行榜数据不可用。</span></p>'; return; }
    var bench = (data.benchmarks || []).find(function (b) { return b.name === trackName; });
    if (!bench) { el.innerHTML = '<p class="wb-lb-empty"><span class="en">No leaderboard for this subset yet.</span><span class="zh">该子集暂无排行榜。</span></p>'; return; }
    var row = primaryRow(bench);
    if (!row) { el.innerHTML = ""; return; }
    var R = rosterMap();
    if (!dimensionTabs && scoreKey === "scores" && row.score_breakdown && row.score_breakdown.rule_checks && row.score_breakdown.llm_judge) {
      metricLeaderboard(el, row, R);
      return;
    }

    // ---- state: which dimension/value slice is active ("" = overall) ----
    var state = { dim: "", value: "" };

    function scoreMapForState() {
      if (state.dim && row.code_dims && row.code_dims[state.dim]) {
        return row.code_dims[state.dim][state.value] || null;
      }
      return row[scoreKey] || null;
    }

    function renderTabs() {
      if (!dimensionTabs || !dimensionTabs.length) return "";
      var tabs = [{ dim: "", label: { en: "Overall", zh: "总览" } }].concat(dimensionTabs);
      var tabHtml = tabs.map(function (t) {
        var active = t.dim === state.dim;
        return '<button class="' + (active ? "active" : "") + '" data-dim="' + esc(t.dim) + '">' + esc(L(t.label)) + "</button>";
      }).join("");
      var valsHtml = "";
      if (state.dim) {
        var cur = dimensionTabs.filter(function (t) { return t.dim === state.dim; })[0];
        if (cur) {
          valsHtml = '<div class="wb-lb-dimvals">' + cur.values.map(function (v) {
            var active = v === state.value;
            return '<button class="chip' + (active ? " is-active" : "") + '" data-val="' + esc(v) + '">' + esc(v) + "</button>";
          }).join("") + "</div>";
        }
      }
      return '<div class="wb-lb-dimtabs"><div class="seg">' + tabHtml + "</div></div>" + valsHtml;
    }

    function renderRows() {
      var scoreMap = scoreMapForState();
      if (!scoreMap) {
        return '<p class="wb-lb-empty"><span class="en">No scored runs for this view yet.</span><span class="zh">该视图暂无已评分结果。</span></p>';
      }
      var extraMap = (extraScore && !state.dim) ? (row[extraScore.key] || null) : null;
      // Roster-driven display: keys absent from the roster (retired models)
      // are dropped rather than rendered with fallback metadata.
      var items = Object.keys(scoreMap).filter(function (k) { return R[k]; }).map(function (k) {
        var ps = primaryScore(parseScores(scoreMap[k]));
        var extraPs = null;
        if (extraMap && extraMap[k]) extraPs = primaryScore(parseScores(extraMap[k]));
        return { m: R[k], score: ps, pv: ps ? ps.v : null, extra: extraPs };
      }).filter(function (x) { return x.pv != null; }).sort(function (a, b) { return b.pv - a.pv; });
      if (!items.length) return "";
      var mx = items[0].pv;
      var rows = items.map(function (x, i) {
        var tg = x.m.type === "open" ? "open" : "prop";
        var top = x.pv === mx;
        var tgTag = x.m.type ? '<span class="wb-lb-tg ' + tg + '">' + tg + "</span>" : "";
        var vendor = x.m.vendor ? '<span class="wb-lb-vendor">' + esc(x.m.vendor) + "</span>" : "";
        var scores = '<span class="wb-lb-sv"><b>' + x.score.v.toFixed(1) + "%</b></span>";
        if (x.extra) {
          scores += '<span class="wb-lb-sv" title="' + esc(L(extraScore.label)) + '"><b>' + x.extra.v.toFixed(1) + "%</b></span>";
        }
        return '<div class="wb-lb-row' + (top ? " is-top" : "") + '">' +
          '<span class="wb-lb-rank">' + (i + 1) + "</span>" +
          '<span class="wb-lb-model"><b>' + esc(x.m.label) + "</b>" + vendor + tgTag + "</span>" +
          '<span class="wb-lb-bar"><i style="width:' + Math.max(2, x.pv).toFixed(1) + '%"></i></span>' +
          '<span class="wb-lb-score">' + scores + "</span>" +
          "</div>";
      }).join("");
      return '<div class="wb-lb">' + rows + "</div>";
    }

    function repaint() {
      el.innerHTML = renderTabs() + renderRows();
      bindEvents();
    }

    function bindEvents() {
      if (!dimensionTabs || !dimensionTabs.length) return;
      var tabBtns = el.querySelectorAll(".wb-lb-dimtabs .seg button");
      tabBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
          var dim = btn.getAttribute("data-dim");
          if (dim === state.dim) return;
          state.dim = dim;
          if (dim) {
            var cur = dimensionTabs.filter(function (t) { return t.dim === dim; })[0];
            state.value = cur && cur.values.length ? cur.values[0] : "";
          } else {
            state.value = "";
          }
          repaint();
        });
      });
      var valBtns = el.querySelectorAll(".wb-lb-dimvals .chip");
      valBtns.forEach(function (btn) {
        btn.addEventListener("click", function () {
          var val = btn.getAttribute("data-val");
          if (val === state.value) return;
          state.value = val;
          repaint();
        });
      });
    }

    repaint();
    // re-render on language toggle so tab/chip labels flip zh/en without losing state
    document.querySelectorAll("[data-lang-btn]").forEach(function (b) {
      b.addEventListener("click", function () { setTimeout(repaint, 30); });
    });
  };
})();
