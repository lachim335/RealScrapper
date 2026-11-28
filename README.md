# RealScrapper

Jednoduchý osobní sběrač nabídek prodeje bytů 2+1, 3+1, 3+kk a atypických
dispozic ze Sreality. Ukládá
údaje do SQLite, zaznamenává změny nabídkové ceny a nabízí přehled na domácí
síti. Nepotřebuje žádné externí Python balíčky; stačí Python 3.10+.

## Spuštění

Interaktivní nabídku všech skriptů spusťte:

```sh
python3 run.py
```

Menu umožní nastavit dostupné volby scraperu, webového serveru i pročištění
databáze; nabízí také zobrazení nápovědy jednotlivých příkazů. U čištění se
změny provedou pouze po výslovném potvrzení.

Sběr lze spustit přímo bez menu:

```sh
python3 scraper.py
```

Sběr můžete opakovat ručně nebo například plánovačem úloh. Proces lze nechat
opakovat také příkazem `python3 scraper.py --interval 3600` (nejméně 60 sekund).
Při každém úspěšném běhu se aktualizují záznamy, ceny a stav inzerátů.

Výchozí vyhledávání používá lokalitu Frýdek-Místek. Pro jiné okolí otevřete
Sreality, nastavte oblast a případný rádius podle potřeby a URL hledání vložte
do `search_url` v [config.json](./config.json). Skript zachovává filtrování
lokality z této URL a prochází všechny její stránky.

## Webový přehled

Po prvním sběru spusťte:

```sh
python3 server.py
```

Na počítači otevřete `http://localhost:8000/`; z domácí sítě použijte IP adresu
počítače, na kterém server běží, například `http://192.168.1.20:8000/`.
Server načítá SQLite databázi při každém požadavku, takže po dalším sběru
zobrazuje aktuální data. Stránka se také sama obnoví každých 30 sekund.
U každého inzerátu odkaz „Historie cen“ rozbalí jeho graf. Scraper ukládá
první dva náhledy fotografií lokálně do `data/images`; server je poskytuje z
této složky.
Web používá tmavé barevné schéma. Oblíbené inzeráty označíte hvězdičkou;
seznam se ukládá v místním úložišti prohlížeče a lze jej omezit filtrem
„Pouze oblíbené“.

Pro Raspberry Pi 3 je vhodný Raspberry Pi OS s Pythonem 3.10 nebo novějším.
Server je záměrně dostupný na všech síťových rozhraních; nevystavujte jej do
veřejného internetu.

## Automatický běh na GitHubu

Workflow [.github/workflows/scrape.yml](./.github/workflows/scrape.yml) spouští
sběr každé pondělí a pátek ve 12:47 pražského času (letní i zimní čas;
GitHub plánované běhy často spouští se zpožděním) a
výsledek publikuje jako statický web na GitHub Pages. Databáze a obrázky se
uchovávají ve větvi `data`, takže historie cen mezi běhy zůstává. Sběr lze
spustit i ručně v záložce Actions („Run workflow“); po pushi do `main` se web
jen znovu nasadí bez sběru.

Statický web lze vytvořit i lokálně: `python3 server.py --export _site`.
Oblíbené inzeráty se ukládají v prohlížeči, tedy zvlášť pro lokální server a
pro GitHub Pages.

## Cena a ukládané údaje

V tabulce lze volit dispozici a volitelně filtrovat minimální a maximální cenu.
Filtr dispozic se automaticky naplní podle hodnot dostupných v databázi.
Scraper ukládá nabídky v podporovaných dispozicích bez
cenového omezení, takže lze sledovat i změnu ceny přes nastavenou hranici.
Historie a jednoduchý graf se aktualizují při každém sběru, pokud se cena změnila.

Databáze `data/reality.sqlite3` obsahuje aktuální údaje, zdrojová ID, historii
cen a otisky textu, lokality a fotografií. Pokud se stejný byt objeví pod novým
ID, scraper jej spojí s předchozím záznamem pouze při dostatečné shodě lokality,
dispozice, plochy a fotografií nebo popisu. Nejednoznačné shody ponechá jako
samostatné záznamy, aby omylem nesloučil různé byty. Inzeráty, které po
úspěšném kompletním sběru zmizí z výsledků, označí jako neaktivní; data ani
historie se nemažou.

Sběr používá veřejně dostupné stránky, bez přihlašování; mezi načítáním detailů
vkládá krátkou prodlevu. Respektujte podmínky Sreality a provozujte jej
ohleduplně.

## Pročištění databáze

Náhled bezpečných úklidových operací zobrazíte příkazem:

```sh
python3 cleanup.py
```

Ve výchozím stavu se nic nemění. Pro odstranění přesně duplicitních záznamů
ceny se stejným inzerátem, časem a částkou, osiřelých řádků a nepoužívaných
lokálních náhledů spusťte `python3 cleanup.py --apply`. Před změnami vytvoří
zálohu databáze vedle původního souboru. Inzeráty ani nejednoznačné cenové
záznamy automaticky nemaže ani neslučuje.
