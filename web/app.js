"use strict";

const el = id => document.getElementById(id);
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
};
const number = new Intl.NumberFormat("zh-CN");
const order = new Intl.Collator("zh-CN", { numeric: true });
const categoryOrder = ["剧情CG", "角色卡面", "场景背景", "剧情演出素材", "游戏预览", "其他贴图"];
const categoryLabel = value => value === "剧情CG" ? "剧情 CG" : value;
const searchText = value => value.normalize("NFKC").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
const assetUrl = (kind, item) => "/" + kind + "?id=" + item.id + "&v=" + item.token;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

let images = [], byId = new Map(), categoryTree = new Map(), filtered = [];
let selected = new Set(), folders = [], folderId = null, category = "剧情CG";
let libraryRevision = 0, output = "", pickerAvailable = false;
let job = {}, busy = false, submitting = false, status = {};
let thumbnailStatus = null;
let completedJob = 0, pendingViewer = null, toastTimer;
let selectionQueue = Promise.resolve(), selectionUpdates = 0;
const expanded = new Set(["剧情CG"]);
const failedThumbs = new Set();

async function api(path, body) {
  const options = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "HTTP " + response.status);
  return data;
}

function notify(text) {
  clearTimeout(toastTimer);
  el("toast").textContent = text;
  el("toast").hidden = false;
  toastTimer = setTimeout(() => { el("toast").hidden = true; }, 3600);
}

function formError(id, message = "") {
  el(id).textContent = message;
  el(id).hidden = !message;
}

function currentFolder() {
  return folders.find(folder => folder.id === folderId);
}

function showCategory(path) {
  folderId = null;
  category = path;
  const parts = path.split("/");
  for (let n = 1; n <= parts.length; n++) expanded.add(parts.slice(0, n).join("/"));
  renderNavigation();
  applyFilters(true);
  closeNavigation();
}

function showFolder(id) {
  folderId = id;
  renderNavigation();
  applyFilters(true);
  closeNavigation();
}

function closeNavigation() {
  el("sidebar").classList.remove("open");
  el("nav-toggle").setAttribute("aria-expanded", "false");
}

el("nav-toggle").addEventListener("click", () => {
  const value = el("sidebar").classList.toggle("open");
  el("nav-toggle").setAttribute("aria-expanded", value);
});

function buildCategories() {
  categoryTree = new Map();
  for (const image of images) {
    let branch = categoryTree, path = "";
    for (const part of image.c.split("/")) {
      path = path ? path + "/" + part : part;
      if (!branch.has(part)) branch.set(part, { part, path, count: 0, children: new Map() });
      const row = branch.get(part);
      row.count++;
      branch = row.children;
    }
  }
}

function navChildrenId(path) {
  return "nav-children-" + encodeURIComponent(path);
}

function navRow(label, count, active, onClick, branch) {
  const row = element("div", "nav-row");
  if (branch?.children.size) {
    const open = expanded.has(branch.path);
    const caret = element("button", "caret");
    caret.type = "button";
    caret.setAttribute("aria-expanded", open);
    caret.setAttribute("aria-label", (open ? "收起" : "展开") + label);
    caret.setAttribute("aria-controls", navChildrenId(branch.path));
    caret.append(element("span", "caret-symbol"));
    caret.addEventListener("click", () => {
      if (open) expanded.delete(branch.path);
      else expanded.add(branch.path);
      renderNavigation(branch.path);
    });
    caret.dataset.path = branch.path;
    row.append(caret);
  } else if (branch) {
    row.append(element("span", "caret-spacer"));
  }
  const button = element("button", "nav-item");
  button.type = "button";
  button.title = label;
  if (active) button.setAttribute("aria-current", "page");
  button.append(element("span", "", label), element("span", "nav-count", number.format(count)));
  button.addEventListener("click", onClick);
  row.append(button);
  return row;
}

function renderNavigation(focusPath = null) {
  const categories = document.createDocumentFragment();
  categories.append(navRow("全部图片", images.length, !folderId && !category, () => showCategory("")));
  function walk(parent, rows) {
    for (const row of rows) {
      const label = categoryLabel(row.part);
      parent.append(navRow(label, row.count, !folderId && category === row.path,
        () => showCategory(row.path), row));
      if (row.children.size) {
        const children = element("div", "nav-children");
        children.id = navChildrenId(row.path);
        children.setAttribute("role", "group");
        children.setAttribute("aria-label", label);
        children.hidden = !expanded.has(row.path);
        if (!children.hidden) {
          walk(children, [...row.children.values()].sort((left, right) => order.compare(left.part, right.part)));
        }
        parent.append(children);
      }
    }
  }
  walk(categories, categoryOrder.map(name => categoryTree.get(name)).filter(Boolean));
  el("categories").replaceChildren(categories);
  if (focusPath) {
    [...el("categories").querySelectorAll("[data-path]")].find(node => node.dataset.path === focusPath)?.focus();
  }
  const folderList = folders.map(folder => navRow(folder.name, folder.count, folder.id === folderId,
    () => showFolder(folder.id)));
  el("folders").replaceChildren(...(folderList.length ? folderList : [element("p", "sidebar-empty", "暂无收藏夹")]));
}

