"""Generate the sample company "Acme Outdoor Gear": a 40-person outdoor-equipment wholesaler in New Zealand.

Every source is generated from one consistent world, so the same customer, supplier or employee appears across the
CRM, orders, shipments, support tickets, meeting notes and the org chart, often under a different spelling. The
world object is also the ground truth for the evaluation: it knows which mention is which real entity and which
source row proves each fact.

    python -m company_brain.sample.generate data/acme
"""

from __future__ import annotations

import csv
import io
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from company_brain.sample.pdf import write_pdf

SEED = 7
START = date(2026, 4, 1)
END = date(2026, 10, 15)


@dataclass
class Employee:
    id: str
    name: str
    email: str
    title: str
    team: str
    location: str
    manager: str | None = None  # employee id
    rec: int = 0  # record number in org/org_chart.json


@dataclass
class Customer:
    id: str
    name: str
    segment: str
    region: str
    since: str
    manager: str  # employee id
    rows: list[int] = field(default_factory=list)  # rows in crm/customers.csv (more than one when duplicated)
    crm_ids: list[str] = field(default_factory=list)


@dataclass
class Contact:
    id: str
    name: str
    email: str
    role: str
    customer: str
    row: int = 0


@dataclass
class Supplier:
    id: str
    name: str
    country: str
    status: str
    row: int = 0


@dataclass
class Product:
    sku: str
    name: str
    category: str
    price: float
    supplier: str
    row: int = 0


@dataclass
class Line:
    sku: str
    qty: int
    total: float
    row: int = 0


@dataclass
class Order:
    id: str
    customer: str  # canonical customer id
    crm_id: str  # the CRM id written in the file (a duplicate account for some orders)
    date: str
    status: str
    lines: list[Line]
    row: int = 0

    @property
    def total(self) -> float:
        return round(sum(li.total for li in self.lines), 2)


@dataclass
class Shipment:
    id: str
    supplier: str
    supplier_text: str
    sku: str
    qty: int
    expected: str
    arrived: str
    status: str
    days_late: int
    row: int = 0


@dataclass
class Ticket:
    id: str
    opened: str
    status: str
    category: str
    priority: str
    customer: str
    company_text: str
    contact: str
    requester_name: str
    requester_email: str
    assignee: str | None  # employee id (None when the mention is ambiguous on purpose)
    assignee_text: str
    order: str | None
    product: str | None
    subject: str
    body: str
    rec: int = 0


@dataclass
class Decision:
    id: str
    text: str
    meeting: str
    line: int
    context_line: int | None
    supplier: str | None = None
    product: str | None = None
    policy: str | None = None
    customer: str | None = None
    owner: str | None = None
    owner_line: int | None = None


@dataclass
class Meeting:
    id: str
    title: str
    date: str
    file: str
    attendees: list[str]
    attendee_line: int
    decisions: list[Decision] = field(default_factory=list)


@dataclass
class Policy:
    id: str
    name: str
    file: str
    owner_team: str
    owner_line: int  # line (md) or page (pdf)
    effective: str
    attrs: dict[str, tuple[float, int]] = field(default_factory=dict)  # prop -> (value, line or page)


@dataclass
class Mention:
    """One place where an entity is named in a source, with the entity it really is."""

    file: str
    pos: int
    field: str
    type: str
    text: str
    true_id: str


@dataclass
class World:
    employees: dict[str, Employee] = field(default_factory=dict)
    teams: dict[str, list[str]] = field(default_factory=dict)
    customers: dict[str, Customer] = field(default_factory=dict)
    contacts: dict[str, Contact] = field(default_factory=dict)
    suppliers: dict[str, Supplier] = field(default_factory=dict)
    products: dict[str, Product] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    shipments: dict[str, Shipment] = field(default_factory=dict)
    tickets: dict[str, Ticket] = field(default_factory=dict)
    meetings: dict[str, Meeting] = field(default_factory=dict)
    policies: dict[str, Policy] = field(default_factory=dict)
    mentions: list[Mention] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------------------------
# The cast

TEAMS = ["Leadership", "Sales", "Customer Support", "Operations", "Purchasing", "Finance", "Marketing"]

# (name, title, team, location, manager name)
STAFF: list[tuple[str, str, str, str, str | None]] = [
    ("Claire Fraser", "Managing Director", "Leadership", "Auckland", None),
    ("Mark Reid", "Head of Sales", "Leadership", "Auckland", "Claire Fraser"),
    ("Tom Baker", "Operations Manager", "Leadership", "Christchurch", "Claire Fraser"),
    ("James Walker", "Account Manager", "Sales", "Auckland", "Mark Reid"),
    ("Hannah Moore", "Account Manager", "Sales", "Auckland", "Mark Reid"),
    ("Ben Turner", "Account Manager", "Sales", "Christchurch", "Mark Reid"),
    ("Chloe Bell", "Account Manager", "Sales", "Auckland", "Mark Reid"),
    ("Daniel Hughes", "Sales Coordinator", "Sales", "Auckland", "Mark Reid"),
    ("Grace Cooper", "Account Manager", "Sales", "Christchurch", "Mark Reid"),
    ("Luke Ward", "Sales Coordinator", "Sales", "Auckland", "Mark Reid"),
    ("Sarah Clarke", "Support Lead", "Customer Support", "Auckland", "Tom Baker"),
    ("Emma Wilson", "Support Specialist", "Customer Support", "Auckland", "Sarah Clarke"),
    ("Ryan Young", "Support Specialist", "Customer Support", "Auckland", "Sarah Clarke"),
    ("Megan King", "Support Specialist", "Customer Support", "Christchurch", "Sarah Clarke"),
    ("Sophie Wright", "Support Specialist", "Customer Support", "Auckland", "Sarah Clarke"),
    ("Nathan Green", "Returns Coordinator", "Customer Support", "Christchurch", "Sarah Clarke"),
    ("Amy Morris", "Support Specialist", "Customer Support", "Auckland", "Sarah Clarke"),
    ("Josh Ellis", "Support Specialist", "Customer Support", "Christchurch", "Sarah Clarke"),
    ("Simon Clarke", "Warehouse Supervisor", "Operations", "Christchurch", "Tom Baker"),
    ("Kate Palmer", "Logistics Coordinator", "Operations", "Christchurch", "Tom Baker"),
    ("Matt Lane", "Warehouse Operator", "Operations", "Christchurch", "Simon Clarke"),
    ("Ella Shaw", "Warehouse Operator", "Operations", "Christchurch", "Simon Clarke"),
    ("Adam Price", "Warehouse Operator", "Operations", "Christchurch", "Simon Clarke"),
    ("Rachel Fox", "Inventory Planner", "Operations", "Christchurch", "Tom Baker"),
    ("Chris Mason", "Warehouse Operator", "Operations", "Christchurch", "Simon Clarke"),
    ("Holly Knight", "Quality Inspector", "Operations", "Christchurch", "Tom Baker"),
    ("Peter Gibson", "Dispatch Lead", "Operations", "Christchurch", "Simon Clarke"),
    ("Laura Marsh", "Warehouse Operator", "Operations", "Christchurch", "Simon Clarke"),
    ("Olivia Grant", "Purchasing Manager", "Purchasing", "Auckland", "Tom Baker"),
    ("Zoe Dixon", "Buyer", "Purchasing", "Auckland", "Olivia Grant"),
    ("Michael Barker", "Buyer", "Purchasing", "Auckland", "Olivia Grant"),
    ("Anna Holmes", "Supplier Quality Analyst", "Purchasing", "Christchurch", "Olivia Grant"),
    ("James Hall", "Finance Manager", "Finance", "Auckland", "Claire Fraser"),
    ("David Lloyd", "Accountant", "Finance", "Auckland", "James Hall"),
    ("Jess Murray", "Accounts Receivable Officer", "Finance", "Auckland", "James Hall"),
    ("Andrew Hunt", "Payroll Officer", "Finance", "Auckland", "James Hall"),
    ("Rebecca Webb", "Marketing Manager", "Marketing", "Auckland", "Claire Fraser"),
    ("Paul Ford", "Content Producer", "Marketing", "Auckland", "Rebecca Webb"),
    ("Isla Taylor", "Digital Marketer", "Marketing", "Auckland", "Rebecca Webb"),
    ("Harry Brown", "Graphic Designer", "Marketing", "Auckland", "Rebecca Webb"),
]

