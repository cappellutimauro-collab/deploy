window.REHAB_EDITOR_CONTENT = {
  "version": 1,
  "pages": {}
};

(function () {
  const data = window.REHAB_EDITOR_CONTENT || { pages: {} };
  const page = location.pathname || "/";
  const pageData = (data.pages && (data.pages[page] || data.pages["*"])) || null;
  if (!pageData) return;

  const css = document.createElement("style");
  css.textContent = `
    .rehab-custom-layer { position:absolute; inset:0; z-index:25; pointer-events:none; }
    .rehab-custom-item { position:absolute; box-sizing:border-box; pointer-events:auto; }
    .rehab-custom-text { color:var(--ink,#101828); font:800 22px Manrope,system-ui,sans-serif; line-height:1.2; }
    .rehab-custom-item.card, .rehab-custom-item.button { position:absolute; }
    .rehab-custom-image { object-fit:cover; border-radius:var(--card-radius,8px); }
  `;
  document.head.appendChild(css);

  const applyStyles = () => {
    Object.values(pageData.overrides || {}).forEach(entry => {
      if (!entry.selector || !entry.style) return;
      const el = document.querySelector(entry.selector);
      if (el) Object.assign(el.style, entry.style);
    });
  };

  const renderItems = () => {
    let layer = document.querySelector(".rehab-custom-layer");
    if (!layer) {
      layer = document.createElement("div");
      layer.className = "rehab-custom-layer";
      document.body.appendChild(layer);
    }
    layer.innerHTML = "";
    (pageData.items || []).forEach(item => {
      const el = item.type === "button" ? document.createElement("a") : item.type === "image" ? document.createElement("img") : document.createElement("div");
      el.className = item.type === "button" ? "rehab-custom-item button" : item.type === "box" ? "rehab-custom-item card" : item.type === "image" ? "rehab-custom-item rehab-custom-image" : "rehab-custom-item rehab-custom-text";
      if (item.type === "button") el.href = item.href || "#";
      if (item.type === "image") { el.src = item.src || ""; el.alt = item.text || ""; }
      else el.textContent = item.text || "";
      Object.assign(el.style, item.style || {});
      layer.appendChild(el);
    });
  };

  const run = () => { applyStyles(); renderItems(); };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
  else setTimeout(run, 80);
})();
