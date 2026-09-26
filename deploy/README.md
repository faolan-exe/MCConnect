# MCConnect deployen

Diese Anleitung bringt MCConnect auf einen Linux-Server mit Docker. Am Ende läuft:

| Adresse | Was |
|---|---|
| `https://mc.tobisit.de` | Startseite, Login und Verwaltung für Server-Admins |
| `https://<server>.mc.tobisit.de` | Seite eines Minecraft-Servers (Spieler, Statistiken) |
| `mc.tobisit.de:9991` (TCP) | Hier verbindet sich das Minecraft-Plugin |

Der Stack besteht aus fünf Containern: **traefik** (HTTPS, Zertifikate), **web** (Website),
**socket** (Plugin-Verbindungen), **db** (PostgreSQL) und **backup** (tägliches DB-Backup).

---

## 1. Voraussetzungen

- Linux-Server mit öffentlicher IP, Docker und dem Compose-Plugin (`docker compose version`)
- Offene Ports: **80**, **443** (Website) und **9991/TCP** (Plugin)
- Domain `tobisit.de` bei Cloudflare
- Ein E-Mail-Postfach bei Strato (für Bestätigungsmails)

## 2. DNS bei Cloudflare

Im Cloudflare-Dashboard unter *DNS → Records* zwei Einträge anlegen:

| Typ | Name | Inhalt | Proxy-Status |
|---|---|---|---|
| A | `mc` | IP des Servers | **DNS only (graue Wolke)** |
| A | `*.mc` | IP des Servers | **DNS only (graue Wolke)** |

Hat der Server auch IPv6, zusätzlich dieselben zwei Einträge als `AAAA`.

> **Wichtig: graue Wolke, nicht orange.** Das kostenlose Cloudflare-Zertifikat deckt nur
> `*.tobisit.de` ab, nicht `*.mc.tobisit.de`. Außerdem leitet der Cloudflare-Proxy den
> Plugin-Port 9991 nicht weiter. Die Zertifikate holt stattdessen Traefik bei Let's Encrypt.

## 3. Cloudflare-API-Token

Traefik braucht den Token, um ein Wildcard-Zertifikat per DNS-Challenge zu bekommen.

1. Cloudflare → *My Profile → API Tokens → Create Token*
2. Vorlage **„Edit zone DNS“** wählen
3. *Zone Resources*: `Include → Specific zone → tobisit.de`
4. Token erstellen und kopieren (er wird nur einmal angezeigt)

## 4. Installation

```bash
git clone https://github.com/Tobias-Auer/MCConnect.git
cd MCConnect/deploy
cp .env.example .env
```

Secrets erzeugen und in `.env` eintragen:

```bash
openssl rand -hex 32   # -> DB_PASSWORD
openssl rand -hex 32   # -> SECRET_KEY
```

Dann in `.env` ausfüllen:

- `CF_DNS_API_TOKEN`: der Token aus Schritt 3
- `ACME_EMAIL`: deine E-Mail-Adresse, Let's Encrypt schreibt dorthin bei Problemen mit dem Zertifikat
- `MCC_SMTP_USER` und `MCC_SMTP_PASSWORD`: Adresse und Passwort des Strato-Postfachs.
  Host `smtp.strato.de`, Port `587` sind schon eingetragen.
- `MCC_LEGAL_*`: deine Angaben für Impressum und Datenschutz. Ein Impressum ist in Deutschland Pflicht.

Starten:

```bash
chmod 600 .env
docker compose up -d --build
docker compose ps          # alle Container "Up", web "healthy"
docker compose logs -f traefik   # Zertifikat: dauert beim ersten Start 1-2 Minuten
```

Das Datenbankschema wird beim ersten Start automatisch angelegt. Bei späteren Updates werden
Migrationen automatisch angewendet.

## 5. Ersten Admin anlegen

Entweder über die Website (`https://mc.tobisit.de/login` → Registrieren, dann den Link in der
E-Mail bestätigen) oder direkt per Kommandozeile:

```bash
docker compose exec web python -m database.manage create-admin tobi tobi@tobisit.de
```

