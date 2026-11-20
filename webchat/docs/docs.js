(() => {
  const content = document.querySelector("#docs-content");
  const title = document.querySelector("#page-title");
  const nav = document.querySelector("#iteration-nav");
  const updated = document.querySelector("#updated-at");
  const todoLink = document.querySelector("#todo-link");
  const archLink = document.querySelector("#arch-link");
  let manifest = null;
  let latestRecord = null;

  async function load() {
    try {
      const res = await fetch("/docs/manifest.json", { cache: "no-store" });
      if (!res.ok) throw new Error(`manifest HTTP ${res.status}`);
      manifest = await res.json();
      renderNav();
      updated.textContent = `updated ${manifest.updated_at || ""}`;
      if (manifest.iterations?.length) {
        await openIteration(manifest.iterations[0]);
      } else {
        content.innerHTML = '<div class="error">还没有迭代记录。</div>';
      }
    } catch (err) {
      content.innerHTML = `<div class="error">Docs 加载失败：${escapeHtml(err.message)}</div>`;
    }
  }

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function renderNav() {
    nav.innerHTML = "";
    for (const iteration of manifest.iterations || []) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "side-link";
      btn.dataset.id = iteration.id;
      btn.innerHTML = `<span class="side-ico">▸</span><span></span>`;
      btn.querySelector("span:last-child").textContent = iteration.title;
      btn.title = iteration.summary || iteration.title;
      btn.addEventListener("click", () => openIteration(iteration));
      nav.append(btn);
    }
  }

  function setActive(button) {
    document.querySelectorAll(".side-link").forEach((el) => el.classList.remove("active"));
    if (button) button.classList.add("active");
  }

  async function openIteration(iteration) {
    try {
      const res = await fetch(`/docs/${iteration.record}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`record HTTP ${res.status}`);
      const html = await res.text();
      content.innerHTML = html;
      latestRecord = iteration;
      const navButton = [...nav.querySelectorAll(".side-link")].find((el) => el.dataset.id === iteration.id);
      setActive(navButton);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (err) {
      content.innerHTML = `<div class="error">记录加载失败：${escapeHtml(err.message)}</div>`;
    }
  }

  todoLink.addEventListener("click", async () => {
    if (!latestRecord && manifest?.iterations?.length) {
      await openIteration(manifest.iterations[0]);
    } else if (latestRecord) {
      const res = await fetch(`/docs/${latestRecord.record}`, { cache: "no-store" });
      content.innerHTML = await res.text();
    }
    setActive(todoLink);
    document.querySelector("#todo")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  archLink.addEventListener("click", () => {
    setActive(archLink);
    const items = manifest?.architecture || [];
    content.innerHTML = `
      <div class="iteration">
        <header class="iteration-head">
          <div class="eyebrow">ARCHITECTURE · HISTORY</div>
          <h1>架构历史</h1>
          <p class="lede">架构图只追加、不覆盖。每张图保留当时的真实状态；计划中的能力不画成已完成。</p>
        </header>
        <section><div class="arch-grid" id="arch-grid"></div></section>
      </div>`;
    const grid = document.querySelector("#arch-grid");
    for (const item of items) {
      const card = document.createElement("article");
      card.className = "arch-card";
      card.innerHTML = `
        <div class="arch-card-head"><strong></strong><span></span></div>
        <a target="_blank" rel="noopener"><img loading="lazy" alt=""></a>
        <p></p>`;
      card.querySelector("strong").textContent = item.label || item.id;
      card.querySelector("span").textContent = item.date || "";
      const link = card.querySelector("a");
      const img = card.querySelector("img");
      const src = `/docs/${item.src}`;
      link.href = src;
      img.src = src;
      img.alt = item.label || "architecture diagram";
      card.querySelector("p").textContent = item.note || "";
      grid.append(card);
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  load();
})();
