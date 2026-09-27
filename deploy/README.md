# MCConnect deployen

Diese Anleitung bringt MCConnect in einen eigenen Proxmox-LXC mit Docker. HTTPS übernimmt der
vorhandene **Nginx Proxy Manager (NPM)**. Am Ende läuft:

| Adresse | Was |
|---|---|
| `https://mc.tobisit.de` | Startseite, Login und Verwaltung für Server-Admins |
| `https://<server>.mc.tobisit.de` | Seite eines Minecraft-Servers (Spieler, Statistiken) |
| `mc.tobisit.de:9991` (TCP) | Hier verbindet sich das Minecraft-Plugin |

```
Internet
  ├─ :80 / :443 ──► Router ──► NPM (HTTPS, Wildcard-Zertifikat)
  │                               └─► MCConnect-LXC :8000   (web)
  └─ :9991 ───────► Router ──────────► MCConnect-LXC :9991   (socket)

MCConnect-LXC (Docker):  web · socket · db (PostgreSQL) · backup
```

---

## 1. LXC anlegen

In Proxmox einen neuen Container anlegen, z.&nbsp;B. mit Debian 12, 2 CPU-Kernen, 2 GB RAM und 16 GB Disk.
Unter *Options → Features* **`nesting`** und **`keyctl`** aktivieren. Beides braucht Docker im LXC.
Dem Container eine feste IP geben (im Folgenden `192.168.1.50`).

Docker installieren:

```bash
apt update && apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sh
docker compose version   # muss funktionieren
```

## 2. Router: Portweiterleitung

| Port | Ziel |
|---|---|
| 80, 443 | bleiben wie bisher beim NPM |
| **9991/TCP** | **MCConnect-LXC** (`192.168.1.50:9991`) |

Port 8000 des LXC wird **nicht** weitergeleitet. Den erreicht nur der NPM im lokalen Netz.

## 3. DNS bei Cloudflare

Im Cloudflare-Dashboard unter *DNS → Records*:

| Typ | Name | Inhalt | Proxy-Status |
|---|---|---|---|
| A | `mc` | öffentliche IP | **DNS only (graue Wolke)** |
| A | `*.mc` | öffentliche IP | **DNS only (graue Wolke)** |

Mit fester IPv6 zusätzlich dieselben zwei Einträge als `AAAA`. Bei wechselnder öffentlicher IP
kann `mc` ein CNAME auf deinen DynDNS-Namen sein, `*.mc` ebenfalls.

> **Graue Wolke, nicht orange.** Cloudflare macht hier nur DNS. Über den Cloudflare-Proxy würde
> Port 9991 nicht funktionieren, und das kostenlose Cloudflare-Zertifikat deckt `*.mc.tobisit.de`
> nicht ab.

## 4. MCConnect installieren

Im LXC:

```bash
git clone https://github.com/faolan-exe/MCConnect.git /opt/mcconnect
cd /opt/mcconnect/deploy
cp .env.example .env
chmod 600 .env
openssl rand -hex 32   # -> DB_PASSWORD
openssl rand -hex 32   # -> SECRET_KEY
nano .env
```

In `.env` ausfüllen:

- `DB_PASSWORD` und `SECRET_KEY`: die beiden erzeugten Werte
- `MCC_SMTP_USER` und `MCC_SMTP_PASSWORD`: Adresse und Passwort des Strato-Postfachs.
  Host `smtp.strato.de`, Port `587` sind schon eingetragen.
- `MCC_LEGAL_*`: deine Angaben für Impressum und Datenschutz (Pflicht für eine öffentliche Seite)

Starten:

```bash
docker compose up -d --build
docker compose ps        # web "healthy", socket/db/backup "Up"
curl -s -H "Host: mc.tobisit.de" http://127.0.0.1:8000/healthz   # {"status":"ok"}
```

Der erste Build dauert ein paar Minuten, weil dabei auch das Minecraft-Plugin gebaut wird.
Das Datenbankschema wird beim ersten Start automatisch angelegt. Bei Updates werden Migrationen
automatisch angewendet.

## 5. Nginx Proxy Manager: Proxy-Host

Für ein Wildcard-Zertifikat verlangt Let's Encrypt die DNS-Challenge. Der NPM legt dafür beim
Ausstellen und Verlängern kurz einen TXT-Eintrag bei Cloudflare an. Das Zertifikat selbst kommt von
Let's Encrypt, nicht von Cloudflare.

