import { useEffect, useRef } from "react";
import { naira, timeOf } from "../api.js";

/**
 * WhatsApp-style message list.
 * Customer messages on the right; every reply on the left, labelled "TD Africa Assistant".
 * The latest order summary gets YES / CHANGE buttons, and the latest product list gets option buttons.
 */
export default function ChatMessages({ messages, onQuickReply }) {
  const endRef = useRef(null);
  const visible = messages.filter((m) => m.sender !== "system");   // internal notes are not shown

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [visible.length]);

  const lastId = visible.length ? visible[visible.length - 1].id : null;

  return (
    <div className="chat-scroll">
      {!visible.length && (
        <div className="chat-empty">Say hello, or try "I want 10 laptops".</div>
      )}
      {visible.map((m) => {
        if (m.sender === "customer") {
          return (
            <div key={m.id} className="bubble out">
              <div className="bubble-text">{m.text}</div>
              <div className="bubble-time">{timeOf(m.created_at)}</div>
            </div>
          );
        }
        const meta = m.meta || {};
        const isActive = m.id === lastId;   // buttons only on the newest reply
        return (
          <div key={m.id} className={`bubble in ${meta.type === "order_summary" ? "wide" : ""}`}>
            <div className="bubble-label">TD Africa Assistant</div>

            {meta.type === "order_summary" ? (
              <OrderSummary meta={meta} active={isActive} onQuickReply={onQuickReply} />
            ) : (
              <div className={`bubble-text ${meta.type === "order_placed" ? "placed" : ""}`}>{m.text}</div>
            )}

            {meta.type === "choices" && isActive && (
              <div className="choice-row">
                {meta.options.map((o) => (
                  <button key={o.n} type="button" className="btn-outline" onClick={() => onQuickReply(String(o.n))}>
                    {o.n}. {o.name}
                  </button>
                ))}
              </div>
            )}
            <div className="bubble-time">{timeOf(m.created_at)}</div>
          </div>
        );
      })}
      <div ref={endRef} />
    </div>
  );
}

function OrderSummary({ meta, active, onQuickReply }) {
  return (
    <div className="summary">
      <div className="summary-title">Order summary</div>
      {meta.items.map((it) => (
        <div key={it.product_id} className="summary-row">
          <span>
            {it.name} <span className="muted">× {it.quantity}</span>
          </span>
          <span>{naira(it.line_total)}</span>
        </div>
      ))}
      <div className="summary-line" />
      <div className="summary-row total">
        <span>Total{meta.discount_percent ? ` (−${meta.discount_percent}%)` : ""}</span>
        <span>{naira(meta.total)}</span>
      </div>
      {active ? (
        <div className="summary-actions">
          <button type="button" className="btn" onClick={() => onQuickReply("YES")}>
            YES, confirm
          </button>
          <button type="button" className="btn-outline" onClick={() => onQuickReply("CHANGE")}>
            CHANGE
          </button>
        </div>
      ) : (
        <div className="muted small-text">Reply YES to confirm or CHANGE to modify.</div>
      )}
    </div>
  );
}
