# -*- coding: utf-8 -*-
"""
cleansing_tools.py
==================
Tools profiling dan cleansing data untuk dataset Bike Store (Kaggle).

Letak file:
    Pangdat/
        archive/            <- 9 file CSV dataset
        cleansing_tools.py  <- file ini

Cara menjalankan (dari folder Pangdat):
    pip install pandas
    python cleansing_tools.py

Hasil (folder Pangdat/output):
    cleaned/*.csv            data bersih (siap di-load ke DWH)
    dq_log.csv               log tiap aturan: baris dicek, bermasalah, tindakan
    dq_reject.csv            baris yang ditolak beserta alasannya
    profil_sebelum.csv       profiling kolom sebelum cleansing
    profil_sesudah.csv       profiling kolom sesudah cleansing
    ringkasan_baris.csv      jumlah baris sebelum dan sesudah per tabel

Alur (mengacu Bab 10 buku): load apa adanya -> profiling -> aturan DQ ->
cleansing di staging -> baris gagal masuk dq_reject (tidak dihapus diam-diam).
"""

import json
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# KONFIGURASI
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "archive"
OUT_DIR = BASE_DIR / "output"
CLEAN_DIR = OUT_DIR / "cleaned"

TABLES = ["brands", "categories", "customers", "order_items",
          "orders", "products", "staffs", "stocks", "stores"]

# Dataset ini mengekspor nilai kosong sebagai teks 'NULL'
NULL_TOKENS = ["NULL", "null", "Null", "NaN", "nan", "N/A", "n/a", ""]

VALID_ORDER_STATUS = [1, 2, 3, 4]   # 1 Pending, 2 Processing, 3 Rejected, 4 Completed
STATUS_COMPLETED = 4
MODEL_YEAR_MIN = 2000
MODEL_YEAR_MAX = date.today().year + 1

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

LOG = []      # catatan hasil tiap aturan
REJECT = []   # baris yang ditolak


# ----------------------------------------------------------------------------
# FUNGSI BANTU: LOG DAN ATURAN
# ----------------------------------------------------------------------------
def log_rule(tabel, karakteristik, aturan, prioritas, dicek, bermasalah, tindakan):
    LOG.append({
        "tabel": tabel,
        "karakteristik": karakteristik,
        "aturan": aturan,
        "prioritas": prioritas,
        "baris_dicek": int(dicek),
        "baris_bermasalah": int(bermasalah),
        "tindakan": tindakan,
    })
    status = "OK    " if bermasalah == 0 else "TEMUAN"
    print(f"  [{status}] {tabel:<11} {aturan} -> {bermasalah}/{dicek}")


def apply_rule(df, mask, tabel, karakteristik, aturan, prioritas, tindakan,
               reject=True):
    """Terapkan satu aturan. mask=True artinya baris melanggar aturan.
    reject=True  : baris dipindah ke dq_reject.
    reject=False : baris hanya ditandai di log (tetap dipakai)."""
    mask = mask.fillna(False).astype(bool)
    n_bad = int(mask.sum())
    log_rule(tabel, karakteristik, aturan, prioritas, len(df), n_bad,
             tindakan if n_bad else "-")
    if n_bad and reject:
        for idx, row in df[mask].iterrows():
            data = {k: (None if pd.isna(v) else str(v)) for k, v in row.items()}
            REJECT.append({
                "tabel": tabel,
                "baris_csv": int(idx) + 2,   # +2: header dan indeks mulai 0
                "karakteristik": karakteristik,
                "aturan": aturan,
                "data_baris": json.dumps(data, ensure_ascii=False),
            })
        return df[~mask].copy()
    return df


def strip_all(df, tabel):
    """Trim spasi, rapikan spasi ganda, string kosong menjadi NULL."""
    df = df.copy()
    changed = 0
    for c in df.columns:
        before = df[c].astype("string")
        s = before.str.strip().str.replace(r"\s+", " ", regex=True)
        s = s.mask(s == "")
        changed += int(((before != s) & before.notna() & s.notna()).sum())
        df[c] = s.astype(object).where(s.notna(), np.nan)
    log_rule(tabel, "Consistency", "Spasi berlebih di awal/akhir/tengah teks",
             "Rendah", len(df), changed, "Dirapikan" if changed else "-")
    return df


