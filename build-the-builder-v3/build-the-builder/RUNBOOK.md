# Final Günü Runbook

Tüm komutlar kit klasöründen (`build-the-builder-v3/build-the-builder`) çalıştırılır. Python: `/Users/ulas/Desktop/Freya/.venv/bin/python` (3.12).
Kısaltma: `PY=/Users/ulas/Desktop/Freya/.venv/bin/python`

## T-1 gün (cumartesi): dondur

1. Testleri çalıştır: `$PY -m unittest discover -s tests`. Hepsi yeşil olmalı.
2. Son kodu commit et. `git status` temiz olmalı. `freya.py` içindeki `AGENT_LLM` değişmemiş olmalı.
3. Dondur: `git tag pre-final`.
4. `$PY tools/preflight.py` çalıştır, tüm satırlar PASS olmalı (WARN'ları oku).
5. Workspace'i kontrol et: 200 workflow sınırına yakınsa eski `dev-*` workflow'ları temizle (preflight sayıyı gösterir).

## Final günü

1. Briefler açılmadan önce: `source /Users/ulas/Desktop/Freya/build-the-builder-v3/build-the-builder/env.sh` (mutlak yol, değerleri asla yazdırma).
2. Abonelik limitine takılmamak için önerilen: `export ANTHROPIC_API_KEY=...` (API anahtarı, abonelik yerine).
3. `$PY tools/preflight.py` : hepsi PASS olmalı (git temiz, mock ayakta, Freya erişilebilir, Claude auth).
4. Briefleri ve her brief'in kendi `openapi.json` dosyasını yeni bir klasöre koy (ör. `/Users/ulas/Desktop/Freya/final/`). Kit içine koyma, repoyu kirletme.
5. Brief 1 (temiz spesifikasyon), 30 dk sınırı için `--budget-min 27`:
   `$PY build.py /Users/ulas/Desktop/Freya/final/brief1.md --openapi /Users/ulas/Desktop/Freya/final/brief1-openapi.json --slot final-1 --budget-min 27`
6. Brief 2 (ham banka materyali: mail, memo, kod listesi). Klasörü doğrudan ver, build.py klasör kabul eder, sonraki dosya öncekini ezer:
   `$PY build.py /Users/ulas/Desktop/Freya/final/brief2/ --openapi /Users/ulas/Desktop/Freya/final/brief2-openapi.json --slot final-2 --budget-min 27`
7. Bir build 30 dakikayı geçemez (self-test dahil). 27 dk güvenli sınırdır. Aynı anda iki build ve iki simülasyon çalıştırma (workspace başına tek simülasyon, `with-lock` kullan).
8. Her build `traces/<slot>-<UTC>.txt` ve `.json` yazar, workflow'u yerinde günceller ve `out/<slot>.workflow.json` olarak export eder.

## N koşudan en iyisini seçmek

1. Kurallar izin verir: istediğin kadar koş, birinin agent'ını ve o koşunun trace'ini gönder. Her koşunun kendi trace dosyası vardır.
2. Aynı `--slot` ile tekrar koşarsan workflow yerinde güncellenir (200 sınırı dolmaz) ama önceki sürümün üzerine yazılır. Farklı koşuları saklamak istiyorsan farklı slot kullan: `final-1-a`, `final-1-b`.
3. Seçim: self-test skoru (trace'teki `test run` satırları), sonra toplam söz verilen tutar, sonra kısa build süresi.
4. Seçtiğin koşunun çıktısı hâlâ `out/<slot>.workflow.json` ve trace'i `traces/` içinde olmalı. Seçmediğin koşunun slotunu paketleme.
5. Bir koşu tutarsız bitti ve diğeri iyiyse, ikincisini slot ismiyle paketle. Yeni koşu eskiyi export'ta ezmesin diye slotları ayrı tut.

## Export ve paketleme

1. Export build.py tarafından yapılır. Elle gerekirse: `$PY -c "import freya,json; s=json.load(open('.freya-agents.json')); freya.export_workflow(s['final-1']['workflow_id'], 'out/final-1.workflow.json')"`.
2. Düzeltme commit'lerini yap, `git status` temiz olsun, sonra: `$PY tools/package_submission.py final-1 final-2`.
3. Çıktı: `submission/<UTC tarih>/` içinde `*.workflow.json`, `traces/`, `code-<sha>.zip` (`git archive HEAD`), `MANIFEST.txt` (commit, tag, SHA256, build süreleri).
4. MANIFEST.txt sonundaki PROBLEMS bölümü boş olmalı. `env.sh`, `.freya-agents.json`, `results/` pakete girmez, zip içini kontrol et: `unzip -l submission/*/code-*.zip | grep -E "env.sh|freya-agents|results"` boş dönmeli.

## Brief açıldıktan sonra kod değişikliği

İzin verilen:
1. Beklenmeyen bir tabloda/formatta builder'ın çökmesini düzeltmek (parser hatası, None kontrolü, kodlama).
2. Genel bir hatayı düzeltmek (yanlış gün hesabı, JSON ayrıştırma, timeout).
3. Hata mesajı veya trace iyileştirmeleri.

Yasak:
1. Final brief'ten hiçbir kural, değer, isim, tatil tarihi, outcome adı, cümle veya alan adını koda, prompt'a veya fixture'a yazmak.
2. Final brief'e özel `if` dalı veya sabit değer eklemek.
3. Üretilen workflow'u elle düzenlemek (agent yalnızca builder çıktısı olmalı).

Her düzeltme: ayrı commit, açık mesaj (ne bozuktu, neden genel). Örnek: `fix: tolerate empty cell in outcome table (parser crash)`. Düzeltmeden sonra testleri çalıştır ve builder'ı yeniden koş.

## Sorun giderme

| Belirti | Neden | Ne yapılır |
|---|---|---|
| Bank mock cevap vermiyor / 5xx | Mock kapalı veya ağ | `curl -s -o /dev/null -w "%{http_code}" $FREYA_BANK_HOST` ile bak, 1-2 dk bekle, tekrar dene. Self-test'i `--no-test` ile atlayıp önce workflow'u üret, sonra test et. |
| Freya `error code: 1010` | `User-Agent` yok | `freya.py` zaten ayarlıyor. Kendi `curl` çağrında UA ekle. |
| Freya 504 / timeout | Platform yavaş | Aynı çağrıyı tekrar dene (artan bekleme), yeni workflow oluşturma. |
| `Workspace plan limit reached` | 200 workflow doldu | Eski `dev-*` workflow'ları sil. Slot kullan, yerinde güncelle. |
| Rate limit / 429 (Claude) | Abonelik limiti | `ANTHROPIC_API_KEY` ile API anahtarını kullan. 2-3 dk bekle, `--best-of 1` ile yeniden dene. |
| `claude -p` cevap vermiyor | Oturum süresi doldu | `claude` ile tekrar giriş yap veya `ANTHROPIC_API_KEY` ver. |
| Build süre sınırına yaklaştı | Self-test/repair uzadı | `--budget-min 27` bütçeyi bitirince en son kayıtlı workflow'u export eder. Gerekirse `--repair-rounds 0` veya `--max-cases 5` ile yeniden koş. Hâlâ olmuyorsa fallback agent (`--no-prompts`, üreticinin kendi prompt'ları) ile koş, trace'te belirt. |
| Simülasyon bekliyor | Workspace'te başka simülasyon var | Birinin bitmesini bekle. Başka `run_cases.py` başlatma. |
| Skor hiç oynamadı | Workflow yayınlanmadı | `freya.save_workflow` yayınlar, düz `PUT` yayınlamaz. |
| `import claude_agent_sdk` hatası | Yanlış python | `.venv/bin/python` kullan, `source` ettikten sonra `which python`. |

## Son kontrol listesi

1. İki `out/*.workflow.json` var ve boş değil.
2. İki trace var (başlangıç, bitiş, model çağrıları, Freya işlemleri, testler).
3. `MANIFEST.txt` PROBLEMS içermiyor, commit sha ve tag doğru.
4. `git status` temiz, `git log` her düzeltmeyi gösteriyor.
5. `env.sh` ve anahtarlar hiçbir yerde commit'lenmedi veya pakete girmedi.
