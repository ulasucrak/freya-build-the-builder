# STATUS – Freya "Build the Builder"

## Hedef ve mimari
Genel (brief'e özel olmayan) bir builder: brief + openapi -> Freya telefon-ajanı workflow'u, elle adım olmadan, 30 dk sınırı içinde.
Akış: extract (Claude -> policy.json) -> deterministik generator (code node'lar ve hook'lar) -> prompt'lar -> self-test/repair -> yerinde kaydet + export + trace.

## Bileşenler ve dosyalar (build-the-builder-v3/build-the-builder)
- `builder/extract.py`, `builder/llm.py`: brief'ten policy.json çıkarma.
- `builder/policy_schema.py`, `oracle.py`, `generate.py`, `codegen/`: şema, referans hesap, workflow üretimi.
- `build.py`, `builder/pipeline.py`, `prompts.py`, `selftest.py`, `casegen.py`, `trace.py`: ana akış, prompt, self-test/repair, vaka üretimi, iz.
- `builder/testmode.py` + `tools/run_synthetic.py`: Freya içinde sahte banka ile sentetik test.
- `tools/scoreboard.py` (results/log.jsonl -> scoreboard.html), `tools/eval_extract.py`, `tools/preflight.py`, `tools/package_submission.py`.
- `RUNBOOK.md`; `fixtures/` S1-S7 (~136 sentetik vaka) + `fixtures/EDGE_CASES.md`.
- Takım arkadaşı edge case'leri: `run_edge.sh`, `tools/edge_cases.py` (99 vaka, gerçek banka mock'u; EDGE_CASES.md bölüm I).

## Ölçümler
- Baseline (kit örnek builder): A 9/10 ve 8/10 (tek koşu), B 2/3; 30 dk aşıldı (35 dk, kill edildi).
- Bizim eval1 (gen @43cc4b2): A pass^3 7/10, B pass^3 1/3.
  Stiller A/B: pieces 8/2, corrects 8/2, talkative 6/1, asks_back 10/3, in_words 9/3; en kötü tur 19 sn.
- T18 düzeltmelerinden sonra: önceden başarısız A vakaları 2/2 (PUB-02, 09, 10; pieces PUB-01, 03); B talkative PUB-12 0/2.
- Eval2 (T18 sonrası, gen @95b89dc): pass^3 A 9/10 (önce 7/10), B 2/3 (önce 1/3).
  Kalan A: PUB-10 (hakem selamlamadaki "this call is recorded" ifadesini dürüstlük ihlali sayıyor; 3'te 2 geçti). Kalan B: PUB-13 (3. koşuda kart sözü 30 Eki yerine 2 Kas).
  Stiller A/B (önceki): pieces 7/2 (8/2), corrects 9/3 (8/2), talkative 3/2 (6/1; simüle müşteri cümleleri yarım kesiliyor, büyük kısmı simülatör kaynaklı şüphesi), asks_back 10/2 (10/3), in_words 8/2 (9/3).
  En kötü tur 20 sn, kapanan çağrı yok.
- Edge 99 (takım arkadaşı vakaları, gerçek mock, 1 tekrar, ajan `brief-a` ab2440a1, sim 12bc801d): 96/99, 22 dk, en yavaş tur medyan 5 sn.
  Önceki 21/99 koşusu geçersizdi: `run_edge.sh` `brief-a-selftest` kopyasını (sahte banka) seçiyordu, verify_identity her çağrıda hata verdi (düzeltildi).
  Kalan: EDGE-10 (söz kaydedilip aynı turda vedalaşıldı, müşteri değişiklik isteyemedi), GEN-09-no-talk (doğrulanmış müşterinin "konuşmak istemiyorum"u kimlik reddi gibi işlendi: not_verified/declined, beklenen no_promise/refused; GEN-08 versiyonu geçti), GEN-3P-offers-pay (üçüncü kişiye "account" kelimesi söylendi).
- Not: Freya API aralıklı 503 veriyor; ortak çalışma alanında başka ekip simülasyonu 40 dk bekletti.
- Extraction holdout: S1-S7 tam puan (ör. S5 246/246, S6 339/339, S7 193/193); extraction başına 104-131 sn.
- 192 offline test yeşil.

## Öğrenilen platform gerçekleri
- Workspace başına tek simülasyon.
- Post-hook'lar HTTP hatalarında çalışmaz.
- Model paralel tool call yapar (bayat değişkenler -> anahtar başına bir değişken).
- Sandbox payload sınırı ~64 KB.
- Chat route ilk code node'u atlar.
- Banka mock'u yalnızca 13 hazır vaka id'sini sunar.

## Yapılacaklar (öncelik sırası)
1. T22 devam: ürün başına ikna denemesi sınırı kodda (PUB-12/PUB-06); WIP: `orch/freya-builder-T22` dalı.
2. Parça parça söylenen sayıları birleştir ("twelve" + "hundred and fifty" = 1250).
3. Yılsız doğum tarihinde yılı sor (yanlış cevap sayma).
4. "Neden soruyorsunuz" sonrası soruyu tekrar sorma yerine açıkla.
5. Erken bant 2. deneme (T22 kapsamı).
6. PUB-13 kart tarihi (30 Eki vs 2 Kas).
7. Selamlamadaki "recorded" kelimesi için hakem riski (PUB-10).
8. Generator/oracle için yeni policy anahtarları: `calendar.week_starts`, `non_holidays`, `floor_expr` (+yuvarlama fonksiyonları), `callback.per_date/per_weekday` `end_inclusive`/`today_requires_business_day`, `identity.max_wrong_scope`, `questions[].tool_format`, `triggers[].condition`.
9. S1-S7 + takım arkadaşı edge case'leri üzerinde canlı sentetik eval (tekrarlı).
10. Ham banka malzemesi S2/S4/S6 üzerinde uçtan uca `build.py`.
11. RUNBOOK prova turu + brief'ler açılmadan `git tag pre-final`.
12. Final koşu için ANTHROPIC_API_KEY.
13. Temiz teslim paketi (env.sh olmadan).

## Güvenlik notu
`env.sh` GitHub'a hiç commit edilmedi; GitHub `main` temiz bir snapshot'tır.
