// All calls go to /api/... (Vite forwards them to FastAPI on port 8000)
const BASE = "/api";

async function req(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : msg;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  return res.json();
}

export const api = {
  chat: (phone, message) =>
    req("/chat", { method: "POST", body: JSON.stringify({ customer_phone: phone, message }) }),
  messages: (phone) => req(`/conversations/${encodeURIComponent(phone)}/messages`),
  clearChat: (phone) => req(`/conversations/${encodeURIComponent(phone)}`, { method: "DELETE" }),
};

export const naira = (n) => "₦" + Math.round(Number(n) || 0).toLocaleString("en-NG");

export const timeOf = (iso) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