async function loadIndex() {
  const data = await api("/index");
  images = data.items.map(item => {
    const name = item.p.split(/[\\/]/).pop();
    const search = searchText([item.p, item.c, item.language].filter(Boolean).join(" "));
    return { ...item, name, search, compact: search.replaceAll(" ", "") };
  });
  byId = new Map(images.map(item => [item.id, item]));
  libraryRevision = data.revision;
  failedThumbs.clear();
  clearGrid();
  buildCategories();
}

function applyFilters(resetScroll = false) {
  const folder = currentFolder();
  if (folderId && !folder) folderId = null;
  const set = folder && new Set(folder.ids);
  const terms = searchText(el("search").value).split(" ").filter(Boolean);
  const availability = el("availability").value;
  const wallpaper = el("wallpaper").checked;
  filtered = images.filter(item => {
    if (set) {
      if (!set.has(item.id)) return false;
    } else if (category && item.c !== category && !item.c.startsWith(category + "/")) {
      return false;
    }
    if (terms.length && !terms.every(term => item.search.includes(term) || item.compact.includes(term))) return false;
    if (wallpaper && !(item.w >= 1920 && item.h >= 1080)) return false;
    if (availability === "local") return !item.missing;
    if (availability === "remote") return item.downloadable;
    if (availability === "missing") return item.missing && !item.downloadable;
    if (availability === "failed") return failedThumbs.has(item.id);
    return true;
  });
  const sort = el("sort").value;
  filtered.sort((left, right) => {
    if (sort === "category") {
      const group = categoryOrder.indexOf(left.c.split("/")[0]) - categoryOrder.indexOf(right.c.split("/")[0]);
      if (group) return group;
      const comparison = order.compare(left.c, right.c);
      if (comparison) return comparison;
    }
    if (sort === "new" && left.new !== right.new) return right.new - left.new;
    if (sort === "size") {
      const difference = (right.w || 0) * (right.h || 0) - (left.w || 0) * (left.h || 0);
      if (difference) return difference;
    }
    return order.compare(left.name, right.name) || left.id - right.id;
  });
  const parts = category.split("/");
  el("location").textContent = folder ? "收藏夹" : parts.slice(0, -1).map(categoryLabel).join(" / ");
  el("gallery-title").textContent = folder ? folder.name : categoryLabel(parts.at(-1)) || "全部图片";
  el("folder-actions").hidden = !folder;
  el("empty").textContent = folder && !folder.count ? "这个收藏夹还没有图片" :
    !filtered.length && !images.length ? "图库索引中没有图片" : "没有符合当前条件的图片";
  const unavailable = folder ? folder.count - folder.ids.length : 0;
  el("result-count").textContent = number.format(filtered.length) + " 张图片" +
    (unavailable ? " · " + number.format(unavailable) + " 张未出现在当前索引中" : "");
  if (resetScroll) {
    nearReady = false;
    el("grid-scroll").scrollTop = 0;
  }
  renderGrid();
  updateActions();
}

let filterTimer;
el("search").addEventListener("input", () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => applyFilters(true), 120);
});
for (const id of ["availability", "sort", "wallpaper"]) {
  el(id).addEventListener("change", () => applyFilters(true));
}

function previewSource(item) {
  if (!item) return null;
  if (!item.missing || item.cached) {
    if (!item.missing) return item;
    const paired = byId.get(item.preview_id);
    if (paired && !paired.missing) return paired;
    return item;
  }
  const paired = byId.get(item.preview_id);
  return paired && (!paired.missing || paired.cached) ? paired : null;
}

function canLoadPreview(item) {
  return item && !previewSource(item) && (item.downloadable || byId.get(item.preview_id)?.downloadable);
}

function saveLabel(item) {
  return item.kind === "gallery" ? "保存预览" : "保存原图";
}

function updateSelection(action, ids = []) {
  if (action === "clear") selected.clear();
  else if (action === "add") ids.forEach(id => selected.add(id));
  else ids.forEach(id => selected.delete(id));
  selectionUpdates++;
  updateActions();
  selectionQueue = selectionQueue.then(async () => {
    try {
      const data = await api("/selection", { action, ids });
      if (selectionUpdates === 1) selected = new Set(data.ids);
    } catch (error) {
      notify("勾选状态写入失败 · " + error.message);
      if (selectionUpdates === 1) {
        try { selected = new Set((await api("/state")).ids); }
        catch { /* Keep the current selection available while disconnected. */ }
      }
    } finally {
      selectionUpdates--;
      updateActions();
    }
  });
}

el("select-filtered").addEventListener("click", () => updateSelection("add", filtered.map(item => item.id)));
el("clear-selection").addEventListener("click", () => updateSelection("clear"));

const mounted = new Map();
const thumbRequests = new Map();
const thumbCache = new Map();
const scroller = el("grid-scroll"), grid = el("grid");
let gridFrame, prefetchTimer;
let nearReady = false;
const gap = 11;

