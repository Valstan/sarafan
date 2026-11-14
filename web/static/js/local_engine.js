/* Панель движка классификации: кто разбирает посты, чем ответили, сколько стоит.
 *
 * Данные — один запрос /api/local-engine/stats (web/api/local_engine.py),
 * отдаёт modules/classifier/local_engine.stats().
 *
 * Два разных среза, и их нельзя мешать:
 *   • путь доставки (`source` в базе): кто принёс вердикт — волна, демон, рутина;
 *   • движок (`model` в базе): кто ответил — платный Дипсик или бесплатный Опенкод.
 * Одна и та же волна умеет считать и тем, и другим, поэтому «облако» ≠ «Дипсик».
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

function num(v) {
  var n = Number(v || 0);
  return n.toLocaleString("ru-RU");
}

// Название пути доставки — от API (`source_labels`): в базе служебные ключи
// (`routine`/`api`/`headless`/`local`), а читать должен человек.
const SOURCE_FALLBACK = {
  headless: "Волна на сервере",
  local: "Демон Опенкода",
  routine: "Старая рутина (удалена)",
  api: "Приём готовых вердиктов",
};
function sourceLabel(src, labels) {
  return (labels && labels[src]) || SOURCE_FALLBACK[src] || src;
}

// Название модели по-русски — от API (`model_labels`).
const MODEL_FALLBACK = {};
function modelLabel(model, labels) {
  var name = String(model || "");
  if (!name) return "движок не записан";
  return (labels && labels[name]) || MODEL_FALLBACK[name] || name;
}

// Название движка — от API (`engine_labels`).
const ENGINE_FALLBACK = {
  deepseek: "Дипсик — платно, по ключу",
  opencode: "Опенкод — бесплатный тир",
  minimax: "Минимакс — аккаунты Кармана, очки за чекин",
  unknown: "неизвестно",
};
// Короткое имя — для колонок и разбивки: полные подписи туда не влезают.
const ENGINE_SHORT = {
  deepseek: "Дипсик (платно)",
  opencode: "Опенкод (бесплатно)",
  minimax: "Минимакс (очки)",
  unknown: "Неизвестно",
};
function engineLabel(eng, labels) {
  return (labels && labels[eng]) || ENGINE_FALLBACK[eng] || eng;
}
function engineShort(eng) {
  return ENGINE_SHORT[eng] || eng;
}

// Тип вердикта по-русски: action/theme/merge — служебные ключи в базе.
const VERDICT_TYPES = [
  ["action", "действие"],
  ["theme", "тема"],
  ["merge", "слияние"],
];
function verdictTypeLabel(t) {
  for (var i = 0; i < VERDICT_TYPES.length; i++) {
    if (VERDICT_TYPES[i][0] === t) return VERDICT_TYPES[i][1];
  }
  return t;
}

// «12 мин назад» из ISO-времени.
function ago(iso) {
  if (!iso) return "—";
  var ts = Date.parse(iso);
  if (isNaN(ts)) return "—";
  var mins = Math.round((Date.now() - ts) / 60000);
  if (mins < 1) return "только что";
  if (mins < 60) return mins + " мин назад";
  var hours = Math.round(mins / 60);
  if (hours < 24) return hours + " ч назад";
  return Math.round(hours / 24) + " дн назад";
}

async function load() {
  var box = document.getElementById("alert-box");
  try {
    var resp = await fetch("/api/local-engine/stats");
    var d = await resp.json();
    if (!d.ok) throw new Error(d.detail || "ошибка API");

    var labels = d.source_labels || {};
    var engineLabels = d.engine_labels || {};
    var modelLabels = d.model_labels || {};
    var win = d.engine_windows || {};
    var w24 = win["24h"] || {by_engine: {}, total: 0};
    var be = w24.by_engine || {};

    // ── Сейчас ──
    var now = d.engine_now || {};
    document.getElementById("c-engine-now").textContent = now.label || "—";
    document.getElementById("c-v-24h").textContent = num(w24.total || 0);
    // Разбивка — по тем движкам, что реально есть в ответе: новый движок
    // (Минимакс) не должен требовать правки этой панели.
    var splitParts = [];
    Object.keys(be).forEach(function (k) {
      if (!be[k]) return;
      splitParts.push(engineShort(k) + ": " + num(be[k]));
    });
    document.getElementById("c-v-24h-split").textContent = splitParts.join(" · ");

    var daemon = d.daemon || {};
    var d24 = daemon.runs_24h || {};
    document.getElementById("c-daemon").textContent = num(d24.recorded || 0);
    document.getElementById("c-daemon-sub").textContent = daemon.decommissioned
      ? (daemon.note || "контур снят")
      : ("прогонов: " + num(d24.runs || 0) +
         " · последний " + ago(daemon.last_run_ts) +
         (d24.errors ? " · с ошибкой: " + num(d24.errors) : ""));

    document.getElementById("c-last").textContent = ago(d.last_verdict_at);
    document.getElementById("c-last-sub").textContent = d.last_verdict_at || "вердиктов ещё нет";
    // ── Кем отвечено (30 дней) ──
    var models = d.models_30d || [];
    var mh = "";
    if (!models.length) {
      mh = '<span class="text-muted">нет данных</span>';
    } else {
      mh = '<table class="table table-sm mb-0"><thead><tr><th>Чем ответил</th>' +
           '<th>Движок</th><th class="text-end">Вердиктов</th></tr></thead><tbody>';
      for (var i = 0; i < models.length; i++) {
        var m = models[i];
        mh += '<tr><td>' + esc(m.label) + '</td>' +
              '<td class="small text-muted">' + esc(engineLabel(m.engine, engineLabels)) + '</td>' +
              '<td class="text-end">' + num(m.n) + '</td></tr>';
      }
      mh += "</tbody></table>";
    }
    document.getElementById("models-box").innerHTML = mh;

    // ── Кто отвечал по дням ──
    var days = d.engine_by_day || [];
    var dh = "";
    if (!days.length) {
      dh = '<span class="text-muted">нет данных</span>';
    } else {
      // Колонки — по тем движкам, что реально встречаются в ответе.
      var dayKeys = [];
      days.forEach(function (r) {
        Object.keys(r).forEach(function (k) {
          if (k !== "day" && dayKeys.indexOf(k) < 0) dayKeys.push(k);
        });
      });
      dh = '<table class="table table-sm mb-0"><thead><tr><th>День</th>';
      dayKeys.forEach(function (k) {
        dh += '<th class="text-end">' + esc(engineShort(k)) + '</th>';
      });
      dh += "</tr></thead><tbody>";
      for (var j = 0; j < days.length; j++) {
        var r = days[j];
        dh += '<tr><td>' + esc(r.day) + '</td>';
        dayKeys.forEach(function (k) {
          dh += '<td class="text-end">' + num(r[k] || 0) + '</td>';
        });
        dh += "</tr>";
      }
      dh += "</tbody></table>";
    }
    document.getElementById("engine-days-box").innerHTML = dh;

    // ── Пути доставки ──
    var meta = d.source_meta || {};
    var bySource = d.verdicts_by_source || {};
    var bySource24 = d.verdicts_by_source_24h || {};
    var keys = Object.keys(meta);
    var sh = "";
    if (!keys.length) {
      sh = '<span class="text-muted">нет данных</span>';
    } else {
      sh = '<table class="table table-sm mb-0"><thead><tr><th>Путь</th><th>Состояние</th>' +
           '<th class="text-end">За 30 дней</th><th class="text-end">За сутки</th>' +
           '<th>Что это</th></tr></thead><tbody>';
      for (var s = 0; s < keys.length; s++) {
        var key = keys[s];
        var info = meta[key] || {};
        var alive = info.alive !== false;
        sh += '<tr' + (alive ? "" : ' class="text-muted"') + '>' +
              '<td>' + esc(info.label || sourceLabel(key, labels)) + '</td>' +
              '<td>' + (alive
                  ? '<span class="badge text-bg-success">работает</span>'
                  : '<span class="badge text-bg-secondary">не работает</span>') + '</td>' +
              '<td class="text-end">' + num(bySource[key] || 0) + '</td>' +
              '<td class="text-end">' + num(bySource24[key] || 0) + '</td>' +
              '<td class="small">' + esc(info.note || "") + '</td></tr>';
      }
      sh += "</tbody></table>";
    }
    document.getElementById("sources-box").innerHTML = sh;

    // ── Прогоны демона ──
    var tb = document.getElementById("runs-body");
    var runs = d.runs || [];
    if (!runs.length) {
      tb.innerHTML = '<tr><td colspan="9" class="text-muted">прогонов пока нет — демон не запускался</td></tr>';
    } else {
      tb.innerHTML = runs.slice().reverse().map(function (r) {
        return '<tr class="' + (r.ok ? "" : "table-warning") + '">' +
          '<td class="text-nowrap">' + esc((r.ts||"").slice(5,19).replace("T"," ")) + '</td>' +
          '<td>' + esc(modelLabel(r.model || r.stage, modelLabels)) + '</td>' +
          '<td class="text-end">' + (r.posts ?? r.verdicts_sent ?? "—") + '</td>' +
          '<td class="text-end">' + (r.verdicts_sent ?? "—") + '</td>' +
          '<td class="text-end">' + (r.recorded ?? "—") + '</td>' +
          '<td class="text-end">' + (r.skipped_existing ?? "—") + '</td>' +
          '<td class="text-end">' + (r.duration_ms ?? "—") + '</td>' +
          '<td class="small">' + esc((r.regions || []).join(", ")) + '</td>' +
          '<td class="small">' + esc(r.error || "") + '</td>' +
        '</tr>';
      }).join("");
    }

    // ── Качество по пути доставки ──
    var q = d.quality_by_source || {};
    var qkeys = Object.keys(q);
    var qh = "";
    if (!qkeys.length) {
      qh = '<span class="text-muted">правок оператора ещё нет</span>';
    } else {
      qh = '<table class="table table-sm mb-0"><thead><tr><th>Путь</th>' +
           '<th class="text-end">Действие</th><th class="text-end">Тема</th>' +
           '<th class="text-end">Слияние</th>' +
           '<th class="text-end">Правок</th></tr></thead><tbody>';
      for (var t = 0; t < qkeys.length; t++) {
        var src = qkeys[t];
        var a = q[src].action || {}, th = q[src].theme || {}, mg = q[src].merge || {};
        var rights = (a.agree||0) + (a.correct||0) + (th.agree||0) + (th.correct||0) +
                     (mg.agree||0) + (mg.correct||0);
        qh += '<tr><td>' + esc(sourceLabel(src, labels)) + '</td>' +
              '<td class="text-end">' + pct(a.agree_pct) + ' <span class="text-muted small">(' +
                (a.agree||0) + '/' + ((a.agree||0)+(a.correct||0)) + ')</span></td>' +
              '<td class="text-end">' + pct(th.agree_pct) + ' <span class="text-muted small">(' +
                (th.agree||0) + '/' + ((th.agree||0)+(th.correct||0)) + ')</span></td>' +
              '<td class="text-end">' + pct(mg.agree_pct) + ' <span class="text-muted small">(' +
                (mg.agree||0) + '/' + ((mg.agree||0)+(mg.correct||0)) + ')</span></td>' +
              '<td class="text-end">' + num(rights) + '</td></tr>';
      }
      qh += "</tbody></table>";
    }
    document.getElementById("quality-box").innerHTML = qh;

    // ── Недельная динамика ──
    var w = d.weekly_quality || {};
    var weeks = Object.keys(w).sort();
    var wh = "";
    if (!weeks.length) {
      wh = '<span class="text-muted">недостаточно данных</span>';
    } else {
      wh = '<table class="table table-sm mb-0"><thead><tr><th>Неделя</th>';
      var srcs = new Set();
      weeks.forEach(function (wk) { Object.keys(w[wk]).forEach(function (s) { srcs.add(s); }); });
      srcs.forEach(function (s) {
        wh += '<th class="text-end">' + esc(sourceLabel(s, labels)) + '</th>';
      });
      wh += "</tr></thead><tbody>";
      weeks.slice().reverse().forEach(function (wk) {
        wh += '<tr><td>' + esc(wk) + '</td>';
        srcs.forEach(function (s) {
          var v = (w[wk][s] || {});
          var tot = (v.agree||0) + (v.correct||0);
          var p = tot ? (100*v.agree/tot).toFixed(1) + "%" : "—";
          wh += '<td class="text-end">' + p + ' <span class="text-muted small">(' +
                (v.agree||0) + '/' + tot + ')</span></td>';
        });
        wh += "</tr>";
      });
      wh += "</tbody></table>";
    }
    document.getElementById("weekly-box").innerHTML = wh;

    // ── Обучение ──
    var rs = d.rules_by_status || {};
    document.getElementById("rules-box").innerHTML =
      '<table class="table table-sm mb-0"><tbody>' +
      [["proposed","предложено"],["approved","утверждено"],["rejected","отклонено"],["retired","выведено"]]
        .map(function (pair) {
          return '<tr><td>' + pair[1] + '</td><td class="text-end"><b>' + num(rs[pair[0]] || 0) +
                 '</b></td></tr>';
        }).join("") +
      "</tbody></table>";

    var cw = d.corrections_by_week || [];
    document.getElementById("corr-box").innerHTML = cw.length
      ? '<table class="table table-sm mb-0"><thead><tr><th>Неделя</th><th class="text-end">Правок</th></tr></thead><tbody>' +
        cw.slice().reverse().map(function (r) {
          return '<tr><td>' + esc(r.week) + '</td><td class="text-end">' + num(r.n) + '</td></tr>';
        }).join("") +
        "</tbody></table>"
      : '<span class="text-muted">правок пока нет</span>';

    box.innerHTML = "";
  } catch (e) {
    box.innerHTML = '<div class="alert alert-warning py-2">Не удалось загрузить сводку: ' + esc(e.message) + "</div>";
  }
}

document.getElementById("btn-refresh").addEventListener("click", load);
load();
