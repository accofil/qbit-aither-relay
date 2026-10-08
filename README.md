# qbit-aither-relay

Kétszerveres seedbox-lánc qBittorrenthez. A gyors (NVMe) gépen letöltött, **`aither` kategóriájú**
torrentek (a kategória dönt, nem a tracker – így ami nem kell, az egyszerűen más kategóriát kap):

1. a letöltés vége után **30 perccel** átköltöznek ugyanazon a gépen a HDD-re (`~/storage/aither`),
2. onnan **rsync** viszi át az adatot a távoli seedboxra (`~/torrents/qbittorrent/aither`),
3. a távoli qBittorrent megkapja a `.torrent` fájlt és **újrahasheli** az adatot,
4. ha a hash-ellenőrzés 100%, a távoli torrent elindul, és a helyi példány azonnal megszűnik:
   - ha más torrent is használja ugyanazokat a fájlokat (**cross-seed**, azonos útvonal vagy hardlink),
     csak a listáról kerül le, az adat marad;
   - különben a torrent **az adattal együtt** törlődik.

A következő átvitel nem vár a távoli hash-ellenőrzésre: az átviteli hely az rsync végén felszabadul,
a hash-ellenőrzésre csak a helyi törlés vár.

Bármilyen hiba esetén a helyi torrent érintetlen marad, és `relay:MANUAL` címkét kap.

Csak Python 3.11 standard könyvtár, `rsync` és `ssh` kell hozzá, `pip` nem.

## Hogyan dolgozik?

A program folyamatosan fut (systemd-szolgáltatás), 20 másodpercenként végignézi a helyi qBittorrentet. Minden
torrent állapota egy JSON-fájlban van, így újraindítás után ott folytatja, ahol abbahagyta; a félbeszakadt
rsync is folytatódik.

| Állapot | Jelentés | Címke a helyi qBittorrentben |
|---|---|---|
| `moving` | a qBittorrent áthelyezi az NVMe-ről a HDD-re | `relay:hdd-move` |
| `on_hdd` | szabad átviteli helyre vár (a HDD-n, cross-seednél az eredeti helyén) | `relay:queued` |
| `added` / `transferring` | a távoli gépre leállítva felkerült a torrent, rsync fut | `relay:sending` |
| `verifying` | a távoli gép hasheli; 100%-nál elindítja, és a helyi példány törlődik | `relay:remote-check` |
| `done` | a helyi példány eltávolítva | – |
| `manual` | emberi beavatkozás kell (lásd a naplót) | `relay:MANUAL` |

**Cross-seed:** ha egy torrent adatát más torrent is használja (azonos útvonal vagy hardlink), a program
**nem költözteti** a HDD-re, mert akkor a másik torrent elveszítené az adatát. Ilyenkor az eredeti helyéről
küldi át, a végén pedig csak a listáról veszi le, az adat a másik torrentnél marad. A törlés előtti
ellenőrzés így mindig megtalálja a közös adatot, akkor is, ha a cross-seed csak később került fel.

### Miért rsync démon és nem SSH?

Tengerentúli kapcsolaton (~80 ms) az OpenSSH csatornaablaka egyetlen rsync-folyamot ~200 Mbit/s-ra
korlátoz. A távoli gépen futó rsync démon közvetlen TCP-n ~840 Mbit/s-ot ért el (a távoli gép 1 Gbit-es
vonalán). A démon csak a forrásszerver IP-címéről és jelszóval fogad kapcsolatot; az adat titkosítatlanul
megy át. Ha ezt nem szeretnéd, hagyd üresen a `rsync_url` beállítást, és SSH-n megy.

## Telepítés

### 1. Távoli gép (ahová az adat kerül)

```bash
mkdir -p ~/.config/aither-relay ~/torrents/qbittorrent
# rsyncd.conf: a deploy/rsyncd.conf alapján, USER és SOURCE_SERVER_IP kicserélve
nano ~/.config/aither-relay/rsyncd.conf
echo "relay:$(head -c 18 /dev/urandom | base64 | tr -dc A-Za-z0-9)" > ~/.config/aither-relay/rsyncd.secrets
chmod 600 ~/.config/aither-relay/rsyncd.secrets
sudo cp deploy/aither-relay-rsyncd@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aither-relay-rsyncd@$USER
```

### 2. Forrásgép (ahol az autobrr tölt)

```bash
git clone https://github.com/accofil/qbit-aither-relay.git ~/qbit-aither-relay
cd ~/qbit-aither-relay
python3 -m unittest discover -s tests

# SSH-kulcs a távoli géphez, és egy "canada" Host bejegyzés a ~/.ssh/config fájlban
ssh-keygen -t ed25519 -N "" -f ~/.ssh/relay_canada
ssh-copy-id -i ~/.ssh/relay_canada.pub -p 6969 USER@REMOTE_IP

mkdir -p ~/.config/aither-relay
cp deploy/config.example.toml ~/.config/aither-relay/config.toml
nano ~/.config/aither-relay/config.toml      # jelszavak, portok, IP
# a távoli rsyncd.secrets fájlban lévő jelszó (a "relay:" utáni rész):
printf '%s' 'JELSZO' > ~/.config/aither-relay/rsync.pass
chmod 600 ~/.config/aither-relay/config.toml ~/.config/aither-relay/rsync.pass

python3 -m aither_relay dry-run              # csak kiírja, mit vinne át
sudo cp deploy/aither-relay@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aither-relay@$USER
```

## Használat

```bash
python3 -m aither_relay status               # minden követett torrent és állapota
python3 -m aither_relay dry-run              # mit vinne át most (semmit nem változtat)
python3 -m aither_relay retry 1a2b3c4d       # MANUAL torrent visszaküldése a folyamatba
tail -f ~/.local/state/aither-relay/relay.log
journalctl -u aither-relay@$USER -f
```

A napló: `~/.local/state/aither-relay/relay.log`, torrentenként külön rsync-napló:
`~/.local/state/aither-relay/<hash>.rsync.log`.

## Beállítások

Lásd: [`deploy/config.example.toml`](deploy/config.example.toml). A fontosabbak:

| Kulcs | Alapérték | Jelentés |
|---|---|---|
| `relay.categories` | `["aither"]` | csak az ilyen kategóriájú torrentek mennek át |
| `relay.move_after_minutes` | 30 | ennyi perc a letöltés vége után a HDD-re költözésig |
| `relay.max_parallel_transfers` | 2 | egyszerre ennyi torrent megy át |
| `relay.poll_seconds` | 20 | ennyi másodpercenként lép tovább a folyamat |
| `relay.min_free_gb_remote` | 100 | ennyi helynek mindig maradnia kell a távoli gépen |
| `relay.verify_timeout_hours` | 6 | ha a távoli hash-ellenőrzés addig sem ér véget: MANUAL |

## Biztonsági szabályok a kódban

- A helyi adat csak akkor törlődik, ha a távoli qBittorrent teljes hash-ellenőrzés után 100%-on áll.
- Cross-seed esetén (azonos fájlútvonal más torrentben, vagy hardlink) a közös adat nem költözik,
  és soha nem törlődik.
- Ha a helyi és a távoli fájlszerkezet eltér (átnevezett fájlok, más mappaszerkezet), a torrent MANUAL lesz.
- Ha a távoli gépen nincs elég hely, az átvitel vár.
- Ha valaki kézzel törli a torrentet a helyi qBittorrentből, a program elengedi.