function createCard(item, source) {
  const card = element("article", "card");
  const media = element("button", "card-media");
  media.type = "button";
  media.setAttribute("aria-label", "预览 " + item.name);
  const placeholder = element("span", "card-placeholder");
  const img = element("img");
  img.alt = "";
  img.hidden = true;
  img.decoding = "async";
  if (source) {
    placeholder.textContent = source.cached ? "正在读取缩略图…" : "正在生成缩略图…";
    img.onload = () => {
      img.hidden = false;
      placeholder.hidden = true;
    };
    img.onerror = () => {
      const url = img.getAttribute("src");
      if (!url || mounted.get(item.id)?.img !== img) return;
      img.removeAttribute("src");
      img.hidden = true;
      placeholder.hidden = false;
      if (url.startsWith("/thumb?")) {
        source.cached = false;
        placeholder.textContent = "正在生成缩略图…";
      } else {
        failedThumbs.add(item.id);
        placeholder.textContent = "缩略图读取失败";
      }
      scheduleGrid();
    };
    media.onclick = () => openViewer(item.id);
  } else if (canLoadPreview(item)) {
    placeholder.textContent = "加载预览";
    media.setAttribute("aria-label", "加载预览 " + item.name);
    media.onclick = () => requestTask("preview", [item.id], item.id);
  } else {
    placeholder.textContent = "暂无可用预览";
    media.disabled = true;
  }
  media.append(img, placeholder);

  const label = element("label", "checkbox-slot");
  const input = element("input");
  input.type = "checkbox";
  input.setAttribute("aria-label", "选中 " + item.name);
  input.onchange = () => updateSelection(input.checked ? "add" : "remove", [item.id]);
  label.append(input);

  const badgeText = item.kind === "gallery" ? "游戏预览" :
    item.missing ? (source && source.id !== item.id ? "游戏预览" : "原图待获取") : item.new ? "更新新增" : "";
  const badge = element("span", "card-badge", badgeText);
  badge.hidden = !badgeText;

  const footer = element("div", "card-meta");
  const text = element("div", "card-text");
  const title = element("span", "card-title", item.name);
  title.title = item.p;
  const size = element("span", "card-size", itemSize(item));
  text.append(title, size);
  const save = element("button", "card-save quiet", "保存");
  save.type = "button";
  save.title = saveLabel(item);
  save.setAttribute("aria-label", saveLabel(item) + " " + item.name);
  save.onclick = () => requestTask("export", [item.id]);
  footer.append(text, save);
  card.append(media, label, badge, footer);
  grid.append(card);
  return { card, img, placeholder, size, input, media, save, item, source };
}

function itemSize(item) {
  return item.w && item.h ? item.w + " × " + item.h : item.language || "";
}

function thumbKey(source) {
  return source.id + "-" + source.token;
}

function paintThumb(card, url) {
  if (card.img.getAttribute("src") !== url) card.img.src = url;
  card.size.textContent = itemSize(card.item);
}

function rememberThumb(key, blob) {
  const url = URL.createObjectURL(blob);
  thumbCache.set(key, url);
  while (thumbCache.size > 350) {
    const oldest = thumbCache.keys().next().value;
    URL.revokeObjectURL(thumbCache.get(oldest));
    thumbCache.delete(oldest);
  }
  return url;
}

function queueThumbnails(ids) {
  const cards = ids.map(id => mounted.get(id)).filter(card => card?.source);
  const needed = new Set(cards.map(card => thumbKey(card.source)));
  for (const [key, controller] of thumbRequests) {
    if (!needed.has(key)) controller.abort();
  }
  for (const card of cards) {
    const key = thumbKey(card.source);
    if (failedThumbs.has(card.item.id)) {
      card.placeholder.textContent = "缩略图读取失败";
    } else if (thumbCache.has(key)) {
      const url = thumbCache.get(key);
      thumbCache.delete(key);
      thumbCache.set(key, url);
      paintThumb(card, url);
    } else if (card.source.cached) {
      paintThumb(card, assetUrl("thumb", card.source));
    } else if (thumbRequests.size < 8 && !thumbRequests.has(key)) {
      loadThumb(card.source);
    }
  }
}

async function loadThumb(source) {
  const key = thumbKey(source);
  const request = new AbortController();
  let networkRetries = 0;
  thumbRequests.set(key, request);
  try {
    while (!request.signal.aborted) {
      let response;
      try {
        response = await fetch(assetUrl("thumb", source), { signal: request.signal });
      } catch (error) {
        if (request.signal.aborted || networkRetries++ >= 2) throw error;
        await delay(650);
        continue;
      }
      if (response.status === 202) {
        await delay(450);
        continue;
      }
      if (response.status === 409) {
        await delay(650);
        continue;
      }
      if (!response.ok) throw new Error(String(response.status));
      const blob = await response.blob();
      if (request.signal.aborted) return;
      source.cached = true;
      source.w = Number(response.headers.get("X-Image-Width")) || source.w;
      source.h = Number(response.headers.get("X-Image-Height")) || source.h;
      const url = rememberThumb(key, blob);
      for (const card of mounted.values()) {
        if (card.source && thumbKey(card.source) === key) paintThumb(card, url);
      }
      break;
    }
  } catch (error) {
    if (!request.signal.aborted) {
      for (const card of mounted.values()) {
        if (card.source && thumbKey(card.source) === key) {
          failedThumbs.add(card.item.id);
          card.placeholder.textContent = "缩略图读取失败";
        }
      }
    }
  } finally {
    thumbRequests.delete(key);
    scheduleGrid();
  }
}

function clearGrid() {
  for (const request of thumbRequests.values()) request.abort();
  mounted.clear();
  grid.replaceChildren();
}

