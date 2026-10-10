# Yerel çalıştırma ve gelecekte dağıtım rehberi

Bu proje Docker kullanmadan `main.py` üzerinden çalışır. VPS bölümleri gelecekteki kurulum rehberidir; bu aşamada sunucuya bağlanılmadı ve VPS komutu çalıştırılmadı.

## 1. Yerel çalıştırma ve doğrulama

Proje kökünde:

~~~bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
~~~

`.env` içinde en az `ETHERSCAN_API_KEY` (veya `ETHERSCAN_API_KEYS`) ile `TELEGRAM_BOT_TOKEN` değerlerini ayarla. Admin komutları için `ADMIN_IDS=123456789,987654321` biçiminde Telegram kullanıcı kimliklerini ekle. `DATA_DIR=./data` kalıcı veriyi, `LOG_DIR=./logs` dönen logları belirler. `HEARTBEAT_URL` boşsa dış heartbeat kapalıdır.

Başlatma ve doğrulama:

~~~bash
python3 main.py
python3 -m tools.healthcheck
~~~

`python3 main.py` mevcut `.venv` Python'ına kendisi geçer. Telegram özel sohbetinde `/health` ve `/stats` yalnız `ADMIN_IDS` içindeki hesaplarda çalışır. `/health` komutu bir dakikadan kısa sürede **NORMAL**, **UYARI** veya **KRİTİK** gösterir; yerel kontrol heartbeat yaşı 120 saniyeden kısa ve son sağlık durumu kritik değilse `healthcheck ok` yazar. `logs/app.log`, `logs/transactions.log`, `logs/detections.log`, `logs/api_usage.log` ve `data/heartbeat` dosyalarını incele.

`Ctrl+C` SIGINT gönderir. Bot görevleri en çok 20 saniye içinde kapatır ve temiz kapanış oturumunu yazar. Yeniden başlatınca `data/runtime_state.json` içindeki cüzdanları, `data/operations.sqlite` içindeki günlük sayaçları ve `data/api_usage.sqlite` içindeki API kotasını kontrol et. Eski `runtime_state.json` varsa yeni hedef boşken `data/` altına kopyalanır; kaynak dosya korunur.

Testler: `.venv/bin/python -m pytest -q`. Testler Telegram veya dış API'ye bağlanmamalıdır.

## 2. Gelecekte VPS seçimi

İlk kurulumda 1 vCPU ve 1 GB RAM düşük/orta izleme yükü için başlangıç noktası olabilir; eşzamanlı cüzdan sayısı, SQLite işlemleri, Python belleği ve log büyümesi ölçüldükten sonra kapasiteyi artır. Desteklenen bir Linux dağıtımının Ubuntu LTS sürümünü seç. Sistem saatini UTC tut. Disk planına günlük loglar, 14 yedek ve en az bir geri yükleme kopyası dâhil olsun; log rotasyonu `LOG_RETENTION_DAYS` ile yönetilir.

## 3. İlk sunucu güvenliği