def to_num(df, cols, tabel, integer=False, prioritas="Tinggi"):
    for c in cols:
        conv = pd.to_numeric(df[c], errors="coerce")
        bad = df[c].notna() & conv.isna()
        if integer:
            bad = bad | (conv.notna() & (conv % 1 != 0))
        aturan = f"{c} harus berupa angka" + (" bulat" if integer else "")
        df = apply_rule(df, bad, tabel, "Data Type", aturan, prioritas,
                        "Dipindah ke dq_reject")
        conv = conv.loc[df.index]
        df[c] = conv.astype("Int64") if integer else conv
    return df


def to_date(df, cols, tabel, prioritas="Menengah"):
    for c in cols:
        conv = pd.to_datetime(df[c], errors="coerce")
        bad = df[c].notna() & conv.isna()
        df = apply_rule(df, bad, tabel, "Data Type",
                        f"{c} harus tanggal valid", prioritas,
                        "Dipindah ke dq_reject")
        df[c] = conv.loc[df.index]
    return df


def require(df, cols, tabel, prioritas="Tinggi"):
    for c in cols:
        df = apply_rule(df, df[c].isna(), tabel, "Completeness",
                        f"{c} wajib terisi", prioritas, "Dipindah ke dq_reject")
    return df


def dedupe(df, subset, tabel, label):
    mask = df.duplicated(subset=subset, keep="first")
    return apply_rule(df, mask, tabel, "Duplication", f"Duplikat {label}",
                      "Tinggi", "Baris pertama dipertahankan, sisanya ke dq_reject")


def check_fk(df, col, valid_ids, tabel, ref, reject=True, prioritas="Tinggi"):
    mask = df[col].notna() & ~df[col].isin(list(valid_ids))
    return apply_rule(df, mask, tabel, "Integritas referensial",
                      f"{col} harus ada di {ref}", prioritas,
                      "Dipindah ke dq_reject" if reject else "Hanya ditandai",
                      reject=reject)


def log_nulls(df, tabel, cols, prioritas, tindakan):
    for c in cols:
        n = int(df[c].isna().sum())
        log_rule(tabel, "Completeness", f"Nilai kosong pada {c}", prioritas,
                 len(df), n, tindakan if n else "-")


def standardize(df, col, func, tabel, aturan, prioritas="Rendah"):
    before = df[col].copy()
    after = before.map(lambda v: func(v) if pd.notna(v) else v)
    changed = int(((before != after) & before.notna()).sum())
    log_rule(tabel, "Consistency", aturan, prioritas, int(before.notna().sum()),
             changed, "Distandarkan" if changed else "-")
    df[col] = after
    return df


def fix_zip(v):
    digits = re.sub(r"\D", "", str(v))
    if digits and len(digits) < 5:
        return digits.zfill(5)
    return str(v)


def outlier_flag(df, col, tabel):
    """Outlier dengan aturan IQR (hanya ditandai, tidak dibuang)."""
    q1, q3 = df[col].quantile(0.25), df[col].quantile(0.75)
    iqr = q3 - q1
    mask = (df[col] < q1 - 3 * iqr) | (df[col] > q3 + 3 * iqr)
    apply_rule(df, mask, tabel, "Data Anomaly",
               f"Outlier {col} (IQR x3)", "Menengah", "Hanya ditandai",
               reject=False)