function scheduleGrid() {
  if (!gridFrame) gridFrame = requestAnimationFrame(() => {
    gridFrame = 0;
    renderGrid();
  });
}

function renderGrid() {
  const width = grid.clientWidth;
  if (!width) return;
  const cols = Math.max(1, Math.floor((width + gap) / ((width < 590 ? 156 : 207) + gap)));
  const cellWidth = (width - (cols - 1) * gap) / cols;
  const cellHeight = Math.round(cellWidth * .71) + 49;
  const step = cellHeight + gap;
  const rowCount = Math.ceil(filtered.length / cols);
  const first = Math.max(0, Math.floor(scroller.scrollTop / step));
  const last = Math.min(rowCount, Math.ceil((scroller.scrollTop + scroller.clientHeight) / step));
  const visible = [], near = [];
  const keep = new Set();
  grid.style.height = rowCount ? rowCount * step - gap + "px" : "0px";
  el("empty").hidden = rowCount > 0;
  for (let row = Math.max(0, first - 2); row < Math.min(rowCount, last + 2); row++) {
    for (let column = 0; column < cols; column++) {
      const item = filtered[row * cols + column];
      if (!item) break;
      const id = item.id;
      keep.add(id);
      (row >= first && row < last ? visible : near).push(id);
      const source = previewSource(item);
      let card = mounted.get(id);
      if (!card || card.source?.id !== source?.id) {
        card?.card.remove();
        card = createCard(item, source);
        mounted.set(id, card);
      }
      const geometry = [row, column, cellWidth, cellHeight].join("/");
      if (card.geometry !== geometry) {
        card.card.style.cssText = "left:" + (column * (cellWidth + gap)) + "px;top:" +
          row * step + "px;width:" + cellWidth + "px;height:" + cellHeight + "px";
        card.geometry = geometry;
      }
      card.input.checked = selected.has(id);
      card.save.disabled = busy || (!item.downloadable && item.missing);
      card.save.hidden = item.missing && !item.downloadable;
      if (!source && canLoadPreview(item)) card.media.disabled = busy;
    }
  }
  for (const [id, card] of mounted) {
    if (!keep.has(id)) {
      card.card.remove();
      mounted.delete(id);
    }
  }
  const waiting = visible.some(id => {
    const card = mounted.get(id);
    return card.source && !card.source.cached && !thumbCache.has(thumbKey(card.source)) && !failedThumbs.has(id);
  });
  clearTimeout(prefetchTimer);
  queueThumbnails(nearReady && !waiting ? [...visible, ...near] : visible);
  if (!nearReady && !waiting && near.length) {
    prefetchTimer = setTimeout(() => {
      nearReady = true;
      queueThumbnails([...visible, ...near]);
    }, 125);
  }
}

scroller.addEventListener("scroll", () => {
  nearReady = false;
  clearTimeout(prefetchTimer);
  scheduleGrid();
}, { passive: true });
new ResizeObserver(scheduleGrid).observe(scroller);

function byteSize(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return value.toLocaleString("zh-CN", { maximumFractionDigits: unit ? 1 : 0 }) + " " + units[unit];
}

function detailsList(title, rows) {
  const block = element("details");
  const list = element("ul");
  list.append(...rows.map(row => element("li", "", row)));
  block.append(element("summary", "", title + "（" + number.format(rows.length) + "）"), list);
  return block;
}

function buttonTask(action, ids) {
  requestTask(action, ids).catch(error => { notify(error.message); });
}

async function requestTask(action, ids = [], previewId = null) {
  const needsImages = action === "preview" || action === "export";
  if (busy || submitting || (needsImages && !ids.length)) return;
  submitting = true;
  updateActions();
  try {
    const request = { action, ids };
    if (needsImages) {
      const estimate = await api("/estimate", { action, ids });
      request.revision = estimate.revision;
      if (estimate.bundles) {
        const amount = estimate.bytes ? "，已知大小约 " + byteSize(estimate.bytes) : "";
        const unknown = estimate.unknown ? "，其中 " + estimate.unknown + " 个大小未知" : "";
        const destination = action === "export" ? "。图片将保存到 " + output : "";
        const approved = await confirmAction(action === "export" ? "下载并保存图片" : "加载预览",
          "需要下载 " + number.format(estimate.bundles) + " 个 Bundle" + amount + unknown + destination + "。", "下载");
        if (!approved) return;
      }
    }
    job = await api("/jobs", request);
    busy = true;
    if (previewId !== null) pendingViewer = { jobId: job.id, imageId: previewId };
    paintTask();
  } catch (error) {
    const message = "无法启动任务 · " + error.message;
    if (action === "thumbnails" && el("thumb-status-dialog").open) formError("thumb-status-error", message);
    else notify(message);
  } finally {
    submitting = false;
    updateActions();
  }
}

