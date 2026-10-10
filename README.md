# Anti-Flash Token

## Etherscan API havuzu

`ETHERSCAN_API_KEYS` virgülle ayrılmış anahtar listesini alır; geriye uyumluluk için yalnız `ETHERSCAN_API_KEY` de kullanılabilir. Etherscan V2'de API anahtarı tüm zincirlerde ortaktır. Aynı hesaptan üretilmiş birden fazla anahtar da günlük ve saniyelik kotayı paylaşabilir; anahtar sayısını kota çarpanı olarak varsaymayın.

API istek ölçümleri ve önbellek istatistikleri Aşama 2 altyapısında tutulur. Anahtar değerleri loglarda ve gösterim metinlerinde maskelenir.

## TRON desteği (TRC-20)

TRON ağı `.env` içindeki `ENABLED_NETWORKS` listesine `tron` eklenerek açılır. Örneğin Ethereum ile birlikte kullanmak için:

~~~dotenv
ENABLED_NETWORKS=ethereum,tron
TRONGRID_API_KEYS=anahtar_1,anahtar_2
TRONGRID_BASE_URL=https://api.trongrid.io
TRONGRID_RATE_PER_KEY=10
TRONGRID_BURST=3
TRONGRID_DAILY_LIMIT=100000
TRONGRID_DAILY_SOFT_RATIO=0.95
TRON_ONLY_CONFIRMED=true
~~~