# ----------------------------------------------------------------------------
# PROFILING (Bab 10.4: column profiling)
# ----------------------------------------------------------------------------
def profile(df, tabel, tahap):
    rows = []
    for c in df.columns:
        s = df[c]
        n = len(df)
        mn, mx = "", ""
        if pd.api.types.is_datetime64_any_dtype(s):
            if s.notna().any():
                mn, mx = str(s.min().date()), str(s.max().date())
        else:
            num = pd.to_numeric(s, errors="coerce")
            if s.notna().any() and num.notna().sum() == s.notna().sum():
                mn, mx = str(num.min()), str(num.max())
        rows.append({
            "tahap": tahap, "tabel": tabel, "kolom": c, "baris": n,
            "jumlah_null": int(s.isna().sum()),
            "persen_null": round(100 * s.isna().mean(), 2) if n else 0,
            "jumlah_unik": int(s.nunique(dropna=True)),
            "min": mn, "max": mx,
            "contoh_nilai": str(s.dropna().iloc[0]) if s.notna().any() else "",
        })
    return rows


# ----------------------------------------------------------------------------
# LOAD DATA (staging)
# ----------------------------------------------------------------------------
def load_tables():
    if not DATA_DIR.exists():
        raise SystemExit(f"Folder dataset tidak ditemukan: {DATA_DIR}")
    raw = {}
    for t in TABLES:
        path = DATA_DIR / f"{t}.csv"
        if not path.exists():
            raise SystemExit(f"File tidak ditemukan: {path}")
        # dtype=str: semua dibaca sebagai teks dulu agar masalah tipe data
        # bisa dideteksi, bukan ditelan diam-diam oleh pandas
        raw[t] = pd.read_csv(path, dtype=str, na_values=NULL_TOKENS,
                             keep_default_na=False, encoding="utf-8-sig")
        print(f"  dimuat {t:<12} {len(raw[t]):>6} baris, {raw[t].shape[1]} kolom")
    return raw


# ----------------------------------------------------------------------------
# CLEANSING PER TABEL
# ----------------------------------------------------------------------------
def clean_brands(df):
    t = "brands"
    df = strip_all(df, t)
    df = to_num(df, ["brand_id"], t, integer=True)
    df = require(df, ["brand_id", "brand_name"], t)
    df = dedupe(df, ["brand_id"], t, "brand_id")
    df = standardize(df, "brand_name", str.title, t, "Penulisan brand_name (Title Case)")
    return df


def clean_categories(df):
    t = "categories"
    df = strip_all(df, t)
    df = to_num(df, ["category_id"], t, integer=True)
    df = require(df, ["category_id", "category_name"], t)
    df = dedupe(df, ["category_id"], t, "category_id")
    df = standardize(df, "category_name", str.title, t, "Penulisan category_name (Title Case)")
    return df


def clean_stores(df):
    t = "stores"
    df = strip_all(df, t)
    df = to_num(df, ["store_id"], t, integer=True)
    df = require(df, ["store_id", "store_name"], t)
    df = dedupe(df, ["store_id"], t, "store_id")
    df = standardize(df, "store_name", str.title, t, "Penulisan store_name (Title Case)")
    df = standardize(df, "city", str.title, t, "Penulisan city (Title Case)")
    df = standardize(df, "state", str.upper, t, "Kode state huruf besar")
    df = standardize(df, "email", str.lower, t, "Email huruf kecil")
    df = standardize(df, "zip_code", fix_zip, t, "zip_code 5 digit (teks)")
    log_nulls(df, t, ["phone", "email"], "Rendah", "Dibiarkan NULL")
    return df