function updateActions() {
  const locked = busy || submitting;
  el("refresh").disabled = el("refresh-dialog").disabled = locked;
  if (el("thumb-status-dialog").open) paintThumbnailStatus();
  el("selection-count").textContent = "已选 " + number.format(selected.size) + " 张";
  el("selection-bar").hidden = !selected.size;
  el("select-filtered").disabled = !filtered.length || filtered.every(item => selected.has(item.id));
  const selectedImages = [...selected];
  el("batch-preview").disabled = locked || !selectedImages.some(id => canLoadPreview(byId.get(id)));
  el("batch-save").disabled = locked;
  el("add-to-folder").disabled = !selectedImages.some(id => byId.has(id));
  const folder = currentFolder();
  el("remove-from-folder").hidden = !folder;
  el("remove-from-folder").disabled = !folder?.ids.some(id => selected.has(id));
  for (const [id, card] of mounted) {
    card.input.checked = selected.has(id);
    card.save.disabled = locked;
    if (!card.source && canLoadPreview(card.item)) card.media.disabled = locked;
  }
  if (viewerId !== null) paintViewerActions();
  scheduleGrid();
}

el("refresh").onclick = () => buttonTask("refresh", []);
el("sync-thumbs").onclick = () => {
  formError("thumb-status-error");
  buttonTask("thumbnails");
};
el("thumb-status-open").onclick = () => {
  thumbnailStatus = null;
  paintThumbnailStatus();
  el("thumb-status-dialog").showModal();
  refreshThumbnailStatus();
};
el("refresh-dialog").onclick = () => {
  el("bundle-dialog").close();
  buttonTask("refresh", []);
};
el("batch-save").onclick = () => buttonTask("export", [...selected]);
el("batch-preview").onclick = () => buttonTask("preview", [...selected].filter(id => canLoadPreview(byId.get(id))));
el("bundle-open").onclick = () => {
  paintBundle();
  el("bundle-dialog").showModal();
};

function paintCounts(id, entries, values) {
  const counts = entries.map(([key, label]) => {
    const cell = element("div");
    const value = values?.[key];
    cell.append(element("dt", "", label), element("dd", "", value === undefined ? "—" : number.format(value)));
    return cell;
  });
  el(id).replaceChildren(...counts);
}

function paintBundle() {
  paintCounts("bundle-counts", [["game", "游戏安装目录"], ["game-cache", "Unity 缓存"],
    ["download", "工具已下载"], ["server", "待下载"]], status.inventory);
  const last = status.last_check;
  const summary = el("last-check");
  summary.dataset.time = String(last?.time);
  summary.replaceChildren();
  if (status.inventory?.catalog) summary.append(element("p", "", "当前索引版本 " + status.inventory.catalog));
  if (!last) {
    summary.append(element("p", "", "尚未手动检查 Bundle。"));
    return;
  }
  const time = new Date(last.time * 1000).toLocaleString("zh-CN");
  summary.append(element("p", "", time + " · " + last.message),
    element("p", "", "清理 " + number.format(last.removed) + " 个重复副本，释放 " + byteSize(last.bytes)));
  if (typeof last.downloaded === "number") {
    summary.append(element("p", "", "下载 " + number.format(last.downloaded) + " 个 Bundle"));
  }
  if (last.thumbs) summary.append(element("p", "", thumbnailSummary(last.thumbs)));
  const failures = Object.entries(last.bundle_failures || {});
  if (failures.length) summary.append(detailsList("Bundle 下载失败", failures.map(([name, reason]) => name + " · " + reason)));
  const warnings = (last.warnings || []).filter(message => message !== last.message);
  if (warnings.length) summary.append(detailsList("检查提示", warnings));
  if (last.skipped.length) summary.append(detailsList("跳过清理", last.skipped.map(row => row.name + " · " + row.reason)));
}

function thumbnailSummary(thumbs) {
  if (!thumbs.saved && !thumbs.failed && !thumbs.cleanup) return "无需补全或清理";
  const parts = ["保存 " + number.format(thumbs.saved) + " 张"];
  if (thumbs.failed) parts.push("失败 " + number.format(thumbs.failed) + " 张");
  if (thumbs.cleanup) {
    const { removed, bytes, skipped } = thumbs.cleanup;
    parts.push("清理 " + number.format(removed) + " 张", "释放 " + byteSize(bytes));
    if (skipped) parts.push("跳过清理 " + number.format(skipped) + " 张");
  }
  return parts.join(" · ");
}

function paintProgress(progress) {
  if (job.total) {
    progress.max = job.total;
    progress.value = job.done || 0;
  } else progress.removeAttribute("value");
}

function paintThumbnailStatus() {
  const syncing = busy && (job.action === "thumbnails" || job.thumbnail_counts);
  const counts = (syncing && job.thumbnail_counts) || thumbnailStatus?.counts;
  paintCounts("thumb-counts", [["total", "图库图片"], ["cached", "已缓存"],
    ["pending", "待补全"], ["unavailable", "暂不能补全"], ["obsolete", "待清理"]], counts);
  const last = thumbnailStatus?.last_sync;
  const summary = el("thumb-last-sync");
  summary.textContent = thumbnailStatus ? "暂无同步记录。" : "";
  if (last) {
    summary.replaceChildren(element("p", "", "上次同步 " + new Date(last.time * 1000).toLocaleString("zh-CN")),
      element("p", "", thumbnailSummary(last)));
  }
  el("thumb-sync-progress").hidden = !syncing;
  el("thumb-sync-message").textContent = syncing ? job.message : "";
  paintProgress(el("thumb-progress"));
  el("sync-thumbs").disabled = busy || submitting;
}

async function refreshThumbnailStatus() {
  formError("thumb-status-error");
  try {
    thumbnailStatus = await api("/thumbnail-status");
  } catch (error) {
    thumbnailStatus = null;
    formError("thumb-status-error", "无法读取缩略图状态 · " + error.message);
  }
  paintThumbnailStatus();
}

