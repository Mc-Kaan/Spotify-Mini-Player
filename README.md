# Spotify Mini Player

Windows için küçük, her zaman üstte kalan bir müzik oynatıcı penceresi. Çalan şarkının adını, sanatçısını, albüm kapağını ve kaçıncı saniyede olduğunu gösterir; önceki, oynat/durdur ve sonraki butonlarıyla kontrol sağlar.

Spotify API'si, Premium hesap veya Client ID gerekmez. Uygulama, Windows'un sistem medya oturumuna (klavyedeki medya tuşlarının kullandığı sistem) bağlanır.

## Özellikler

- Şarkı adı ve sanatçı
- Albüm kapağı
- Geçen süre / toplam süre ve ilerleme çubuğu
- Önceki, oynat/durdur, sonraki kontrolleri
- Diğer pencerelerin üstünde kalır
- Ücretsiz Spotify hesabıyla da çalışır

## Kullanım

1. [Releases](../../releases) sayfasından `SpotifyMiniPlayer.exe` dosyasını indir.
2. Çift tıklayarak aç. Spotify'ı açıp bir şarkı çaldırdığında pencerede görünür.

> Windows SmartScreen imzasız bir exe olduğu için uyarı gösterebilir. "Daha fazla bilgi" → "Yine de çalıştır" ile açılır.

## Kaynak koddan çalıştırma

Python 3.12 önerilir.

```
pip install -r requirements.txt
python mini_player.py
```

## Kendi exe'ni oluşturma

```
pip install pyinstaller
pyinstaller --onefile --noconsole --collect-submodules winsdk --name SpotifyMiniPlayer mini_player.py
```

Çıktı `dist/SpotifyMiniPlayer.exe` içinde oluşur. Bu depoda `v*` biçiminde bir etiket (ör. `v1.0.0`) gönderildiğinde GitHub Actions exe'yi otomatik oluşturup Release'e ekler.

## Sınırlamalar

- Sadece Windows 10/11 üzerinde çalışır.
- Uygulama, Spotify'ı uzaktan yönetmez; şarkı seçmek veya çalma listesi açmak için Spotify'ın kendi penceresini kullanman gerekir.
- Varsa Spotify oturumunu gösterir; Spotify açık değilse Windows'un o an aktif saydığı medya oturumunu (ör. tarayıcıda çalan müzik) gösterebilir.

## Lisans

MIT