def clean_customers(df):
    t = "customers"
    df = strip_all(df, t)
    df = to_num(df, ["customer_id"], t, integer=True)
    df = require(df, ["customer_id", "first_name", "last_name"], t)
    df = dedupe(df, ["customer_id"], t, "customer_id")
    for c in ["first_name", "last_name", "city"]:
        df = standardize(df, c, str.title, t, f"Penulisan {c} (Title Case)")
    df = standardize(df, "email", str.lower, t, "Email huruf kecil")
    df = standardize(df, "state", str.upper, t, "Kode state huruf besar")
    df = standardize(df, "zip_code", fix_zip, t, "zip_code 5 digit (teks)")

    # Validitas format (hanya ditandai, prioritas rendah)
    bad_email = df["email"].notna() & ~df["email"].fillna("").map(
        lambda v: bool(EMAIL_RE.match(v)))
    apply_rule(df, bad_email, t, "Accuracy", "Format email tidak valid",
               "Rendah", "Hanya ditandai", reject=False)
    bad_state = df["state"].notna() & (df["state"].fillna("").str.len() != 2)
    apply_rule(df, bad_state, t, "Domain integrity",
               "state harus 2 huruf", "Rendah", "Hanya ditandai", reject=False)

    # Dependency analysis: satu kota harus berada di satu state
    multi = df.dropna(subset=["city", "state"]).groupby("city")["state"].nunique()
    apply_rule(df, df["city"].isin(multi[multi > 1].index), t, "Consistency",
               "Satu kota berada di lebih dari satu state", "Menengah",
               "Hanya ditandai", reject=False)

    # Kemungkinan pelanggan ganda (nama + email sama, id berbeda)
    dup = df["email"].notna() & df.duplicated(
        ["first_name", "last_name", "email"], keep=False)
    apply_rule(df, dup, t, "Duplication",
               "Kemungkinan pelanggan ganda (nama+email sama)", "Menengah",
               "Hanya ditandai, perlu tinjau manual", reject=False)

    log_nulls(df, t, ["phone", "email", "street"], "Rendah",
              "phone diisi 'Tidak diketahui'; lainnya dibiarkan NULL")
    df["phone"] = df["phone"].fillna("Tidak diketahui")
    return df


def clean_products(df, brand_ids, category_ids):
    t = "products"
    df = strip_all(df, t)
    df = to_num(df, ["product_id", "brand_id", "category_id", "model_year"],
                t, integer=True)
    df = to_num(df, ["list_price"], t)
    df = require(df, ["product_id", "product_name", "brand_id",
                      "category_id", "list_price"], t)
    df = dedupe(df, ["product_id"], t, "product_id")
    df = apply_rule(df, df["list_price"] <= 0, t, "Domain integrity",
                    "list_price harus lebih dari 0", "Tinggi",
                    "Dipindah ke dq_reject")
    df = apply_rule(df, df["model_year"].notna() &
                    ~df["model_year"].between(MODEL_YEAR_MIN, MODEL_YEAR_MAX),
                    t, "Domain integrity",
                    f"model_year harus {MODEL_YEAR_MIN}-{MODEL_YEAR_MAX}",
                    "Menengah", "Dipindah ke dq_reject")
    df = check_fk(df, "brand_id", brand_ids, t, "brands")
    df = check_fk(df, "category_id", category_ids, t, "categories")
    apply_rule(df, df.duplicated(["product_name"], keep=False), t, "Duplication",
               "Nama produk kembar", "Menengah", "Hanya ditandai", reject=False)
    return df


def clean_staffs(df, store_ids):
    t = "staffs"
    df = strip_all(df, t)
    df = to_num(df, ["staff_id", "store_id", "manager_id", "active"], t,
                integer=True, prioritas="Rendah")
    df = require(df, ["staff_id"], t)
    df = dedupe(df, ["staff_id"], t, "staff_id")
    for c in ["first_name", "last_name"]:
        df = standardize(df, c, str.title, t, f"Penulisan {c} (Title Case)")
    df = standardize(df, "email", str.lower, t, "Email huruf kecil")
    apply_rule(df, df["active"].notna() & ~df["active"].isin([0, 1]), t,
               "Domain integrity", "active harus 0 atau 1", "Rendah",
               "Hanya ditandai", reject=False)
    df = check_fk(df, "store_id", store_ids, t, "stores", reject=False,
                  prioritas="Rendah")
    df = check_fk(df, "manager_id", df["staff_id"].dropna(), t, "staffs",
                  reject=False, prioritas="Rendah")
    return df


