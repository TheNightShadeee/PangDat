# -*- coding: utf-8 -*-
"""
etl_dwh.py
==========
ETL: CSV bersih (hasil cleansing_tools.py) -> Data Warehouse MySQL (dw_bikestores).

Letak file (satu folder dengan cleansing_tools.py):
    Pangdat/
        archive/
        output/cleaned/     <- dibuat oleh cleansing_tools.py
        cleansing_tools.py
        etl_dwh.py

Urutan kerja:
    1. python cleansing_tools.py
    2. Jalankan 01_setup_dwh.sql di MySQL
    3. pip install pandas mysql-connector-python
    4. Isi DB_CONFIG di bawah, lalu: python etl_dwh.py

Uji transformasi tanpa database:
    python etl_dwh.py --dry-run

Strategi load:
    dim_toko, dim_produk : upsert berdasarkan ID OLTP (id_*_oltp), jadi
                           surrogate key stabil walau ETL diulang.
    fakta_penjualan_bulanan, fakta_pengiriman_bulanan : dikosongkan lalu
                           dimuat ulang (selalu bisa dibangun ulang dari OLTP).
    fakta_stok_toko      : snapshot per bulan. Snapshot bulan ini dihapus
                           lalu dimuat ulang; snapshot bulan lalu dibiarkan
                           sebagai riwayat.
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# KONFIGURASI
# ----------------------------------------------------------------------------
DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "",          # <- isi password MySQL kamu
    "database": "dw_bikestores",
}

BASE_DIR = Path(__file__).resolve().parent
CLEAN_DIR = BASE_DIR / "output" / "cleaned"

STATUS_COMPLETED = 4
STATUS_REJECTED = 3


# ----------------------------------------------------------------------------
# EXTRACT
# ----------------------------------------------------------------------------
def read_clean():
    names = ["brands", "categories", "products", "stores",
             "orders", "order_items", "stocks"]
    data = {}
    for n in names:
        path = CLEAN_DIR / f"{n}.csv"
        if not path.exists():
            raise SystemExit(
                f"File {path} tidak ada. Jalankan cleansing_tools.py dulu.")
        kw = {"encoding": "utf-8-sig"}
        if n == "orders":
            kw["parse_dates"] = ["order_date", "required_date", "shipped_date"]
        if n == "stores":
            kw["dtype"] = {"zip_code": str, "phone": str}
        data[n] = pd.read_csv(path, **kw)
    return data


# ----------------------------------------------------------------------------
# TRANSFORM (murni pandas, tidak butuh database)
# ----------------------------------------------------------------------------
def month_key(series):
    return series.dt.year * 100 + series.dt.month


def build_dim_toko(stores):
    return pd.DataFrame({
        "nama_toko": stores["store_name"],
        "kota": stores["city"],
        "provinsi": stores["state"],
        "kode_pos": stores["zip_code"],
        "telepon": stores["phone"],
        "email": stores["email"],
        "id_toko_oltp": stores["store_id"],
    })


def build_dim_produk(products, brands, categories):
    m = (products
         .merge(brands, on="brand_id", how="left")
         .merge(categories, on="category_id", how="left"))
    return pd.DataFrame({
        "nama_produk": m["product_name"],
        "merek": m["brand_name"],
        "kategori": m["category_name"],
        "tahun_model": m["model_year"],
        "harga_list": m["list_price"],
        "id_produk_oltp": m["product_id"],
    })


def build_fakta_penjualan(orders, items):
    o = orders.loc[orders["order_status"] == STATUS_COMPLETED,
                   ["order_id", "store_id", "order_date"]]
    m = items.merge(o, on="order_id", how="inner")
    m["id_waktu"] = month_key(m["order_date"])
    g = (m.groupby(["id_waktu", "store_id", "product_id"], as_index=False)
          .agg(total_qty=("quantity", "sum"),
               total_penjualan_kotor=("nilai_kotor", "sum"),
               total_diskon=("nilai_diskon", "sum"),
               total_penjualan_bersih=("nilai_bersih", "sum")))
    for c in ["total_penjualan_kotor", "total_diskon", "total_penjualan_bersih"]:
        g[c] = g[c].round(2)
    return g   # masih memakai ID OLTP (store_id, product_id)


def build_fakta_pengiriman(orders):
    o = orders.copy()
    o["id_waktu"] = month_key(o["order_date"])
    selesai = o["order_status"] == STATUS_COMPLETED
    o["dikirim"] = selesai.astype(int)
    o["ditolak"] = (o["order_status"] == STATUS_REJECTED).astype(int)
    o["terlambat"] = (selesai & (o["shipped_date"] > o["required_date"])).astype(int)
    hari = (o["shipped_date"] - o["order_date"]).dt.days
    o["hari_proses"] = np.where(selesai, hari, 0)
    o["hari_proses"] = o["hari_proses"].fillna(0).astype(int)
    g = (o.groupby(["id_waktu", "store_id"], as_index=False)
          .agg(jumlah_order=("order_id", "count"),
               jumlah_order_dikirim=("dikirim", "sum"),
               jumlah_order_ditolak=("ditolak", "sum"),
               jumlah_terlambat=("terlambat", "sum"),
               total_hari_proses=("hari_proses", "sum")))
    return g   # masih memakai store_id OLTP


def build_fakta_stok(stocks, products, id_waktu):
    m = stocks.merge(products[["product_id", "list_price"]],
                     on="product_id", how="left")
    m["id_waktu"] = id_waktu
    m["qty_stok"] = m["quantity"]
    m["nilai_stok"] = (m["quantity"] * m["list_price"]).round(2)
    return m[["id_waktu", "store_id", "product_id", "qty_stok", "nilai_stok"]]


# ----------------------------------------------------------------------------
# LOAD (butuh MySQL)
# ----------------------------------------------------------------------------
def to_rows(df, cols):
    """DataFrame -> list tuple bertipe Python biasa (NaN/NA jadi None)."""
    rows = []
    for tup in df[cols].itertuples(index=False, name=None):
        rows.append(tuple(
            None if pd.isna(v) else (v.item() if hasattr(v, "item") else v)
            for v in tup))
    return rows


def load_dim(cur, tabel, df, cols, oltp_col):
    updates = ", ".join(f"{c}=VALUES({c})" for c in cols if c != oltp_col)
    sql = (f"INSERT INTO {tabel} ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))}) "
           f"ON DUPLICATE KEY UPDATE {updates}")
    cur.executemany(sql, to_rows(df, cols))
    id_col = "id_" + tabel.replace("dim_", "")
    cur.execute(f"SELECT {oltp_col}, {id_col} FROM {tabel}")
    return {oltp: sk for oltp, sk in cur.fetchall()}


def insert_fact(cur, tabel, df, cols):
    sql = (f"INSERT INTO {tabel} ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))})")
    cur.executemany(sql, to_rows(df, cols))
    return len(df)


def map_keys(df, toko_map, produk_map=None):
    df = df.copy()
    df["id_toko"] = df["store_id"].map(toko_map)
    if produk_map is not None:
        df["id_produk"] = df["product_id"].map(produk_map)
    kunci = ["id_toko"] + (["id_produk"] if produk_map is not None else [])
    hilang = int(df[kunci].isna().any(axis=1).sum())
    if hilang:
        print(f"  PERINGATAN: {hilang} baris dibuang karena kunci dimensi tidak ketemu")
    df = df.dropna(subset=kunci)
    for k in kunci:
        df[k] = df[k].astype(int)
    return df


def check(label, nilai_sumber, nilai_dw, toleransi=0.01):
    ok = abs(float(nilai_sumber) - float(nilai_dw)) <= toleransi
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: sumber={nilai_sumber} | DW={nilai_dw}")
    return ok


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv

    print("=== EXTRACT: baca CSV bersih ===")
    d = read_clean()
    for k, v in d.items():
        print(f"  {k:<12} {len(v):>6} baris")

    print("\n=== TRANSFORM ===")
    snapshot = int(datetime.now().strftime("%Y%m"))
    dim_toko = build_dim_toko(d["stores"])
    dim_produk = build_dim_produk(d["products"], d["brands"], d["categories"])
    f_jual = build_fakta_penjualan(d["orders"], d["order_items"])
    f_kirim = build_fakta_pengiriman(d["orders"])
    f_stok = build_fakta_stok(d["stocks"], d["products"], snapshot)
    print(f"  dim_toko                 {len(dim_toko):>6} baris")
    print(f"  dim_produk               {len(dim_produk):>6} baris")
    print(f"  fakta_penjualan_bulanan  {len(f_jual):>6} baris")
    print(f"  fakta_pengiriman_bulanan {len(f_kirim):>6} baris")
    print(f"  fakta_stok_toko          {len(f_stok):>6} baris (snapshot {snapshot})")

    # Total kontrol dari sumber, dipakai untuk rekonsiliasi setelah load
    ctl = {
        "qty": int(f_jual["total_qty"].sum()),
        "bersih": round(float(f_jual["total_penjualan_bersih"].sum()), 2),
        "order": int(f_kirim["jumlah_order"].sum()),
        "stok": int(f_stok["qty_stok"].sum()),
        "nilai_stok": round(float(f_stok["nilai_stok"].sum()), 2),
    }

    if dry_run:
        print("\n[DRY RUN] Tidak ada yang ditulis ke database.")
        print("Total kontrol dari sumber:", ctl)
        print("\nContoh fakta_penjualan_bulanan:")
        print(f_jual.head().to_string(index=False))
        return

    print("\n=== LOAD ke MySQL ===")
    try:
        import mysql.connector
    except ImportError:
        raise SystemExit("Jalankan dulu: pip install mysql-connector-python")

    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor()
    try:
        toko_map = load_dim(cur, "dim_toko", dim_toko,
                            ["nama_toko", "kota", "provinsi", "kode_pos",
                             "telepon", "email", "id_toko_oltp"], "id_toko_oltp")
        produk_map = load_dim(cur, "dim_produk", dim_produk,
                              ["nama_produk", "merek", "kategori", "tahun_model",
                               "harga_list", "id_produk_oltp"], "id_produk_oltp")
        print(f"  dim_toko   : {len(toko_map)} baris di DW")
        print(f"  dim_produk : {len(produk_map)} baris di DW")

        # Fakta penjualan dan pengiriman: muat ulang penuh
        cur.execute("DELETE FROM fakta_penjualan_bulanan")
        cur.execute("DELETE FROM fakta_pengiriman_bulanan")
        j = map_keys(f_jual, toko_map, produk_map)
        n1 = insert_fact(cur, "fakta_penjualan_bulanan", j,
                         ["id_waktu", "id_toko", "id_produk", "total_qty",
                          "total_penjualan_kotor", "total_diskon",
                          "total_penjualan_bersih"])
        k = map_keys(f_kirim, toko_map)
        n2 = insert_fact(cur, "fakta_pengiriman_bulanan", k,
                         ["id_waktu", "id_toko", "jumlah_order",
                          "jumlah_order_dikirim", "jumlah_order_ditolak",
                          "jumlah_terlambat", "total_hari_proses"])

        # Fakta stok: ganti snapshot bulan ini saja
        cur.execute("DELETE FROM fakta_stok_toko WHERE id_waktu = %s", (snapshot,))
        s = map_keys(f_stok, toko_map, produk_map)
        n3 = insert_fact(cur, "fakta_stok_toko", s,
                         ["id_waktu", "id_toko", "id_produk", "qty_stok", "nilai_stok"])
        print(f"  fakta_penjualan_bulanan  : {n1} baris dimuat")
        print(f"  fakta_pengiriman_bulanan : {n2} baris dimuat")
        print(f"  fakta_stok_toko          : {n3} baris dimuat")
        conn.commit()

        print("\n=== REKONSILIASI (sumber vs DW) ===")
        cur.execute("SELECT SUM(total_qty), SUM(total_penjualan_bersih) "
                    "FROM fakta_penjualan_bulanan")
        qty, bersih = cur.fetchone()
        cur.execute("SELECT SUM(jumlah_order) FROM fakta_pengiriman_bulanan")
        (order,) = cur.fetchone()
        cur.execute("SELECT SUM(qty_stok), SUM(nilai_stok) FROM fakta_stok_toko "
                    "WHERE id_waktu = %s", (snapshot,))
        stok, nilai_stok = cur.fetchone()
        hasil = [
            check("total_qty penjualan", ctl["qty"], qty or 0),
            check("total_penjualan_bersih", ctl["bersih"], bersih or 0),
            check("jumlah_order", ctl["order"], order or 0),
            check("qty_stok", ctl["stok"], stok or 0),
            check("nilai_stok", ctl["nilai_stok"], nilai_stok or 0),
        ]
        print("\nHASIL:", "SEMUA PASS" if all(hasil) else "ADA YANG FAIL, periksa log di atas")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
