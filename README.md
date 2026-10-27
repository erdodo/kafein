# ☕ Kafein
Bilgisayarın ekranını açık tutar (Windows, macOS; Linux best-effort). Tepsi/menü çubuğu simgesi, yerel web paneli ve telefondan uzaktan kontrol.

## Kurulum
[Releases](../../releases) sayfasından indir:
- **Windows:** `Kafein.exe` → çift tıkla.
- **macOS:** `Kafein.dmg` → `Kafein`'i Applications'a sürükle. İmzasız olduğu için ilk açılışta sağ tık → Aç de.

Kaynaktan: `pip install -r requirements.txt && python kafein.py`

## Özellikler (panelden ayarlanır)
- Süreli / süresiz uyanık tut, +15 dk, +1 saat
- **Bilgisayar açılınca otomatik çalış**
- **Zamanlama:** seçili gün + saat aralığında otomatik başla (örn. hafta içi 09:00–18:00). Pencere içinde elle durdurursan o pencere bitene dek yeniden başlamaz.
- Boşta kalınca fareyi oynat (varsayılan 5 dk)
- Pil %10'un altına inince (şarjda değilken) bitir
- Uzaktan: başlangıçta yazılan `http://IP:8765/?token=...` adresini telefondan aç. Sunucu token'la korunur; internete açma.

Ayarlar: `%APPDATA%\Kafein\config.json` · `~/Library/Application Support/Kafein/config.json`

API (başlık `X-Token`): `GET /api/status|config`, `POST /api/extend|set {minutes}`, `/api/stop`, `/api/config`, `/api/quit`.

## Sürüm çıkarma
`git tag v1.0.0 && git push --tags` → Actions exe ve dmg'yi üretip Release'e ekler.