def clean_orders(df, store_ids, customer_ids, staff_ids):
    t = "orders"
    df = strip_all(df, t)
    df = to_num(df, ["order_id", "customer_id", "order_status",
                     "store_id", "staff_id"], t, integer=True)
    df = to_date(df, ["order_date", "required_date", "shipped_date"], t)
    df = require(df, ["order_id", "customer_id", "order_status",
                      "order_date", "store_id"], t)
    df = dedupe(df, ["order_id"], t, "order_id")
    df = apply_rule(df, ~df["order_status"].isin(VALID_ORDER_STATUS), t,
                    "Domain integrity", "order_status harus 1-4", "Tinggi",
                    "Dipindah ke dq_reject")
    df = apply_rule(df, df["required_date"] < df["order_date"], t,
                    "Business rule", "required_date tidak boleh sebelum order_date",
                    "Menengah", "Dipindah ke dq_reject")
    df = apply_rule(df, df["shipped_date"] < df["order_date"], t,
                    "Business rule", "shipped_date tidak boleh sebelum order_date",
                    "Menengah", "Dipindah ke dq_reject")
    df = apply_rule(df, (df["order_status"] == STATUS_COMPLETED) &
                    df["shipped_date"].isna(), t, "Business rule",
                    "Order Completed wajib punya shipped_date", "Tinggi",
                    "Dipindah ke dq_reject")
    apply_rule(df, (df["order_status"] != STATUS_COMPLETED) &
               df["shipped_date"].notna(), t, "Business rule",
               "Order non-Completed tetapi punya shipped_date", "Menengah",
               "Hanya ditandai", reject=False)
    df = check_fk(df, "store_id", store_ids, t, "stores")
    df = check_fk(df, "customer_id", customer_ids, t, "customers",
                  reject=False, prioritas="Menengah")
    df = check_fk(df, "staff_id", staff_ids, t, "staffs",
                  reject=False, prioritas="Rendah")
    return df


def clean_order_items(df, order_ids, product_ids):
    t = "order_items"
    df = strip_all(df, t)
    df = to_num(df, ["order_id", "item_id", "product_id", "quantity"], t,
                integer=True)
    df = to_num(df, ["list_price", "discount"], t)
    df = require(df, ["order_id", "item_id", "product_id", "quantity",
                      "list_price"], t)
    log_nulls(df, t, ["discount"], "Tinggi", "Diisi 0 (tanpa diskon)")
    df["discount"] = df["discount"].fillna(0.0)
    df = dedupe(df, ["order_id", "item_id"], t, "(order_id, item_id)")
    df = apply_rule(df, df["quantity"] <= 0, t, "Domain integrity",
                    "quantity harus lebih dari 0", "Tinggi",
                    "Dipindah ke dq_reject")
    df = apply_rule(df, df["list_price"] <= 0, t, "Domain integrity",
                    "list_price harus lebih dari 0", "Tinggi",
                    "Dipindah ke dq_reject")
    df = apply_rule(df, ~df["discount"].between(0, 1), t, "Domain integrity",
                    "discount harus 0 sampai 1", "Tinggi",
                    "Dipindah ke dq_reject")
    df = check_fk(df, "order_id", order_ids, t, "orders (bersih)")
    df = check_fk(df, "product_id", product_ids, t, "products (bersih)")
    outlier_flag(df, "list_price", t)
    outlier_flag(df, "quantity", t)
    # Kolom turunan untuk fakta_penjualan
    df["nilai_kotor"] = (df["quantity"] * df["list_price"]).round(2)
    df["nilai_diskon"] = (df["nilai_kotor"] * df["discount"]).round(2)
    df["nilai_bersih"] = (df["nilai_kotor"] - df["nilai_diskon"]).round(2)
    return df


