/* Запускает движок на Python прямо в браузере (Pyodide). Подключается только в автономной сборке для GitHub Pages. */
(function () {
  var MODULES = __MODULES__;
  var BASES = ["https://cdn.jsdelivr.net/pyodide/v0.26.4/full/", "https://unpkg.com/pyodide@0.26.4/"];
  var box = document.createElement("div");
  box.setAttribute("role", "status");
  box.style.cssText = "position:fixed;inset:0;z-index:99;display:grid;place-items:center;background:#0d4727;color:#fff;font:16px/1.5 system-ui,sans-serif;text-align:center;padding:24px";
  box.innerHTML = '<div style="max-width:420px"><div style="font-size:22px;font-weight:800;margin-bottom:8px">Запускаю помощника…</div>' +
    '<div id="boot-msg" style="opacity:.85">Движок работает прямо в вашем браузере. Первый запуск занимает 5–15 секунд (загрузка около 10 МБ), дальше быстрее.</div></div>';
  document.addEventListener("DOMContentLoaded", function () { document.body.appendChild(box); });
  if (document.body) document.body.appendChild(box);

  function load(src) {
    return new Promise(function (ok, fail) {
      var s = document.createElement("script");
      s.src = src; s.onload = ok; s.onerror = function () { fail(new Error(src)); };
      document.head.appendChild(s);
    });
  }

  window.__boot = (async function () {
    var base = null;
    for (var i = 0; i < BASES.length && !base; i++) {
      try { await load(BASES[i] + "pyodide.js"); base = BASES[i]; } catch (e) { /* пробуем следующий источник */ }
    }
    if (!base) throw new Error("Не удалось загрузить Pyodide: проверьте интернет и попробуйте ещё раз");
    var py = await loadPyodide({ indexURL: base });
    for (var j = 0; j < MODULES.length; j++) {
      var res = await fetch("./" + MODULES[j]);
      if (!res.ok) throw new Error("Нет файла " + MODULES[j]);
      py.FS.writeFile("/home/pyodide/" + MODULES[j], await res.text());
    }
    py.runPython("import sys; sys.path.insert(0, '/home/pyodide')");
    var webapi = py.pyimport("webapi");
    window.__localApi = async function (path, body) {
      var raw = webapi.handle(body === undefined ? "GET" : "POST", "/api" + path, body === undefined ? "" : JSON.stringify(body));
      var out = JSON.parse(raw);
      if (out.status >= 400) throw Object.assign(new Error(out.payload.error.message), { code: out.payload.error.code, info: out.payload.error });
      return out.payload;
    };
    box.remove();
  })().catch(function (e) {
    document.getElementById("boot-msg").textContent = "Не получилось запустить: " + e.message;
    throw e;
  });
})();
