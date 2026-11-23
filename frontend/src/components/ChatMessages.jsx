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
      <div className="summary-title">Quotation</div>
      {meta.items.map((it) => (
        <div key={it.product_id}>
          <div className="summary-row">
            <span>
              {it.name} <span className="muted">× {it.quantity}</span>
            </span>
            <span>{naira(it.line_total)}</span>
          </div>
          {it.stock != null && (
            <div className="muted small-text">
              Unit price {naira(it.unit_price)} · Available stock {Number(it.stock).toLocaleString("en-NG")}
            </div>
          )}
        </div>
      ))}
      <div className="summary-line" />
      <div className="summary-row total">
        <span>Quotation total{meta.discount_percent ? ` (−${meta.discount_percent}%)` : ""}</span>
        <span>{naira(meta.total)}</span>
      </div>
      {active ? (
        <>
          <div className="small-text">Your quotation is ready. Would you like to proceed with this order?</div>
          <div className="summary-actions">
            <button type="button" className="btn" onClick={() => onQuickReply("Yes")}>
              1. Yes, Place Order
            </button>
            <button type="button" className="btn-outline" onClick={() => onQuickReply("Edit Order")}>
              2. Edit Order
            </button>
            <button type="button" className="btn-outline" onClick={() => onQuickReply("Cancel Order")}>
              3. Cancel Order
            </button>
          </div>
        </>
      ) : (
        <div className="muted small-text">Reply 1 (Yes, Place Order), 2 (Edit Order) or 3 (Cancel Order).</div>
      )}
    </div>
  );
}
