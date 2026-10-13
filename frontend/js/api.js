export async function apiGet(path) {
  let response;
  try {
    response = await fetch(path, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
  } catch {
    throw new Error("Panele ulaşılamadı.");
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (response.status === 401) {
    document.dispatchEvent(new CustomEvent("aft-unauthorized"));
  }
  if (!response.ok) {
    const message = payload && typeof payload.error === "string" ? payload.error : "Veri alınamadı.";
    throw new Error(message);
  }
  if (!payload || typeof payload !== "object") {
    throw new Error("Veri alınamadı.");
  }
  return payload;
}

export async function apiPost(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    throw new Error("Panele ulaşılamadı.");
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const message = payload && typeof payload.error === "string" ? payload.error : "Giriş yapılamadı.";
    throw new Error(message);
  }
  return payload;
}
