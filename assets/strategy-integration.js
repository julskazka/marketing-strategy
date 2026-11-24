/* Questionnaire UI and guarded GetCourse integration. */
(() => {
  "use strict";
  const cfg = window.STRATEGY_CONFIG;
  if (!cfg) return;
  const tokenKey = "loons-strategy-session-v1";
  let token = "", confirmed = false, busy = false, pending = Promise.resolve(), saveError = "";
  try { token = sessionStorage.getItem(tokenKey) || ""; } catch {}
  if (!/^[a-f0-9]{64}$/.test(token)) token = "";
  const originalRender = render, originalGo = go, originalSave = save;
  const footer = document.querySelector(".footer small");
  const preview = document.querySelector(".preview-bar");
  if (preview) preview.hidden = true;
  if (preview) preview.style.display = "none";
  if (footer) footer.remove();
  answers = {};
  function ensureToken() {
    if (!token) {
      token = Array.from(crypto.getRandomValues(new Uint8Array(32)), x => x.toString(16).padStart(2,"0")).join("");
      try { sessionStorage.setItem(tokenKey, token); } catch {}
    }
    return token;
  }
  async function call(action, extra = {}) {
    const response = await fetch("/api/strategy/" + action, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({token: ensureToken(), ...extra})
    });
    let data;
    try { data = await response.json(); } catch { throw new Error("Сервер не ответил. Попробуйте ещё раз."); }
    if (!response.ok || !data.ok) throw new Error(data.message || "Не удалось сохранить данные.");
    return data;
  }
  function displayStatus() {
    if (!["question","review"].includes(screen)) return;
    let status = document.getElementById("strategy-status");
    if (!status) {
      status = document.createElement("p");
      status.id = "strategy-status";
      status.className = "muted";
      status.setAttribute("role", "status");
      status.style.fontSize = "14px";
      const anchor = app.querySelector(".actions") || app.querySelector(".form-card");
      if (anchor) anchor.append(status);
    }
    if (status) status.textContent = saveError;
  }
  render = function () {
    originalRender();
    const form = document.getElementById("contact-form");
    if (form) {
      const old = form.querySelector("p.muted");
      if (old) old.remove();
      const box = document.createElement("label");
      box.style.cssText = "display:flex;align-items:flex-start;gap:10px;font-size:14px;line-height:1.5;margin:12px 0;cursor:pointer";
      const check = document.createElement("input");
      check.type = "checkbox"; check.name = "consent"; check.required = true;
      check.checked = confirmed; check.style.cssText = "width:18px;height:18px;flex:0 0 18px;margin-top:3px";
      const copy = document.createElement("span");
      const link = (url, name) => { const a = document.createElement("a"); a.href = url; a.textContent = name; a.target = "_blank"; a.rel = "noopener noreferrer"; a.style.textDecoration = "underline"; return a; };
      copy.append(link(cfg.consent_url, "Согласна на обработку данных и получение сообщения от LOONSISTERS по результатам диагностики."));
      box.append(check,copy);
      form.insertBefore(box, form.querySelector("#contact-error"));
      if (confirmed) {
        for (const input of form.querySelectorAll("input[name=name],input[name=email],input[name=phone]")) input.readOnly = true;
      }
    }
    displayStatus();
  };
  go = function (s) {
    if (!confirmed && ["question","review","result","program"].includes(s)) s = "contact";
    originalGo(s);
  };
  save = function () {
    if (!confirmed) return;
    originalSave();
    const snapshot = JSON.parse(JSON.stringify(answers));
    pending = pending.catch(()=>{}).then(async () => {
      try {
        await call("save", {answers:snapshot});
        saveError = "";
      } catch {
        saveError = "Ответ пока не сохранён на сервере. Следующая отправка повторит сохранение.";
      }
      displayStatus();
    });
  };
  function restore(data) {
    person = data.person;
    answers = data.answers || {};
    confirmed = true;
    originalSave();
  }
  function advance() {
    current = Engine.route(answers).find(k => answers[k] === undefined) || "note";
    selected = Array.isArray(answers[current]) ? [...answers[current]] : [];
    editing = false;
    go("question");
  }
  app.addEventListener("submit", async e => {
    if (e.target.id !== "contact-form") return;
    e.preventDefault(); e.stopImmediatePropagation();
    if (busy || !e.target.reportValidity()) return;
    const f = new FormData(e.target);
    const phone = String(f.get("phone"));
    const name = String(f.get("name")).trim(), email = String(f.get("email")).trim();
    const err = e.target.querySelector("#contact-error");
    if (!cfg.enabled) { err.textContent = "Приём анкет временно недоступен. Попробуйте немного позже."; return; }
    if (!name || phone.replace(/\D/g,"").length < 10 || phone.replace(/\D/g,"").length > 15) {
      err.textContent = "Проверьте имя и номер телефона."; return;
    }
    const btn = e.target.querySelector("button[type=submit]");
    busy = true; btn.disabled = true; btn.textContent = "Сохраняем…"; err.textContent = "";
    try {
      if (confirmed) { advance(); return; }
      restore(await call("start", {name,email,phone,consent:f.get("consent")==="on"}));
      advance();
    } catch (error) {
      err.textContent = error.message || "Не удалось подтвердить заказ. Попробуйте ещё раз.";
    } finally {
      busy = false; btn.disabled = false; btn.textContent = "Перейти к вопросам";
    }
  }, true);
  app.addEventListener("click", async e => {
    const b = e.target.closest("button");
    if (!b || screen !== "review" || b.dataset.go !== "result") return;
    e.preventDefault(); e.stopImmediatePropagation();
    if (busy || !confirmed || Engine.conflicts(answers).length) return;
    busy = true; b.disabled = true; b.textContent = "Сохраняем анкету…";
    saveError = ""; displayStatus();
    try {
      await pending;
      await call("finish", {answers: Engine.clean(answers)});
      saveError = "";
      go("result");
    } catch (error) {
      saveError = error.message || "Не удалось подтвердить отправку. Попробуйте ещё раз.";
      displayStatus();
    } finally {
      busy = false; b.disabled = false; b.textContent = "Да, показать мой план";
    }
  }, true);
  if (token && cfg.enabled) {
    busy = true;
    call("resume").then(data => { restore(data); if (screen === "welcome" || screen === "contact") render(); })
      .catch(()=>{}).finally(()=>{busy=false;});
  }
  render();
})();
