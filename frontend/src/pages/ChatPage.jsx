import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import ChatMessages from "../components/ChatMessages.jsx";

const CUSTOMER_PHONE = "08012345678";   // the customer this chat is sent from

export default function ChatPage() {
  const phone = CUSTOMER_PHONE;
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api
      .messages(phone)
      .then((c) => {
        setMessages(c.messages);
        setError("");
      })
      .catch((e) => setError(e.message));
  }, [phone]);

  useEffect(() => {
    load();
  }, [load]);

  async function restart() {
    if (sending || !confirm("Start a new chat?")) return;
    try {
      await api.clearChat(phone);   // deletes only this chat's messages + state
      setMessages([]);
      setInput("");
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }

  async function send(text) {
    const msg = (text ?? input).trim();
    if (!msg || sending) return;
    setSending(true);
    setInput("");
    setMessages((m) => [
      ...m,
      { id: `tmp-${Date.now()}`, sender: "customer", text: msg, created_at: new Date().toISOString() },
    ]);
    try {
      await api.chat(phone, msg);
    } catch (e) {
      setError(e.message);
    } finally {
      setSending(false);
      load();
    }
  }

  return (
    <div className="chat-window">
      <header className="chat-header">
        <div className="avatar" aria-hidden="true">TD</div>
        <div className="chat-title">
          <div className="chat-name">TD Africa</div>
          <div className="chat-status">{sending ? "typing…" : "online"}</div>
        </div>
        <button type="button" className="restart-btn" onClick={restart} disabled={sending}>
          Restart
        </button>
      </header>

      <ChatMessages messages={messages} onQuickReply={send} />

      {error && <div className="error-bar">⚠ {error}. Is the backend running on port 8000?</div>}

      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(); }}>
        <label htmlFor="msg" className="sr-only">Type a message</label>
        <input id="msg" value={input} onChange={(e) => setInput(e.target.value)} placeholder="Type a message"
          autoComplete="off" />
        <button type="submit" className="send" aria-label="Send" disabled={sending || !input.trim()}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
            strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M22 2 11 13" />
            <path d="M22 2 15 22l-4-9-9-4 20-7z" />
          </svg>
        </button>
      </form>
    </div>
  );
}
