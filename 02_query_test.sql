-- =============================================================================
-- 02_query_test.sql
-- Query uji untuk Data Warehouse Bike Store (MySQL 8.0 ke atas)
-- Jalankan SETELAH 01_setup_dwh.sql dan etl_dwh.py.
-- Bagian A-B: uji kebenaran data. Bagian C: query analitik (bukti DWH berfungsi).
-- =============================================================================
USE dw_bikestores;

-- =============================================================================
-- A. SANITY CHECK: tabel terisi?
-- =============================================================================
SELECT 'dim_waktu' AS tabel, COUNT(*) AS baris FROM dim_waktu
UNION ALL SELECT 'dim_produk', COUNT(*) FROM dim_produk
UNION ALL SELECT 'dim_toko', COUNT(*) FROM dim_toko
UNION ALL SELECT 'fakta_penjualan_bulanan', COUNT(*) FROM fakta_penjualan_bulanan
UNION ALL SELECT 'fakta_pengiriman_bulanan', COUNT(*) FROM fakta_pengiriman_bulanan
UNION ALL SELECT 'fakta_stok_toko', COUNT(*) FROM fakta_stok_toko;
-- Harapan: dim_waktu 144, dim_toko 3, semua tabel lain > 0.

-- Rentang waktu data penjualan
SELECT MIN(id_waktu) AS bulan_awal, MAX(id_waktu) AS bulan_akhir,
       COUNT(DISTINCT id_waktu) AS jumlah_bulan
FROM fakta_penjualan_bulanan;

-- =============================================================================
-- B. UJI INTEGRITAS (harapan: SEMUA mengembalikan 0 baris / nilai 0)
-- =============================================================================

-- B1. Duplikasi pada grain (satu kombinasi harus satu baris)
SELECT 'penjualan' AS fakta, id_waktu, id_toko, id_produk, COUNT(*) AS n
FROM fakta_penjualan_bulanan
GROUP BY id_waktu, id_toko, id_produk HAVING COUNT(*) > 1;

SELECT 'pengiriman' AS fakta, id_waktu, id_toko, COUNT(*) AS n
FROM fakta_pengiriman_bulanan
GROUP BY id_waktu, id_toko HAVING COUNT(*) > 1;

SELECT 'stok' AS fakta, id_waktu, id_toko, id_produk, COUNT(*) AS n
FROM fakta_stok_toko
GROUP BY id_waktu, id_toko, id_produk HAVING COUNT(*) > 1;

-- B2. Foreign key yatim (orphan). FK sudah dijaga constraint, ini pemeriksaan ganda.
SELECT f.id_fakta FROM fakta_penjualan_bulanan f
LEFT JOIN dim_waktu  w ON f.id_waktu  = w.id_waktu
LEFT JOIN dim_toko   t ON f.id_toko   = t.id_toko
LEFT JOIN dim_produk p ON f.id_produk = p.id_produk
WHERE w.id_waktu IS NULL OR t.id_toko IS NULL OR p.id_produk IS NULL;

-- B3. Konsistensi hitungan: bersih = kotor - diskon
SELECT id_fakta, total_penjualan_kotor, total_diskon, total_penjualan_bersih
FROM fakta_penjualan_bulanan
WHERE ABS(total_penjualan_bersih - (total_penjualan_kotor - total_diskon)) > 0.01;

-- B4. Nilai tidak masuk akal pada fakta penjualan
SELECT id_fakta FROM fakta_penjualan_bulanan
WHERE total_qty <= 0 OR total_penjualan_kotor <= 0
   OR total_diskon < 0 OR total_diskon > total_penjualan_kotor;

-- B5. Logika fakta pengiriman: komponen tidak boleh melebihi total
SELECT id_fakta_pengiriman FROM fakta_pengiriman_bulanan
WHERE jumlah_order_dikirim + jumlah_order_ditolak > jumlah_order
   OR jumlah_terlambat > jumlah_order_dikirim
   OR total_hari_proses < 0;

-- B6. Stok: tidak negatif dan nilai_stok = qty_stok x harga_list
SELECT f.id_fakta_stok, f.qty_stok, f.nilai_stok, p.harga_list
FROM fakta_stok_toko f
JOIN dim_produk p ON f.id_produk = p.id_produk
WHERE f.qty_stok < 0
   OR ABS(f.nilai_stok - f.qty_stok * p.harga_list) > 0.01;

-- B7. Dimensi: surrogate key unik dan lineage tidak kosong
SELECT 'dim_produk' AS dim, COUNT(*) AS lineage_kosong
FROM dim_produk WHERE id_produk_oltp IS NULL
UNION ALL
SELECT 'dim_toko', COUNT(*) FROM dim_toko WHERE id_toko_oltp IS NULL;