**Cloudflare-API-Token** (einmalig):
Cloudflare → *My Profile → API Tokens → Create Token* → Vorlage **„Edit zone DNS“** →
*Zone Resources*: `Include → Specific zone → tobisit.de` → Token kopieren.

**Im NPM:** *Hosts → Proxy Hosts → Add Proxy Host*

*Tab „Details“*
- Domain Names: `mc.tobisit.de` **und** `*.mc.tobisit.de`
- Scheme: `http`
- Forward Hostname / IP: `192.168.1.50` (IP des MCConnect-LXC)
- Forward Port: `8000`
- Block Common Exploits: an
- Websockets Support: egal (wird nicht genutzt)

*Tab „SSL“*
- SSL Certificate: *Request a new SSL Certificate*
- Force SSL: an, HTTP/2 Support: an
- **Use a DNS Challenge**: an → DNS Provider **Cloudflare** → in *Credentials File Content* den
  Platzhalter-Token ersetzen:
  ```
  dns_cloudflare_api_token = <dein Token>
  ```
- E-Mail eintragen, AGB akzeptieren, speichern. Das Ausstellen dauert etwa eine Minute.

*Tab „Advanced“*: nichts nötig. Die App sagt nginx selbst, dass die Live-Updates (Spielerzahl,
Online-Status) nicht gepuffert werden sollen.

Jetzt sollte `https://mc.tobisit.de` die Startseite zeigen.

## 6. Ersten Admin anlegen

Entweder über die Website (`https://mc.tobisit.de/login` → Registrieren, dann den Link in der
E-Mail bestätigen) oder direkt im LXC:

```bash
cd /opt/mcconnect/deploy
docker compose exec web python -m database.manage create-admin tobi tobi@tobisit.de
```

## 7. Minecraft-Server verbinden

1. Auf `https://mc.tobisit.de` einloggen → **Server hinzufügen**. Die Subdomain bestimmt die
   Adresse, z.&nbsp;B. `survival` → `https://survival.mc.tobisit.de`.
2. Auf der Verwaltungsseite **MCDataLink.jar herunterladen** und in den `plugins`-Ordner des
   Minecraft-Servers legen (Spigot oder Paper ab 1.13).
3. Den Minecraft-Server einmal starten. Danach `plugins/MCDataLink/config.yml` durch den
   angezeigten Block ersetzen (Button „Kopieren“):
   ```yaml
   key: <dein key>
   host: mc.tobisit.de
   port: 9991
   ```
4. Den Minecraft-Server neu starten. Im Server-Log steht dann `Connected to MCConnect`, und auf der
   Verwaltungsseite erscheint **„Plugin verbunden“**.

Test des Logins: Auf der Serverseite *Login* klicken und den Spielernamen eingeben, während du
online bist. Die PIN erscheint im Minecraft-Chat.

### Verschlüsselte Plugin-Verbindung (optional)

Ohne Zertifikat läuft die Verbindung Plugin → MCConnect unverschlüsselt über Port 9991. Mit einem
Zertifikat für `mc.tobisit.de` bietet der Socket-Server zusätzlich **Port 9992 mit TLS** an; Plugins ab
Version 3.8 nutzen ihn mit `tls: true` (die Verwaltungsseite zeigt dann automatisch diese Konfiguration).
Ältere Plugins verbinden sich weiter über 9991.

1. Router: **9992/TCP** ebenfalls auf den MCConnect-LXC weiterleiten.
2. Zertifikat nach `/opt/mcconnect/deploy/tls/` legen: `fullchain.pem` und `privkey.pem`. Am einfachsten das
   Wildcard-Zertifikat des NPM verwenden. Es liegt im NPM unter `/etc/letsencrypt/live/npm-<nr>/`
   (Nummer: im NPM unter *SSL Certificates* per Maus über dem Zertifikat). Per Cronjob auf dem NPM-Host
   täglich kopieren, z.&nbsp;B.:
   ```bash
   scp -L /pfad/zum/npm/letsencrypt/live/npm-3/{fullchain,privkey}.pem root@192.168.1.50:/opt/mcconnect/deploy/tls/
   ```
   Der Socket-Server lädt ein erneuertes Zertifikat bei der nächsten Verbindung selbst neu.
