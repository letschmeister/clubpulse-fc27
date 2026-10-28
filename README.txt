CLUBPULSE – FC 27 Clubs Stats Tracker (Startversion)
===================================================

ANLEITUNG (Windows)
1. Den gesamten ZIP-Ordner entpacken.
2. Python 3.9 oder neuer installieren (https://www.python.org/downloads/).
   Beim Installieren „Add Python to PATH“ auswählen.
3. Im entpackten Ordner in die Adressleiste des Explorers „cmd“ eingeben,
   Enter drücken.
4. Diesen Befehl ausführen:
      py server.py
   Alternativ (wenn py nicht funktioniert): python server.py
5. Im Browser öffnen:
      http://127.0.0.1:8000
6. Clubnamen suchen, Suchergebnis auswählen und echte Statistiken ansehen.
7. Zum Stoppen Strg+C im cmd-Fenster drücken.

FUNKTIONEN
- Clubs anhand ihres Namens suchen
- Club-Spiele, Bilanz, Skill Rating, Tore
- Mitglieder mit Saisontoren, Assists, Bewertung, Spielen
- Spieler-Detailansicht und Karrierewerte für diesen Club
- Bis zu 10 letzte Ligaspiele, sofern EA sie bereitstellt
- Spieler sortieren / filtern, Club-Link kopieren
- Mobile Darstellung

WICHTIGE GRENZEN
- FC 27 PS5, Xbox Series X|S, PC: gemeinsame Plattform 'common-gen5'.
- Die EA-Schnittstelle ist öffentlich erreichbar, aber nicht offiziell dokumentiert.
  Sie kann ausfallen oder sich ändern. Wenn EA 403 liefert, funktioniert
  die Datenabfrage temporär nicht.
- Die Daten werden 2 Minuten im Speicher zwischengespeichert.
- Es sind nur die letzten bis zu 10 Spiele direkt abrufbar.
- Weder EA-Login noch Passwort nötig.
- Eine reine GitHub-Pages-Seite genügt nicht für diese Version:
  der Python-Server muss mitlaufen. Zur öffentlichen Veröffentlichung
  braucht es ein Hosting mit Python-Server / Backend (ggf. kostenloses Kontingent).
- Keine offizielle Verbindung oder Partnerschaft mit Electronic Arts.
- Nur private, lokale Entwicklungsfassung. Für öffentliche Nutzung
  Rate-Limiting, Caching und Nutzungsbedingungen der Datenquelle prüfen.

Technik: Python-Standardbibliothek, HTML, CSS, JavaScript. Keine Installation
weiterer Pakete erforderlich.

Quellen:
https://www.ea.com/games/ea-sports-fc/clubs
https://github.com/1erkandogan/fc27-clubs-api
