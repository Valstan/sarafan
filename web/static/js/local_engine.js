/* Панель движка классификации: кто разбирает посты, чем ответили, сколько стоит,
 * что ушло в ленты и на сайты, какое качество и работает ли обучение.
 *
 * Данные — один запрос /api/local-engine/stats (web/api/local_engine.py):
 *   • modules/classifier/local_engine.stats()  — движок, качество, обучение;
 *   • modules/classifier/engine_panel.stats() — волны, публикации, сайты,
 *     источники и живой журнал вызовов Минимакса.
 *
 * Два разных среза, и их нельзя мешать:
 *   • путь доставки (`source` в базе): кто принёс вердикт — волна, демон, рутина;
 *   • движок (`model` в базе): кто ответил — Дипсик, Минимакс или Опенкод.
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
  local: "Демон Опенкода (снят 14.11)",
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

// Дата-время из ISO — коротко, без «T».
function dtShort(iso) {
  if (!iso) return "—";
  var s = String(iso);
  return s.slice(5, 16).replace("T", " ");
}

function tbl(head, body, cls) {
  return '<table class="table table-sm mb-0 ' + (cls || "") + '"><thead><tr>' + head +
         '</tr></thead><tbody>' + body + '</tbody></table>';
}

function td(v, cls) {
  return '<td' + (cls ? ' class="' + cls + '"' : "") + '>' + v + '</td>';
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

    var mm = d.minimax || {};
    var m24 = mm.summary_24h || {};
    document.getElementById("c-minimax").textContent = num(m24.calls || 0) + " вызовов";
    document.getElementById("c-minimax-sub").textContent = m24.calls
      ? ("успешных " + num(m24.ok || 0) + " (" + pct(m24.ok_pct) + ")" +
         " · отказов " + num(m24.failed || 0) +
         " · токенов " + num(m24.tokens || 0))
      : "движок ещё не звали";

    document.getElementById("c-last").textContent = ago(d.last_verdict_at);
    document.getElementById("c-last-sub").textContent = d.last_verdict_at || "вердиктов ещё нет";

    // ── Кем отвечено (30 дней) ──
    var models = d.models_30d || [];
    var total30 = 0;
    models.forEach(function (m) { total30 += Number(m.n || 0); });
    var mh = "";
    if (!models.length) {
      mh = '<span class="text-muted">нет данных</span>';
    } else {
      var mbody = "";
      for (var i = 0; i < models.length; i++) {
        var m = models[i];
        var share = total30 ? (100 * m.n / total30).toFixed(1) + "%" : "—";
        mbody += "<tr>" +
          td(esc(m.label)) +
          td(esc(engineLabel(m.engine, engineLabels)), "small text-muted") +
          td(num(m.n), "text-end") +
          td(share, "text-end") +
          "</tr>";
      }
      mh = tbl("<th>Чем ответил</th><th>Движок</th>" +
               '<th class="text-end">Вердиктов</th><th class="text-end">Доля</th>', mbody);
    }
    document.getElementById("models-box").innerHTML = mh;

    // ── Кто отвечал по дням: 10 последних + спойлер ──
    var days = d.engine_by_day || [];
    var dh = "";
    if (!days.length) {
      dh = '<span class="text-muted">нет данных</span>';
    } else {
      var dayKeys = [];
      days.forEach(function (r) {
        Object.keys(r).forEach(function (k) {
          if (k !== "day" && dayKeys.indexOf(k) < 0) dayKeys.push(k);
        });
      });
      var head = "<th>День</th>";
      dayKeys.forEach(function (k) {
        head += '<th class="text-end">' + esc(engineShort(k)) + "</th>";
      });
      function dayRows(list) {
        var out = "";
        for (var j = 0; j < list.length; j++) {
          var r = list[j];
          out += "<tr>" + td(esc(r.day));
          dayKeys.forEach(function (k) {
            out += td(num(r[k] || 0), "text-end");
          });
          out += "</tr>";
        }
        return out;
      }
      var recent = days.slice(0, 10);
      var older = days.slice(10);
      dh = tbl(head, dayRows(recent));
      if (older.length) {
        dh += "<details class='mt-2'><summary class='small text-muted'>" +
              "Ещё " + older.length + " дн. (" + esc(older[older.length - 1].day) +
              " — " + esc(older[0].day) + ")</summary>" +
              tbl(head, dayRows(older)) + "</details>";
      }
    }
    document.getElementById("engine-days-box").innerHTML = dh;

    // ── Пути доставки ──
    var meta = d.source_meta || {};
    var bySource = d.verdicts_by_source || {};
    var bySource24 = d.verdicts_by_source_24h || {};
    var engBySource = d.engine_by_source || {};
    var keys = Object.keys(meta);
    var aliveKeys = keys.filter(function (k) { return (meta[k] || {}).alive !== false; });
    var deadKeys = keys.filter(function (k) { return (meta[k] || {}).alive === false; });
    var sh = "";
    if (!aliveKeys.length) {
      sh = '<span class="text-muted">нет данных</span>';
    } else {
      var sbody = "";
      for (var s = 0; s < aliveKeys.length; s++) {
        var key = aliveKeys[s];
        var info = meta[key] || {};
        var engs = engBySource[key] || {};
        var engTxt = Object.keys(engs)
          .filter(function (k) { return engs[k]; })
          .sort(function (a, b) { return engs[b] - engs[a]; })
          .map(function (k) { return engineShort(k) + " " + num(engs[k]); })
          .join(" · ");
        sbody += "<tr>" +
          td(esc(info.label || sourceLabel(key, labels))) +
          td('<span class="badge text-bg-success">работает</span>') +
          td(num(bySource[key] || 0), "text-end") +
          td(num(bySource24[key] || 0), "text-end") +
          td(esc(engTxt || "—"), "small") +
          td(esc(info.note || ""), "small text-muted") +
          "</tr>";
      }
      sh = tbl("<th>Путь</th><th>Состояние</th><th class='text-end'>За 30 дней</th>" +
               "<th class='text-end'>За сутки</th><th>Чем отвечает</th><th>Что это</th>", sbody);
      if (deadKeys.length) {
        var deadTxt = deadKeys.map(function (k) {
          return esc(sourceLabel(k, labels)) + " — " + num(bySource[k] || 0) + " вердиктов в корпусе (контур снят)";
        }).join(" · ");
        sh += '<div class="small text-muted mt-2">Снятые контуры: ' + deadTxt + "</div>";
      }
    }
    document.getElementById("sources-box").innerHTML = sh;

    // ── Минимакс: последние прогоны ──
    var mb = document.getElementById("minimax-runs-body");
    var runs = mm.runs || [];
    if (!runs.length) {
      mb.innerHTML = '<tr><td colspan="7" class="text-muted">движок ещё не звали — журнала нет</td></tr>';
    } else {
      mb.innerHTML = runs.map(function (r) {
        var why = r.ok ? "—" : esc(r.reason || "без причины");
        return '<tr' + (r.ok ? "" : ' class="table-warning"') + ">" +
          td(esc(dtShort(r.ts)), "text-nowrap") +
          td(r.ok ? '<span class="badge text-bg-success">ок</span>'
                  : '<span class="badge text-bg-danger">отказ</span>') +
          td(why, "small") +
          td(num(r.tokens), "text-end") +
          td(num(r.input), "text-end") +
          td(num(r.output), "text-end") +
          td(num(r.cache), "text-end") +
          "</tr>";
      }).join("");
    }

    // ── Качество по движку ──
    var q = (d.quality_by_engine || {}).by_engine || {};
    var qkeys = Object.keys(q);
    var qh = "";
    if (!qkeys.length) {
      qh = '<span class="text-muted">правок оператора ещё нет</span>';
    } else {
      var qbody = "";
      for (var t = 0; t < qkeys.length; t++) {
        var eng = qkeys[t];
        var v = q[eng] || {};
        qbody += "<tr>" +
          td(esc(engineLabel(eng, engineLabels))) +
          td(num(v.agree || 0), "text-end") +
          td(num(v.correct || 0), "text-end") +
          td(num(v.total || 0), "text-end") +
          td(pct(v.agree_pct), "text-end") +
          "</tr>";
      }
      qh = tbl("<th>Движок</th><th class='text-end'>Согласено</th>" +
               "<th class='text-end'>Исправлено</th><th class='text-end'>Всего правок</th>" +
               "<th class='text-end'>Согласие</th>", qbody);
      var lastC = (d.quality_by_engine || {}).last_correction_at;
      var stale = false;
      if (lastC) {
        var ageDays = (Date.now() - Date.parse(lastC)) / 86400000;
        stale = ageDays > 7;
      }
      qh += '<div class="small text-muted mt-2">Последняя правка оператора: ' +
            (lastC ? esc(dtShort(lastC)) + " (" + ago(lastC) + ")" : "правок не было") +
            (stale
              ? ". <b>Поток правок остановился</b> — пока оператор не правит, доля согласия стоит на месте: "
                + "растёт она только от новых правок, а не от времени."
              : ". Пока оператор правит, метрика живёт.") +
            "</div>";
    }
    document.getElementById("quality-box").innerHTML = qh;

    // ── Недельная динамика по движкам ──
    var w = d.weekly_quality_by_engine || [];
    var wh = "";
    if (!w.length) {
      wh = '<span class="text-muted">недостаточно данных</span>';
    } else {
      var engs2 = new Set();
      w.forEach(function (wk) {
        Object.keys(wk).forEach(function (k) { if (k !== "week") engs2.add(k); });
      });
      var head2 = "<th>Неделя</th>";
      engs2.forEach(function (s2) {
        head2 += '<th class="text-end">' + esc(engineShort(s2)) + "</th>";
      });
      function weekRows(list) {
        var out = "";
        for (var wi = list.length - 1; wi >= 0; wi--) {
          var wk = list[wi];
          out += "<tr>" + td(esc(wk.week));
          engs2.forEach(function (s2) {
            var v2 = (wk[s2] || {});
            var tot2 = (v2.agree || 0) + (v2.correct || 0);
            var p2 = tot2 ? (100 * (v2.agree || 0) / tot2).toFixed(1) + "%" : "—";
            out += td(p2 + ' <span class="text-muted small">(' + (v2.agree || 0) + "/" + tot2 + ")</span>",
                      "text-end");
          });
          out += "</tr>";
        }
        return out;
      }
      var wRecent = w.slice(-12);
      var wOlder = w.slice(0, Math.max(0, w.length - 12));
      wh = tbl(head2, weekRows(wRecent));
      if (wOlder.length) {
        wh += "<details class='mt-2'><summary class='small text-muted'>Ещё " +
              wOlder.length + " нед.</summary>" + tbl(head2, weekRows(wOlder)) + "</details>";
      }
    }
    document.getElementById("weekly-box").innerHTML = wh;

    // ── Обучение: воронка ──
    var rl = d.rules || {};
    var rs = rl.by_status || {};
    var rb = rl.by_source || {};
    var corr = d.corrections_by_week || [];
    var corrTotal = corr.reduce(function (a, r) { return a + Number(r.n || 0); }, 0);
    var lastCorr = (d.quality_by_engine || {}).last_correction_at;
    var rh = "";
    var steps = [
      ["Правок оператора", corrTotal, lastCorr ? dtShort(lastCorr) + " (" + ago(lastCorr) + ")" : "правок не было"],
      ["Предложено правил (черновики)", Number(rs.proposed || 0), "ждут утверждения оператора"],
      ["Утверждено", Number(rs.approved || 0), "работают со следующей волны"],
      ["Отклонено", Number(rs.rejected || 0), "не подошли"],
      ["Выведено из работы", Number(rs.retired || 0), "заменили новым"],
    ];
    var rbody = "";
    steps.forEach(function (st) {
      rbody += "<tr>" +
        td(esc(st[0])) +
        td(num(st[1]), "text-end") +
        td(esc(st[2]), "small text-muted") +
        "</tr>";
    });
    rh = tbl("<th>Шаг</th><th class='text-end'>Сколько</th><th>Когда / что это</th>", rbody);
    rh += '<div class="small text-muted mt-2">Источники правил: ' +
          Object.keys(rb).map(function (k) {
            return esc(k === "operator" ? "оператор" : (k === "routine" ? "рутина" : k)) +
                   " — " + num(rb[k]);
          }).join(" · ") +
          ". Правило действует, если его <code>last_effective_at</code> свежий: последнее вступило в силу " +
          (rl.last_effective_at ? esc(dtShort(rl.last_effective_at)) : "—") + ".</div>";
    document.getElementById("rules-box").innerHTML = rh;

    // ── Обучение: последние правила ──
    var latest = rl.latest || [];
    var lb = "";
    if (!latest.length) {
      lb = '<span class="text-muted">правил пока нет</span>';
    } else {
      var lbody = "";
      for (var li = 0; li < latest.length; li++) {
        var r2 = latest[li];
        var badge = r2.status === "approved" ? "text-bg-success"
                  : (r2.status === "proposed" ? "text-bg-warning" : "text-bg-secondary");
        lbody += "<tr>" +
          td('<span class="badge ' + badge + '">' + esc(r2.status) + "</span>") +
          td(esc(r2.text), "small") +
          td(esc(r2.source === "operator" ? "оператор" : (r2.source === "routine" ? "рутина" : r2.source)), "small") +
          td(esc(dtShort(r2.created_at)), "small text-nowrap") +
          td(esc(dtShort(r2.last_effective_at)), "small text-nowrap") +
          "</tr>";
      }
      lb = tbl("<th>Статус</th><th>Правило</th><th>Кто</th><th>Создано</th><th>В силе</th>", lbody);
    }
    document.getElementById("rules-list-box").innerHTML = lb;

    // ── Волны ──
    var wv = d.waves || {};
    var wvb = "";
    if (!wv.runs) {
      wvb = '<span class="text-muted">нет данных</span>';
    } else {
      var wvbody = "";
      [["Прогонов", wv.runs], ["С публикацией", wv.published],
       ["Пропущено (нечего публиковать)", wv.skipped], ["Упало с ошибкой", wv.failed],
       ["Постов просмотрено", wv.scanned], ["Постов дошло до публикации", wv.final]
      ].forEach(function (pair) {
        wvb += "<tr>" + td(esc(pair[0])) + td(num(pair[1]), "text-end") + "</tr>";
      });
      wvb = tbl("<th>Показатель (7 дней)</th><th class='text-end'>Значение</th>", wvb);
      if ((wv.errors || []).length) {
        var errBody = "";
        wv.errors.forEach(function (e) {
          errBody += "<tr>" + td(esc(e.reason || "(пусто)"), "small") + td(num(e.n), "text-end") + "</tr>";
        });
        wvb += '<div class="small text-muted mt-2 mb-1">Почему прогоны падали:</div>' +
               tbl("<th>Ошибка</th><th class='text-end'>Сколько</th>", errBody);
      }
    }
    document.getElementById("waves-box").innerHTML = wvb;

    // ── Волны по дням и по темам ──
    var wbd = d.waves_by_day || [];
    var wbdb = "";
    if (!wbd.length) {
      wbdb = '<span class="text-muted">нет данных</span>';
    } else {
      var dayBody = "";
      for (var di = wbd.length - 1; di >= 0; di--) {
        var dr = wbd[di];
        dayBody += "<tr>" +
          td(esc(dr.day)) +
          td(num(dr.runs), "text-end") +
          td(num(dr.published), "text-end") +
          td(num(dr.failed), "text-end") +
          "</tr>";
      }
      wbdb = tbl("<th>День</th><th class='text-end'>Прогонов</th>" +
                 "<th class='text-end'>С публикацией</th><th class='text-end'>Упало</th>", dayBody);
      var themes = wv.by_theme || [];
      if (themes.length) {
        var thBody = "";
        themes.slice(0, 10).forEach(function (t) {
          thBody += "<tr>" +
            td(esc(t.theme)) +
            td(num(t.runs), "text-end") +
            td(num(t.published), "text-end") +
            td(num(t.scanned), "text-end") +
            "</tr>";
        });
        wbdb += '<div class="small text-muted mt-2 mb-1">По темам:</div>' +
                tbl("<th>Тема</th><th class='text-end'>Прогонов</th>" +
                    "<th class='text-end'>С публикацией</th><th class='text-end'>Постов просмотрено</th>", thBody);
      }
    }
    document.getElementById("waves-day-box").innerHTML = wbdb;

    // ── Опубликовано по регионам ──
    var pub = d.published || {};
    var pubb = "";
    if (!pub.total) {
      pubb = '<span class="text-muted">за 7 дней публикаций нет</span>';
    } else {
      var pbody = "";
      (pub.regions || []).forEach(function (r) {
        pbody += "<tr>" + td(esc(r.region)) + td(num(r.n), "text-end") + "</tr>";
      });
      pubb = tbl("<th>Регион</th><th class='text-end'>Постов</th>", pbody);
      pubb = '<div class="small text-muted mb-1">Всего ' + num(pub.total) +
             " постов в " + num(pub.regions_total || 0) + " регионах за 7 дней.</div>" + pubb;
      var themes2 = pub.themes || [];
      if (themes2.length) {
        var tp = "";
        themes2.forEach(function (t) {
          tp += "<tr>" + td(esc(t.theme)) + td(num(t.n), "text-end") + "</tr>";
        });
        pubb += '<div class="small text-muted mt-2 mb-1">По темам волн:</div>' +
                tbl("<th>Тема</th><th class='text-end'>Постов</th>", tp);
      }
    }
    document.getElementById("published-box").innerHTML = pubb;

    // ── Сайты ──
    var st = d.sites || {};
    var stb = "";
    if (!(st.sites || []).length) {
      stb = '<span class="text-muted">доставок нет</span>';
    } else {
      var stbody = "";
      st.sites.forEach(function (s3) {
        var sts = s3.statuses || {};
        stbody += "<tr>" +
          td("<b>" + esc(s3.title || s3.site) + "</b>" +
             (s3.active ? "" : ' <span class="badge text-bg-secondary">не активен</span>'), "small") +
          td(esc(s3.source_region || "—"), "small text-muted") +
          td(num(sts.delivered || 0), "text-end") +
          td(num(sts.rejected || 0), "text-end") +
          td(num(sts.held || 0), "text-end") +
          td(num(sts.failed || 0), "text-end") +
          td(num(s3.total || 0), "text-end") +
          td(esc(dtShort(s3.last_at)), "small text-nowrap") +
          "</tr>";
      });
      stb = tbl("<th>Сайт</th><th>Источник</th><th class='text-end'>Доставлено</th>" +
                "<th class='text-end'>Отклонено</th><th class='text-end'>Придержано</th>" +
                "<th class='text-end'>Ошибка</th><th class='text-end'>Всего</th>" +
                "<th>Последняя активность</th>", stbody);
      var reasons = [];
      st.sites.forEach(function (s4) {
        (s4.reasons || []).slice(0, 3).forEach(function (r) {
          reasons.push(esc(s4.site) + ": " + esc(r.reason) + " — " + num(r.n));
        });
      });
      if (reasons.length) {
        stb += '<div class="small text-muted mt-2">Почему отклоняли: ' + reasons.join(" · ") + "</div>";
      }
      stb += '<div class="small text-muted mt-1">Активные сайты (CONVEYOR_SITES): ' +
             esc((st.active || []).join(", ") || "—") + " · окно " + num(st.days) + " дн.</div>";
    }
    document.getElementById("sites-box").innerHTML = stb;

    // ── Откуда инфа для сайтов ──
    var col = d.collected || {};
    var colb = "";
    if (!(col.themes || []).length) {
      colb = '<span class="text-muted">нет данных</span>';
    } else {
      var cbody = "";
      col.themes.forEach(function (t) {
        cbody += "<tr>" +
          td(esc(t.theme)) +
          td(num(t.regions || 0), "text-end") +
          td(num(t.lips || 0), "text-end") +
          "</tr>";
      });
      colb = '<div class="small text-muted mb-1">Сайт получает посты из <b>опубликованных сводок района</b> — ' +
             "то есть из этого потока. За " + num(col.days) + " дн. собрано " + num(col.lips) +
             " постов.</div>" +
             tbl("<th>Тема</th><th class='text-end'>Регионов</th><th class='text-end'>Постов</th>", cbody);
    }
    document.getElementById("collected-box").innerHTML = colb;

    box.innerHTML = "";
  } catch (e) {
    box.innerHTML = '<div class="alert alert-warning py-2">Не удалось загрузить сводку: ' + esc(e.message) + "</div>";
  }
}

document.getElementById("btn-refresh").addEventListener("click", load);
load();
