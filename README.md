<p align="center"><img src="assets/logo.png" width="128" alt="Spotify Mini Player logosu"></p>

# Spotify Mini Player

**Türkçe** | [English](README.en.md)

Windows için küçük, her zaman üstte kalan bir müzik oynatıcı penceresi. Çalan şarkının adını, sanatçısını, albüm kapağını ve kaçıncı saniyede olduğunu gösterir; önceki, oynat/durdur ve sonraki butonlarıyla kontrol sağlar.

Spotify API'si, Premium hesap veya Client ID gerekmez. Uygulama, Windows'un sistem medya oturumuna (klavyedeki medya tuşlarının kullandığı sistem) bağlanır.

## Özellikler

- HUD tarzı, yuvarlak köşeli yatay pencere: solda albüm kapağı, yanında şarkı adı ve sanatçı, altta ilerleme çubuğu ile geçen / kalan süre
- İlerleme çubuğuna tıklayıp basılı tutarak istediğin dakika ve saniyeye sürükleyebilirsin (sarma)
- İlerleme çubuğunun altında senkronize şarkı sözleri: satır satır ilerler, çalan satır vurgulanır, kayarak animasyonlu geçer ([LRCLIB](https://lrclib.net) kaynaklı)
- Kapaktan otomatik vurgu rengi: ilerleme çubuğu, ekolayzır ve dalga efekti her şarkıda albüm kapağının rengine yumuşakça geçer
- Ses seviyesi: fareyi üstüne getirince üstte ses çubuğu belirir; sürükleyebilir ya da fare tekerleğini çevirebilirsin
- Kompakt mod: boş bir yere çift tıklayınca sadece kapak, şarkı adı ve ince bir ilerleme çizgisi kalan küçük bir şeride dönüşür
- Animasyonlar: açılışta kayarak belirme, şarkı değişince kapak ve yazıların kayarak gelmesi, çalarken hareket eden ekolayzır çubukları, yumuşak ilerleyen çubuk
- Fareyi üstüne getirince önceki, oynat/durdur ve sonraki butonları belirir ve fareyi takip eder; tıklayınca dalga efekti çıkar, ileri/geri basınca içerik ilgili yöne kayar
- Sağ üstteki çark simgesiyle ayarlar: hazır temalar, HEX / RGB / renk seçici ile tüm renkleri değiştirme, pencere boyutu (%60-%200), şeffaflık, yazı tipi seçimi, sözleri ve kompakt modu açma/kapatma
- Ctrl + fare tekerleği ile pencereyi hızlıca büyütüp küçültebilirsin
- Tema, boyut, şeffaflık, yazı tipi, mod, pencere konumu ve "her zaman üstte" seçimi kapatıp açınca hatırlanır (`%APPDATA%\SpotifyMiniPlayer\config.json`)
- Uygulama arayüzü İngilizcedir
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

- Ses çubuğu, Spotify'ın kendi ses seviyesini (Spotify çalmıyorsa sistem sesini) ayarlar ve `pycaw` paketini kullanır. Paket kurulu değilse ya da ses aygıtı bulunamazsa ses çubuğu görünmez, uygulamanın geri kalanı çalışmaya devam eder.
- Şarkı sözleri Spotify'dan değil, açık bir servis olan LRCLIB'den gelir. Her şarkının senkronize sözü bulunmayabilir ve zamanlama Spotify'ın kendi sözlerinden farklı olabilir. Sözleri getirmek için internet gerekir.
- Sarma özelliği, Spotify'ın Windows medya kontrolünde konum değiştirmeyi desteklemesine bağlıdır. Desteklemezse çubuk eski yerine döner.
- Sadece Windows 10/11 üzerinde çalışır.
- Uygulama, Spotify'ı uzaktan yönetmez; şarkı seçmek veya çalma listesi açmak için Spotify'ın kendi penceresini kullanman gerekir.
- Varsa Spotify oturumunu gösterir; Spotify açık değilse Windows'un o an aktif saydığı medya oturumunu (ör. tarayıcıda çalan müzik) gösterebilir.

## Lisans

MIT
