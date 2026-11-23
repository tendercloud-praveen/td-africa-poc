"""Demo data so the POC works straight away. All prices are TEST values."""
from sqlalchemy.orm import Session
from app.models.tables import Salesperson, Customer, Product, Order, OrderItem

PRODUCTS = [
    # sku, name, brand, category, nicknames, price, stock
    ("HP-250-G9", "HP 250 G9 Notebook", "HP", "Laptop", "hp 15, hp laptop, hp 250, hp notebook", 450000, 2000),
    ("HP-PROBOOK-450", "HP ProBook 450 G10", "HP", "Laptop", "probook, hp probook, hp 450", 780000, 30),
    ("DELL-3520", "Dell Inspiron 3520", "Dell", "Laptop", "dell laptop, dell 15, inspiron", 520000, 40),
    ("LEN-V15", "Lenovo V15 G4", "Lenovo", "Laptop", "lenovo laptop, lenovo v15, v15", 410000, 25),
    ("HP-LJ-M111W", "HP LaserJet M111w Printer", "HP", "Printer", "hp printer, laserjet, small printer", 150000, 15),
    ("EPSON-L3250", "Epson EcoTank L3250 Printer", "Epson", "Printer", "epson printer, ecotank, ink tank printer", 210000, 18),
    ("HP-65W-ADP", "HP 65W Laptop Charger", "HP", "Accessory", "charger, hp charger, adapter, laptop charger", 25000, 200),
    ("LOGI-M185", "Logitech M185 Wireless Mouse", "Logitech", "Accessory", "mouse, wireless mouse, logitech mouse", 9500, 300),
    ("SANDISK-64", "SanDisk Ultra 64GB Flash Drive", "SanDisk", "Storage", "flash, flash drive, usb, pendrive, 64gb", 7000, 500),
    ("APC-650", "APC Back-UPS 650VA", "APC", "Power", "ups, apc ups, inverter ups", 95000, 35),
]


def seed_if_empty(db: Session) -> bool:
    if db.query(Product).count() > 0:
        return False
    seed(db)
    return True


def seed(db: Session):
    chidi = Salesperson(name="Chidi", phone="2348011111111")
    amaka = Salesperson(name="Amaka", phone="2348022222222")
    db.add_all([chidi, amaka])
    db.flush()

    tunde = Customer(phone="08012345678", contact_name="Tunde", shop_name="ABC Stores, Ikeja",
                     credit_limit=50_000_000, credit_used=2_000_000, salesperson_id=chidi.id)
    bola = Customer(phone="08098765432", contact_name="Bola", shop_name="Bola Computers, Yaba",
                    credit_limit=2_000_000, credit_used=1_800_000, salesperson_id=amaka.id)
    emeka = Customer(phone="08055555555", contact_name="Emeka", shop_name="Emeka Tech, Onitsha",
                     credit_limit=5_000_000, credit_used=0, can_order=False, salesperson_id=chidi.id)
    db.add_all([tunde, bola, emeka])

    products = [Product(sku=s, name=n, brand=b, category=c, nicknames=nk, price=p, stock=st)
                for s, n, b, c, nk, p, st in PRODUCTS]
    db.add_all(products)
    db.flush()

    # One old order for Tunde, so "same as last time" works in the demo
    hp = products[0]
    old = Order(order_code="ORD-1000", customer_id=tunde.id, status="delivered",
                total=hp.price * 5, path="A")
    db.add(old)
    db.flush()
    db.add(OrderItem(order_id=old.id, product_id=hp.id, quantity=5, unit_price=hp.price))
    db.commit()
