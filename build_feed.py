#!/usr/bin/env python3
"""Builds a Google Merchant Center feed from Shopify: one row per product plus one row per compatible printer."""
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SHOP = os.environ.get("SHOPIFY_SHOP", "")
SITE = os.environ.get("SITE_URL", "https://myinktoner.co.uk").rstrip("/")
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-04")
BRAND = os.environ.get("FEED_BRAND", "MyInkToner")
MAX_PRINTERS = int(os.environ.get("MAX_PRINTERS_PER_PRODUCT", "0") or 0)  # 0 = all printers
OUT_DIR = os.environ.get("OUT_DIR", "site")
MIN_PRODUCTS = int(os.environ.get("MIN_PRODUCTS", "100"))
CURRENCY = "GBP"
GOOGLE_CATEGORY = "356"  # Electronics > Print, Copy, Scan & Fax > Printer, Copier & Fax Machine Accessories > Printer Consumables > Toner & Inkjet Cartridges

PRINTER_KEYS = ["compatible_printer_collections"] + [f"compatible_printer_collections_{i}" for i in range(2, 9)]
COLUMNS = [
    "id", "title", "description", "link", "image_link", "availability", "price", "brand", "mpn",
    "identifier_exists", "condition", "item_group_id", "google_product_category", "product_type",
    "custom_label_0", "custom_label_1",
]