function paintTask() {
  el("task-strip").hidden = !job.id;
  el("task-message").textContent = job.message || "";
  const progress = el("task-progress");
  progress.hidden = job.status !== "running";
  paintProgress(progress);
  const bundleFailures = Object.entries(job.bundle_failures || {});
  el("status-dot").className = job.status === "running" ? "running" :
    job.status === "error" || bundleFailures.length || job.thumbs?.failed || job.thumbs?.cleanup?.skipped || job.warnings?.length ||
      status.last_check?.warnings?.length ? "warning" : "";
  const body = el("task-body");
  const stamp = [job.id, job.status, job.message].join("/");
  if (el("task-dialog").open && body.dataset.stamp !== stamp) {
    body.dataset.stamp = stamp;
    body.replaceChildren(element("p", "", job.message));
    if (job.out) body.append(element("p", "subtle", job.out));
    if (job.cleanup) {
      body.append(element("p", "", "清理 " + number.format(job.cleanup.removed) +
        " 个重复副本，释放 " + byteSize(job.cleanup.bytes)));
      if (job.cleanup.skipped.length) {
        body.append(detailsList("跳过清理", job.cleanup.skipped.map(row => row.name + " · " + row.reason)));
      }
    }
    if (job.warnings?.length) body.append(detailsList("检查提示", job.warnings));
    if (job.thumbs) body.append(element("p", "", thumbnailSummary(job.thumbs)));
    if (bundleFailures.length) {
      body.append(detailsList("Bundle 下载失败", bundleFailures.map(([name, reason]) => name + " · " + reason)));
    }
    if (job.failures?.length) {
      body.append(detailsList("图片保存失败", job.failures.map(row => (row.path || "图片 " + row.id) + " · " + row.reason)));
    } else if (job.action === "preview" && job.failed_ids?.length) {
      body.append(detailsList("无法加载预览", job.failed_ids.map(id => byId.get(id)?.p || "图片 " + id)));
    }
  }
  const libraryFailed = (job.action === "refresh" || job.action === "thumbnails") &&
    (job.status === "error" || bundleFailures.length || job.thumbs?.failed || job.thumbs?.cleanup?.skipped);
  el("retry").hidden = busy || !(job.failed_ids?.length || libraryFailed);
  el("open-task-output").hidden = !job.out;
}

el("task-detail").onclick = () => {
  el("task-dialog").showModal();
  paintTask();
};
el("retry").onclick = () => {
  el("task-dialog").close();
  buttonTask(job.action, job.failed_ids || []);
};
el("open-task-output").onclick = () => api("/openout", { job: job.id }).catch(error => notify(error.message));

async function pollStatus() {
  try {
    status = await api("/status");
    const latest = status.job;
    if (latest.id >= (job.id || 0)) job = latest;
    busy = job.status === "running";
    if (latest.revision !== libraryRevision) {
      await loadIndex();
      const data = await api("/state");
      folders = data.folders;
      renderNavigation();
      applyFilters();
      if (viewerId !== null) {
        if (byId.has(viewerId)) refreshViewer(viewerId, false);
        else viewer.close();
      }
    }
    paintTask();
    if (el("bundle-dialog").open && el("last-check").dataset.time !== String(status.last_check?.time)) paintBundle();
    if (el("thumb-status-dialog").open && job.id > completedJob && !busy &&
        (job.action === "thumbnails" || job.action === "refresh")) await refreshThumbnailStatus();
    updateActions();
    if (job.id && job.id > completedJob && !busy) {
      completedJob = job.id;
      if (pendingViewer?.jobId === job.id) {
        const target = byId.get(pendingViewer.imageId);
        pendingViewer = null;
        if (previewSource(target)) openViewer(target.id);
      }
    }
  } catch (error) {
    if (el("task-strip").hidden) notify("暂时无法连接图库 · " + error.message);
  } finally {
    setTimeout(pollStatus, busy ? 450 : 1400);
  }
}

for (const dialog of document.querySelectorAll("dialog")) {
  dialog.querySelectorAll(".close").forEach(button => button.addEventListener("click", () => dialog.close()));
}

function confirmAction(title, message, label) {
  const dialog = el("confirm-dialog");
  el("confirm-title").textContent = title;
  el("confirm-message").textContent = message;
  el("confirm-yes").textContent = label;
  dialog.showModal();
  return new Promise(resolve => {
    el("confirm-yes").onclick = () => dialog.close("yes");
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "yes");
      dialog.returnValue = "";
    }, { once: true });
  });
}

el("settings-open").onclick = () => {
  el("output-path").value = output;
  el("pick-folder").disabled = !pickerAvailable;
  formError("settings-error");
  el("settings-dialog").showModal();
};

el("pick-folder").onclick = async () => {
  const button = el("pick-folder");
  button.disabled = true;
  formError("settings-error");
  try {
    const data = await api("/pick", { out: el("output-path").value });
    if (data.out) el("output-path").value = data.out;
  } catch (error) {
    formError("settings-error", error.message);
  } finally {
    button.disabled = false;
  }
};

