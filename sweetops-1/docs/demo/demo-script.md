# SweetOps Demo Script — 3 Dakika

Dükkan sahibine sistemi göstermek için adım adım akış.

---

## Hazırlık (Demo Öncesi)

```bash
# 1. Sistemi başlat
docker compose up -d

# 2. Base seed (sadece ilk seferde)
docker compose exec api python seed.py

# 3. Demo verisi (14 günlük gerçekçi sipariş geçmişi)
docker compose exec api python /app/scripts/demo_seed.py
```

> 💡 dbt çalıştırılırsa analytics panelleri de dolar: `docker compose run dbt run`

---

## Adım 1 — Müşteri Siparişi (45 saniye)

Telefondan aç: `http://localhost:3000?demo=true&store=1&table=1`

**Göster:**
1. "Bakın, müşteri QR kodu okuyor" → telefonu göster
2. Üstteki **⚡ Hızlı Sipariş** butonlarından birine tıkla: **🍫 Klasik**
   - *"Nutella, Muz, Fındık otomatik seçildi"*
3. Alttaki fiyatı göster: **"Bakın, toplam ₺75 — müşteri anlık görüyor"**
4. **"Siparişi Gönder"** butonuna bas
5. Başarı sayfası: *"Siparişiniz alındı! Waffle'ınız hazırlanıyor 🧇"*

**Söyle:** *"Müşteri masasından kalkmadan 10 saniyede sipariş veriyor. Siz garsonla uğraşmıyorsunuz."*

---

## Adım 2 — Mutfak Ekranı (30 saniye)

Tabletten aç: `http://localhost:3001`

**Göster:**
1. Yeni sipariş anında geldi — **"Bakın, 1 saniyede mutfakta"**
2. Siparişte malzemeleri göster: *"Nutella, Muz, Fındık — net, karışıklık yok"*
3. **"HAZIRLANIYOR"** butonuna bas
   - *"Stok otomatik düşüyor. Siz saymıyorsunuz, sistem sayıyor."*
4. **"HAZIR ✓"** butonuna bas
   - *"Hazırlık süresi de ölçülüyor."*

**Söyle:** *"Kağıt yok. Bağırma yok. Hata yok."*

---

## Adım 3 — İşletme Paneli (90 saniye) ⭐

Bilgisayardan aç: `http://localhost:3002`

**Üstten başla — Value Summary:**
1. Başlığı göster: *"SweetOps bu hafta ₺X gelir korumanıza yardımcı oldu"*
2. Kartları göster:
   - 💰 Bu haftaki geliriniz — **₺X**
   - 🚨 Stok tükenme riski — **₺X/hafta**
   - 🛡️ Korunan gelir — **₺X**
   - ⏱️ Ort. hazırlık süresi — **Xdk Xsn**

**Söyle:** *"Bu bir sayıyla ifade edersek, bu sistem size ayda ₺X kazandırıyor."*

3. **🚨 Kritik Uyarılar** paneli:
   - *"Bakın, Nutella 1.5 gün sonra bitiyor. Günlük ₺85 kayıp riski var."*
   - *"Sistem sizi önceden uyarıyor, siz de toptan sipariş veriyorsunuz."*

**Söyle:** *"Bu uyarı olmasaydı, Cumartesi Nutella biterdi. 20 müşteri 'Nutella yok mu?' deyip giderdi."*

4. **📈 Trend Malzemeler:**
   - *"Lotus Biscoff bu hafta %45 arttı. Daha fazla stok almalısınız."*

5. **🤝 Popüler Kombinasyonlar:**
   - *"Müşterileriniz en çok Nutella + Muz seçiyor. Bunu hazır set yapabilirsiniz."*

---

## Kapanış (15 saniye)

**Söyle:**

> *"Bu sistemi kullanmadan önce her gün 30 dakika stok sayıyordunuz. Her hafta Nutella tükeniyordu. Her ay ne kazandığınızı bilmiyordunuz."*
>
> *"SweetOps bunu 8 dakikaya düşürüyor. Ve size ne zaman ne alacağınızı söylüyor."*
>
> *"İlk 14 gün ücretsiz. Ne kaybedersiniz?"*

---

## Demo URL'leri

| Ekran | URL |
|---|---|
| Müşteri (demo modlu) | `http://localhost:3000?demo=true&store=1&table=1` |
| Mutfak | `http://localhost:3001` |
| İşletme Paneli | `http://localhost:3002` |
