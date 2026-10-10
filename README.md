# Anti-Flash Token

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
