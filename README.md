# Kaçan Çağrı Botu

PBX üzerinden günlük kaçan / cevapsız çağrıları tespit edip yetkili Telegram grubuna ve personele anlık bildirim gönderen bot.

**Desteklenen PBX sağlayıcıları:**

| Provider | Env | Kaçan çağrı kaynağı |
| --- | --- | --- |
| **Toniva** (varsayılan) | `PBX_PROVIDER=toniva` | `GET /reports/queue-detail` + durum **Cevapsız** |
| Invekto | `PBX_PROVIDER=invekto` | reportType 2 (missed-calls) |

## Özellikler

- Toniva Public API veya Invekto PBX entegrasyonu
- Belirli kuyruk/departman filtreleme
- Tekrar gönderimi önleyen kalıcı deduplication (45 güne kadar)
- `/kacancagri` ile tarih aralığı Excel raporu
- Personel yönetimi ve özel mesaj (DM) bildirimi
- `/stats`, `/kuyruklar`, `/ayar`, `/temizle` gibi yönetim komutları
- Railway için kolay deploy (volume ile kalıcı data)

## Gereksinimler

- Python 3.11 (Railway); test matrisi: 3.11 ve 3.14
- Telegram Bot Token + Grup Chat ID
- **Toniva:** API key (`tva_...`, scope: `reports:read`)
- **Invekto:** 8 haneli firma kodu
- (Opsiyonel) PBX tarafında istek IP'si whitelist

## ⚠️ Güvenlik (Çok Önemli)

- **Asla** gerçek `TELEGRAM_BOT_TOKEN` veya `TONIVA_API_KEY` değerini commit etme.
- `.env` dosyası `.gitignore` ile yoksayılır.
- Secret'ları yalnızca `.env` veya Railway Environment Variables içinde tut.
- Token sızarsa BotFather / Toniva panel üzerinden yenile.

## Kurulum (Yerel)

1. Repoyu klonla
2. `.env` oluştur (`.env.example` örneğini kullan)
3. Bağımlılıkları kur:

     ```bash
     python -m pip install -r requirements.txt
     ```

4. Botu çalıştır:

     ```bash
     python bot.py
     ```

## Ortam Değişkenleri (.env)

### Toniva (önerilen)

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_GROUP_CHAT_ID=
PBX_PROVIDER=toniva
TONIVA_API_KEY=tva_...
TONIVA_QUEUE=1000
POLLING_INTERVAL_SECONDS=30
BOT_TIMEZONE=Europe/Istanbul
DAILY_REPORT_HOUR=10
# Railway volume:
# DATA_DIR=/app/data
```

Toniva API: `https://crm.toniva.net/api/public/v1`  
Dokümantasyon: `https://crm.toniva.net/api/public/v1/docs`  
Gerekli scope: **`reports:read`**

### Invekto

```env
PBX_PROVIDER=invekto
TELEGRAM_BOT_TOKEN=
TELEGRAM_GROUP_CHAT_ID=
INVEKTO_DEPARTMENT_NAME=Gelen Arama,MESAI DIŞI
# Firma kodu: /firmakodu 12345678
```

`TELEGRAM_GROUP_CHAT_ID` için gruba botu ekledikten sonra `/chatid` yazın.

## Toniva: kaçan çağrı mantığı

UI'da cevapsızlar **Kuyruk Detay Raporu** altında görünür (`Durum = Cevapsız`).  
Bot aynı kaynağı kullanır:

1. `GET /reports/queue-detail?startDate=...&endDate=...&queue=1000`
2. Satırlarda status **Cevapsız** (env: `TONIVA_MISSED_STATUS`)
3. Personel eşlemesi için `GET /reports/conversations` (telefon → son dahili, 15 gün cache)

## Komutlar (Sadece yetkili grupta)