async function saveSettings(value) {
  formError("settings-error");
  const buttons = [el("save-output"), el("reset-output")];
  buttons.forEach(button => { button.disabled = true; });
  try {
    const data = await api("/settings", { out: value });
    output = data.out;
    el("output-path").value = output;
    el("settings-dialog").close();
    notify("保存目录已更新");
  } catch (error) {
    formError("settings-error", error.message);
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}

el("save-output").onclick = () => saveSettings(el("output-path").value);
el("reset-output").onclick = () => saveSettings("");
el("open-output").onclick = () => api("/openout", {}).catch(error => formError("settings-error", error.message));

let editFolderId = null, addAfterCreate = false;

function editFolder(folder, fromAdd = false) {
  editFolderId = folder?.id || null;
  addAfterCreate = fromAdd;
  el("folder-title").textContent = folder ? "重命名收藏夹" : "新建收藏夹";
  el("folder-name").value = folder?.name || "";
  formError("folder-error");
  el("folder-dialog").showModal();
  el("folder-name").focus();
}

function setFolders(data) {
  folders = data.folders;
  if (folderId && !currentFolder()) folderId = null;
  renderNavigation();
  applyFilters();
}

el("new-folder").onclick = () => editFolder();
el("rename-folder").onclick = () => editFolder(currentFolder());
el("add-new-folder").onclick = () => editFolder(null, true);
el("folder-form").onsubmit = async event => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    const data = await api("/collections", {
      action: editFolderId ? "rename" : "create",
      id: editFolderId,
      name: el("folder-name").value
    });
    setFolders(data);
    el("folder-dialog").close();
    if (addAfterCreate) renderFolderOptions(data.id);
    else if (!editFolderId) showFolder(data.id);
  } catch (error) {
    formError("folder-error", error.message);
  } finally {
    submit.disabled = false;
  }
};

el("delete-folder").onclick = async () => {
  const folder = currentFolder();
  if (!folder || !await confirmAction("删除收藏夹", "删除「" + folder.name + "」及其中的收藏记录？", "删除")) return;
  try {
    setFolders(await api("/collections", { action: "delete", id: folder.id }));
    notify("收藏夹已删除");
  } catch (error) { notify(error.message); }
};

function renderFolderOptions(checkedId) {
  const checked = new Set([...el("add-folders").querySelectorAll(":checked")].map(input => input.value));
  if (checkedId) checked.add(checkedId);
  const container = el("add-folders");
  container.replaceChildren();
  for (const folder of folders) {
    const label = element("label");
    const input = element("input");
    input.type = "checkbox";
    input.value = folder.id;
    input.checked = checked.has(folder.id);
    label.append(input, element("span", "", folder.name));
    container.append(label);
  }
  if (!folders.length) container.append(element("p", "subtle", "请先新建一个收藏夹。"));
  el("apply-folders").disabled = !folders.length;
}

el("add-to-folder").onclick = () => {
  el("add-folders").replaceChildren();
  formError("add-error");
  renderFolderOptions();
  el("add-dialog").showModal();
};

el("apply-folders").onclick = async () => {
  const choices = [...el("add-folders").querySelectorAll(":checked")];
  if (!choices.length) {
    formError("add-error", "请至少选择一个收藏夹");
    return;
  }
  const ids = [...selected];
  el("apply-folders").disabled = true;
  let changed = 0;
  try {
    for (const choice of choices) {
      setFolders(await api("/collections", { action: "add", id: choice.value, ids }));
      changed++;
    }
    el("add-dialog").close();
    notify("已加入 " + changed + " 个收藏夹");
  } catch (error) {
    formError("add-error", (changed ? "已加入 " + changed + " 个收藏夹 · " : "") + error.message);
  } finally {
    el("apply-folders").disabled = false;
  }
};

el("remove-from-folder").onclick = async () => {
  const folder = currentFolder();
  if (!folder) return;
  try {
    setFolders(await api("/collections", { action: "remove", id: folder.id, ids: [...selected] }));
    notify("已从「" + folder.name + "」移除选中的图片");
  } catch (error) { notify(error.message); }
};

const viewer = el("viewer"), viewerImage = el("viewer-image"), viewerStage = el("viewer-stage");
let viewerId = null, viewRequest = null, fullObjectUrl = "", viewerFocus = null;
let scale = 1, x = 0, y = 0, drag = null;

function paintViewerActions() {
  const item = byId.get(viewerId);
  if (!item) return;
  const position = filtered.findIndex(entry => entry.id === viewerId);
  el("previous-image").disabled = position <= 0;
  el("next-image").disabled = position < 0 || position >= filtered.length - 1;
  el("viewer-select").textContent = selected.has(viewerId) ? "取消选中" : "选中";
  const preview = el("viewer-preview");
  preview.hidden = !canLoadPreview(item);
  preview.disabled = busy || submitting;
  const save = el("viewer-save");
  save.textContent = saveLabel(item);
  save.hidden = item.missing && !item.downloadable;
  save.disabled = busy || submitting;
  const paired = byId.get(item.preview_id);
  const savePreview = el("viewer-save-preview");
  savePreview.hidden = !paired || (paired.missing && !paired.downloadable);
  savePreview.disabled = busy || submitting;
  savePreview.onclick = () => buttonTask("export", [paired.id]);
}

function transformViewer() {
  viewerImage.style.transform = "translate(" + x + "px," + y + "px) scale(" + scale + ")";
  el("zoom-reset").textContent = Math.round(scale * 100) + "%";
}