-- =============================================================================
-- C. QUERY ANALITIK (menjawab pertanyaan bisnis di dokumen rancangan)
-- =============================================================================

-- ---- Penjualan ---------------------------------------------------------------

-- C1. Tren omzet bersih bulanan
SELECT w.tahun, w.bulan, w.nama_bulan,
       SUM(f.total_qty)               AS unit_terjual,
       SUM(f.total_penjualan_bersih)  AS omzet_bersih
FROM fakta_penjualan_bulanan f
JOIN dim_waktu w ON f.id_waktu = w.id_waktu
GROUP BY w.tahun, w.bulan, w.nama_bulan
ORDER BY w.tahun, w.bulan;

-- C2. Omzet per toko per tahun beserta porsinya terhadap total tahun itu
SELECT w.tahun, t.nama_toko,
       SUM(f.total_penjualan_bersih) AS omzet_bersih,
       ROUND(100 * SUM(f.total_penjualan_bersih)
             / SUM(SUM(f.total_penjualan_bersih)) OVER (PARTITION BY w.tahun), 2)
             AS persen_dari_total_tahun
FROM fakta_penjualan_bulanan f
JOIN dim_waktu w ON f.id_waktu = w.id_waktu
JOIN dim_toko  t ON f.id_toko  = t.id_toko
GROUP BY w.tahun, t.nama_toko
ORDER BY w.tahun, omzet_bersih DESC;

-- C3. Omzet per kuartal
SELECT w.tahun, w.kuartal,
       SUM(f.total_penjualan_bersih) AS omzet_bersih
FROM fakta_penjualan_bulanan f
JOIN dim_waktu w ON f.id_waktu = w.id_waktu
GROUP BY w.tahun, w.kuartal
ORDER BY w.tahun, w.kuartal;

-- C4. 10 produk terlaris (berdasarkan omzet bersih)
SELECT p.nama_produk, p.merek, p.kategori,
       SUM(f.total_qty)              AS unit_terjual,
       SUM(f.total_penjualan_bersih) AS omzet_bersih
FROM fakta_penjualan_bulanan f
JOIN dim_produk p ON f.id_produk = p.id_produk
GROUP BY p.id_produk, p.nama_produk, p.merek, p.kategori
ORDER BY omzet_bersih DESC
LIMIT 10;

-- C5. Omzet per kategori dan per merek
SELECT p.kategori,
       SUM(f.total_qty)              AS unit_terjual,
       SUM(f.total_penjualan_bersih) AS omzet_bersih
FROM fakta_penjualan_bulanan f
JOIN dim_produk p ON f.id_produk = p.id_produk
GROUP BY p.kategori
ORDER BY omzet_bersih DESC;

SELECT p.merek,
       SUM(f.total_qty)              AS unit_terjual,
       SUM(f.total_penjualan_bersih) AS omzet_bersih
FROM fakta_penjualan_bulanan f
JOIN dim_produk p ON f.id_produk = p.id_produk
GROUP BY p.merek
ORDER BY omzet_bersih DESC;

-- C6. Seberapa besar diskon menggerus omzet, per kategori
SELECT p.kategori,
       SUM(f.total_penjualan_kotor) AS omzet_kotor,
       SUM(f.total_diskon)          AS total_diskon,
       ROUND(100 * SUM(f.total_diskon) / SUM(f.total_penjualan_kotor), 2) AS persen_diskon
FROM fakta_penjualan_bulanan f
JOIN dim_produk p ON f.id_produk = p.id_produk
GROUP BY p.kategori
ORDER BY persen_diskon DESC;

-- C7. Pertumbuhan omzet bulan ke bulan (MoM)
-- Catatan: bila ada bulan tanpa penjualan, LAG membandingkan dengan bulan data sebelumnya.
WITH bulanan AS (
    SELECT id_waktu, SUM(total_penjualan_bersih) AS omzet
    FROM fakta_penjualan_bulanan
    GROUP BY id_waktu
)
SELECT id_waktu, omzet,
       LAG(omzet) OVER (ORDER BY id_waktu) AS omzet_bulan_sebelumnya,
       ROUND(100 * (omzet - LAG(omzet) OVER (ORDER BY id_waktu))
             / NULLIF(LAG(omzet) OVER (ORDER BY id_waktu), 0), 2) AS pertumbuhan_persen
FROM bulanan
ORDER BY id_waktu;

-- ---- Pengiriman --------------------------------------------------------------