ACCOUNT_MANAGERS = ["James Walker", "Hannah Moore", "Ben Turner", "Chloe Bell", "Grace Cooper"]
SUPPORT_AGENTS = ["Emma Wilson", "Ryan Young", "Megan King", "Sophie Wright", "Nathan Green", "Amy Morris", "Josh Ellis", "Sarah Clarke"]

CUSTOMER_NAMES = [
    "Summit Trail Outfitters",
    "Kea Kids Club",
    "Harbourside Adventure Store",
    "Riverstone Outdoors",
    "Riverstone Outdoor Club",
    "Glacier Bay Supplies",
    "Tasman Tramping Club",
    "Ironsand Surf and Trail",
    "Pinecrest High School",
    "Totara Valley Scouts",
    "Coastal Edge Sports",
    "Mountain Mile Gear",
    "Waypoint Online",
    "Kowhai Camping Co",
    "Lakeside Leisure Shop",
    "Bluff Road Outdoors",
    "Northern Ridge Alpine Club",
    "Driftwood Kayak Hire",
    "Red Tussock Retail",
    "Silver Fern Gear Hub",
    "Fiordland Expeditions Store",
    "Cape Light Camping",
    "Granite Peak Outfitters",
    "Westland Wanderers",
    "Harbour City Climbing",
    "Rimu Grove College",
    "Basecamp Direct",
    "Matai Street Sports",
    "Southern Lakes Trading",
    "Pohutukawa Paddlers",
    "Clearwater Outdoor Centre",
    "High Country Hikers",
    "Sandspit Surf Shop",
    "Moa Point Adventure",
    "Twin Rivers School",
    "Trailhead Collective",
    "Kaikoura Coast Outdoors",
    "Eastgate Camping World",
    "Hilltop Harriers",
    "Cloudline Online",
]
SEGMENT_BY_WORD = {
    "Club": "club",
    "Scouts": "club",
    "Harriers": "club",
    "Paddlers": "club",
    "Wanderers": "club",
    "Hikers": "club",
    "School": "school",
    "College": "school",
    "Online": "online reseller",
    "Direct": "online reseller",
    "Collective": "online reseller",
}
REGIONS = ["Auckland", "Wellington", "Canterbury", "Otago", "Waikato", "Bay of Plenty"]

FIRST = [
    "John",
    "Jennifer",
    "Kevin",
    "Natalie",
    "Scott",
    "Fiona",
    "Greg",
    "Helen",
    "Ian",
    "Karen",
    "Liam",
    "Nicole",
    "Owen",
    "Phoebe",
    "Rob",
    "Tessa",
    "Victoria",
    "Will",
    "Alice",
    "Lucy",
    "Sam",
    "Jack",
    "Mia",
    "Oscar",
    "Ruby",
    "Hugo",
    "Leah",
    "Noah",
    "Ivy",
    "Felix",
    "Eve",
    "Toby",
    "Freya",
    "Max",
    "Elise",
    "Jake",
    "Rose",
    "Caleb",
    "Abby",
    "Dylan",
]
LAST = [
    "Smith",
    "Carter",
    "Bennett",
    "Hayes",
    "Doyle",
    "Pearce",
    "Sutton",
    "Rowe",
    "Fletcher",
    "Barnes",
    "Watts",
    "Hammond",
    "Lowe",
    "Burke",
    "Parsons",
    "Chambers",
    "Holt",
    "Nash",
    "Quinn",
    "Read",
    "Stone",
    "Vaughan",
    "Booth",
    "Harding",
    "Lamb",
    "Norris",
    "Payne",
    "Riley",
    "Tucker",
    "Wade",
    "Ashby",
    "Blake",
    "Cross",
    "Dunn",
    "Fowler",
    "Gray",
    "Hart",
    "Kemp",
    "Moss",
    "Page",
]
ROLES = ["Owner", "Store Manager", "Buyer", "Club Secretary", "Head of Outdoor Education", "Operations Lead"]

SUPPLIERS = [
    ("Ridgeback Textiles Ltd", "China"),
    ("Alpine Loom Co", "New Zealand"),
    ("Tussock Wool Mills", "New Zealand"),
    ("Northfold Packs Ltd", "Vietnam"),
    ("Bluegum Down Supply", "Australia"),
    ("Coastline Polymers", "Taiwan"),
    ("Kiwi Canvas Works", "New Zealand"),
    ("Ember Cookware Ltd", "China"),
]
SUPPLIER_VARIANTS = {
    "Ridgeback Textiles Ltd": ["Ridgeback Textiles Limited", "RIDGEBACK TEXTILES", "Ridgeback Textlies Ltd"],
    "Northfold Packs Ltd": ["Northfold Packs", "Northfold Packs Limited"],
    "Ember Cookware Ltd": ["Ember Cookware", "EMBER COOKWARE LTD"],
    "Alpine Loom Co": ["Alpine Loom Company", "Alpine Loom Co."],
    "Bluegum Down Supply": ["Bluegum Down Supplies"],
}