TronGrid API anahtarı [TronGrid geliştirici panelinden](https://developers.tron.network/reference/select-network) alınır ve yalnızca `TRON-PRO-API-KEY` HTTP başlığında gönderilir. Anahtarlar virgülle ayrılır; loglara veya URL'lere yazılmaz. Havuz, anahtar başına saniyede 10 istek ve 3 istek burst varsayılanlarını, ayrıca günlük yumuşak kota sınırını uygular. TronGrid 429 yanıtında ilgili anahtar en az 30 saniye bekletilir. TRON etkin ama anahtar tanımlı değilse uygulama uyarı verir ve TRON isteklerini reddeder. Geçmiş sorgusu ve Transfer olay yapısı için [TronGrid TRC-20 hesap geçmişi](https://developers.tron.network/reference/get-trc20-transaction-info-by-account-address) ve [TRON olay dokümantasyonu](https://developers.tron.network/docs/event) esas alınır.

Bu sürüm yalnızca TRC-20 token transferlerini izler. TRX yerel para transferleri ve TRC-10 tokenları kapsam dışıdır. İzlenen USDT kontratı Tether'ın [resmî desteklenen protokoller listesinde](https://tether.to/en/supported-protocols/) yayınlanan TRON adresidir: `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`. Ondalık basamak sayısı sabitlenmez; kontrattan okunur. TRON adresleri Base58Check biçimindedir ve büyük/küçük harfleri korunur. İşlem ve adres bağlantıları Tronscan'a gider; [işlem](https://tronscan.org/#/transaction/), [adres](https://tronscan.org/#/address/) ve [TRC-20 token](https://tronscan.org/#/token20/) sayfa şablonlarının geçerliliğini dağıtım öncesi doğrulayın.

Örnek sorgu: `/check tron aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` (64 karakterlik işlem kimliği örneği). Başarılı yanıtta ağ `TRON`, işlem kimliği öneksiz gösterilir ve işlem/kontrat için Tronscan düğmeleri sunulur. İzleme bildirimi örneği:

~~~text
🌐 Ağ: TRON
👛 Cüzdan: <code>T...</code>
🪙 Token: <code>USDT</code>
🔗 İşlem: <code>...</code>
~~~

## Geri bildirim ve cüzdan ayarları

Transfer bildirimindeki düğmeler tespiti değiştirmez. `CORRECT`, `FALSE_POSITIVE` ve `UNSURE` değerleri `data/user_state.sqlite` içinde ilgili transfer kaydına bağlanır. Son tercih geçerlidir; aynı düğme kaydı çoğaltmaz.

`expected` alanı yalnızca `gercek`, `sahte` ve `supheli` olmaya devam eder. `CORRECT` resmi tespiti `gercek`, bilinen sahte tespiti `sahte`, varyant veya bilinmeyen tespiti `supheli` yapar. `FALSE_POSITIVE` resmi olmayan bir tespiti `gercek` yapar; bu, yanlış alarmdır. `UNSURE` bu alana kesin etiket yazmaz. `expected_source=manual` olan etiketler korunur.

Cüzdan ayarları izlenen adresi değiştirmez. Takma ad boş olabilir. Minimum tutar o transferin kendi token miktarıdır; tokenlar birbirine veya USD'ye çevrilmez. `0` filtreyi kapatır. Yalnızca şüpheli seçeneği mevcut sayaçla aynıdır: `VARIANT` ve `KNOWN_FRAUD`. Sessiz saatler UTC'dir. Başlangıç ve bitiş aynıysa bildirimler kapanmaz. Bu filtreler kaydı ve sınıflandırmayı silmez; yalnızca Telegram gönderimini atlar. Atlanan bildirimler kuyruğa alınmaz.

## Risk sinyalleri, adres zehirlenmesi ve approval izleme

Risk skoru, token sınıflandırmasından (`CANONICAL`/`VARIANT`/`KNOWN_FRAUD`/`UNKNOWN`) ve kullanıcı geri bildiriminden ayrı bir kavramdır. Sınıflandırmayı değiştirmez, `expected` alanına yazmaz. `analyzer/risk_engine.py` içindeki `Signal(name, weight, evidence, category, verified, source, network, event_key)` modeli üzerinden hesaplanır; skor 0–100 arasına sıkıştırılır. Aynı ada sahip sinyallerden yalnızca en güçlüsü sayılır; `verified=False` (sezgisel) sinyallerin ağırlığı yarıya iner. Seviyeler: `DÜŞÜK` <25, `ORTA` 25–49, `YÜKSEK` 50–74, `KRİTİK` ≥75. Ağırlıklar ve eşikler gerekçeli başlangıç değerleridir; etiketli veriyle kalibre edilmemiştir. Kayıtlı olmayan token tek başına 5 puan (DÜŞÜK) üretir. Her skor `weights_version` ile `data/user_state.sqlite` içindeki `detections` tablosuna yazılır; `event_key` alanı `transfer_feedback` ile birleştirilebilir, böylece yanlış pozitif geri bildirimleri sinyallerle birlikte değerlendirilebilir. `transactions.log` içindeki `signals` listesi artık tetiklenen sinyal adlarını taşır; diğer alanlar değişmez.

Adres zehirlenmesi (address poisoning) tespiti sezgiseldir ve bildirimde "şüphe" olarak gösterilir. Her cüzdan için daha önce etkileşim kurulan karşı taraf adresleri `address_history` tablosunda tutulur; ilk izleme turundaki mevcut transferler bu geçmişi doldurur, bildirim üretmez. Yeni bir karşı taraf, aynı ağdaki bilinen bir adresle baş/son en az 4'er karakter örtüşüyor veya konumsal benzerlik ≥%60 ise `address_lookalike` sinyali üretilir; cüzdanın bu adresle ilk teması `lookalike_first_contact`, tutar ≤0.001 token ise `lookalike_dust_amount` eklenir. Ethereum adresleri küçük harfle karşılaştırılır; TRON adresleri Base58Check olarak büyük/küçük harf korunarak karşılaştırılır. Farklı ağlardaki adresler hiçbir zaman karşılaştırılmaz. Bulgu, "yalnızca şüpheli" filtresini geçer; minimum tutar ve sessiz saat filtreleri aynen uygulanır.

Approval (ERC-20 `Approval` olayı) izleme yalnızca EVM ağlarında çalışır; TRON sağlayıcısı `supports_approvals=False` döner ve atlanır. Ayrı bir döngü (`APPROVAL_POLL_INTERVAL`, varsayılan 60 sn) her cüzdan için Etherscan `getLogs` ile owner=cüzdan filtresi uygular; ilk turda yalnızca güncel blok imleci kaydedilir, geçmiş yetkiler bildirilmez. Durumlar: `NEW`, `INCREASED`, `DECREASED`, `REVOKED`, `UNLIMITED` (ham değer ≥ 2^255). Sinyaller: `unlimited_allowance` 30, `large_allowance` 15 (yalnızca ondalık biliniyorsa ve ≥1.000.000 token), `allowance_increase` 10, `new_allowance` 5, `known_fraud_spender` 60 (harcayıcı `known_fraud_tokens.json` içinde), `listed_risky_spender` 40 (`registry/known_spenders.json`, yalnızca `source` alanı olan kayıtlar). Bu dosya başlangıçta boştur; kaynaksız adres eklenmez. Büyük bir allowance tek başına dolandırıcılık sayılmaz. Approval bildirimi bilgilendirme amaçlıdır: bot yetki iptal etmez, işlem imzalamaz, özel anahtar istemez. Approval olayı transfer değildir; `transactions.log` ve karışıklık matrisine yazılmaz, `detections` tablosuna `kind=approval` olarak kaydedilir. Kullanıcı ayarlarından sessiz saat uygulanır, minimum tutar uygulanmaz; "yalnızca şüpheli" açıkken skor ≥25 olan approval'lar gönderilir.

## Log formatı

Log dosyaları LOG_DIR altında tutulur; varsayılan konum proje kökündeki logs/ klasörüdür. Klasör ihtiyaç olduğunda oluşturulur. LOG_LEVEL varsayılanı INFO, LOG_RETENTION_DAYS varsayılanı 90 gündür. Dosyalar her gün UTC gece yarısında döner. Gizli anahtarlar mesaj, argüman ve traceback'lerde maskelenir. Dosya yazma hataları uygulamanın çalışmasını durdurmaz.

### app.log

İnsan tarafından okunabilen satır biçimi:

~~~text
2026-10-10 14:03:22 UTC | INFO | main | Bot başlatıldı | run_id=a1b2c3d4
~~~

Alanlar UTC zaman damgası, seviye, Python modül adı ve tek satıra indirgenmiş mesajdır. Başlangıç/duruş, izlenen cüzdan sayısı, API ve Telegram hataları burada yer alır. Her 10 dakikada bir heartbeat; run kimliğini, benzersiz izlenen cüzdan sayısını, son aralıktaki yeni transferleri, hata sayısını ve çalışma süresini içerir.

### transactions.log ve detections.log

Her satır bağımsız bir JSON nesnesidir (ensure_ascii=False). transactions.log izlenen tüm transferleri, detections.log ise CANONICAL dışındaki sınıflandırmaları (şüpheli, sahte ve bilinmeyen dâhil) içerir. Tekrarlanan kayıtlar SQLite tabanlı kalıcı görüldü kümesiyle engellenir.

| Alan | Açıklama |
|---|---|
| ts | Kaydın UTC zamanı; milisaniye hassasiyetinde ISO 8601 |
| run_id | Bot çalıştırmasına özgü kısa kimlik |
| source | Kaynağın türü: monitor, manual veya test |
| network | Blokzincir ağı |
| tx_hash, log_index, event_key | İşlem ve transfer olayının kimliği |
| block_number, block_time | Blok numarası ve zincir zamanı |
| direction, wallet, sender, receiver | Transfer yönü, izlenen cüzdan ve taraflar |
| token_address, token_symbol, amount | Token kontratı, temizlenmiş en çok 20 karakterlik sembol ve tam hassasiyetli miktar metni |
| classification, classification_label | Mevcut analiz anahtarı ve okunabilir Türkçe etiketi |
| signals | Analiz nesnesinde varsa sinyaller; yoksa boş liste |
| notified | Bildirim başarılıysa true, deneme başarısızsa false, denenmediyse null |
| telegram_message_id | Telegram gönderim yanıtındaki mesaj kimliği; yoksa null |
| notify_error, error | Bildirim veya analiz hatası; yoksa null |
| detection_latency_s | Transferin zincir zamanından kayıt/bildirim denemesine kadar geçen saniye |
| processing_ms | Analiz için geçen milisaniye |
| expected, note | İsteğe bağlı manuel değerlendirme etiketi ve not |
| expected_source | Önceden izlenmiş bir olaya sonradan manuel etiket eklendiyse manual |

Manuel değerlendirme için scripts/check_transaction.py, verilen cüzdanın son transfer sayfalarında hash'i arar, mevcut analiz sonucunu kaydeder ve isteğe bağlı beklenen etiketi ekler:

~~~bash
python3 scripts/check_transaction.py --wallet 0x... --tx-hash 0x... --expected sahte --note "Bilinen sahte token"
~~~

--expected değerleri gercek, sahte ve supheli olabilir. Bu komut çalıştırıldığında Etherscan API'sine istek gönderilir.

### tests.log

JSONL kayıtları her pytest testi için ts, run_id, test_nodeid, outcome (passed, failed, skipped) ve duration_s alanlarını içerir. Entegrasyon testleri eval_logger fixture'ını kullanarak tx_hash, expected, actual ve isteğe bağlı note alanlarıyla değerlendirme kaydı ekleyebilir.

### Özet ve ölçüm tanımları

scripts/log_summary.py, etkin ve döndürülmüş transactions.log / detections.log dosyalarını tarih aralığıyla okuyabilir; Markdown veya CSV çıktısı verir:

~~~bash
python3 scripts/log_summary.py --start 2026-10-01 --end 2026-10-31 --format csv --output logs/tez-ozeti.csv
~~~

Karışıklık matrisi için gercek negatif, sahte ve supheli pozitif kabul edilir. CANONICAL güvenli tahmin, diğer sınıflandırmalar (bilinmeyen dâhil) uyarı tahmini sayılır. Bildirim başarı oranının paydası yalnızca notified değeri boolean olan denemelerdir.