-- C8. Kinerja pengiriman per toko per tahun
-- (rasio dihitung dari penjumlahan komponen, bukan rata-rata rasio)
SELECT w.tahun, t.nama_toko,
       SUM(f.jumlah_order)          AS total_order,
       SUM(f.jumlah_order_dikirim)  AS dikirim,
       SUM(f.jumlah_order_ditolak)  AS ditolak,
       SUM(f.jumlah_terlambat)      AS terlambat,
       ROUND(100 * SUM(f.jumlah_terlambat)
             / NULLIF(SUM(f.jumlah_order_dikirim), 0), 2) AS persen_terlambat,
       ROUND(SUM(f.total_hari_proses)
             / NULLIF(SUM(f.jumlah_order_dikirim), 0), 2) AS rata_hari_proses
FROM fakta_pengiriman_bulanan f
JOIN dim_waktu w ON f.id_waktu = w.id_waktu
JOIN dim_toko  t ON f.id_toko  = t.id_toko
GROUP BY w.tahun, t.nama_toko
ORDER BY w.tahun, persen_terlambat DESC;

-- C9. Bulan dengan keterlambatan tertinggi (minimal 5 order terkirim)
SELECT w.tahun, w.nama_bulan, t.nama_toko,
       f.jumlah_order_dikirim, f.jumlah_terlambat,
       ROUND(100 * f.jumlah_terlambat / f.jumlah_order_dikirim, 2) AS persen_terlambat
FROM fakta_pengiriman_bulanan f
JOIN dim_waktu w ON f.id_waktu = w.id_waktu
JOIN dim_toko  t ON f.id_toko  = t.id_toko
WHERE f.jumlah_order_dikirim >= 5
ORDER BY persen_terlambat DESC
LIMIT 10;

-- ---- Stok --------------------------------------------------------------------

-- C10. Nilai persediaan per toko (snapshot terbaru)
SELECT t.nama_toko,
       SUM(f.qty_stok)   AS total_unit,
       SUM(f.nilai_stok) AS nilai_persediaan
FROM fakta_stok_toko f
JOIN dim_toko t ON f.id_toko = t.id_toko
WHERE f.id_waktu = (SELECT MAX(id_waktu) FROM fakta_stok_toko)
GROUP BY t.nama_toko
ORDER BY nilai_persediaan DESC;

-- C11. Stok per kategori per toko (snapshot terbaru)
SELECT t.nama_toko, p.kategori,
       SUM(f.qty_stok)   AS total_unit,
       SUM(f.nilai_stok) AS nilai_persediaan
FROM fakta_stok_toko f
JOIN dim_toko   t ON f.id_toko   = t.id_toko
JOIN dim_produk p ON f.id_produk = p.id_produk
WHERE f.id_waktu = (SELECT MAX(id_waktu) FROM fakta_stok_toko)
GROUP BY t.nama_toko, p.kategori
ORDER BY t.nama_toko, nilai_persediaan DESC;

-- ---- Lintas fakta (drill-across lewat dimensi yang sama) ---------------------

-- C12. Produk laris tetapi stok tipis: 15 produk dengan penjualan tertinggi
-- dibandingkan stok terakhir di tiap toko
SELECT p.nama_produk, t.nama_toko,
       s.unit_terjual, k.qty_stok AS stok_terakhir
FROM (
    SELECT id_produk, id_toko, SUM(total_qty) AS unit_terjual
    FROM fakta_penjualan_bulanan
    GROUP BY id_produk, id_toko
) s
JOIN (
    SELECT id_produk, id_toko, qty_stok
    FROM fakta_stok_toko
    WHERE id_waktu = (SELECT MAX(id_waktu) FROM fakta_stok_toko)
) k ON s.id_produk = k.id_produk AND s.id_toko = k.id_toko
JOIN dim_produk p ON s.id_produk = p.id_produk
JOIN dim_toko   t ON s.id_toko   = t.id_toko
ORDER BY s.unit_terjual DESC, k.qty_stok ASC
LIMIT 15;

-- C13. Ringkasan satu baris per toko dari ketiga fakta
SELECT t.nama_toko,
       (SELECT SUM(total_penjualan_bersih) FROM fakta_penjualan_bulanan
         WHERE id_toko = t.id_toko)                          AS omzet_bersih,
       (SELECT SUM(jumlah_order) FROM fakta_pengiriman_bulanan
         WHERE id_toko = t.id_toko)                          AS total_order,
       (SELECT SUM(nilai_stok) FROM fakta_stok_toko
         WHERE id_toko = t.id_toko
           AND id_waktu = (SELECT MAX(id_waktu) FROM fakta_stok_toko)) AS nilai_persediaan
FROM dim_toko t
ORDER BY omzet_bersih DESC;