# (name, category, price, supplier name)
PRODUCTS = [
    ("Ridgeline 2P Tent", "tents", 489.0, "Ridgeback Textiles Ltd"),
    ("Ridgeline 3P Tent", "tents", 629.0, "Ridgeback Textiles Ltd"),
    ("Storm Shelter Fly", "tents", 149.0, "Ridgeback Textiles Ltd"),
    ("Tarn Rain Shell", "apparel", 259.0, "Ridgeback Textiles Ltd"),
    ("Basin Hiking Pants", "apparel", 119.0, "Ridgeback Textiles Ltd"),
    ("Kepler Tent Poles", "tents", 89.0, "Alpine Loom Co"),
    ("Alpine Base Layer", "apparel", 99.0, "Alpine Loom Co"),
    ("Merino Hiking Socks", "apparel", 29.0, "Tussock Wool Mills"),
    ("Merino Beanie", "apparel", 39.0, "Tussock Wool Mills"),
    ("Merino Midlayer", "apparel", 169.0, "Tussock Wool Mills"),
    ("Trailhead 45L Pack", "packs", 219.0, "Northfold Packs Ltd"),
    ("Trailhead 65L Pack", "packs", 289.0, "Northfold Packs Ltd"),
    ("Daytripper 22L Pack", "packs", 109.0, "Northfold Packs Ltd"),
    ("Summit Hydration Pack", "packs", 139.0, "Northfold Packs Ltd"),
    ("Kids Explorer Pack", "packs", 69.0, "Northfold Packs Ltd"),
    ("Aurora Down Bag -5C", "sleeping", 399.0, "Bluegum Down Supply"),
    ("Aurora Down Bag 0C", "sleeping", 329.0, "Bluegum Down Supply"),
    ("Down Hut Booties", "footwear", 79.0, "Bluegum Down Supply"),
    ("Cloudrest Sleeping Mat", "sleeping", 149.0, "Coastline Polymers"),
    ("Inflatable Camp Pillow", "sleeping", 35.0, "Coastline Polymers"),
    ("Trail Runner Gaiters", "footwear", 49.0, "Coastline Polymers"),
    ("Stream Crossing Sandals", "footwear", 89.0, "Coastline Polymers"),
    ("Canvas Bell Tent", "tents", 899.0, "Kiwi Canvas Works"),
    ("Canvas Gear Bag", "packs", 79.0, "Kiwi Canvas Works"),
    ("Camp Kitchen Tarp", "tents", 129.0, "Kiwi Canvas Works"),
    ("Ember Camp Stove", "cooking", 119.0, "Ember Cookware Ltd"),
    ("Ember Titanium Pot", "cooking", 79.0, "Ember Cookware Ltd"),
    ("Ember Mess Kit", "cooking", 49.0, "Ember Cookware Ltd"),
    ("Ember Fuel Canister", "cooking", 12.0, "Ember Cookware Ltd"),
    ("Ember Folding Spork", "cooking", 9.0, "Ember Cookware Ltd"),
]
CATEGORY_CODE = {"tents": "TEN", "packs": "PAK", "sleeping": "SLP", "apparel": "APP", "footwear": "FTW", "cooking": "COK"}


def slug_email(name: str, domain: str) -> str:
    return name.lower().replace(" ", ".").replace("'", "") + "@" + domain


def company_domain(name: str) -> str:
    words = [w for w in name.lower().replace(" and ", " ").split() if w not in {"co", "the"}]
    return "".join(words[:2]) + ".example"