## 6. Minecraft-Server verbinden

1. Auf `https://mc.tobisit.de` einloggen → **Server hinzufügen**. Die Subdomain bestimmt die
   Adresse, z.&nbsp;B. `survival` → `https://survival.mc.tobisit.de`.
2. Auf der Verwaltungsseite **MCDataLink.jar herunterladen** und in den `plugins`-Ordner des
   Minecraft-Servers legen (Spigot oder Paper ab 1.13).
3. Den Minecraft-Server einmal starten. Danach `plugins/MCDataLink/config.yml` mit dem angezeigten
   Block ersetzen (Button „Kopieren“):
   ```yaml
   key: <dein key>
   host: mc.tobisit.de
   port: 9991
   ```
4. Den Minecraft-Server neu starten. Im Server-Log steht dann `Connected to MCConnect`, und auf der
   Verwaltungsseite erscheint **„Plugin verbunden“**.

Test des Logins: Auf der Serverseite auf *Login* klicken und den Spielernamen eingeben, während du
online bist. Die PIN erscheint im Minecraft-Chat.

## 7. Updates

```bash
cd MCConnect && git pull
cd deploy && docker compose up -d --build
```

## 8. Backups

Der `backup`-Container schreibt jeden Tag einen Dump nach `deploy/backups/` und behält 14 Tage.
Kopiere das Verzeichnis regelmäßig auf einen anderen Rechner, denn ein Backup auf demselben Server
hilft nicht, wenn der Server ausfällt.

Wiederherstellen:

```bash
docker compose stop web socket
docker compose exec -T db pg_restore -U mcconnect -d mcconnect --clean --if-exists < backups/mcconnect-2026-01-31.dump
docker compose start web socket
```

## 9. Wenn du schon einen Traefik hast

Dann den `traefik`-Service aus `docker-compose.yml` entfernen und:

- das Netzwerk `web` auf dein bestehendes Traefik-Netzwerk zeigen lassen:
  ```yaml
  networks:
    web:
      name: <dein-traefik-netzwerk>
      external: true
  ```
- in den Labels von `web` `certresolver=cloudflare` durch den Namen deines Resolvers ersetzen.
  Dieser muss die DNS-Challenge für Cloudflare können, denn ohne sie gibt es kein Wildcard-Zertifikat.
- Port 9991 bleibt direkt am `socket`-Container veröffentlicht, er läuft nicht über Traefik.

## 10. Fehlersuche

| Problem | Prüfen |
|---|---|
| Zertifikatsfehler im Browser | `docker compose logs traefik`: Token richtig? Hat er die Berechtigung „DNS Edit“? Ist die Wolke grau? |
| Plugin verbindet nicht | Vom Minecraft-Server aus `nc -vz mc.tobisit.de 9991`. Firewall/Port 9991 offen? `docker compose logs socket` |
| „MCConnect rejected the server key“ | Der Key in `config.yml` passt nicht. Auf der Verwaltungsseite neu kopieren |
| PIN kommt nicht an | Ist das Plugin verbunden (Verwaltungsseite)? Ist der Spieler online? `docker compose logs socket` |
| Keine Bestätigungsmail | `docker compose logs web \| grep -i smtp`. Ohne SMTP-Konfiguration steht der Link im Log |
| Seite zeigt 404 | Gibt es die Subdomain wirklich? Ist sie in Kleinbuchstaben angelegt? |

Manuell eine Test-Mail schicken:

```bash
docker compose exec web python -m database.SMTPMailer du@example.com
```

## Bekannte Einschränkungen

- Die Verbindung Plugin → Server auf Port 9991 ist **nicht verschlüsselt**; der Server-Key wird im
  Klartext übertragen. Wenn jemand den Key abgreift, kann er falsche Statistiken für den Server senden.
  In dem Fall auf der Verwaltungsseite einen neuen Key erzeugen. TLS für den Socket ist geplant.
- Spielerstatistiken werden beim Autosave der Welt (standardmäßig alle 5 Minuten) und beim
  Verlassen des Servers aktualisiert.