| Komut | Açıklama |
| --- | --- |
| `/start` `/help` | Yardım |
| `/ping` | Bağlantı ve yetki testi |
| `/chatid` | Grup ID |
| `/ayar` | Bot ayarları (provider, kuyruk, …) |
| `/firmakodu 12345678` | Invekto firma kodu (Toniva'da gerekmez) |
| `/stats` | Dedup / poll istatistikleri |
| `/kuyruklar` | PBX kuyruk listesi |
| `/kacancagri 15.06.2026, 25.06.2026` | Excel kaçan çağrı raporu |
| `/iletilenkacancagri 28.06.2026` | İletilen çağrı + geri arama raporu |
| `/gonder 20.07.2026,21.07.2026` | Seçili günleri gruba+DM yeniden ilet (arka plan) |
| `/gonder durdur` | Devam eden gönderim kaydedildikten sonra işi durdur |
| `/gonder sessiz` | Kalan dedup’u bildirimsiz kapat (flood acil kes) |
| `/eslestir 9053… 585` | Telefon→dahili kalıcı eşleme (API CDR eksikse) |
| `/debugeslesme 9053…` | Eşleme teşhisi (cache + API) |
| `/personelekle` `/personelsil` `/personeller` | Personel yönetimi |
| `/temizle` | Eski dedup kayıtlarını temizle |
| Excel (.xlsx) yükle | Toplu personel: A=isim, B=dahili, C=@username |

**DM için:** Personel bota özel sohbetten `/start` yazmalıdır.

Yönetim komutları (`/firmakodu`, `/temizle`, `/gonder`, `/eslestir`,
`/debugeslesme`, personel komutları ve Excel yükleme) yalnızca tanımlı grubun
yöneticilerine açıktır. Telegram yetki sorgusu başarısızsa işlem yapılmaz.
Botu grup yöneticisi yapın; diğer üyelerin yetkilerinin sorgulanması için gereklidir.
Personelin kullanıcı adı değişirse eski DM bağlantısı kaldırılır; yeni hesap `/start`
ile bağlanmalıdır. Mevcut bağlantı başka Telegram kullanıcı ID'siyle ele geçirilemez.

## Excel Raporu

`/kacancagri` sütunları: ID, Telefon, Tarih, Saat, Departman/Kuyruk, Durum, Tamamlandı, süreler, Trunk, Extension.

## Deploy: Railway

### 1. Temel Deploy

- GitHub repo'yu bağla.
- Environment Variables:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_GROUP_CHAT_ID=
PBX_PROVIDER=toniva
TONIVA_API_KEY=
TONIVA_QUEUE=1000
BOT_TIMEZONE=Europe/Istanbul
DATA_DIR=/app/data
```

- `railway.toml` → `python bot.py`, `numReplicas = 1`

### 2. Volume (kalıcı veri) — ZORUNLU öneri

1. Service → **Add Volume**
2. Mount path: **`/app/data`**
3. Env: `DATA_DIR=/app/data`
4. Redeploy

Saklananlar: `sent_calls.json`, `config.json`, `personnels.json`, `phone_map.json`,
`delivered_calls.json`, `gonder_state.json`, `health.json`, bunların `.bak` kopyaları ve `logs/`.

Tek replica zorunludur; JSON depoları çok süreçli eşzamanlı yazım için tasarlanmamıştır.
Atomik yazımdan sonra son geçerli içerik `.bak` dosyasına da yazılır. JSON bozulursa
yedekten kurtarılır; bozuk dosya `.corrupt` olarak saklanır. İki kopya da okunamıyorsa
veri sıfırlanmaz, servis açık hata verir. Şema hataları da sessizce sıfırlanmaz.
Bu kopyalar aynı volume üzerindedir; volume kaybına karşı ayrıca harici yedek alınmalıdır.

### 3. Toniva IP whitelist

Tenant'ta IP kısıtı varsa Railway outbound IP'yi Toniva whitelist'e ekle (`403 CRM-2093` alırsan).

### 4. Rate limit

Toniva: 100 istek/dakika. Çok günlük tarama ve raporlar bu sınıra yaklaşabilir;
poll aralığını veri hacmine göre ayarlayın. PBX istemcisi `429` için yeniden dener.
Telegram çağrı bildirimleri ve Excel raporları `RetryAfter` süresine uyarak en fazla
üç kez denenir. DM başarısızsa bekleyen kayıt korunur.

## Mimari

```text
bot.py → pbx_provider.py → toniva_client.py | invekto_client.py
                ↓
     notifications / sent_store / personnel / delivered / excel
```

- Polling (JobQueue): bugün, dün ve kalıcı bekleyen çağrıların günleri
- Kesinti sonrası son başarılı taramadan bugüne kadar eksik günler (en fazla 45 gün) taranır
- Dedup: `Phone|dd.mm.yyyy|HH:MM:SS|Queue`
- DM veya grup gönderimi başarısızsa sadece eksik gönderim yeniden denenir
- Dahili/personel eşleşmeyen çağrılar kalıcı olarak bekler; düzeltme sonrası yeniden işlenir
- İlk kurulumda depo boşsa bugün ve dün **seed** edilir; eski çağrılar bildirimsiz kapatılır (`SEED_TODAY_ON_STARTUP`)
- Kuyruk eşlemesi: `1000` ↔ `1000 (1000)` alias uyumu
- Takvim: `BOT_TIMEZONE` (varsayılan `Europe/Istanbul`)

### Rapor ve yeniden gönderim

- İletim raporuna yalnızca başarılı personel DM'leri yazılır; grup mesajı tek başına teslim değildir.
- `/gonder` eski geçmişi silmez; başarılı tekrar DM'leri yeni teslim satırı olarak saklanır.
- `/gonder durdur` eski kayıtları değiştirmez. Yalnız `/gonder sessiz` kalan çağrıları bildirimsiz kapatır.
- Geri arama için bildirimden sonraki dış arama, aynı telefon ve aynı dahili/tam personel adı gerekir.
- PBX sorgusu veya sayfalama başarısızsa rapor `Kontrol Edilemedi (PBX Hatası)` gösterir.
- Yön bilgisi eksik kayıtlar geri arama kanıtı sayılmaz; canlı PBX yön alanı doğrulanmalıdır.
- Manuel telefon eşlemesi API cache tarafından ezilmez. Eski telefon haritasında kaynak bilgisi
     olmadığı için mevcut kayıtlar güvenli geçiş amacıyla manuel kabul edilir.
- Excel hücrelerindeki formül benzeri metinler çalıştırılabilir formül olarak yazılmaz.

### İzleme ve sınırlar

Başarılı taramada `health.json` güncellenir. En az 5 dakika başarılı tarama olmazsa
Telegram uyarısı üretilir; tekrar uyarılar 15 dakika aralıklıdır. Manuel yeniden
gönderim sırasında bu kontrol askıdadır. Tamamen durmuş süreç kendi kendine uyarı
veremez; Railway izleme ve harici heartbeat denetimi ayrıca kurulmalıdır.

Loglarda telefon ve bilinen gizli anahtarlar maskelenir; gelen mesaj metinleri loglanmaz.
JSON kayıtları ve raporlar kişisel veri içerir; volume erişimi ve yedekler korunmalıdır.
Bekleyen çağrılar sessizce süre aşımına uğratılmaz; eski bekleyen günler için PBX veri
erişimi gerekir. Telegram'ın mesajı kabul ettiği an ile yerel kayıt arasındaki ani
süreç/disk arızasında mutlak tek-sefer teslim garantisi yoktur.

## Geliştirme

```bash
python -m pip check
python -m pytest -q
```

Testler geçici veri klasöründe çalışır; gerçek PBX ve Telegram ağ çağrıları engellenir.
GitHub Actions Linux/Windows ve Python 3.11/3.14 matrisinde testleri çalıştırır.
Üretime almadan önce test personeliyle DM, grup yetkisi, PBX yön alanı ve yeniden
başlatma sonrası volume kalıcılığı ayrıca doğrulanmalıdır. Eski rapor kayıtları geçmiş
sürümde grup teslimine göre oluşmuş olabilir; yeni DM kuralı geçmiş kayıtları geriye dönük doğrulamaz.

## Sorun Giderme

| Belirti | Kontrol |
| --- | --- |
| Bildirim yok | `/ping`, `/ayar`, `TONIVA_QUEUE`, API key scope |
| Toniva 401/403 | Key, scope `reports:read`, IP whitelist |
| Toniva 429 | Poll aralığını artır |
| Veri siliniyor | Volume `/app/data` + `DATA_DIR` |
| Personel DM yok | Personel bota `/start` yazdı mı? |

## Lisans

İç kullanım için.
