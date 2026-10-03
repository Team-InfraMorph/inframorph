"use strict";
const $ = (id) => document.getElementById(id);
const keyPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(png|jpg)$/i;
let selectedSample = null;
let uploadedURL = null;
let dialogSample = null;
function status(id, message, error = false) { $(id).textContent = message; $(id).dataset.error = String(error); }
async function request(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal, cache: "no-store" });
    if (!response.ok) throw new Error(response.status === 404 ? "찾을 수 없습니다. 확인키를 확인해 주세요." : `요청에 실패했습니다 (${response.status}). 입력을 확인하고 다시 시도하세요.`);
    return response;
  } catch (error) {
    if (error.name === "AbortError" || error instanceof TypeError) throw new Error("서버에 연결하지 못했습니다. 연결을 확인한 뒤 다시 시도하세요.");
    throw error;
  } finally { clearTimeout(timer); }
}
async function busy(button, action) { if (button.disabled) return; button.disabled = true; try { await action(); } finally { button.disabled = false; } }
async function health() {
  status("connection", "서버 확인 중");
  try { const data = await (await request("/health")).json(); if (data.status !== "ok") throw new Error("서버 응답을 확인할 수 없습니다."); status("connection", "● 서버 연결됨"); }
  catch (error) { status("connection", error.message, true); }
}
$("check-health").addEventListener("click", (event) => busy(event.currentTarget, health));
async function loadNotes() {
  const notes = await (await request("/api/notes")).json();
  if (!Array.isArray(notes)) throw new Error("메모 목록 응답을 확인할 수 없습니다.");
  const rows = notes.map((note) => { const row = document.createElement("li"); row.textContent = note.text; const time = document.createElement("time"); time.dateTime = note.createdAt; time.textContent = `#${note.id} · ${new Date(note.createdAt).toLocaleString()}`; row.append(time); return row; });
  if (!rows.length) { const row = document.createElement("li"); row.textContent = "아직 메모가 없어요. 첫 번째 생각을 남겨 보세요."; rows.push(row); }
  $("notes").replaceChildren(...rows);
}
$("note").addEventListener("input", () => { $("note-count").textContent = `${$("note").value.length} / 500`; });
$("note-form").addEventListener("submit", (event) => {
  event.preventDefault(); const button = event.currentTarget.querySelector("button");
  busy(button, async () => {
    const text = $("note").value; if (!text.trim() || text.length > 500) { status("note-status", "1~500자의 메모를 입력해 주세요.", true); return; }
    status("note-status", "메모를 저장하고 있습니다…");
    try { const note = await (await request("/api/notes", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) })).json();
      $("note").value = ""; $("note-count").textContent = "0 / 500";
      try { await loadNotes(); status("note-status", `메모 #${note.id} 저장 완료 · 서버 목록을 갱신했습니다.`); }
      catch { status("note-status", `메모 #${note.id}는 저장됐지만 목록을 가져오지 못했습니다. 새로고침해 주세요.`, true); }
    } catch (error) { status("note-status", error.message, true); }
  });
});
$("refresh-notes").addEventListener("click", (event) => busy(event.currentTarget, async () => { status("note-status", "목록을 확인하고 있습니다…"); try { await loadNotes(); status("note-status", "서버의 최신 목록입니다."); } catch (error) { status("note-status", error.message, true); } }));
$("image").addEventListener("change", () => { selectedSample = null; $("selection").textContent = $("image").files[0]?.name || "선택한 이미지가 없습니다."; });
async function validImage(blob) {
  if (!["image/png", "image/jpeg"].includes(blob.type) || !blob.size || blob.size > 5 * 1024 * 1024) throw new Error("PNG 또는 JPEG 파일을 5MB 이하로 선택해 주세요.");
  const bytes = new Uint8Array(await blob.slice(0, 8).arrayBuffer());
  const png = [137,80,78,71,13,10,26,10].every((b, i) => bytes[i] === b);
  const jpeg = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
  if (blob.type === "image/png" ? !png : !jpeg) throw new Error("파일 내용이 선택한 이미지 형식과 다릅니다.");
  // Decode as an image as well as checking its signature.
  const url = URL.createObjectURL(blob);
  try { await new Promise((resolve, reject) => { const img = new Image(); img.onload = resolve; img.onerror = () => reject(new Error("읽을 수 없는 이미지입니다.")); img.src = url; }); }
  finally { URL.revokeObjectURL(url); }
}
async function showStoredImage(key) {
  if (!keyPattern.test(key)) throw new Error("UUID 형식의 .png 또는 .jpg 이미지 확인키를 입력해 주세요.");
  const blob = await (await request(`/api/images/${encodeURIComponent(key)}`)).blob(); await validImage(blob);
  const url = URL.createObjectURL(blob); if (uploadedURL) URL.revokeObjectURL(uploadedURL); uploadedURL = url;
  $("uploaded").src = url; $("image-key").value = key; $("upload-result").hidden = false;
}
$("image-form").addEventListener("submit", (event) => {
  event.preventDefault(); busy(event.currentTarget.querySelector("button"), async () => {
    status("image-status", "이미지를 확인하고 있습니다…");
    let savedKey = null;
    try { const blob = selectedSample?.blob || $("image").files[0]; if (!blob) throw new Error("사진 또는 프로젝트 그림을 먼저 선택해 주세요."); await validImage(blob);
      status("image-status", "이미지를 서버에 업로드하고 있습니다…");
      const result = await (await request("/api/images", { method: "POST", headers: { "Content-Type": blob.type }, body: blob })).json();
      if (!keyPattern.test(result.key)) throw new Error("서버의 이미지 확인키 형식을 확인할 수 없습니다.");
      savedKey = result.key; $("lookup-key").value = savedKey; await showStoredImage(savedKey); status("image-status", "업로드 완료 · 서버에서 같은 이미지를 다시 확인했습니다.");
    } catch (error) { status("image-status", savedKey ? `저장됐지만 다시 열지 못했습니다. 아래 확인키로 다시 열어 주세요. ${error.message}` : error.message, true); }
  });
});
$("lookup-form").addEventListener("submit", (event) => { event.preventDefault(); busy(event.currentTarget.querySelector("button"), async () => { status("image-status", "서버에서 이미지를 찾고 있습니다…"); try { await showStoredImage($("lookup-key").value.trim()); status("image-status", "확인키로 서버의 이미지를 다시 불러왔습니다."); } catch (error) { status("image-status", error.message, true); } }); });
$("copy-key").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("image-key").value); status("image-status", "이미지 확인키를 복사했습니다."); } catch { $("image-key").focus(); $("image-key").select(); status("image-status", "확인키를 선택했습니다. 직접 복사해 주세요."); } });
function selectSample(sample) { selectedSample = sample; $("image").value = ""; $("selection").textContent = `프로젝트 그림 선택: ${sample.title}`; $("image-form").scrollIntoView({ behavior: "smooth", block: "center" }); $("image-form").querySelector("button").focus({ preventScroll: true }); }
$("close-dialog").addEventListener("click", () => $("image-dialog").close());
$("use-dialog-image").addEventListener("click", () => { $("image-dialog").close(); selectSample(dialogSample); });
async function gallery() {
  const ids = ["flow", "policy", "storage", "builder", "retention", "control-plane"];
  let failed = 0;
  for (const id of ids) {
    try {
      const asset = await (await request(`/assets/${id}.json`)).json();
      if (asset.id !== id || !["image/png", "image/jpeg"].includes(asset.mime)) throw new Error("invalid_asset");
      const bytes = Uint8Array.from(atob(asset.data), c => c.charCodeAt(0)); const blob = new Blob([bytes], { type: asset.mime }); await validImage(blob);
      const sample = { ...asset, blob, url: URL.createObjectURL(blob) };
      const card = document.createElement("article"); card.className = "gallery-card";
      const img = document.createElement("img"); img.src = sample.url; img.alt = sample.title; img.loading = "lazy";
      const copy = document.createElement("div"); copy.className = "copy";
      const kind = document.createElement("small"); kind.textContent = sample.kind === "capture" ? "실제 화면 캡처" : "설명 그림";
      const title = document.createElement("h3"); title.textContent = sample.title;
      const description = document.createElement("p"); description.textContent = sample.description;
      const expand = document.createElement("button"); expand.textContent = "확대 보기"; expand.setAttribute("aria-label", `${sample.title} 확대 보기`);
      expand.addEventListener("click", () => { dialogSample = sample; $("dialog-title").textContent = sample.title; $("dialog-image").src = sample.url; $("dialog-image").alt = sample.title; $("dialog-description").textContent = sample.description; $("image-dialog").showModal(); });
      const use = document.createElement("button"); use.textContent = "업로드에 사용"; use.setAttribute("aria-label", `${sample.title} 업로드에 사용`); use.addEventListener("click", () => selectSample(sample));
      copy.append(kind, title, description, expand, use); card.append(img, copy); $("gallery-grid").append(card);
    } catch { failed++; }
  }
  if (failed) status("gallery-status", `그림 ${failed}개를 불러오지 못했습니다. 화면을 새로고침해 주세요.`, true);
}
health(); loadNotes().catch((error) => { $("notes").replaceChildren(); status("note-status", error.message, true); }); gallery();
request("/edition.json").then(r => r.json()).then(data => { $("edition").textContent = data.label; }).catch(() => { $("edition").textContent = "구성 확인 불가"; });
