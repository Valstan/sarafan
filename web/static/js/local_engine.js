/* Панель локального движка: прогоны демона, качество по источникам, обучение.
 *
 * Данные — один запрос /api/local-engine/stats (web/api/local_engine.py),
 * отдаёт modules/classifier/local_engine.stats().
 *
 * Файл отдельный, а не inline в шаблоне: так его проверяет node --check
 * (тест в tests/test_classifier/test_local_engine.py) и так он кэшируется
 * по версии статики.
 */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function pct(v) { return (v == null) ? "?" : (v + "%"); }

// Название источника вердикта — от API (``source_labels``): в базе служебные
// ключи (``routine``/``api``/``headless``/``local``), а читать должен человек.
const SOURCE_FALLBACK = {
  routine: "рутина",
  api: "через API",
  headless: "облако",
  local: "локальный",
};
function sourceLabel(src, labels) {
  return (labels && labels[src]) || SOURCE_FALLBACK[src] || src;
}

async function load() {
  const box = document.getElementById("alert-box");
  try {
    const resp = await fetch("/api/local-engine/stats");
    const d = await resp.json();
    if (!d.ok) throw new Error(d.detail || "ошибка API");

    // ── Движок ──
    document.getElementById("c-runs").textContent = d.runs_total ?? 0;
    document.getElementById("c-posts").textContent = d.runs_posts ?? 0;
    document.getElementById("c-recorded").textContent = d.runs_recorded ?? 0;
    document.getElementById("c-errors").textContent = d.runs_errors ?? 0;

    const tb = document.getElementById("runs-body");
    const runs = d.runs || [];
    if (!runs.length) {
      tb.innerHTML = '<tr><td colspan="9" class="text-muted">прогонов пока нет — демон не запускался</td></tr>';
    } else {
      tb.innerHTML = runs.slice().reverse().map(r => `
        <tr class="${r.ok ? "" : "table-warning"}">
          <td class="text-nowrap">${esc((r.ts||"").slice(5,19).replace("T"," "))}</td>
          <td>${esc(r.model || r.stage || "—")}</td>
          <td class="text-end">${r.posts ?? r.verdicts_sent ?? "—"}</td>
          <td class="text-end">${r.verdicts_sent ?? "—"}</td>
          <td class="text-end">${r.recorded ?? "—"}</td>
          <td class="text-end">${r.skipped_existing ?? "—"}</td>
          <td class="text-end">${r.duration_ms ?? "—"}</td>
          <td class="small">${esc((r.regions || []).join(", "))}</td>
          <td class="small">${esc(r.error || "")}</td>
        </tr>`).join("");
    }

    // ── Качество по источнику ──
    const labels = d.source_labels || {};
    const q = d.quality_by_source || {};
    const keys = Object.keys(q);
    let html = "";
    if (!keys.length) {
      html = '<span class="text-muted">правок оператора ещё нет</span>';
    } else {
      html = '<table class="table table-sm mb-0"><thead><tr><th>источник</th>' +
             '<th class="text-end">action</th><th class="text-end">theme</th>' +
             '<th class="text-end">правок</th></tr></thead><tbody>';
      for (const src of keys) {
        const a = q[src].action || {}, t = q[src].theme || {};
        const rights = (a.agree||0) + (a.correct||0) + (t.agree||0) + (t.correct||0);
        html += `<tr><td>${esc(sourceLabel(src, labels))}</td>` +
                `<td class="text-end">${pct(a.agree_pct)} <span class="text-muted small">(${a.agree||0}/${(a.agree||0)+(a.correct||0)})</span></td>` +
                `<td class="text-end">${pct(t.agree_pct)} <span class="text-muted small">(${t.agree||0}/${(t.agree||0)+(t.correct||0)})</span></td>` +
                `<td class="text-end">${rights}</td></tr>`;
      }
      html += "</tbody></table>";
    }
    document.getElementById("quality-box").innerHTML = html;

    // ── Недельная динамика ──
    const w = d.weekly_quality || {};
    const weeks = Object.keys(w).sort();
    let wh = "";
    if (!weeks.length) {
      wh = '<span class="text-muted">недостаточно данных</span>';
    } else {
      wh = '<table class="table table-sm mb-0"><thead><tr><th>неделя</th>';
      const srcs = new Set();
      weeks.forEach(wk => Object.keys(w[wk]).forEach(s => srcs.add(s)));
      for (const s of srcs) wh += `<th class="text-end">${esc(sourceLabel(s, labels))}</th>`;
      wh += "</tr></thead><tbody>";
      for (const wk of weeks) {
        wh += `<tr><td>${esc(wk)}</td>`;
        for (const s of srcs) {
          const v = (w[wk][s] || {});
          const tot = (v.agree||0) + (v.correct||0);
          const p = tot ? (100*v.agree/tot).toFixed(1) + "%" : "—";
          wh += `<td class="text-end">${p} <span class="text-muted small">(${v.agree||0}/${tot})</span></td>`;
        }
        wh += "</tr>";
      }
      wh += "</tbody></table>";
    }
    document.getElementById("weekly-box").innerHTML = wh;

    // ── Обучение ──
    const rs = d.rules_by_status || {};
    document.getElementById("rules-box").innerHTML =
      '<table class="table table-sm mb-0"><tbody>' +
      [["proposed","предложено"],["approved","утверждено"],["rejected","отклонено"],["retired","выведено"]]
        .map(([k,label]) => `<tr><td>${label}</td><td class="text-end"><b>${rs[k] ?? 0}</b></td></tr>`).join("") +
      "</tbody></table>";

    const cw = d.corrections_by_week || [];
    document.getElementById("corr-box").innerHTML = cw.length
      ? '<table class="table table-sm mb-0"><thead><tr><th>неделя</th><th class="text-end">коррекций</th></tr></thead><tbody>' +
        cw.slice().reverse().map(r => `<tr><td>${esc(r.week)}</td><td class="text-end">${r.n}</td></tr>`).join("") +
        "</tbody></table>"
      : '<span class="text-muted">коррекций пока нет</span>';

    // ── Вердикты по дням ──
    const daily = d.daily_by_source || {};
    const dsrc = Object.keys(daily);
    let dh = "";
    if (!dsrc.length) {
      dh = '<span class="text-muted">нет данных</span>';
    } else {
      dh = '<table class="table table-sm mb-0"><thead><tr><th>день</th>';
      dsrc.forEach(s => dh += `<th class="text-end">${esc(sourceLabel(s, labels))}</th>`);
      dh += "</tr></thead><tbody>";
      const days = new Set();
      dsrc.forEach(s => (daily[s]||[]).forEach(r => days.add(r.day)));
      [...days].sort().reverse().slice(0, 30).forEach(day => {
        dh += `<tr><td>${esc(day)}</td>`;
        dsrc.forEach(s => {
          const row = (daily[s]||[]).find(r => r.day === day);
          dh += `<td class="text-end">${row ? row.n : "—"}</td>`;
        });
        dh += "</tr>";
      });
      dh += "</tbody></table>";
    }
    document.getElementById("daily-box").innerHTML = dh;

    box.innerHTML = "";
  } catch (e) {
    box.innerHTML = '<div class="alert alert-warning py-2">Не удалось загрузить сводку: ' + esc(e.message) + "</div>";
  }
}

document.getElementById("btn-refresh").addEventListener("click", load);
load();