def build_world(seed: int = SEED) -> World:  # noqa: C901 - one linear script that tells the world's story
    rnd = random.Random(seed)
    w = World()

    # ----- employees and teams -----
    by_name: dict[str, str] = {}
    for i, (name, title, team, loc, _) in enumerate(STAFF, start=1):
        eid = f"E{i:03d}"
        by_name[name] = eid
        w.employees[eid] = Employee(eid, name, slug_email(name, "acmeoutdoor.example"), title, team, loc)
        w.teams.setdefault(team, []).append(eid)
    for name, *_rest in STAFF:
        mgr = _rest[3]
        if mgr:
            w.employees[by_name[name]].manager = by_name[mgr]
    E = by_name  # shorthand: name -> id

    # ----- suppliers and products -----
    sup_by_name: dict[str, str] = {}
    for i, (name, country) in enumerate(SUPPLIERS, start=1):
        sid = f"S{i:02d}"
        sup_by_name[name] = sid
        w.suppliers[sid] = Supplier(sid, name, country, "active", row=i)
        w.mentions.append(Mention("ops/suppliers.csv", i, "supplier_name", "Supplier", name, sid))
    counters: dict[str, int] = {}
    for i, (name, cat, price, sup) in enumerate(PRODUCTS, start=1):
        code = CATEGORY_CODE[cat]
        counters[code] = counters.get(code, 0) + 1
        sku = f"ACM-{code}-{counters[code]:03d}"
        w.products[sku] = Product(sku, name, cat, price, sup_by_name[sup], row=i)
    prod_by_name = {p.name: p.sku for p in w.products.values()}

    # ----- customers (with one planted duplicate CRM account) and contacts -----
    for i, name in enumerate(CUSTOMER_NAMES, start=1):
        cid = f"C{i:03d}"
        seg = next((v for k, v in SEGMENT_BY_WORD.items() if k in name), "retail store")
        since = (date(2019, 1, 1) + timedelta(days=rnd.randrange(0, 2400))).isoformat()
        w.customers[cid] = Customer(cid, name, seg, rnd.choice(REGIONS), since, E[ACCOUNT_MANAGERS[(i - 1) % len(ACCOUNT_MANAGERS)]])
    w.customers["C001"].region = "Canterbury"
    w.customers["C004"].region = "Otago"
    w.customers["C005"].region = "Otago"  # Riverstone Outdoors and Riverstone Outdoor Club: different customers, similar names

    crm_rows: list[tuple[str, str, Customer]] = []  # (crm id, name as written, customer)
    for c in w.customers.values():
        crm_rows.append((c.id, c.name, c))
    # Duplicate CRM accounts, the way they appear after a sales system migration
    crm_rows.append(("C041", "Summit Trail Outfitters Ltd", w.customers["C001"]))
    crm_rows.append(("C042", "Glacier Bay Supplies Limited", w.customers["C006"]))
    crm_rows.append(("C043", "Kowhai Camping Company", w.customers["C014"]))
    for row, (crm_id, written, c) in enumerate(crm_rows, start=1):
        c.rows.append(row)
        c.crm_ids.append(crm_id)
        w.mentions.append(Mention("crm/customers.csv", row, "company", "Customer", written, c.id))
        w.mentions.append(Mention("crm/customers.csv", row, "account_manager_email", "Employee", w.employees[c.manager].email, c.manager))

    used_names: set[str] = set()
    ci = 0
    for c in w.customers.values():
        for _ in range(rnd.choice([1, 2, 2, 3])):
            while True:
                name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
                if name not in used_names:
                    used_names.add(name)
                    break
            if c.id == "C001" and ci == 0:
                name = "John Smith"
            ci += 1
            kid = f"P{ci:03d}"
            w.contacts[kid] = Contact(kid, name, slug_email(name, company_domain(c.name)), rnd.choice(ROLES), c.id, row=ci)
    for k in w.contacts.values():
        w.mentions.append(Mention("crm/contacts.csv", k.row, "full_name", "Contact", k.name, k.id))

    # ----- orders -----
    cust_ids = list(w.customers)
    weights = [rnd.uniform(0.4, 3.0) for _ in cust_ids]
    weights[0] = 7.0  # Summit Trail is the biggest account
    skus = list(w.products)
    o_num = 1000
    line_row = 0
    order_list: list[Order] = []
    for day in range((END - START).days + 1):
        d = START + timedelta(days=day)
        if d.weekday() >= 5:
            continue
        for _ in range(rnd.choice([1, 2, 2, 3, 3, 4])):
            cid = rnd.choices(cust_ids, weights)[0]
            c = w.customers[cid]
            crm_id = c.crm_ids[1] if len(c.crm_ids) > 1 and rnd.random() < 0.4 else c.crm_ids[0]
            o_num += 1
            lines = []
            for sku in rnd.sample(skus, rnd.choice([1, 1, 2, 2, 3])):
                qty = rnd.choice([2, 4, 5, 6, 8, 10, 12, 20])
                lines.append(Line(sku, qty, round(qty * w.products[sku].price * 0.6, 2)))
            age = (END - d).days
            status = "delivered" if age > 10 else rnd.choice(["shipped", "processing"]) if age > 2 else "processing"
            if rnd.random() < 0.04:
                status = "cancelled"
            order_list.append(Order(f"ORD-{o_num}", cid, crm_id, d.isoformat(), status, lines))
    for row, o in enumerate(order_list, start=1):
        o.row = row
        for li in o.lines:
            line_row += 1
            li.row = line_row
        w.orders[o.id] = o

    # ----- inbound shipments -----
    s_num = 300
    ship_list: list[Shipment] = []
    for week in range(20):
        base = date(2026, 6, 1) + timedelta(weeks=week)
        for _ in range(rnd.choice([2, 3, 3, 4])):
            sku = rnd.choice(skus)
            p = w.products[sku]
            supplier = w.suppliers[p.supplier]
            expected = base + timedelta(days=rnd.randrange(0, 5))
            late = rnd.choice([0, 0, 0, 0, 0, 1, 2, 4, 6])
            s_num += 1
            ship_list.append(Shipment(f"SHP-{s_num}", supplier.id, supplier.name, sku, rnd.choice([50, 100, 150, 200]), expected.isoformat(), "", "", late))
    # The story the evaluation leans on: a badly delayed Ridgeback shipment expected last week (5-11 Oct 2026)
    ridge = sup_by_name["Ridgeback Textiles Ltd"]
    ship_list = [s for s in ship_list if not ("2026-10-05" <= s.expected <= "2026-10-11" and s.days_late > 0)]
    s_num += 1
    ship_list.append(Shipment(f"SHP-{s_num}", ridge, "Ridgeback Textiles Limited", prod_by_name["Ridgeline 2P Tent"], 120, "2026-10-06", "", "", 9))
    s_num += 1
    ship_list.append(Shipment(f"SHP-{s_num}", ridge, "RIDGEBACK TEXTILES", prod_by_name["Tarn Rain Shell"], 200, "2026-10-07", "", "", 8))
    ship_list.sort(key=lambda s: (s.expected, s.id))
    for row, s in enumerate(ship_list, start=1):
        s.row = row
        exp = date.fromisoformat(s.expected)
        arrived = exp + timedelta(days=s.days_late)
        if arrived > END:
            s.status, s.arrived, s.days_late = "in transit", "", 0
        else:
            s.status = "delayed" if s.days_late >= 2 else "on time"
            s.arrived = arrived.isoformat()
        if s.status == "on time":
            s.days_late = max(0, (arrived - exp).days) if s.arrived else 0
        variants = SUPPLIER_VARIANTS.get(w.suppliers[s.supplier].name, [])
        if s.supplier_text == w.suppliers[s.supplier].name and variants and rnd.random() < 0.45:
            s.supplier_text = rnd.choice(variants)
        w.shipments[s.id] = s
        w.mentions.append(Mention("ops/shipments.csv", row, "supplier", "Supplier", s.supplier_text, s.supplier))

    # ----- support tickets -----
    contacts_by_cust: dict[str, list[Contact]] = {}
    for k in w.contacts.values():
        contacts_by_cust.setdefault(k.customer, []).append(k)
    orders_by_cust: dict[str, list[Order]] = {}
    for o in w.orders.values():
        orders_by_cust.setdefault(o.customer, []).append(o)

    def company_variant(c: Customer) -> str:
        r = rnd.random()
        if r < 0.55:
            return c.name
        options = [c.name + " Ltd", c.name.lower(), c.name.upper(), c.name + " Limited"]
        if c.name.endswith(" Co"):
            options.append(c.name[:-3] + " Company")
        return rnd.choice(options)

    def requester_variant(k: Contact) -> tuple[str, str]:
        first, last = k.name.split(" ", 1)
        r = rnd.random()
        if r < 0.5:
            return k.name, k.email
        if r < 0.7:
            return f"{first[0]}. {last}", k.email
        if r < 0.85:
            return f"{last}, {first}", k.email.upper() if rnd.random() < 0.3 else k.email
        return k.name.upper(), ""

    def assignee_variant(eid: str) -> str:
        e = w.employees[eid]
        first, last = e.name.split(" ", 1)
        return rnd.choice([e.name, e.name, f"{first} {last[0]}.", f"{first[0]}. {last}", e.email])

    complaint_subjects = {
        "tents": ["Seam leaking after first trip", "Tent pole snapped", "Zip failure on fly"],
        "apparel": ["Jacket delaminating", "Stitching coming apart", "Wrong sizes received"],
        "packs": ["Buckle broke", "Hip belt strap torn", "Pack frame bent in transit"],
        "sleeping": ["Mat will not hold air", "Down clumping after wash", "Valve leaking"],
        "footwear": ["Sole separating", "Strap broke", "Sizes run small"],
        "cooking": ["Stove not igniting", "Pot handle loose", "Canister thread damaged"],
    }
    t_num = 2000
    tick_list: list[Ticket] = []
    for _ in range(170):
        cid = rnd.choices(cust_ids, weights)[0]
        c = w.customers[cid]
        if cid not in contacts_by_cust or cid not in orders_by_cust:
            continue
        k = rnd.choice(contacts_by_cust[cid])
        o = rnd.choice(orders_by_cust[cid])
        li = rnd.choice(o.lines)
        p = w.products[li.sku]
        cat = rnd.choices(["complaint", "question", "return", "warranty"], [4, 3, 2, 2])[0]
        opened = date.fromisoformat(o.date) + timedelta(days=rnd.randrange(3, 30))
        if opened > END:
            opened = END - timedelta(days=rnd.randrange(0, 4))
        age = (END - opened).days
        status = "closed" if age > 21 and rnd.random() < 0.85 else rnd.choice(["open", "open", "pending", "closed"])
        prio = rnd.choices(["low", "normal", "high"], [2, 5, 2])[0]
        agent = rnd.choice(SUPPORT_AGENTS)
        if cat == "complaint":
            subject = rnd.choice(complaint_subjects[p.category])
            body = f"Hi team, regarding order {o.id}: the {p.name} we received has a problem. {subject}. Please advise on a replacement."
        elif cat == "return":
            subject = "Return request"
            body = f"We would like to return {li.qty // 2 or 1} x {p.name} from {o.id}. They are unused and still boxed."
        elif cat == "warranty":
            subject = "Warranty claim"
            body = f"A customer brought back a {p.name} bought through us (our order {o.id}). Is this covered by warranty?"
        else:
            subject = "Stock question"
            body = f"Do you have more {p.name} coming in? Our last order was {o.id}."
        t_num += 1
        tick_list.append(Ticket(f"TCK-{t_num}", opened.isoformat(), status, cat, prio, cid, "", k.id, "", "", E[agent], "", o.id, li.sku, subject, body))

    # Planted: open complaints from buyers of Ridgeback products, so the flagship question has a real answer
    tick_list.sort(key=lambda t: (t.opened, t.id))
    for rec, t in enumerate(tick_list, start=1):
        t.rec = rec
        t.id = f"TCK-{2000 + rec}"
        c = w.customers[t.customer]
        k = w.contacts[t.contact]
        t.company_text = company_variant(c)
        t.requester_name, t.requester_email = requester_variant(k)
        if t.customer == "C001" and t.contact == "P001" and rec % 3 == 0:
            t.requester_name = "J. Smith"
        t.assignee_text = assignee_variant(t.assignee) if t.assignee else ""
        if rec in (37, 101):  # "S. Clarke" is ambiguous on purpose: Sarah (support) or Simon (warehouse)
            t.assignee = E["Sarah Clarke"]
            t.assignee_text = "S. Clarke"
        w.tickets[t.id] = t
        w.mentions.append(Mention("support/tickets.json", rec, "company", "Customer", t.company_text, t.customer))
        w.mentions.append(Mention("support/tickets.json", rec, "requester", "Contact", t.requester_name, t.contact))
        if t.assignee:
            w.mentions.append(Mention("support/tickets.json", rec, "assignee", "Employee", t.assignee_text, t.assignee))

    # ----- org chart records -----
    rec = 0
    for team in TEAMS:
        for eid in w.teams[team]:
            rec += 1
            w.employees[eid].rec = rec
            w.mentions.append(Mention("org/org_chart.json", rec, "name", "Employee", w.employees[eid].name, eid))

    # ----- policies -----
    pol = [
        ("POL-RET", "Returns Policy", "policies/returns-policy.md", "Customer Support", "2026-02-01"),
        ("POL-WAR", "Warranty Policy", "policies/warranty-policy.pdf", "Customer Support", "2025-07-01"),
        ("POL-SHP", "Shipping Policy", "policies/shipping-policy.md", "Operations", "2026-03-15"),
        ("POL-DSC", "Discount Approval Policy", "policies/discount-approval.md", "Finance", "2026-01-10"),
        ("POL-SUP", "Support Response Standard", "policies/support-response-standard.md", "Customer Support", "2025-11-01"),
        ("POL-SQP", "Supplier Quality Policy", "policies/supplier-quality-policy.pdf", "Purchasing", "2026-05-01"),
        ("POL-DPP", "Data Protection Policy", "policies/data-protection.md", "Leadership", "2025-09-01"),
        ("POL-ONB", "Account Onboarding Process", "policies/account-onboarding.md", "Sales", "2026-04-20"),
    ]
    for pid, name, file, team, eff in pol:
        w.policies[pid] = Policy(pid, name, file, team, 0, eff)

    # ----- meetings and decisions (lines are filled in when the notes are written) -----
    meetings = [
        ("M01", "Weekly Ops Review", "2026-08-03", ["Tom Baker", "Simon Clarke", "Kate Palmer", "Olivia Grant"]),
        ("M02", "Sales Pipeline Meeting", "2026-08-10", ["Mark Reid", "James Walker", "Hannah Moore", "Ben Turner", "Chloe Bell"]),
        ("M03", "Supplier Quality Review", "2026-08-24", ["Olivia Grant", "Anna Holmes", "Holly Knight", "Tom Baker"]),
        ("M04", "Support Team Retro", "2026-09-01", ["Sarah Clarke", "Emma Wilson", "Ryan Young", "Megan King", "Nathan Green"]),
        ("M05", "Pricing and Discounts Meeting", "2026-09-07", ["Mark Reid", "James Hall", "Claire Fraser", "Grace Cooper"]),
        ("M06", "Weekly Ops Review", "2026-09-14", ["Tom Baker", "Simon Clarke", "Rachel Fox", "Peter Gibson"]),
        ("M07", "Range Planning Meeting", "2026-09-21", ["Olivia Grant", "Zoe Dixon", "Rebecca Webb", "Mark Reid"]),
        ("M08", "Leadership Meeting", "2026-09-28", ["Claire Fraser", "Mark Reid", "Tom Baker", "James Hall", "Rebecca Webb"]),
        ("M09", "Support Team Retro", "2026-10-06", ["Sarah Clarke", "Sophie Wright", "Amy Morris", "Josh Ellis"]),
        ("M10", "Supplier Quality Review", "2026-10-12", ["Olivia Grant", "Anna Holmes", "Tom Baker", "Simon Clarke", "Holly Knight"]),
        ("M11", "Key Accounts Meeting", "2026-10-13", ["Mark Reid", "James Walker", "Hannah Moore", "James Hall"]),
        ("M12", "Weekly Ops Review", "2026-10-14", ["Tom Baker", "Kate Palmer", "Simon Clarke", "Matt Lane"]),
    ]
    for mid, title, mdate, people in meetings:
        file = f"meetings/{mdate}-{title.lower().replace(' ', '-')}.md"
        w.meetings[mid] = Meeting(mid, title, mdate, file, [E[p] for p in people], 0)
    return w