def get_token():
    token = os.environ.get("SHOPIFY_TOKEN")
    if token:
        return token
    body = urllib.parse.urlencode({
        "client_id": os.environ["SHOPIFY_CLIENT_ID"],
        "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"],
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(
        f"https://{SHOP}/admin/oauth/access_token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["access_token"]


class Shopify:
    def __init__(self):
        self.token = get_token()

    def gql(self, query, variables=None):
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json", data=payload,
            headers={"Content-Type": "application/json", "X-Shopify-Access-Token": self.token},
        )
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    result = json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise
            if "errors" in result and not result.get("data"):
                if attempt < 4 and "throttled" in json.dumps(result["errors"]).lower():
                    time.sleep(2 ** attempt * 2)
                    continue
                raise RuntimeError(f"GraphQL errors: {result['errors']}")
            return result["data"]

    def online_store_publication(self):
        data = self.gql("{ publications(first: 50) { edges { node { id name } } } }")
        for edge in data["publications"]["edges"]:
            if edge["node"]["name"] == "Online Store":
                return edge["node"]["id"]
        raise RuntimeError("Online Store publication not found (does the app have read_publications?)")

    def bulk(self, query):
        deadline = time.time() + 1800
        while True:
            data = self.gql(
                "mutation($q: String!) { bulkOperationRunQuery(query: $q) { bulkOperation { id status } userErrors { message } } }",
                {"q": query},
            )["bulkOperationRunQuery"]
            errors = data["userErrors"]
            if not errors:
                break
            if "in progress" in json.dumps(errors).lower() and time.time() < deadline:
                time.sleep(15)
                continue
            raise RuntimeError(f"Bulk operation rejected: {errors}")
        op_id = data["bulkOperation"]["id"]
        while True:
            node = self.gql(
                "query($id: ID!) { node(id: $id) { ... on BulkOperation { status errorCode objectCount url } } }",
                {"id": op_id},
            )["node"]
            if node["status"] == "COMPLETED":
                break
            if node["status"] in ("FAILED", "CANCELED", "EXPIRED"):
                raise RuntimeError(f"Bulk operation {node['status']}: {node.get('errorCode')}")
            if time.time() > deadline:
                raise RuntimeError("Bulk operation timed out")
            time.sleep(5)
        if not node["url"]:
            return []
        with urllib.request.urlopen(node["url"], timeout=300) as resp:
            return [json.loads(line) for line in resp.read().decode("utf-8").splitlines() if line.strip()]


def products_query(pub_id):
    printer_fields = " ".join(
        f'p{i}: metafield(namespace: "custom", key: "{key}") {{ value }}' for i, key in enumerate(PRINTER_KEYS, 1)
    )
    return (
        '{ products(query: "status:active") { edges { node { id title handle vendor productType tags description '
        f'publishedOnPublication(publicationId: "{pub_id}") '
        "featuredMedia { preview { image { url } } } "
        'mpn: metafield(namespace: "custom", key: "mpn") { value } '
        f"{printer_fields} "
        "variants { edges { node { id sku barcode price inventoryQuantity inventoryPolicy } } } } } } }"
    )


def collections_query(pub_id):
    return (
        "{ collections { edges { node { id title handle "
        f'publishedOnPublication(publicationId: "{pub_id}") '
        "} } } }"
    )


def clean(text):
    return re.sub(r"\s+", " ", (text or "")).replace('"', "'").strip()


def numeric_id(gid):
    return gid.rsplit("/", 1)[-1]


def make_id(*parts):
    return re.sub(r"[^A-Za-z0-9_.-]", "-", "-".join(parts))[:50]


def printer_title(title, printer):
    full = f"{title} for {printer}"
    if len(full) <= 150:
        return full
    short = re.sub(r"\s*\([^)]*\)\s*$", "", title)
    full = f"{short} for {printer}"
    return full if len(full) <= 150 else None


def tag_value(tags, prefix):
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return ""


def build_rows(product_lines, collection_lines):
    printers_by_gid = {
        c["id"]: (clean(c["title"]), c["handle"])
        for c in collection_lines if c.get("publishedOnPublication")
    }
    variants = {}
    products = []
    for line in product_lines:
        if "__parentId" in line:
            variants.setdefault(line["__parentId"], []).append(line)
        else:
            products.append(line)

    rows, stats = [], {"products": 0, "printer_rows": 0, "skipped_no_image": 0, "skipped_long_title": 0, "skipped_missing_printer": 0}
    for p in products:
        if not p.get("publishedOnPublication"):
            continue
        image = ((p.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}
        if not image.get("url"):
            stats["skipped_no_image"] += 1
            continue
        pvars = variants.get(p["id"], [])
        if not pvars:
            continue

        printers, seen_titles = [], set()
        for i in range(1, len(PRINTER_KEYS) + 1):
            meta = p.get(f"p{i}")
            if not meta or not meta.get("value"):
                continue
            for gid in json.loads(meta["value"]):
                if gid not in printers_by_gid:
                    stats["skipped_missing_printer"] += 1
                    continue
                name, handle = printers_by_gid[gid]
                if name.casefold() in seen_titles:
                    continue
                seen_titles.add(name.casefold())
                printers.append((name, handle, numeric_id(gid)))
        printers.sort(key=lambda x: x[0].casefold())
        if MAX_PRINTERS:
            printers = printers[:MAX_PRINTERS]

        title = clean(p["title"])[:150]
        description = clean(p["description"]) or title
        mpn = clean((p.get("mpn") or {}).get("value"))
        tags = p.get("tags") or []
        product_type = {"toner": "Toner Cartridges", "ink": "Ink Cartridges"}.get(tag_value(tags, "type-"), "")
        multi = len(pvars) > 1
        group_id = make_id(pvars[0].get("sku") or numeric_id(p["id"]))

        for v in pvars:
            sku = make_id(v.get("sku") or numeric_id(v["id"]))
            in_stock = (v.get("inventoryQuantity") or 0) > 0 or v.get("inventoryPolicy") == "CONTINUE"
            base_link = f"{SITE}/products/{p['handle']}"
            query = [f"variant={numeric_id(v['id'])}"] if multi else []
            common = {
                "image_link": image["url"],
                "availability": "in stock" if in_stock else "out of stock",
                "price": f"{float(v['price']):.2f} {CURRENCY}",
                "brand": BRAND,
                "mpn": mpn,
                "identifier_exists": "" if (mpn or v.get("barcode")) else "no",
                "condition": "new",
                "item_group_id": group_id,
                "google_product_category": GOOGLE_CATEGORY,
                "product_type": product_type,
                "custom_label_0": tag_value(tags, "brand-"),
            }
            rows.append({
                **common, "id": sku, "title": title, "description": description[:5000],
                "link": base_link + ("?" + "&".join(query) if query else ""), "custom_label_1": "base",
            })
            stats["products"] += 1
            for name, handle, coll_id in printers:
                ptitle = printer_title(title, name)
                if not ptitle:
                    stats["skipped_long_title"] += 1
                    continue
                rows.append({
                    **common, "id": make_id(sku, coll_id), "title": ptitle,
                    "description": f"Compatible with {name}. {description}"[:5000],
                    "link": base_link + "?" + "&".join(query + [f"printer={urllib.parse.quote(handle)}"]),
                    "custom_label_1": "printer",
                })
                stats["printer_rows"] += 1
    return rows, stats


def write_outputs(rows, stats):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "feed.txt"), "w", encoding="utf-8", newline="") as f:
        f.write("\t".join(COLUMNS) + "\n")
        for r in rows:
            f.write("\t".join(clean(r.get(c, "")) for c in COLUMNS) + "\n")
    with open(os.path.join(OUT_DIR, "feed.csv"), "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in COLUMNS})
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(
            '<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">'
            "<title>Feed status</title><body style=\"font-family:sans-serif;max-width:40rem;margin:2rem auto\">"
            f"<h1>Feed status</h1><p>Last built: {built}</p>"
            f"<p>{len(rows)} rows ({stats['products']} products, {stats['printer_rows']} printer rows)</p>"
            '<p><a href="feed.txt">feed.txt</a> (for Merchant Center) &middot; <a href="feed.csv">feed.csv</a> (open in Excel)</p>'
        )


def main():
    if not SHOP:
        sys.exit("SHOPIFY_SHOP is not set")
    shop = Shopify()
    pub_id = shop.online_store_publication()
    print("Fetching products...")
    product_lines = shop.bulk(products_query(pub_id))
    print("Fetching printer collections...")
    collection_lines = shop.bulk(collections_query(pub_id))
    rows, stats = build_rows(product_lines, collection_lines)
    print(f"Built {len(rows)} rows: {stats}")
    if stats["products"] < MIN_PRODUCTS:
        sys.exit(f"Only {stats['products']} products found (minimum {MIN_PRODUCTS}); refusing to publish a broken feed")
    write_outputs(rows, stats)


if __name__ == "__main__":
    main()
