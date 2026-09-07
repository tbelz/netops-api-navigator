# Central MCP Workshop – Vorbereitung

Dieses Verzeichnis ist für den lokalen, Docker-freien Betrieb des Central MCP
Servers vorgesehen. Central-Zugangsdaten bleiben im lokalen VS-Code-Prozess.
Das Workshop-Profil stellt ausschließlich API-Suche und Central-GET-Aufrufe
bereit; Skriptausführung, GreenLake, Graph-Schreibzugriffe und mutierende API-
Methoden sind nicht verfügbar.

## Unterstützte Plattformen

- Windows 10/11 x64 ohne WSL und Docker
- macOS auf Apple Silicon
- VS Code mit GitHub Copilot und freigeschalteter MCP-Nutzung

## Einmalige Vorbereitung

1. VS Code und GitHub Copilot installieren beziehungsweise beantragen.
2. `uv` installieren:
   - Windows PowerShell: `winget install --id=astral-sh.uv -e`
   - macOS mit Homebrew: `brew install uv`
3. Dieses Release-Bundle entpacken und in VS Code als Ordner öffnen.
4. Installation und Knowledge DB vorab prüfen:

   ```text
   uv tool install --python 3.12 --no-build --constraints constraints.txt hpe-networking-central-mcp==0.3.0
   uv tool run --python 3.12 --no-build --constraints constraints.txt --from hpe-networking-central-mcp==0.3.0 hpe-networking-central-mcp doctor --profile workshop --knowledge-release-tag knowledge-db-20260907-043531 --knowledge-sha256 c9730faf52ecb99b6409d8777814982d684ffed7c673c4a749842cb520c8a56e --skip-credentials
   ```

   `uv` lädt Python 3.12 bei Bedarf in den Benutzerkontext. Administratorrechte,
   WSL und Docker sind nicht erforderlich.

## Verbindung in VS Code

VS Code erkennt `.vscode/mcp.json`. Beim ersten Start fragt es nach Central URL,
Client ID und Client Secret. Das Secret ist als Password Input markiert und steht
nicht in einer Projektdatei. Zugangsdaten niemals in Quellcode, Dashboard-Dateien
oder Chat-Ausgaben kopieren.

Die Konfiguration muss im lokalen VS-Code-Extension-Host gestartet werden.
Copilots separater Agent Host übernimmt laut VS-Code-Dokumentation keine Server,
die interaktive `${input:...}`-Variablen benötigen.

Nach erfolgreichem Start den MCP-Tool `get_server_status` aufrufen. Erwartet sind:

- `profile`: `workshop`
- `read_only`: `true`
- `central_connected`: `true`
- `status`: `ready`

## Kurzer Windows-Pilottest

Vor dem Workshop auf einem verwalteten Windows-x64-Rechner prüfen:

1. `uv --version`
2. ob der oben gezeigte `uv tool install` ohne Compiler durchläuft
3. ob `doctor` den Status `ready` meldet
4. ob VS Code im verbundenen Betrieb genau die sieben Workshop-Tools anzeigt
5. ob eine reale, kleine Central-GET-Abfrage funktioniert
6. ob ein Prompt für eine Änderung keinen schreibenden MCP-Tool angeboten bekommt

Bei Fehlern die `doctor`-Ausgabe weitergeben, niemals Client Secrets oder Tokens.