# ---------------------------------------------------------------------------------------------------------------
# Writing the sources


def _csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(header)
    wr.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8", newline="\n")


def _attendee_text(w: World, eid: str, rnd: random.Random) -> str:
    e = w.employees[eid]
    first, last = e.name.split(" ", 1)
    if e.name in ("Sarah Clarke", "Simon Clarke"):
        return rnd.choice([e.name, f"{first} {last[0]}."])
    return rnd.choice([e.name, e.name, e.name, f"{first} {last[0]}.", f"{first[0]}. {last}"])


def write_meetings(w: World, root: Path, rnd: random.Random) -> None:
    E = {e.name: e.id for e in w.employees.values()}
    sup = {s.name: s.id for s in w.suppliers.values()}
    prod = {p.name: p.sku for p in w.products.values()}
    late = w.shipments[max(w.shipments, key=lambda s: w.shipments[s].days_late if w.shipments[s].expected >= "2026-10-05" else -1)]

    # Per meeting: discussion bullets, then decision specs (text, links, context line index in discussion or None).
    D = Decision
    plan: dict[str, tuple[list[str], list[tuple[str, dict[str, str], int | None, str | None]]]] = {
        "M01": (
            [
                "Simon said dispatch is running two days behind because of the August stocktake.",
                "Kate confirmed the new courier contract starts on 1 September.",
                "Olivia flagged that Ember Cookware Ltd has raised fuel canister prices by 8%.",
            ],
            [
                ("Move the weekly stocktake to Saturday mornings so dispatch is not interrupted.", {}, None, "Simon Clarke"),
                (
                    "Keep buying Ember Fuel Canister from Ember Cookware Ltd for now and review the price in October.",
                    {"supplier": "Ember Cookware Ltd", "product": "Ember Fuel Canister"},
                    None,
                    "Olivia Grant",
                ),
            ],
        ),
        "M02": (
            [
                "Mark reviewed the spring pipeline. School orders are up on last year.",
                "Hannah said Kea Kids Club wants a quote for 40 Kids Explorer Packs before their summer camp.",
            ],
            [
                (
                    "Offer Kea Kids Club a 10% club discount on the Kids Explorer Pack order.",
                    {"customer": "C002", "product": "Kids Explorer Pack"},
                    None,
                    "Hannah Moore",
                )
            ],
        ),
        "M03": (
            [
                "Anna presented the July defect report. Seam failures on the Ridgeline 2P Tent are up to 3.1% of units sold.",
                "Holly said the failures trace back to a change in seam tape at the factory.",
            ],
            [
                (
                    "Ask Ridgeback Textiles Ltd for a corrective action plan on the Ridgeline 2P Tent seam tape by 15 September.",
                    {"supplier": "Ridgeback Textiles Ltd", "product": "Ridgeline 2P Tent"},
                    None,
                    "Anna Holmes",
                )
            ],
        ),
        "M04": (
            [
                "Sarah said first responses are averaging 11 hours against the 8 hour standard.",
                "Emma suggested saved replies for the five most common questions.",
            ],
            [
                ("Introduce saved replies for the top five ticket types.", {}, None, "Emma Wilson"),
                ("Update the Support Response Standard to add a weekend on-call roster.", {"policy": "POL-SUP"}, None, "Sarah Clarke"),
            ],
        ),
        "M05": (
            [
                "James Hall reported that average discounts crept up to 9% in the last quarter.",
                "Grace asked whether long-standing accounts could get a loyalty rate.",
            ],
            [("Discounts above 15% now need sign-off from the Managing Director, not just the Head of Sales.", {"policy": "POL-DSC"}, None, "James Hall")],
        ),
        "M06": (
            [
                "Rachel said Trailhead 45L Pack stock will last six weeks at the current sell rate.",
                "Peter reported three pallets damaged by the forklift in the Christchurch warehouse.",
            ],
            [("Book forklift refresher training for all warehouse staff.", {}, None, "Simon Clarke")],
        ),
        "M07": (
            [
                "Zoe showed sales of the Trailhead 45L Pack are down 40% since the 65L model launched.",
                "Rebecca said marketing can clear remaining stock with an end-of-line promotion.",
            ],
            [
                ("Discontinue the Trailhead 45L Pack once current stock is sold.", {"product": "Trailhead 45L Pack"}, None, "Zoe Dixon"),
                ("Add a second sleeping mat supplier alongside Coastline Polymers before winter.", {"supplier": "Coastline Polymers"}, None, "Michael Barker"),
            ],
        ),
        "M08": (
            [
                "Claire reviewed the half-year results. Revenue is ahead of plan but freight costs are up.",
                "Tom proposed raising the free shipping threshold to protect margin.",
            ],
            [("Raise the free shipping threshold from $200 to $250 from 1 November.", {"policy": "POL-SHP"}, 1, "Tom Baker")],
        ),
        "M09": (
            [
                "Sarah said the saved replies cut first response time to 7 hours.",
                "Sophie raised that Riverstone Outdoors has logged repeated complaints about Cloudrest Sleeping Mat valves.",
            ],
            [
                (
                    "Send Riverstone Outdoors replacement valves for every Cloudrest Sleeping Mat they bought this year.",
                    {"customer": "C004", "product": "Cloudrest Sleeping Mat"},
                    None,
                    "Sophie Wright",
                )
            ],
        ),
        "M10": (
            [
                f"Tom reported that shipment {late.id} from Ridgeback Textiles Ltd arrived {late.days_late} days late. It is the third late delivery this quarter.",
                "Anna said the corrective action plan for the Ridgeline 2P Tent seam tape has still not arrived.",
                "Olivia said Alpine Loom Co can supply tent fabric from January at a similar price.",
            ],
            [
                (
                    "Put them on probation for 90 days under the Supplier Quality Policy, effective immediately.",
                    {"supplier": "Ridgeback Textiles Ltd", "policy": "POL-SQP"},
                    0,
                    "Olivia Grant",
                ),
                (
                    "Request a quote from Alpine Loom Co for Ridgeline tent fabric for the January production run.",
                    {"supplier": "Alpine Loom Co"},
                    None,
                    "Zoe Dixon",
                ),
            ],
        ),
        "M11": (
            [
                "James Walker said Summit Trail Outfitters is our largest account this year and is asking for better terms.",
                "James Hall confirmed their payment record is clean.",
            ],
            [("Offer Summit Trail Outfitters a 12% loyalty discount for 2027, approved by the Managing Director.", {"customer": "C001"}, None, "James Walker")],
        ),
        "M12": (
            [
                "Kate said the delayed Ridgeback shipments mean Ridgeline 2P Tent stock runs out in nine days.",
                "Matt reported that the new racking in aisle 4 is finished.",
            ],
            [("Prioritise existing back-orders for the Ridgeline 2P Tent when the delayed stock arrives.", {"product": "Ridgeline 2P Tent"}, 0, "Kate Palmer")],
        ),
    }
    dnum = 0
    for mid, m in w.meetings.items():
        disc, decs = plan[mid]
        att = ", ".join(_attendee_text(w, e, rnd) for e in m.attendees)
        lines = [f"# {m.title}, {date.fromisoformat(m.date).strftime('%d %B %Y').lstrip('0')}", "", f"Attendees: {att}", "", "## Discussion", ""]
        m.attendee_line = 3
        disc_lines: list[int] = []
        for d in disc:
            lines.append(f"- {d}")
            disc_lines.append(len(lines))
        lines += ["", "## Decisions", ""]
        for text, links, ctx, owner in decs:
            dnum += 1
            first = owner.split(" ")[0] if owner else ""
            lines.append(f"- {text}")
            line = len(lines)
            lines.append(f"  Action owner: {first if rnd.random() < 0.5 else owner}.")
            dec = D(f"D{dnum:02d}", text, mid, line, disc_lines[ctx] if ctx is not None else None)
            dec.supplier = sup.get(links.get("supplier", ""))
            dec.product = prod.get(links.get("product", ""))
            dec.policy = links.get("policy")
            dec.customer = links.get("customer")
            dec.owner = E[owner] if owner else None
            dec.owner_line = len(lines)
            m.decisions.append(dec)
        lines += ["", "## Next meeting", "", "Same time next fortnight."]
        path = root / m.file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        for e in m.attendees:
            w.mentions.append(Mention(m.file, m.attendee_line, "attendee", "Employee", w.employees[e].name, e))