function zoom(factor) {
  scale = Math.max(.25, Math.min(8, scale * factor));
  transformViewer();
}

function resetZoom() {
  scale = 1;
  x = y = 0;
  transformViewer();
}

function resetViewerImage() {
  viewRequest?.abort();
  if (fullObjectUrl) URL.revokeObjectURL(fullObjectUrl);
  fullObjectUrl = "";
  viewerImage.removeAttribute("src");
  viewerImage.hidden = true;
  el("viewer-feedback").textContent = "";
}

function openViewer(id) {
  if (!byId.has(id)) return;
  if (!viewer.open) {
    viewerFocus = document.activeElement;
    viewer.showModal();
  }
  refreshViewer(id, true);
}

async function refreshViewer(id, reset) {
  const item = byId.get(id);
  if (!item) return;
  resetViewerImage();
  if (reset) resetZoom();
  viewerId = id;
  const source = previewSource(item);
  const label = item.kind === "gallery" || (source && source.id !== id) ? "游戏预览" :
    item.missing ? "缓存缩略图 · 原图待获取" : "原图";
  el("viewer-title").textContent = item.name;
  el("viewer-title").title = item.p;
  const meta = el("viewer-meta");
  meta.textContent = [label, itemSize(source || item)].filter(Boolean).join(" · ");
  viewerImage.alt = item.name;
  paintViewerActions();
  if (!source) {
    el("viewer-feedback").textContent = "暂无可用预览";
    return;
  }
  const request = new AbortController();
  viewRequest = request;
  const key = thumbKey(source);
  const thumb = thumbCache.get(key);
  viewerImage.src = thumb || assetUrl("thumb", source);
  viewerImage.hidden = false;
  if (source.missing) {
    el("viewer-feedback").textContent = "仅有缓存缩略图";
    return;
  }
  el("viewer-feedback").textContent = "正在载入大图…";
  try {
    const response = await fetch(assetUrl("full", source), { signal: request.signal });
    if (!response.ok) throw new Error(String(response.status));
    const blob = await response.blob();
    if (request.signal.aborted) return;
    fullObjectUrl = URL.createObjectURL(blob);
    viewerImage.src = fullObjectUrl;
    viewerImage.hidden = false;
    el("viewer-feedback").textContent = "";
  } catch (error) {
    if (!request.signal.aborted) el("viewer-feedback").textContent = "大图读取失败";
  }
}

function navigateViewer(offset) {
  const index = filtered.findIndex(item => item.id === viewerId);
  if (index >= 0 && filtered[index + offset]) refreshViewer(filtered[index + offset].id, true);
}

viewer.addEventListener("close", () => {
  viewerId = null;
  resetViewerImage();
  if (viewerFocus?.isConnected) viewerFocus.focus();
  viewerFocus = null;
});
viewer.addEventListener("keydown", event => {
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    navigateViewer(event.key === "ArrowLeft" ? -1 : 1);
    event.preventDefault();
  }
});
viewerImage.addEventListener("load", () => {
  if (!fullObjectUrl || viewerImage.src !== fullObjectUrl) return;
  const item = byId.get(viewerId), source = previewSource(item);
  if (!source) return;
  source.w = viewerImage.naturalWidth;
  source.h = viewerImage.naturalHeight;
  const label = item.kind === "gallery" || source.id !== item.id ? "游戏预览" : "原图";
  el("viewer-meta").textContent = label + " · " + itemSize(source);
});
viewerImage.addEventListener("error", () => {
  if (!fullObjectUrl) viewerImage.hidden = true;
});
el("previous-image").onclick = () => navigateViewer(-1);
el("next-image").onclick = () => navigateViewer(1);
el("viewer-select").onclick = () => updateSelection(selected.has(viewerId) ? "remove" : "add", [viewerId]);
el("viewer-preview").onclick = () => requestTask("preview", [viewerId], viewerId);
el("viewer-save").onclick = () => buttonTask("export", [viewerId]);
el("zoom-in").onclick = () => zoom(1.2);
el("zoom-out").onclick = () => zoom(1 / 1.2);
el("zoom-reset").onclick = resetZoom;
viewerStage.addEventListener("wheel", event => {
  if (viewerImage.hidden) return;
  event.preventDefault();
  zoom(event.deltaY < 0 ? 1.13 : 1 / 1.13);
}, { passive: false });
viewerImage.addEventListener("pointerdown", event => {
  if (event.button !== 0) return;
  viewerImage.setPointerCapture(event.pointerId);
  drag = { x: event.clientX - x, y: event.clientY - y };
});
viewerImage.addEventListener("pointermove", event => {
  if (!drag) return;
  x = event.clientX - drag.x;
  y = event.clientY - drag.y;
  transformViewer();
});
viewerImage.addEventListener("lostpointercapture", () => { drag = null; });

(async function start() {
  try {
    const [data] = await Promise.all([api("/state"), loadIndex()]);
    selected = new Set(data.ids);
    folders = data.folders;
    output = data.out;
    pickerAvailable = data.picker;
    renderNavigation();
    applyFilters();
    pollStatus();
  } catch (error) {
    el("empty").textContent = "图库加载失败 · " + error.message;
    el("empty").hidden = false;
  }
})();