3. In `.env` einkommentieren:
   ```
   MCC_SOCKET_TLS_CERT=/tls/fullchain.pem
   MCC_SOCKET_TLS_KEY=/tls/privkey.pem
   ```
   und `docker compose up -d` ausführen. Im Log des Socket-Containers steht dann
   `Socket server listening with TLS on 0.0.0.0:9992`.

## 8. Updates

```bash
cd /opt/mcconnect && git pull
cd deploy && docker compose up -d --build
```

Wenn sich das Plugin geändert hat (Version in `java plugin/MCDataLink/pom.xml`), danach auf der
Verwaltungsseite **MCDataLink.jar** neu herunterladen, im `plugins`-Ordner des Minecraft-Servers
ersetzen und den Server neu starten. Ältere Plugins laufen weiter, ihnen fehlen nur die neuen
Funktionen (ab 3.1: Erfolge und Wettbewerbe im Chat, ab 3.2: Serverzustand).

## 9. Backups

Der `backup`-Container schreibt jeden Tag einen Dump der Datenbank nach
`/opt/mcconnect/deploy/backups/` und behält 14 Tage. Die hochgeladenen Serverbilder liegen im
Docker-Volume `mcconnect_uploads` und sind **nicht** im Dump enthalten. Nimm deshalb den ganzen LXC
in deine Proxmox-Backups auf. Alternativ sicherst du das Volume selbst:

```bash
docker run --rm -v mcconnect_uploads:/uploads -v "$PWD/backups":/backup alpine \
  tar czf /backup/uploads-$(date +%F).tar.gz -C /uploads .
```

Wiederherstellen:

```bash
docker compose stop web socket
docker compose exec -T db pg_restore -U mcconnect -d mcconnect --clean --if-exists < backups/mcconnect-2026-01-31.dump
docker compose start web socket
```

## 10. Fehlersuche

| Problem | Prüfen |
|---|---|
| NPM zeigt „502 Bad Gateway“ | Vom NPM-Host aus: `curl -H "Host: mc.tobisit.de" http://192.168.1.50:8000/healthz`. Richtige IP? `docker compose ps` im LXC |
| Zertifikat wird nicht ausgestellt | NPM-Log. Token-Berechtigung „DNS Edit“ für `tobisit.de`? Beide Domains im Proxy-Host eingetragen? |
| Seite lädt ohne Design | `https://mc.tobisit.de/static/css/admin.css` erreichbar? Ist `*.mc` **und** `mc` im Proxy-Host eingetragen? |
| Unbekannte Subdomain → 404 | So gewollt. Nur angelegte Server haben eine Seite |
| Plugin verbindet nicht | Vom Minecraft-Server aus `nc -vz mc.tobisit.de 9991`. Portweiterleitung auf den LXC? `docker compose logs socket` |
| „MCConnect rejected the server key“ | Der Key in `config.yml` passt nicht. Auf der Verwaltungsseite neu kopieren |
| PIN kommt nicht an | Plugin verbunden (Verwaltungsseite)? Spieler online? `docker compose logs socket` |
| Keine Bestätigungsmail | `docker compose logs web \| grep -i smtp`. Ohne SMTP-Konfiguration steht der Link im Log |

Manuell eine Test-Mail schicken:

```bash
docker compose exec web python -m database.SMTPMailer du@example.com
```

## Alternative: HTTPS mit dem mitgelieferten Traefik

Wenn 80/443 direkt auf den MCConnect-LXC zeigen (ohne NPM), kann der mitgelieferte Traefik das
Wildcard-Zertifikat selbst holen. Dazu in `.env` `ACME_EMAIL`, `CF_DNS_API_TOKEN` (Token wie in
Schritt 5) und `WEB_BIND=127.0.0.1` setzen und mit Profil starten:

```bash
docker compose --profile traefik up -d --build
```

## Bekannte Einschränkungen

- Ohne Zertifikat (siehe „Verschlüsselte Plugin-Verbindung“) ist die Verbindung Plugin → Server auf Port 9991
  **nicht verschlüsselt**; der Server-Key wird dann im Klartext übertragen. Wenn jemand den Key abgreift, kann er
  falsche Statistiken für den Server senden. In dem Fall auf der Verwaltungsseite einen neuen Key erzeugen.
- Die Statistiken von Spielern, die online sind, kommen ab Plugin 3.3 jede Minute, sonst beim Autosave der Welt
  (standardmäßig alle 5 Minuten) und beim Verlassen des Servers.