POLICY_TEXT: dict[str, list[str]] = {
    "POL-RET": [
        "# Returns Policy",
        "Owner: Customer Support team. Effective 1 February 2026.",
        "## Purpose",
        "This policy explains when a wholesale customer can send stock back to Acme Outdoor Gear and how refunds are handled.",
        "## Return window",
        "Customers may return unused items in their original packaging within thirty (30) days of delivery.",
        "Items returned after 30 days are accepted only with approval from the Support Lead.",
        "## Refunds",
        "Refunds are issued as credit to the customer's account within five working days of the goods arriving at the Christchurch warehouse.",
        "Faulty goods are handled under the Warranty Policy, not this policy.",
    ],
    "POL-WAR": [
        "# Warranty Policy",
        "Owner: Customer Support team. Effective 1 July 2025.",
        "All Acme Outdoor Gear products carry a warranty against manufacturing defects for twenty-four (24) months from the date of sale to the end customer.",
        "The warranty does not cover normal wear, accidental damage, or damage from improper care such as machine-washing down products.",
        "Warranty claims must include the original order number and a photo of the fault. Support staff assess claims within three working days.",
        "Where a supplier defect is confirmed, Purchasing recovers the cost from the supplier under the Supplier Quality Policy.",
    ],
    "POL-SHP": [
        "# Shipping Policy",
        "Owner: Operations team. Effective 15 March 2026.",
        "## Free shipping",
        "Orders over $200 (excluding GST) ship free anywhere in New Zealand.",
        "Orders under that amount pay a flat freight charge of $18.",
        "## Dispatch times",
        "Orders received before 1 pm on a working day are dispatched the same day from the Christchurch warehouse.",
        "Rural delivery adds one to two working days.",
    ],
    "POL-DSC": [
        "# Discount Approval Policy",
        "Owner: Finance team. Effective 10 January 2026.",
        "## Approval levels",
        "Account managers may offer discounts of up to 10% on any order without further approval.",
        "Any discount greater than 10% must be approved by the Head of Sales before it is offered to the customer.",
        "## Recording",
        "Every approved discount is recorded against the customer account in the CRM, with the approver's name.",
    ],
    "POL-SUP": [
        "# Support Response Standard",
        "Owner: Customer Support team. Effective 1 November 2025.",
        "## First response",
        "Every new ticket must receive a first response from a person within eight (8) business hours.",
        "High-priority tickets must be acknowledged within two business hours.",
        "## Escalation",
        "Tickets about faulty goods that affect more than one customer are escalated to the Support Lead and to Purchasing.",
    ],
    "POL-SQP": [
        "# Supplier Quality Policy",
        "Owner: Purchasing team. Effective 1 May 2026.",
        "Suppliers must deliver on or before the agreed date. A shipment that arrives late incurs a penalty of five percent (5%) of the shipment value, deducted from the next invoice.",
        "A supplier with three late or defective deliveries in one quarter is placed on probation. During probation, Purchasing reviews every new order with that supplier.",
        "Defect rates above 2% of units sold require a written corrective action plan from the supplier within 30 days.",
        "Suppliers that remain on probation for more than 90 days may be ended.",
    ],
    "POL-DPP": [
        "# Data Protection Policy",
        "Owner: Leadership team. Effective 1 September 2025.",
        "## Customer data",
        "Customer and contact details are kept in the CRM only. Do not export contact lists to personal devices.",
        "## Access",
        "Staff get access to customer records based on their role, reviewed every six months by the Managing Director.",
        "## Breaches",
        "Any suspected privacy breach must be reported to the Managing Director on the same day.",
    ],
    "POL-ONB": [
        "# Account Onboarding Process",
        "Owner: Sales team. Effective 20 April 2026.",
        "## Steps",
        "1. The account manager collects the customer's trading details and credit references.",
        "2. Finance runs a credit check and sets a credit limit.",
        "3. The account manager creates the account in the CRM and sends the welcome pack and price list.",
        "4. The first order is reviewed by the Head of Sales.",
    ],
}
POLICY_ATTRS = {
    "POL-RET": {"return_window_days": (30.0, "within thirty (30) days")},
    "POL-WAR": {"warranty_months": (24.0, "twenty-four (24) months")},
    "POL-SHP": {"free_shipping_over_nzd": (200.0, "Orders over $200")},
    "POL-DSC": {"discount_approval_over_pct": (10.0, "greater than 10%")},
    "POL-SUP": {"first_response_hours": (8.0, "eight (8) business hours")},
    "POL-SQP": {"late_shipment_penalty_pct": (5.0, "five percent (5%)")},
}


