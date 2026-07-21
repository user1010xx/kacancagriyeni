# Kaçan Çağrı Botu

PBX üzerinden günlük kaçan / cevapsız çağrıları tespit edip yetkili Telegram grubuna ve personele anlık bildirim gönderen bot.

**Desteklenen PBX sağlayıcıları:**

| Provider | Env | Kaçan çağrı kaynağı |
|----------|-----|---------------------|
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

- Python 3.10+
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
pip install -r requirements.txt
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
|-------|----------|
| `/start` `/help` | Yardım |
| `/ping` | Bağlantı ve yetki testi |
| `/chatid` | Grup ID |
| `/ayar` | Bot ayarları (provider, kuyruk, …) |
| `/firmakodu 12345678` | Invekto firma kodu (Toniva'da gerekmez) |
| `/stats` | Dedup / poll istatistikleri |
| `/kuyruklar` | PBX kuyruk listesi |
| `/kacancagri 15.06.2026, 25.06.2026` | Excel kaçan çağrı raporu |
| `/iletilenkacancagri 28.06.2026` | İletilen çağrı + geri arama raporu |
| `/gonder 20.07.2026,21.07.2026` | Seçili günleri gruba+DM yeniden ilet (dedup temizler) |
| `/personelekle` `/personelsil` `/personeller` | Personel yönetimi |
| `/temizle` | Eski dedup kayıtlarını temizle |
| Excel (.xlsx) yükle | Toplu personel: A=isim, B=dahili, C=@username |

**DM için:** Personel bota özel sohbetten `/start` yazmalıdır.

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

Saklananlar: `sent_calls.json`, `config.json`, `personnels.json`, `delivered_calls.json`, `logs/`

### 3. Toniva IP whitelist

Tenant'ta IP kısıtı varsa Railway outbound IP'yi Toniva whitelist'e ekle (`403 CRM-2093` alırsan).

### 4. Rate limit

Toniva: 100 istek/dakika. Poll ~30 sn + conversation cache 5 dk ile limit altında kalınır. `429` durumunda `Retry-After` ile beklenir.

## Mimari

```
bot.py → pbx_provider.py → toniva_client.py | invekto_client.py
                ↓
     notifications / sent_store / personnel / delivered / excel
```

- Polling (JobQueue), her poll **bugünün** verisi
- Dedup: `Phone|dd.mm.yyyy|HH:MM:SS|Queue`
- DM başarısızsa kayıt tamamlanmaz; sonraki poll yeniden dener
- İlk kurulumda (`sent_calls` boş) bugünkü kayıtlar **seed** edilir (flood yok); `SEED_TODAY_ON_STARTUP`
- Kuyruk eşlemesi: `1000` ↔ `1000 (1000)` alias uyumu
- Takvim: `BOT_TIMEZONE` (varsayılan `Europe/Istanbul`)

## Geliştirme

```bash
pytest -q
```

## Sorun Giderme

| Belirti | Kontrol |
|---------|---------|
| Bildirim yok | `/ping`, `/ayar`, `TONIVA_QUEUE`, API key scope |
| Toniva 401/403 | Key, scope `reports:read`, IP whitelist |
| Toniva 429 | Poll aralığını artır |
| Veri siliniyor | Volume `/app/data` + `DATA_DIR` |
| Personel DM yok | Personel bota `/start` yazdı mı? |

## Lisans

İç kullanım için.
