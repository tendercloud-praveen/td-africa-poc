from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Boolean, Text
from sqlalchemy.orm import relationship
from app.db.database import Base


class Salesperson(Base):
    __tablename__ = "salespeople"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    phone = Column(String(30), unique=True)

    customers = relationship("Customer", back_populates="salesperson")


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    phone = Column(String(30), unique=True, index=True, nullable=False)
    contact_name = Column(String(100))
    shop_name = Column(String(150))
    credit_limit = Column(Float, default=0)
    credit_used = Column(Float, default=0)
    can_order = Column(Boolean, default=True)
    salesperson_id = Column(Integer, ForeignKey("salespeople.id"))

    salesperson = relationship("Salesperson", back_populates="customers")
    orders = relationship("Order", back_populates="customer")


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    sku = Column(String(60), unique=True, index=True)
    name = Column(String(200), nullable=False)
    brand = Column(String(60))
    category = Column(String(60))
    nicknames = Column(String(500))          # "hp 15, hp laptop"
    price = Column(Float, nullable=False)
    stock = Column(Integer, default=0)

    embedding = relationship("ProductEmbedding", back_populates="product", uselist=False,
                             cascade="all, delete-orphan")


class ProductEmbedding(Base):
    """RAG: one embedding vector per product, stored as a JSON float array (no pgvector needed)."""
    __tablename__ = "product_embeddings"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    model = Column(String(120), nullable=False)    # e.g. "ollama:nomic-embed-text" (change model = recompute)
    source_text = Column(Text, nullable=False)     # the product text that was embedded (text changed = recompute)
    vector = Column(Text, nullable=False)          # JSON list of floats
    dimensions = Column(Integer)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    product = relationship("Product", back_populates="embedding")


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    order_code = Column(String(30), unique=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    status = Column(String(30), default="confirmed")
    total = Column(Float, default=0)
    discount_percent = Column(Float, default=0)
    path = Column(String(5))                 # A = auto, B = assisted, C = salesperson
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="orders")
    items = relationship("OrderItem", back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    product_id = Column(Integer, ForeignKey("products.id"))
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)

    order = relationship("Order", back_populates="items")
    product = relationship("Product")


class Alert(Base):
    """'Help needed' cards for the salesperson, plus 'order placed' notices."""
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    customer_phone = Column(String(30), index=True)
    salesperson_id = Column(Integer, ForeignKey("salespeople.id"), nullable=True)
    kind = Column(String(20), default="handoff")   # handoff / notice
    reason = Column(String(40))                    # discount / big_order / credit / ... / order_placed
    message = Column(Text)                         # what the customer said, or the notice text
    details = Column(Text)                         # JSON: draft order etc.
    status = Column(String(20), default="open")    # open / resolved
    created_at = Column(DateTime, default=datetime.utcnow)

    salesperson = relationship("Salesperson")


class Message(Base):
    """Every chat message: customer, ai, salesperson or system."""
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    customer_phone = Column(String(30), index=True)
    sender = Column(String(20))           # customer / ai / salesperson / system
    sender_name = Column(String(100))
    text = Column(Text)
    meta = Column(Text)                   # JSON: order summary card etc.
    created_at = Column(DateTime, default=datetime.utcnow)


class ConversationState(Base):
    """Where each chat is in the order flow, and who is talking (AI or human)."""
    __tablename__ = "conversation_state"
    id = Column(Integer, primary_key=True)
    customer_phone = Column(String(30), unique=True, index=True)
    mode = Column(String(10), default="ai")        # ai / human
    stage = Column(String(30), default="idle")
    data = Column(Text, default="{}")              # JSON
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
