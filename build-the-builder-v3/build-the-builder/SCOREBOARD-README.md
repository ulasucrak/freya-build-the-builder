# Scoreboard (sonuç tablosu)
Tüm test sonuçları tek dosyada toplanır: `results/log.jsonl` (git'e girmez).
Terminal: `python tools/scoreboard.py` (son 30 koşu, en yeni en altta; `--watch` her 60 sn yeniler).
Tarayıcı: `python tools/scoreboard.py --html` sonra `results/scoreboard.html` dosyasını açın (60 sn'de kendini yeniler).
Markdown: `--md` ile `results/SCOREBOARD.md` yazılır.
Sütunlar: time = koşu zamanı; label/slot = "ours" veya "baseline" ve workflow slotu; bank = soru bankası/brief.
source = public/synthetic/build; style = müşteri stili; reps = tekrar sayısı; pass = geçen/toplam vaka.
all-reps = tüm tekrarlarda geçen vaka sayısı; % = başarı oranı (yeşil >=90, sarı >=70, kırmızı altı).
med s = medyan tur süresi (sn); failed = kalan vaka kimlikleri (kısaltılır).
Özet blok: her bank için "ours" ve "baseline" en iyi/son skor; ok (^ v =) aynı slot+bank+style'daki önceki koşuya göre eğilimdir.