def write_policies(w: World, root: Path) -> None:
    for pid, p in w.policies.items():
        paras = POLICY_TEXT[pid]
        path = root / p.file
        path.parent.mkdir(parents=True, exist_ok=True)
        if p.file.endswith(".pdf"):
            write_pdf(path, [paras])
            p.owner_line = 1  # page
            for prop, (val, _phrase) in POLICY_ATTRS.get(pid, {}).items():
                p.attrs[prop] = (val, 1)
            continue
        lines: list[str] = []
        for para in paras:
            if para.startswith("## ") and lines:
                lines.append("")
            lines.append(para)
            if para.startswith("#"):
                lines.append("")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        p.owner_line = next(i for i, ln in enumerate(lines, start=1) if ln.startswith("Owner:"))
        for prop, (val, phrase) in POLICY_ATTRS.get(pid, {}).items():
            p.attrs[prop] = (val, next(i for i, ln in enumerate(lines, start=1) if phrase in ln))


def write_sources(w: World, root: Path) -> None:
    rnd = random.Random(SEED + 1)
    written = {m.pos: m.text for m in w.mentions if m.file == "crm/customers.csv" and m.field == "company"}
    crm = sorted((row, crm_id, c) for c in w.customers.values() for crm_id, row in zip(c.crm_ids, c.rows, strict=True))
    _csv(
        root / "crm/customers.csv",
        ["customer_id", "company", "segment", "region", "customer_since", "account_manager_email"],
        [[crm_id, written[row], c.segment, c.region, c.since, w.employees[c.manager].email] for row, crm_id, c in crm],
    )
    _csv(
        root / "crm/contacts.csv",
        ["contact_id", "customer_id", "full_name", "email", "role"],
        [[k.id, w.customers[k.customer].crm_ids[0], k.name, k.email, k.role] for k in w.contacts.values()],
    )
    _csv(root / "ops/suppliers.csv", ["supplier_id", "supplier_name", "country", "status"], [[s.id, s.name, s.country, s.status] for s in w.suppliers.values()])
    _csv(
        root / "ops/products.csv",
        ["sku", "product_name", "category", "price_nzd", "supplier_id"],
        [[p.sku, p.name, p.category, f"{p.price:.2f}", p.supplier] for p in w.products.values()],
    )
    _csv(
        root / "ops/orders.csv",
        ["order_id", "customer_id", "order_date", "status", "total_nzd"],
        [[o.id, o.crm_id, o.date, o.status, f"{o.total:.2f}"] for o in w.orders.values()],
    )
    _csv(
        root / "ops/order_lines.csv",
        ["order_id", "line_no", "sku", "qty", "line_total_nzd"],
        [[o.id, i, li.sku, li.qty, f"{li.total:.2f}"] for o in w.orders.values() for i, li in enumerate(o.lines, start=1)],
    )
    _csv(
        root / "ops/shipments.csv",
        ["shipment_id", "supplier", "sku", "qty", "expected_date", "arrived_date", "status", "days_late"],
        [[s.id, s.supplier_text, s.sku, s.qty, s.expected, s.arrived, s.status, s.days_late] for s in w.shipments.values()],
    )
    tickets = []
    for t in w.tickets.values():
        req: dict[str, str] = {"name": t.requester_name}
        if t.requester_email:
            req["email"] = t.requester_email
        tickets.append(
            {
                "ticket_id": t.id,
                "opened": t.opened,
                "status": t.status,
                "category": t.category,
                "priority": t.priority,
                "company": t.company_text,
                "requester": req,
                "assignee": t.assignee_text,
                "subject": t.subject,
                "body": t.body,
            }
        )
    (root / "support").mkdir(parents=True, exist_ok=True)
    (root / "support/tickets.json").write_text(json.dumps(tickets, indent=1) + "\n", encoding="utf-8", newline="\n")
    teams_out: list[dict[str, object]] = []
    for team in TEAMS:
        members = []
        for eid in w.teams[team]:
            e = w.employees[eid]
            members.append(
                {"name": e.name, "email": e.email, "title": e.title, "location": e.location, "reports_to": w.employees[e.manager].email if e.manager else None}
            )
        teams_out.append({"team": team, "members": members})
    org = {"company": "Acme Outdoor Gear", "teams": teams_out}
    (root / "org").mkdir(parents=True, exist_ok=True)
    (root / "org/org_chart.json").write_text(json.dumps(org, indent=1) + "\n", encoding="utf-8", newline="\n")
    write_meetings(w, root, rnd)
    write_policies(w, root)


def generate(root: Path) -> World:
    w = build_world()
    write_sources(w, root)
    return w


def world() -> World:
    """The ground-truth world, with every line number filled in, without writing files that already exist."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        return generate(Path(tmp))


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "data/acme")
    w = generate(out)
    print(
        f"wrote {out}: {len(w.customers)} customers, {len(w.contacts)} contacts, {len(w.orders)} orders, "
        f"{len(w.shipments)} shipments, {len(w.tickets)} tickets, {len(w.employees)} staff, {len(w.meetings)} meetings, {len(w.policies)} policies"
    )