def clean_stocks(df, store_ids, product_ids):
    t = "stocks"
    df = strip_all(df, t)
    df = to_num(df, ["store_id", "product_id", "quantity"], t, integer=True)
    df = require(df, ["store_id", "product_id", "quantity"], t)
    df = dedupe(df, ["store_id", "product_id"], t, "(store_id, product_id)")
    df = apply_rule(df, df["quantity"] < 0, t, "Domain integrity",
                    "quantity stok tidak boleh negatif", "Tinggi",
                    "Dipindah ke dq_reject")
    df = check_fk(df, "store_id", store_ids, t, "stores (bersih)")
    df = check_fk(df, "product_id", product_ids, t, "products (bersih)")
    return df


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    OUT_DIR.mkdir(exist_ok=True)
    CLEAN_DIR.mkdir(exist_ok=True)

    print("\n=== 1. LOAD KE STAGING ===")
    raw = load_tables()

    print("\n=== 2. PROFILING SEBELUM CLEANSING ===")
    profil_sebelum = []
    for t in TABLES:
        profil_sebelum += profile(raw[t], t, "sebelum")
    pd.DataFrame(profil_sebelum).to_csv(OUT_DIR / "profil_sebelum.csv",
                                        index=False, encoding="utf-8-sig")
    print("  tersimpan: output/profil_sebelum.csv")

    print("\n=== 3. CLEANSING (urutan mengikuti dependensi FK) ===")
    clean = {}
    clean["brands"] = clean_brands(raw["brands"])
    clean["categories"] = clean_categories(raw["categories"])
    clean["stores"] = clean_stores(raw["stores"])
    clean["customers"] = clean_customers(raw["customers"])
    clean["products"] = clean_products(
        raw["products"], clean["brands"]["brand_id"].dropna(),
        clean["categories"]["category_id"].dropna())
    clean["staffs"] = clean_staffs(raw["staffs"],
                                   clean["stores"]["store_id"].dropna())
    clean["orders"] = clean_orders(
        raw["orders"], clean["stores"]["store_id"].dropna(),
        clean["customers"]["customer_id"].dropna(),
        clean["staffs"]["staff_id"].dropna())
    clean["order_items"] = clean_order_items(
        raw["order_items"], clean["orders"]["order_id"].dropna(),
        clean["products"]["product_id"].dropna())
    clean["stocks"] = clean_stocks(
        raw["stocks"], clean["stores"]["store_id"].dropna(),
        clean["products"]["product_id"].dropna())

    print("\n=== 4. SIMPAN HASIL ===")
    profil_sesudah = []
    ringkasan = []
    for t in TABLES:
        clean[t].to_csv(CLEAN_DIR / f"{t}.csv", index=False, encoding="utf-8-sig")
        profil_sesudah += profile(clean[t], t, "sesudah")
        ringkasan.append({"tabel": t, "baris_sebelum": len(raw[t]),
                          "baris_sesudah": len(clean[t]),
                          "baris_ditolak": len(raw[t]) - len(clean[t])})
    pd.DataFrame(profil_sesudah).to_csv(OUT_DIR / "profil_sesudah.csv",
                                        index=False, encoding="utf-8-sig")
    pd.DataFrame(ringkasan).to_csv(OUT_DIR / "ringkasan_baris.csv",
                                   index=False, encoding="utf-8-sig")
    pd.DataFrame(LOG).to_csv(OUT_DIR / "dq_log.csv", index=False,
                             encoding="utf-8-sig")
    rej_cols = ["tabel", "baris_csv", "karakteristik", "aturan", "data_baris"]
    pd.DataFrame(REJECT, columns=rej_cols).to_csv(
        OUT_DIR / "dq_reject.csv", index=False, encoding="utf-8-sig")

    print("\n=== RINGKASAN ===")
    print(pd.DataFrame(ringkasan).to_string(index=False))
    temuan = [r for r in LOG if r["baris_bermasalah"] > 0]
    print(f"\nAturan dijalankan : {len(LOG)}")
    print(f"Aturan bertemuan  : {len(temuan)}")
    print(f"Baris ditolak     : {len(REJECT)}  (lihat output/dq_reject.csv)")
    print(f"Data bersih       : {CLEAN_DIR}")


if __name__ == "__main__":
    main()