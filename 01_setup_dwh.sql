-- =============================================================================
-- 01_setup_dwh.sql
-- Setup Data Warehouse Bike Store (MySQL 8.0.16 ke atas)
-- Isi: database, 3 tabel dimensi, 3 tabel fakta, dan pengisian dim_waktu.
-- Jalankan sekali (aman diulang: tabel lama akan di-drop dulu).
-- =============================================================================

CREATE DATABASE IF NOT EXISTS dw_bikestores
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE dw_bikestores;

SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS fakta_penjualan_bulanan;
DROP TABLE IF EXISTS fakta_pengiriman_bulanan;
DROP TABLE IF EXISTS fakta_stok_toko;
DROP TABLE IF EXISTS dim_waktu;
DROP TABLE IF EXISTS dim_produk;
DROP TABLE IF EXISTS dim_toko;
SET FOREIGN_KEY_CHECKS = 1;

-- -----------------------------------------------------------------------------
-- DIMENSI
-- -----------------------------------------------------------------------------
CREATE TABLE dim_waktu (
    id_waktu    INT          NOT NULL COMMENT 'Format YYYYMM, contoh 201601',
    tahun       INT          NOT NULL,
    bulan       TINYINT      NOT NULL,
    nama_bulan  VARCHAR(20)  NOT NULL,
    kuartal     VARCHAR(5)   NOT NULL,
    PRIMARY KEY (id_waktu),
    CONSTRAINT chk_dim_waktu_bulan CHECK (bulan BETWEEN 1 AND 12)
) ENGINE = InnoDB;

CREATE TABLE dim_produk (
    id_produk       INT           NOT NULL AUTO_INCREMENT COMMENT 'Surrogate key',
    nama_produk     VARCHAR(255)  NOT NULL,
    merek           VARCHAR(50),
    kategori        VARCHAR(50),
    tahun_model     INT,
    harga_list      DECIMAL(10,2),
    id_produk_oltp  INT           NOT NULL COMMENT 'Lineage: product_id di OLTP',
    PRIMARY KEY (id_produk),
    UNIQUE KEY uq_dim_produk_oltp (id_produk_oltp)
) ENGINE = InnoDB;

CREATE TABLE dim_toko (
    id_toko        INT           NOT NULL AUTO_INCREMENT COMMENT 'Surrogate key',
    nama_toko      VARCHAR(100)  NOT NULL,
    kota           VARCHAR(50),
    provinsi       VARCHAR(10),
    kode_pos       VARCHAR(10),
    telepon        VARCHAR(25),
    email          VARCHAR(100),
    id_toko_oltp   INT           NOT NULL COMMENT 'Lineage: store_id di OLTP',
    PRIMARY KEY (id_toko),
    UNIQUE KEY uq_dim_toko_oltp (id_toko_oltp)
) ENGINE = InnoDB;

-- -----------------------------------------------------------------------------
-- FAKTA 1: penjualan per produk per toko per bulan order
-- -----------------------------------------------------------------------------
CREATE TABLE fakta_penjualan_bulanan (
    id_fakta                INT            NOT NULL AUTO_INCREMENT,
    id_waktu                INT            NOT NULL,
    id_toko                 INT            NOT NULL,
    id_produk               INT            NOT NULL,
    total_qty               INT            NOT NULL,
    total_penjualan_kotor   DECIMAL(15,2)  NOT NULL,
    total_diskon            DECIMAL(15,2)  NOT NULL,
    total_penjualan_bersih  DECIMAL(15,2)  NOT NULL,
    PRIMARY KEY (id_fakta),
    UNIQUE KEY uq_penjualan_grain (id_waktu, id_toko, id_produk),
    CONSTRAINT fk_penjualan_waktu  FOREIGN KEY (id_waktu)  REFERENCES dim_waktu  (id_waktu),
    CONSTRAINT fk_penjualan_toko   FOREIGN KEY (id_toko)   REFERENCES dim_toko   (id_toko),
    CONSTRAINT fk_penjualan_produk FOREIGN KEY (id_produk) REFERENCES dim_produk (id_produk)
) ENGINE = InnoDB;

-- -----------------------------------------------------------------------------
-- FAKTA 2: pengiriman (pemenuhan order) per toko per bulan order
-- -----------------------------------------------------------------------------
CREATE TABLE fakta_pengiriman_bulanan (
    id_fakta_pengiriman   INT  NOT NULL AUTO_INCREMENT,
    id_waktu              INT  NOT NULL,
    id_toko               INT  NOT NULL,
    jumlah_order          INT  NOT NULL,
    jumlah_order_dikirim  INT  NOT NULL,
    jumlah_order_ditolak  INT  NOT NULL,
    jumlah_terlambat      INT  NOT NULL,
    total_hari_proses     INT  NOT NULL,
    PRIMARY KEY (id_fakta_pengiriman),
    UNIQUE KEY uq_pengiriman_grain (id_waktu, id_toko),
    CONSTRAINT fk_pengiriman_waktu FOREIGN KEY (id_waktu) REFERENCES dim_waktu (id_waktu),
    CONSTRAINT fk_pengiriman_toko  FOREIGN KEY (id_toko)  REFERENCES dim_toko  (id_toko)
) ENGINE = InnoDB;

-- -----------------------------------------------------------------------------
-- FAKTA 3: stok (periodic snapshot) per produk per toko per bulan snapshot
-- -----------------------------------------------------------------------------
CREATE TABLE fakta_stok_toko (
    id_fakta_stok  INT            NOT NULL AUTO_INCREMENT,
    id_waktu       INT            NOT NULL COMMENT 'Bulan snapshot',
    id_toko        INT            NOT NULL,
    id_produk      INT            NOT NULL,
    qty_stok       INT            NOT NULL,
    nilai_stok     DECIMAL(15,2)  NOT NULL COMMENT 'qty_stok x harga_list (semi-aditif)',
    PRIMARY KEY (id_fakta_stok),
    UNIQUE KEY uq_stok_grain (id_waktu, id_toko, id_produk),
    CONSTRAINT fk_stok_waktu  FOREIGN KEY (id_waktu)  REFERENCES dim_waktu  (id_waktu),
    CONSTRAINT fk_stok_toko   FOREIGN KEY (id_toko)   REFERENCES dim_toko   (id_toko),
    CONSTRAINT fk_stok_produk FOREIGN KEY (id_produk) REFERENCES dim_produk (id_produk)
) ENGINE = InnoDB;

-- -----------------------------------------------------------------------------
-- ISI dim_waktu: Januari 2016 sampai Desember 2027 (144 baris)
-- Mencakup periode order dan bulan snapshot stok.
-- -----------------------------------------------------------------------------
INSERT INTO dim_waktu (id_waktu, tahun, bulan, nama_bulan, kuartal)
WITH RECURSIVE bln AS (
    SELECT DATE('2016-01-01') AS tgl
    UNION ALL
    SELECT DATE_ADD(tgl, INTERVAL 1 MONTH) FROM bln WHERE tgl < '2027-12-01'
)
SELECT YEAR(tgl) * 100 + MONTH(tgl),
       YEAR(tgl),
       MONTH(tgl),
       ELT(MONTH(tgl), 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
                       'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember'),
       CONCAT('Q', QUARTER(tgl))
FROM bln;

-- -----------------------------------------------------------------------------
-- CEK HASIL SETUP
-- -----------------------------------------------------------------------------
SHOW TABLES;
SELECT COUNT(*) AS baris_dim_waktu, MIN(id_waktu) AS awal, MAX(id_waktu) AS akhir
FROM dim_waktu;   -- harapan: 144, 201601, 202712