- Ubuntu örneğinde bot için root olmayan kullanıcı oluştur: `sudo adduser --disabled-password --gecos "" anti-flash`. Proje, `.env`, `data/` ve `logs/` sahipliğini bu kullanıcıya ver; sıradan shell geçmişine token yazma.
- SSH anahtarını kur ve ikinci terminalden yeni bir anahtar oturumu açarak doğrula. Ancak bundan sonra `PasswordAuthentication no` ayarını sshd yapılandırmasına ekle; önce `sudo sshd -t`, ardından `sudo systemctl reload ssh` çalıştır ve açık olan mevcut SSH oturumunu doğrulama bitene kadar kapatma. Bazı cloud imajlarında parola girişi varsayılan olarak zaten kapalıdır; sağlayıcının erişim yöntemini kontrol et.
- Ubuntu'da UFW için önce `sudo ufw app list` ile SSH profilini kontrol et; `sudo ufw allow OpenSSH` (veya gerçek özel SSH portuna izin) ekle, `sudo ufw status numbered` ile doğrula, sonra `sudo ufw enable` uygula ve ikinci bir SSH bağlantısı aç. UFW, Ubuntu Server'ın varsayılan firewall arayüzüdür; etkinleştirme öncesi erişim kuralını doğrula ([Ubuntu UFW rehberi](https://documentation.ubuntu.com/server/how-to/security/firewalls/)).
- `sudo apt update && sudo apt install fail2ban` ile Fail2ban kurulumunu yap; `sudo systemctl status fail2ban` ve günlükleri kontrol et. Paket ve jail yapılandırmasının seçilen Ubuntu sürümünde bulunduğunu doğrula.
- Desteklenen Ubuntu LTS sürümlerinde `unattended-upgrades` çoğunlukla hazır gelir. Gerekirse `sudo apt install unattended-upgrades` ve `sudo dpkg-reconfigure unattended-upgrades` ile yapılandır, ardından güncelleme loglarını ve yeniden başlatma gereksinimini kontrol et ([Ubuntu otomatik güvenlik güncellemeleri](https://documentation.ubuntu.com/security/security-updates/)).
- UTC saatini kontrol et: `timedatectl status`; gerekiyorsa `sudo timedatectl set-timezone Etc/UTC` ve tekrar `timedatectl status` ([Ubuntu saat eşitleme rehberi](https://documentation.ubuntu.com/server/how-to/networking/timedatectl-and-timesyncd/)).
- Botun Telegram ve zincir API'sine giden bağlantısı gerekir; dinleyen bir web portu açması gerekmez.

## 4. Docker olmadan Linux kurulumu

Kodu Git üzerinden sunucuya aktar, proje için sanal ortam oluştur ve `requirements.txt` içindeki bağımlılıkları kur. Botu çalıştıracak kullanıcıya `data/` ve `logs/` dizinlerinin sahipliğini ver. `.env` dosyasını güvenli şekilde oluştur ve `chmod 600` uygula; dosyayı Git'e ekleme.

Örnek systemd birimi (yolları sunucudaki gerçek kurulumla değiştir):

~~~ini
[Unit]
Description=Anti-Flash Token Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=anti-flash
Group=anti-flash
WorkingDirectory=/opt/anti-flash-token
EnvironmentFile=/opt/anti-flash-token/.env
ExecStart=/opt/anti-flash-token/.venv/bin/python /opt/anti-flash-token/main.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=25
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
~~~

Unit dosyasını kurmadan önce Python, proje ve `.env` yollarını doğrula. systemd'ye kaydettikten sonra servis durumunu ve `journalctl` çıktısını kontrol et; gerekmeyen hiçbir portu açma. `SIGTERM` kontrollü kapanışı tetikler.

Örnek unit'i `/etc/systemd/system/anti-flash-token.service` konumuna yerleştirdikten sonra `sudo systemctl daemon-reload`, `sudo systemctl enable --now anti-flash-token`, `systemctl status anti-flash-token` ve `journalctl -u anti-flash-token -f` ile kurulum ve logları doğrula. Ubuntu systemd birimleri `Restart=on-failure` gibi davranışları destekler; uygulamadan önce gerçek yolları ve kullanıcıyı gözden geçir ([Ubuntu systemd birim açıklamaları](https://documentation.ubuntu.com/server/explanation/software/changing-package-files/)).

## 5. Güncelleme ve geri alma

1. Mevcut veriyi `scripts/backup.sh` ile yedekle.
2. İstenen sürüm etiketini doğrula ve bu etikete geç.
3. Bağımlılıkları sanal ortamda yükle; önce yerel testleri çalıştır.
4. Servisi başlat ve `/health`, `/stats`, heartbeat ile logları kontrol et.
5. Sorunda önce servisi durdur, son yedeği sakla ve önceki Git etiketine dön. Veritabanı şeması değişikliklerinde eski kopyanın yeni sürümle uyumunu test etmeden üzerine yazma.

## 6. Yedekler

Yedek oluştur:

~~~bash
DATA_DIR=./data LOG_DIR=./logs BACKUP_DIR=./backups scripts/backup.sh
~~~

Betik SQLite veritabanlarını SQLite online backup API'siyle alır, arşivde `integrity_check` yapar ve son 14 `.tar.gz` yedeğini tutar. Örnek cron (her gün 03:15 UTC):

~~~cron
15 3 * * * cd /opt/anti-flash-token && DATA_DIR=/opt/anti-flash-token/data LOG_DIR=/opt/anti-flash-token/logs BACKUP_DIR=/var/backups/anti-flash-token scripts/backup.sh
~~~

Yedekleri şifreli bir yöntemle sunucu dışına kopyala. Geri yükleme: botu durdur, arşivi önce geçici bir dizine aç, içindeki SQLite dosyalarında `PRAGMA integrity_check` çalıştır, mevcut `data/` ve `logs/` dizinlerini ayrıca sakla ve doğrulanmış yedeği geri yükle. Botu başlatıp cüzdanları, sayaçları ve `/health` yanıtını kontrol et. Geri yüklemeyi düzenli olarak ayrı bir test kopyasında dene.

## 7. İzleme

Adminler `/health` ile süreç, izleme döngüsü, anahtar durumu, disk ve beklenmeyen kapanışları; `/stats` ile iş ve API sayaçlarını görür. `python3 -m tools.healthcheck` yerel heartbeat yaşı ve kritik durum için çıkış kodu verir. İsteğe bağlı `HEARTBEAT_URL` beş dakikada bir çağrılır. `LOG_DIR` boyutunu, boş disk alanını ve `backups/` büyümesini izle.

## 8. Sorun giderme

- **API key COOLDOWN / EXHAUSTED:** `/health` içindeki yalnız etiketli durumu kontrol et; aynı Etherscan hesabındaki anahtarların ortak kota kullanabileceğini hesaba kat.
- **Rate limit:** `api_usage.log`, günlük kullanım ve izleme aralığını incele; erişim anahtarlarını loga kopyalama.
- **Disk dolu:** log rotasyonunu, eski yedekleri ve boş alanı incele; çalışma verisini silmeden önce yedek al.
- **Sürekli yeniden başlama:** systemd/journal loglarını ve son `/health`/heartbeat kaydını incele.
- **Telegram bağlantı hataları:** bot token'ını `.env` içinde doğrula; token'ı komut satırına veya loga yazma.
- **Bozuk heartbeat/SQLite:** `tools.healthcheck` güvenli biçimde başarısız olur. Botu durdurup doğrulanmış yedekten geri yükle; orijinal dosyanın kopyasını koru.
- **DATA_DIR izinleri:** bot kullanıcısının yalnız gerekli veri ve log dizinlerine yazabildiğini kontrol et.
- **Yedekleme hatası:** sıfırdan farklı çıkış kodunu ve stderr mesajını incele; kısmi arşiv başarılı yedek sayılmaz.
