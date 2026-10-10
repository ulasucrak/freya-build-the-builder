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
- Eval2 (T21): devam ediyor.
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
2. Generator/oracle için yeni policy anahtarları: `calendar.week_starts`, `non_holidays`, `floor_expr` (+yuvarlama fonksiyonları), `callback.per_date/per_weekday` `end_inclusive`/`today_requires_business_day`, `identity.max_wrong_scope`, `questions[].tool_format`, `triggers[].condition`.
3. S1-S7 + takım arkadaşı edge case'leri üzerinde canlı sentetik eval (tekrarlı).
4. Ham banka malzemesi S2/S4/S6 üzerinde uçtan uca `build.py`.
5. RUNBOOK prova turu + brief'ler açılmadan `git tag pre-final`.
6. Final koşu için ANTHROPIC_API_KEY.
7. Temiz teslim paketi (env.sh olmadan).

## Güvenlik notu
`env.sh` GitHub'a hiç commit edilmedi; GitHub `main` temiz bir snapshot'tır.
