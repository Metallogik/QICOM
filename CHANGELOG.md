# Changelog

All notable changes to QICOM are documented in this file.

QICOM follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html):
`MAJOR.MINOR.PATCH`.

## How to read this file

Every release carries the usual `Added` / `Changed` / `Fixed` / `Removed`
sections, plus one that matters more than the others in a scientific tool:

> **⚠️ Changes affecting numerical results**

Anything listed there means that **the same inputs can produce different
numbers before and after that version**: a corrected or altered metric
formula, a change in preprocessing, in the radiomics discretisation, in how a
mask or a background is derived, in resampling or interpolation, or in the
default value of any parameter that enters a computation. Such a change is
never folded into a generic "bug fixes" line, and it always states what
changed, why, and which metrics or features it touches.

If you have results produced by an earlier version and this section is not
empty between that version and yours, re-run the analysis before comparing.

## [Unreleased]

### Added

- **Strumenti di utilità**, quarta sezione del launcher accanto ai tre flussi e sottomenu del menu
  Strumenti di ogni finestra di lavoro: un catalogo di strumenti autonomi in una finestra dedicata
  (catalogo a sinistra, strumento a destra), che torna al launcher alla chiusura. Gli strumenti
  leggono le sorgenti e non le modificano mai; ogni insieme di file in uscita compare completo o
  non compare (cartella di staging rinominata alla fine), in una cartella nuova o vuota. Il NIfTI
  scritto da uno strumento è posizionato dalla geometria della sorgente
  (`qicom/io/geometry.py`: per il DICOM l'ordine colonna, riga, slice di dcm2niix e SimpleITK,
  verificato contro SimpleITK anche con orientamento obliquo) e non copia alcun campo dell'header.
  Ogni elaborazione e ogni salvataggio apre una finestra con la percentuale di avanzamento; nei
  canvas la rotella scorre le slice e Ctrl+rotella ingrandisce, come nei flussi, e un clic
  sull'area vuota (o un file trascinato) carica l'immagine; in ogni strumento un solo tipo di
  pulsante è colorato alla volta, il passo da fare, come nella rimozione dell'aria.
  - **Segmentazione semi-automatica**: si costruisce una mappa di etichette un passo alla volta,
    in sei pagine a schede (Immagine, Etichetta, Campione, Selezione, Applica, Salva) con
    Indietro/Avanti e il ciclo «Segmenta un'altra regione». Si sceglie un'etichetta (preset aria, polmone, grasso, tessuti molli, mezzo di contrasto,
    tessuto osseo, metallo, lettino, o con un nome libero), si disegna una ROI (rettangolo,
    ellisse, mano libera, scelti nella colonna di destra), se ne campiona l'intervallo di intensità (percentili o media ± k·DS,
    regolabile sull'istogramma; per la CT anche un intervallo HU indicativo del preset, non una
    soglia clinica), si sceglie dove può arrivare la selezione (questa slice o il volume, ovunque
    o solo collegato alla ROI), con rifiniture facoltative (mediana, buchi, regioni piccole,
    apertura e chiusura, sempre nel piano della slice), si guarda l'anteprima e si decide:
    «Togli dall'immagine», «Mantieni solo questo», «Assegna» o «Cancella dall'etichetta». Un
    pixel appartiene a un'etichetta al massimo; un passo prende solo i pixel non assegnati,
    salvo sovrascrittura esplicita. Annulla/Ripeti senza limite di passi entro 256 MiB di
    cronologia. La tabella delle etichette mostra visibilità, colore e volume (mL con geometria
    nota); la vista mostra i colori sopra l'immagine o solo le parti spuntate. Salvataggio e
    riapertura come mappa NIfTI con tabella JSON delle etichette (nuovo formato versione 1,
    `OPERATIONS_SCHEMAS.md`), importabile dalla radiomica; export dell'immagine con solo le parti
    spuntate (NIfTI, o PNG per le immagini 2D).
  - **Operazioni e confronto tra maschere**: unione, intersezione, differenza, XOR di due mappe
    di etichette (anche per singolo valore), riempimento delle slice vuote tra quelle disegnate
    (interpolazione di mappe di distanza), confronto con Dice, Jaccard, volumi, Hausdorff, HD95
    e distanza media delle superfici in mm; export CSV.
  - **Converti immagini**: DICOM, NIfTI o pile di bitmap in NIfTI (tipo intero più piccolo senza
    perdita, altrimenti float32), slice PNG a 8 o 16 bit nella finestra a schermo o slice TIFF
    float con i valori reali. Il risultato va in una cartella nuova creata dentro quella scelta e
    chiamata come l'immagine e il formato (`<nome>_nifti`, `<nome>_png16`, numerata se esiste
    già), mai sparso tra i file già presenti.
  - **Ricampiona**: a una spaziatura scelta (anche isotropa) o sulla griglia di un'immagine di
    riferimento per coordinate paziente, senza registrazione; interpolazione lineare, al vicino
    più prossimo o cubica. Il ricampionamento sulla griglia coincide con quello di SimpleITK.
    Lineare e vicino più prossimo si calcolano per blocchi di slice, che danno l'avanzamento.
  - **Aritmetica tra immagini**: differenza, differenza assoluta, somma, media, rapporto (i
    denominatori nulli sono contati e portati al valore scelto), con avviso se le spaziature
    differiscono.
  - **Degradazione controllata**: rumore gaussiano o di Poisson, sfocatura, perdita di
    risoluzione, compressione JPEG, da un unico seed, per verificare come reagisce una metrica. Un
    effetto alla volta: tasti degli effetti in alto, intensità, Applica, elenco degli effetti
    applicati (ognuno rimovibile) e salvataggio; ogni modifica ricalcola la catena dall'originale.
    La provenance (nuovo formato versione 1) basta a riprodurre il risultato bit per bit.
  - **Ispeziona e modifica tag DICOM**: tutti i tag di un file o di un file di una serie, con
    meta-informazioni, sequenze annidate e tag privati, ricerca, filtro dei tag privati, copia ed
    export CSV di ciò che è mostrato, non anonimizzato (vedi `SECURITY.md`). Header malformati:
    un elemento non decodificabile diventa una riga marcata, sequenze troppo profonde o troppo
    grandi si troncano dichiarandolo. Dal menu Strumenti del quantitativo e della radiomica,
    «Ispeziona i tag DICOM dell'immagine corrente»; non dal qualitativo, per non dare l'header di
    un'immagine valutata in cieco. Nello stesso albero, la colonna «Nuovo valore» modifica,
    aggiunge o elimina elementi di primo livello di un file o di tutta la serie e scrive copie in
    una cartella nuova; i tag che definiscono i pixel e il
    gruppo 0002 sono bloccati, quelli che cambiano le misure (spaziatura, rescale, geometria
    delle slice) chiedono conferma; UID nuovi coerenti per istanza, serie o studio
    (SOPInstanceUID segue sempre questa scelta e la file meta resta allineata); i testi a valore
    singolo (LT, ST, UT, UR) conservano le barre rovesciate; registro delle modifiche con hash,
    mai i valori precedenti (nuovo formato versione 1).
  - **Confronta header DICOM** (sola lettura): due header elemento per elemento con esito
    diverso / solo da un lato / identico; si apre con entrambi gli header interi, e «Solo
    differenze» e «Nascondi UID» restringono la vista.
  - **Controlla e organizza cartelle DICOM**: raggruppa per studio e serie e segnala istanze
    mancanti o ripetute, dimensioni miste, spaziatura o orientamento irregolari, slice senza
    posizione; report CSV e copia ordinata con nomi che non identificano il paziente.
  - **Decomprimi DICOM**: un file DICOM compresso (JPEG lossless, JPEG-LS, JPEG 2000, RLE,
    JPEG baseline) riscritto in Explicit VR Little Endian, un file per frame, in una cartella
    nuova con il nome del file dentro quella scelta (il dialogo parte dalla cartella della
    sorgente), tutta insieme o per niente (`qicom/io/dicom_decompress.py`). I pixel sono
    decodificati dagli stessi decoder e nello stesso ordine del loader, quindi le copie
    contengono esattamente ciò che QICOM mostra. Un file a frame singolo mantiene tutti gli UID (cambia solo la
    codifica); un multi-frame diventa un'istanza per frame, con SOP Instance UID nuovi, un unico
    Series Instance UID nuovo e il numero del frame come Instance Number, mentre studio e frame
    of reference restano. Un oggetto Enhanced resta Enhanced: ogni copia porta il proprio item
    per frame e i functional group condivisi; i vettori indicati da Frame Increment Pointer sono
    ridotti al frame. Una sorgente JPEG baseline o extended senza Lossy Image Compression riceve
    «01», perché la decompressione non annulla la perdita.
  - **Anonimizza immagini**: DICOM, NIfTI e bitmap con le regole fail-closed degli export. I file
    scelti (o una cartella con le sottocartelle) si vedono prima come albero, con l'esito previsto
    per ciascuno; la copia va in `<cartella>_anonymized` accanto all'originale (o in un'altra
    posizione scelta), con le stesse sottocartelle e gli stessi nomi di file. I nomi non sono
    de-identificati e lo strumento lo dice; in alternativa un'opzione li sostituisce con nomi
    generici (`folder_001`, `image_0001.dcm`, ...) in una cartella `anonymized`, senza tabella di
    corrispondenza, e l'albero mostra il nome che ogni elemento avrà nella copia. I file che non
    sono immagini restano fuori. I file
    non verificabili sono elencati prima di scrivere e scritti solo su scelta esplicita, come
    non verificati.
- Le primitive di segmentazione (Otsu, riempimento dei buchi, componente maggiore, dilatazione,
  erosione, bounding box) sono passate da `qicom/radiomics/segmentation.py` al nuovo package
  senza Qt né SimpleITK `qicom/segmentation/`, condiviso da radiomica e strumenti; la radiomica le
  importa da lì con gli stessi nomi e lo stesso comportamento. Nuovi package senza Qt anche
  `qicom/processing/`; entrambi verificati dalla mappa dell'architettura.
- Codice di accesso per le valutazioni da inviare. Nel passo **Pubblica** l'autore può inserire o
  generare un codice (almeno 10 caratteri) e salvare una **copia protetta per i valutatori**; con
  il codice inserito anche la **consegna in cieco** e la sua mappa di decodifica sono protette. La
  copia di lavoro dell'autore resta senza codice; un pacchetto protetto aperto in modifica si
  salva di nuovo protetto. Chi esegue, modifica o aggrega una valutazione protetta deve inserire il
  codice, chiesto subito dopo la scelta del file; un valutatore con utente non lo reinserisce
  quando riprende, perché la chiave è conservata nella sua bozza cifrata. Le risposte di un
  pacchetto protetto si salvano cifrate con lo stesso codice (al posto del report Excel), e
  l'aggregazione le apre solo con il codice, chiesto una volta. L'aggregazione da riga di comando
  legge il codice da un file (`access_code_file`). Pacchetto, risposte e mappa sono cifrati con
  AES-256-GCM e chiave derivata con scrypt; il pacchetto a blocchi da 1 MiB, così che pacchetti
  grandi non stiano in memoria e uno troncato venga rifiutato. Nuovo formato "file protetti da
  codice" versione 1 (`OPERATIONS_SCHEMAS.md`); limiti in `SECURITY.md`
  (`qicom/session/protected_files.py`, `qicom/session/key_derivation.py`,
  `qicom/gui/widgets/access_code_dialog.py`).
- Utenti valutatore e valutazioni sospese, cifrate. **Esegui valutazione** chiede ora di accedere
  con ID e password (minimo 5 caratteri, nessun requisito di complessità) o di creare un utente;
  in alternativa si può continuare senza accedere, ma allora nulla viene conservato e la
  valutazione non si può sospendere (chiudere prima di salvare il report chiede conferma, perché
  le risposte andrebbero perse). Dopo l'accesso **Le mie valutazioni** elenca solo le valutazioni
  di quell'utente, divise in **In sospeso** (Riprendi, sulla pagina lasciata) e **Concluse**
  (Riapri, per salvare di nuovo il report), con Rimuovi dopo conferma — elimina le risposte, mai il
  pacchetto .zip né i report — e Apri un'altra valutazione. Durante la valutazione **Sospendi e
  riprendi più tardi** chiude e torna alla schermata iniziale; la barra di stato mostra l'utente.
  Cambio password ed eliminazione dell'utente (con tutte le sue valutazioni) dal menu Utente e
  dalla finestra di accesso. Le risposte sono cifrate (AES-256-GCM) a ogni salvataggio automatico
  con la chiave dell'utente, protetta dalla password tramite scrypt; la password non è salvata e
  non si può recuperare. Le bozze in chiaro delle versioni precedenti si recuperano riaprendo il
  pacchetto e confermando che sono proprie; vengono allora cifrate e la copia in chiaro eliminata.
  Formati nuovi: account, involucro cifrato e bozze, tutti versione 1 (vedi
  `OPERATIONS_SCHEMAS.md`); limiti in `SECURITY.md`. Nuova dipendenza `cryptography` (Apache-2.0 o
  BSD-3-Clause, con `cffi` e `pycparser`), nei lock di tutte le piattaforme
  (`qicom/session/evaluator_accounts.py`, `encrypted_files.py`, `evaluator_drafts.py`;
  `qicom/gui/widgets/evaluator_login_dialog.py`, `my_evaluations_dialog.py`).
- Interfaccia in spagnolo (`es`), portoghese brasiliano (`pt`), cinese semplificato (`zh-Hans`)
  e giapponese (`ja`), selezionabili come le altre lingue dal launcher, dal menu Lingua e dalle
  impostazioni. Coprono tutte le chiavi, comprese le schede delle metriche, con placeholder
  verificati contro l'inglese. Sono una prima traduzione non ancora rivista da madrelingua: i
  testi su uso previsto, desidentificazione e limiti delle metriche vanno rivisti prima di una
  release. Per cinese e giapponese il tema aggiunge i font CJK della lingua scelta, così i kanji
  giapponesi non vengono disegnati con le forme cinesi (e viceversa).
- Golden esteso (`tests/performance/golden_outputs_extended.json`): fissa i valori di tutte le
  metriche oltre le 8 del golden originale, che resta invariato. Le metriche classiche 2D e 3D e le
  ROI sono confrontate con la tolleranza originale; le sezioni PIQ, learned e NR canoniche, in
  float32, a rel 1e-5, e si saltano dichiarando il motivo quando mancano backend o pesi.
- BRISQUE e NIQE canonici (`brisque_canonical`, `niqe_canonical`): il punteggio BRISQUE di PIQ
  0.8.0 con il suo SVM pre-addestrato e la distanza NIQE dal modello MVG LIVE originale, accanto
  alle distanze NSS esistenti `brisque`/`niqe`, che restano invariate. I modelli si scaricano solo
  su richiesta esplicita e sono verificati per SHA-256. Il profilo `complete` è passato alla 1.1
  per includerli (vedi la sezione sui risultati numerici).
- Profili di risorsa (`qicom.runtime`: ridotto, standard, workstation) scelti da RAM e CPU
  disponibili: regolano cache, modelli tenuti in memoria e coppie in parallelo, mai i valori.
- Controllo della memoria prima di allocare un'immagine (`qicom.io.memory_preflight`): un volume
  che non ci starebbe chiede conferma prima di essere decodificato, invece di esaurire la RAM.
- Export con DICOM non anonimizzabili in modo verificato: quando i controlli fail-closed bloccano
  il salvataggio di una valutazione qualitativa o di un progetto quantitativo (per esempio un DICOM
  senza `BurnedInAnnotation=NO`, un attributo che `dicom-anonymizer` non riesce a trattare, un
  DICOMDIR), QICOM non si ferma più senza alternative: mostra il motivo e chiede se esportare
  comunque. Il default resta il blocco (Annulla è il pulsante predefinito). Se l'utente accetta, i
  file rifiutati sono anonimizzati al meglio — profilo PS3.15 se applicabile, poi rimozione dei tag
  privati e svuotamento di ogni attributo identificativo residuo — e marcati
  `PatientIdentityRemoved=NO` con `DeidentificationMethod` che nomina l'override; un file che non si
  riesce nemmeno a leggere o riscrivere viene incluso invariato. Ogni file esportato così è elencato
  nel registro errori. Lato codice: parametro `unverified` di `anonymize_dicom_file` e
  `unverified_dicom` nei salvataggi (`qicom/io/dicom_anonymize.py`, `qicom/gui/error_reporting.py`).
- Report errori da inviare allo sviluppatore: dalla finestra Errori (indicatore nella barra di
  stato), dal pannello Errori e da Aiuto ▸ Segnala un bug si selezionano uno o più errori (Ctrl/Maiusc
  + clic; nessuna selezione = tutti) e si salvano in un file di testo con versione, revisione,
  piattaforma e le ultime voci del log applicativo con i traceback. Testo semplice, da controllare
  prima dell'invio; percorso home oscurato; nessuna immagine (`qicom/session/error_report.py`).
- La finestra qualitativa ha ora un registro errori persistente con indicatore nella barra di stato,
  come quella quantitativa: prima gli errori comparivano solo per pochi secondi nella barra di stato.
- Le eccezioni non gestite nei gestori Qt sono registrate nel log con traceback e compaiono nel
  registro errori delle finestre aperte, invece di finire solo su una console che il bundle non ha.

- Metrica **RIQE** (`riqe`, Radiology Image Quality Evaluator) per TC: distanza in stile NIQE
  dal modello `riqe-v1.0` (doi:10.5281/zenodo.23055559), stimato su 3.792 slice a dose piena di
  158 pazienti TCIA invece che su fotografie. Il modello (CC BY 4.0) è incluso nel pacchetto e
  verificato con SHA-256 prima di ogni lettura: nessun download, nessuna rete. Si calcola solo su
  immagini dichiarate in unità Hounsfield (GUI: DICOM con Modality CT o NIfTI; CLI
  `--hounsfield-units`; batch `"hounsfield_units": true`); altrimenti l'esito è
  `invalid/degenerate-input`; una slice con meno dell'1% di aria (≤ −900 HU) nel campo di vista è
  rifiutata come non in HU, così RM, bitmap o un NIfTI non TC non ricevono un punteggio. Come
  nella validazione pubblicata, l'immagine di confronto è
  valutata sulle maschere di campo di vista e corpo del riferimento. Il calcolo riproduce il
  codice di riferimento rilasciato: bit-identico sulle fixture sintetiche e sulle 480 coppie a
  dose ridotta del test split TCIA (`validation/no_reference/riqe_verification.json`). Adatto al
  confronto appaiato di un'immagine degradata con la sua sorgente; non va usato da solo per
  scegliere o regolare un denoiser (`qicom/metrics/riqe.py`, `NO_REFERENCE_METHODOLOGY.md`).

- Valutazione qualitativa: nuovo primo passo "Impostazioni" nel percorso di creazione. Anonimato
  delle risposte, randomizzazione di pagine/domande/pannelli, controbilanciamento, etichette neutre,
  seed, ripetizioni nascoste e raccolta dei tempi si decidono ora in una sezione dedicata, prima di
  costruire qualsiasi pagina, invece di essere sparsi tra due caselle sopra l'elenco delle domande e
  una finestra modale dietro un pulsante. Le impostazioni sono le stesse e i pacchetti già salvati si
  riaprono senza modifiche: cambia solo dove si impostano
  (`qicom/gui/widgets/evaluation_setup_panel.py`).

- Export "Markdown per assistente AI": un unico file autoconsistente con i valori delle metriche
  accanto alla definizione presa dalle schede `descriptors/*.yaml`, da incollare in una chat con un
  assistente AI. Disponibile dalla GUI (spunta nel pannello di export, con avviso mostrato una sola
  volta) e dalla CLI con `--output report.md`. QICOM non esegue alcuna elaborazione AI, non contatta
  servizi e non carica nulla: scrive un file, l'eventuale invio è una scelta dell'utente. Il
  contenuto è deciso da una whitelist esplicita in `qicom/reporting/llm_export.py` — nomi e percorsi
  dei file, header DICOM/NIfTI, identificativi di paziente/studio/serie, date, istituzione,
  operatore, hostname e utente non hanno accesso al file, e i nomi delle ROI sono contati anziché
  riprodotti. L'output è deterministico (nessun timestamp) e segue la lingua dell'interfaccia. Le
  tabelle radiomiche restano fuori da questa v1: richiedono una strategia di sintesi propria.

- Formato qualitativo v4 e risposte v3: piano deterministico per consegna con randomizzazione di
  casi/domande/pannelli, controbilanciamento, seed/ordine persistiti, ripetizioni nascoste e
  consegna in cieco con nomi generici più mappa autore separata. I formati precedenti restano
  leggibili.
- Scale qualitative ancorate, istruzioni generali e per pagina, pagine di calibrazione escluse,
  commenti condizionalmente obbligatori, `not_assessable` distinto da skip/missing, 2-AFC
  forzata, preferenza appaiata e timing con disclosure configurabile.
- Statistica qualitativa ordinale: mediana/IQR e distribuzioni, Kendall W, alpha di Krippendorff
  primaria, Gwet AC2 avanzata, affidabilità intra-rater, confronto appaiato valutatore×caso,
  bootstrap a due cluster e Holm. QC e precisione non producono esclusioni automatiche.
- Fondazioni scientifiche condivise: `StatisticalUnit`, registro esplicito degli esiti delle
  metriche e ricevuta di riproducibilità versionata con hash di input, maschere, configurazione
  e risultati. Persistenza SQLite ed export mantengono la lettura dei progetti precedenti.
- Profili quantitativi versionati (Core rapido come default GUI, Strutturale, Percettivo
  sperimentale, Rumore/ROI QC, Completo, Fantoccio/task-based e Personalizzato) e Indice QICOM
  con profilo/composizione dichiarati, stato sperimentale e controllo di confrontabilità.
- Diagnostica quantitativa: bias `comparison - reference`, SD residua, mediane e percentili,
  Wasserstein-1, calibrazione lineare/ortogonale dichiarata, phase cross-correlation 2D/3D senza
  registrazione automatica, statistiche robuste degli stack e navigazione alla slice peggiore.
- Mappa della differenza con segno con LUT divergente centrata su zero, range simmetrico,
  tissue/ROI mask ed export PNG più configurazione JSON della scala.
- NPS tracciabile da ROI dedicate `noise_texture_roi`, profili di accettazione YAML/JSON senza
  soglie predefinite, modello NSS locale BRISQUE/NIQE da corpus QA e strumenti esperti rise
  distance, MTF/TTF/MTF50 e detectability d′ NPW(E).
- Verifica IBSI-1 condivisa fra GUI e CI: manifest versionato, download ufficiale con consenso,
  checksum fissati, confronto di 104 feature del digital phantom, PASS/FAIL/INCOMPLETE ed export
  JSON/CSV/Excel/HTML senza dichiarazioni di certificazione.
- Progetto radiomico v2 con identità subject/case/lesion/timepoint/acquisition, registro esiti,
  ricevuta comune, QC di maschere e feature, import/export NIfTI multi-label e compatibilità in
  lettura con i progetti v1.
- Robustness check fisico e sweep del preprocessing; aggregatore multi-progetto table-only con
  unità indipendenti dichiarate, ICC/CCC di coorte, correlazioni/effective N, associazioni col
  volume e cluster di ridondanza senza selezione predittiva.
- Trending SQLite con baseline versionate, Shewhart/EWMA e limiti utente separati; tre comandi
  batch/headless con log, cancellazione/exit code, tidy CSV/JSON, ricevute e isolamento errori.
- Report HTML locale autoconsistente con CSP, escaping e limite dimensionale, più bundle demo
  deterministico privo di PHI accessibile dal launcher.
- Documentazione metodologica radiomica e degli schemi operativi in
  `RADIOMICS_METHODOLOGY.md` e `OPERATIONS_SCHEMAS.md`.
- `qicom/gui/themes/gray.qss`: a third theme, gray, sitting between light and
  dark - same "Clinico" rules and teal accent as the dark theme with every
  neutral lifted into the mid grays, for when the light theme glares and the
  dark one crushes. Selectable from View ▸ Gray Theme in all three
  workbenches and from Settings ▸ Theme, and persisted like the other two;
  `Follow system` still resolves only to light or dark, since that is all the
  OS reports.
- `qicom/gui/widgets/page_overview_panel.py`: the qualitative form builder's
  `All pages` step - one row per page with its image and question counts, and
  the actions that open, duplicate, rename or delete a page already built.
- `qicom/gui/widgets/wizard_step_bar.py`: the Back/Next footer shared by the
  qualitative building steps, carrying the caption of the page being worked on.
- Qualitative evaluations can shuffle their questions: an author who ticks
  `Shuffle the question order for each evaluator` in the questions step hands
  out a package whose questions are drawn in a different order for every
  evaluator, once, and kept stable if they close it and resume. Presentation
  only - answers, reports and aggregation stay keyed to the question, and the
  package itself keeps the author's own order.
- Qualitative image boxes are named by the author: an unnamed box asks to be
  named instead of showing a default title, and the name is saved in the
  package. It is the only label anyone answering the evaluation sees - their
  copy no longer shows the file name, patient or series behind an image, so a
  comparison cannot be given away by a file called `denoised.dcm`. The
  author's own builder still shows all of it; the preview step shows what the
  evaluator will see.
- `qicom/io/nifti_anonymize.py`: NIfTI de-identification for exported
  qualitative packages - the two free-text header fields (`descrip`,
  `aux_file`), which converters routinely fill with the source series
  description or the original path, are cleared on the copy going into the
  package. A byte-level header patch: no voxel is read or rewritten.
- The qualitative builder now says what it does to the images: a standing
  notice on the images step, and a one-off dialog the first time a DICOM or
  NIfTI image is loaded, stating that the copies going into the package are
  de-identified, that the author's own files are never modified, and that
  file names are copied unchanged.
- `LICENSE` (Apache-2.0), `NOTICE`, `CITATION.cff` and this changelog.
- `qicom/about.py`: one Qt-free module holding the application's identity,
  licence, use scope, DICOM attribution, privacy statement and citation, so
  that the About dialog, the exports and the documentation cannot drift
  apart.
- Help ▸ About QICOM in all three workbenches (quantitative, qualitative,
  radiomics): version and build, authorship, licence, intended use, DICOM
  trademark attribution and NEMA non-affiliation, privacy statement, and a
  copyable citation (plain text and BibTeX).
- Metadati di versione Windows (`CompanyName`, prodotto, descrizione, versione
  e copyright) nel bundle PyInstaller, con verifica automatica durante il
  packaging.
- Manifest `SHA256SUMS` dentro ogni bundle e checksum sidecar dell'archivio CI,
  utilizzabili per verifica d'integrità e allow-listing puntuale da parte IT.

#### Release readiness

- **Identità di build nella ricevuta.** `qicom/build_identity.py` risolve il
  commit esatto che ha prodotto un risultato: da `qicom/_build_identity.py`
  (generato in fase di build), poi da git in un checkout, poi `unknown` —
  dichiarato tale, mai indovinato, e mai "clean" se ignoto. La ricevuta di
  riproducibilità porta ora `build_revision` e `source_dirty` (schema 3; gli
  schemi 1 e 2 restano leggibili). Una versione nomina una release; fra due
  build che portano lo stesso numero possono esserci un numero qualsiasi di
  commit, e al momento dell'audit di 1.0.0 ce n'erano 57. Vedi `VERSIONING.md`.
- **Gate di coerenza della release** (`packaging/check_release_consistency.py`):
  rifiuta versioni divergenti fra `qicom/__version__.py`, `pyproject.toml` e
  `CITATION.cff`, una versione stabile senza la sua sezione di changelog, e una
  versione stabile che lascia voci sotto "Changes affecting numerical results"
  in `[Unreleased]`. Eseguito in CI e prima di ogni build di packaging.
- **Inventario licenze e SBOM generati** (`packaging/attribution.py`): per ogni
  profilo realmente distribuito, dall'ambiente da cui il bundle viene
  congelato, con fallimento quando manca versione, origine, licenza o testo di
  licenza. `check-bundle` confronta ciò che PyInstaller ha davvero raccolto con
  l'inventario. Inventario, SBOM CycloneDX e licenze viaggiano con il bundle,
  dentro il manifest dei checksum.
- **Gate IBSI obbligatorio sui tag**: un rilascio non può essere costruito da
  una revisione le cui feature radiomiche divergono dal riferimento ufficiale.
  Un problema di rete è classificato separatamente solo nelle pull request, mai
  su un tag.
- **Gate di coverage** (`packaging/check_coverage.py`): soglia globale di
  non-regressione misurata (non scelta) e obiettivo di copertura *branch* sui
  trust boundary selezionati. Vedi `coverage-floor.json`.
- **Ratchet di complessità** (`packaging/check_complexity.py`): nessuna nuova
  funzione oltre C901 20 di ruff, nessuna esistente che peggiora. Lo strumento
  è dichiarato: radon, mccabe e ruff danno numeri diversi sulla stessa funzione.
- **Manifest dell'ambiente installato per profilo e piattaforma**
  (`packaging/generate_constraints.py`): versioni esatte, tag locali e origini
  VCS realmente congelati, pubblicati accanto al bundle senza presentarli come
  un lock degli artefatti con hash.
- **`ARCHITECTURE.md`: la mappa del progetto, generata dal codice.** Ogni modulo
  con una riga su cosa fa (presa dalla prima frase del suo docstring: li hanno
  tutti e 167), i punti di ingresso, i trust boundary, le costanti di versione
  dei formati persistiti e i gate di build. Scritta a mano sarebbe il documento
  a più alta deriva del repository; generata da `packaging/generate_architecture_map.py`
  e verificata in CI, non può mentire. Le righe sono arrotondate al centinaio
  perché una mappa che cambia a ogni funzione allungata di tre righe è una mappa
  che nessuno rigenera.
- `packaging/DISTRIBUTION.md` raccoglie requisiti di distribuzione e firma. La specifica
  dell'export AI quantitativo è implementata in ogni suo punto; l'export AI per le tabelle
  radiomiche resta un lavoro futuro, invece di essere implementato a metà.
- Export Markdown per la lettura assistita da AI: una sezione **Citazione** chiede al modello che
  un articolo, un report o una presentazione scritti a partire dal file citino QICOM con lo stesso
  riferimento di Informazioni su QICOM e di `CITATION.cff`. Quando fra i risultati c'è RIQE, il
  file aggiunge lo studio (arXiv:2610.00384), la release del modello
  (doi:10.5281/zenodo.23055559) e le citazioni e il ringraziamento che la licenza CC BY 4.0 del
  modello richiede (`qicom.metrics.riqe_model.CITATIONS`). I riferimenti restano in inglese in
  ogni lingua; nessun dato in più entra nel file.

### Changed

- Mouse sulle immagini, in tutti i flussi e negli strumenti di utilità: il trascinamento con il
  tasto sinistro regola finestra e livello (orizzontale il contrasto, a destra la finestra si
  allarga; verticale la luminosità, in basso l'immagine si scurisce) invece di spostare
  l'immagine; lo spostamento resta sul tasto centrale e lo zoom sul destro, la rotella scorre le
  slice. Ogni trascinamento è una sola modifica annullabile e «Sincronizza livelli» lo segue dal
  vivo. Con uno strumento ROI selezionato, anche dopo aver aggiunto la ROI, trascinamento,
  spostamento e zoom restano attivi (prima erano tutti spenti finché non si tornava a Zoom /
  Pan): trascinare sopra una ROI la sposta, trascinare fuori regola i livelli. Restano inattivi
  in modalità Annotazione, dove il tasto sinistro posa un'annotazione, e mentre si cancella o si
  aggiunge sfondo.
- Editor delle domande: per i formati a scelta (singola, multipla, menu a discesa, classifica) le
  opzioni si scrivono in un elenco numerato, una riga per opzione, con il numero di opzioni scelto
  esplicitamente (da 2 a 20, predefinito 5) al posto della casella «una opzione per riga». Una riga
  lasciata vuota usa il testo suggerito: una scala di qualità (da Inaccettabile a Eccellente) per i
  formati a scelta, «Opzione n» per la classifica e oltre sette opzioni. Le righe che il formato
  scelto non usa (ancore e intervallo della scala, elenco delle opzioni) sono ora nascoste invece
  che disattivate. Il formato salvato non cambia.
- Sigle e nomi standard restano in inglese in tutte le lingue, come nel testo di riferimento: CT,
  MRI, CI, QC, AI, IQR, MAD, HU, MB e Z-score sostituiscono TC, RM, IRM, MRT, TDM, IC, KI, CQ,
  IA, ÉIQ, ÉAM, UH e Mo nelle traduzioni italiana, francese e tedesca. Le descrizioni italiana,
  francese e tedesca di Wasserstein-1 riportano ora anche le unità calibrate e l'assenza di
  p-value KS, come l'originale inglese.
- Revisione dei testi in tutte le lingue. Italiano, francese e tedesco usano le maiuscole della
  propria lingua invece del Title Case inglese ("Nuovo progetto", "Projekt laden"). I nomi delle
  metriche seguono la stessa regola ("Errore quadratico medio"). Sono stati corretti refusi,
  termini incoerenti e frasi incomplete: ad esempio *repeatability* è "ripetibilità",
  "répétabilité" e "Wiederholbarkeit", non "riproducibilità". Il testo inglese di riferimento
  usa ora solo l'ortografia americana ("color", "analyzed", "neighbor").
- **Ricevute allo schema 4: JSON canonico rigoroso.** Nella forma precedente il float NaN e la
  stringa "NaN" davano lo stesso hash, le chiavi `1` e `"1"` collassavano e un booleano NumPy
  diventava la stringa "True". Ora una stringa uguale a un token speciale viene marcata, le chiavi
  in collisione sono rifiutate e i booleani NumPy restano booleani. Per tutto il resto la codifica
  è identica byte per byte. Una ricevuta allo schema 3 resta schema 3 e conserva il proprio hash;
  identità dei modelli NSS e profili di accettazione mantengono la forma precedente.
- Immagini non anonimizzabili: il salvataggio di un progetto o di una valutazione non si ferma più
  alla prima immagine rifiutata. Raccoglie tutte le immagini che non si possono anonimizzare, per
  qualunque motivo, non scrive nulla e dice all'utente quali sono, perché, e che può comunque
  procedere senza anonimizzazione per quelle immagini. I writer sollevano
  `ImageDeidentificationRefused` (nuovo `qicom/io/deidentification.py`, con l'elenco completo in
  `refusals`) al posto di `DicomDeidentificationError`; `anonymize_dicom_file` accetta un
  collettore `refused`.
- La descrizione di NIQE (`niqe_canonical`) avverte che il modello è stimato su fotografie, che
  il suo rapporto con la qualità TC non è dimostrato e che per la TC va usato RIQE. Solo testo:
  formula, modello e valori sono invariati.

- Valutazione qualitativa: la finestra Nuova/Modifica Domanda è divisa in tre sezioni con titolo -
  "Domanda", "Formato della risposta" (tipo di risposta, opzioni, ancore della scala, intervallo del
  punteggio) e "Regole di risposta" - al posto di un unico elenco piatto di dieci righe. Quando si
  cambia formato, i campi che non si applicano si disattivano dentro la loro sezione anziché in punti
  sparsi dell'elenco.

- Il bundle sintetico deterministico non è più un bottone sulla prima pagina del launcher, dove
  competeva con la scelta del flusso di lavoro e nessuno lo consumava. Ora sta in
  **Impostazioni → Diagnostica**, è offerto anche da **Aiuto → Segnala un Bug** (il momento in cui
  serve davvero) ed è disponibile senza GUI con `qicom-cli demo <cartella>`. Il corpo della mail di
  segnalazione e il nuovo template di issue GitHub chiedono esplicitamente di allegarlo. La
  generazione resta atomica e non sovrascrive mai una cartella esistente; dalla GUI il nome libero
  successivo (`-2`, `-3`, …) viene scelto automaticamente, così il comando funziona anche la seconda
  volta, e al termine si può aprire direttamente la cartella. Le chiavi di traduzione
  `launcher.demo_*` diventano `demo.*`.
- Defined and audited the public API deprecation policy: the three deprecated
  compatibility adapters have no QICOM runtime callers, remain covered by
  compatibility tests, and are retained until the next major release rather
  than being removed from the 1.x line.
- Qualitative form builder: the authoring steps are now a guided pass over one
  page at a time (`Images → Questions`) followed by a new `All pages` overview,
  and each building step ends in a Back/Next bar rather than relying on the
  workflow rail being clicked. The overview lists every page with how many
  images and questions it holds and is the only place a page is opened,
  duplicated, renamed or deleted; adding one returns to the same `Images` step
  instead of stacking another section on screen. The page strip that used to
  sit above the question cards is gone, and `Add` in the question list now
  reads `Add question`.
- Background (air) exclusion: the step declares itself optional in a notice
  pinned to the top of the column and is switched on with a full-width toggle
  rather than a small checkbox, so skipping it is a deliberate choice instead
  of an unnoticed one. The column's controls stack vertically and fill its
  height, and the air sample is now explicitly a single ROI - the Add button
  turns itself off once the circle exists, since a second sample would quietly
  widen what counts as air.
- Every piece of text the application displays now uses a plain hyphen `-`
  where it used an em dash or an en dash: window and section titles, the
  metric rows, the history entries, chart titles, slice ranges and the
  translated strings in all four languages. The `-`/`+` stepper buttons keep
  their U+2212 minus sign, which is sized to match the `+` beside it.
- Analysis setup: the metric checklist is laid out in two columns, and
  `Calculate` spans the full bottom edge of the page in a colour used nowhere
  else (indigo), so it is no longer one more coloured rectangle among the
  green/amber/coral effort badges above it.
- Background (air) exclusion guides with one coloured button at a time: the
  step that is actually due wears the teal accent and every other button in
  the column - including the ones already used - drops to a muted grey, so
  once the exclusion has been detected nothing in the column is coloured any
  more. Previously the toggle, `Add air ROI` and `Detect air` were all teal
  simultaneously, which said nothing about which one to press.
- Calculate reports its progress inside the Analysis setup page instead of in
  a modal dialog floating over it: the bottom edge of the page swaps the
  `Calculate` button for a progress bar, the metric being computed and a
  `Cancel` button. The run still owns the application while it lasts - the
  other workflow steps, the menus, the toolbars, the image canvas and the
  other panels are disabled until it finishes or is cancelled, which is what
  the dialog's modality used to provide. Export keeps its own modal progress
  dialog.
- Version raised from `0.1.0` to `1.0.0`: the first release published under
  semantic versioning, with a documented licence and a citation form.
- Project metadata in `pyproject.toml`: licence declared as `Apache-2.0`
  (previously `Proprietary`, which contradicted the intent to distribute
  QICOM freely), full authorship, project URLs and classifiers.

#### Release readiness

- **Versione di sviluppo su `main`.** `1.0.0` → `1.1.0.dev0` in tutte e tre le
  fonti. `main` porta la versione della release verso cui si lavora, non quella
  dell'ultima release: finché `[Unreleased]` contiene modifiche che alterano i
  numeri, qualsiasi numero stabile dichiarerebbe una release che non le
  descrive. **Nessun tag `v1.0.0` è stato creato**: un tag afferma *questo
  commit è ciò che è stato distribuito*, e va dimostrato prima.
- **Suite ermetica.** Ogni test gira con data/config/cache/log home
  reindirizzate in una directory temporanea; un tripwire fa fallire la sessione
  se la suite scrive nel profilo reale. Prima, riaprire un progetto radiomico in
  un test scriveva nella vera `recent_projects.json` dell'utente.
- **Authoring qualitativo transazionale.** Il documento author ha una revisione
  monotona; Close/New/Open chiedono Save/Discard/Cancel; il salvataggio scrive
  uno snapshot immutabile. *Create Blinded Delivery* combinava pagine, media e
  domande dell'archivio salvato con il design **vivo** del pannello: ora usa le
  due metà dello stesso snapshot, o rifiuta.
- **Salvataggio qualitativo fuori dal thread GUI**: anonimizzazione, copia e ZIP
  in un worker con cancellazione cooperativa e commit atomico; niente più
  `QApplication.processEvents()` nel percorso di salvataggio.
- **Policy learned dichiarata e dimostrata**: i checkpoint non sono inclusi in
  alcun bundle (il README affermava il contrario). Lo smoke frozen gira con home
  e cache vuote e verifica sia il rifiuto offline sia il percorso esplicito di
  preparazione.
- **Budget reali** su download e archivi: `size_bytes` nei manifest IBSI,
  `Content-Length` verificato e contatore streaming sempre attivo, controllo
  preventivo dello spazio libero, quote per tipo di pacchetto (progetto,
  valutazione, radiomica, coorte, workbook) e contatore sui byte realmente
  estratti.
- **Bootstrap dell'installer**: pip aggiornato a ≥26.2 prima di ogni dipendenza,
  in CI e nel packaging. La revisione auditata girava con pip 24.0 e sette
  vulnerabilità note. L'ambiente che produce il bundle è ora auditato anch'esso.
- CI: matrice Python 3.11/3.12, Action fissate a commit SHA con aggiornamento
  Dependabot, risorse IBSI in cache sull'hash del manifest, e la seconda
  esecuzione completa della suite sostituita dalla sola collection (che è ciò
  che verificava davvero la differenza di `sys.path`).
- Benchmark: `excel_only` e `full_export_with_ai` separati, schema versionato,
  e confronto rifiutato fra schemi, profili o host differenti.
- `packaging/THIRD_PARTY_LICENSES.txt` non descrive più QICOM come proprietario
  (è Apache-2.0 dal primo commit pubblico) e non dichiara più fuori scope il
  profilo learned, che i tag costruiscono.

### Fixed

- **Valutazione qualitativa: le risposte sparivano scorrendo le slice.** Ogni risposta era
  registrata sulla slice della reference in quel momento, quindi su una serie con più slice il
  modulo si svuotava a ogni cambio di slice e le risposte ricomparivano solo tornando su quella
  slice, come se ogni slice fosse una domanda a sé. Il design ha ora l'impostazione «Le risposte
  valgono per» (passo «Impostazioni della valutazione»): «L'intera serie», predefinita per le
  nuove valutazioni, registra una sola risposta per pagina, confronto e domanda (a `slice_index`
  0) e lascia scorrere la serie senza toccare risposte né tempi; «Ogni slice separatamente»
  mantiene il comportamento precedente. L'indicatore di slice dice quale delle due vale. Il
  campo `design.answer_scope` viaggia nel pacchetto e nella consegna in cieco; un design salvato
  prima, senza il campo, è letto come per slice, così le risposte già raccolte restano dove
  erano (`OPERATIONS_SCHEMAS.md`, `qicom/session/qualitative_design.py`,
  `qicom/gui/qualitative_controller.py`).
- **Trackpad: un gesto saltava molte slice.** Ogni evento della rotella spostava di una slice
  intera; il trackpad (soprattutto su macOS) ne manda decine per gesto, più quelli dell'inerzia
  dopo aver sollevato le dita e uno finale senza movimento verticale, letto come «su» e quindi
  una slice indietro. Ora gli spostamenti si sommano fino a uno scatto di rotella (120 unità),
  l'inerzia è ignorata e un evento senza movimento verticale non sposta nulla, nei flussi e negli
  strumenti di utilità (`qicom/gui/wheel_slice_steps.py`). Una rotella a scatti si comporta come
  prima.
- **Consegne in cieco ricostruibili dal pacchetto del valutatore.** Il piano pubblico conteneva il
  seed, il design pubblico il master seed, e pagine e domande tenevano gli ID dell'autore, con le
  ripetizioni nascoste numerate dopo tutte le altre: dal solo ZIP del valutatore si ricostruivano
  quale pannello era il riferimento e quali pagine erano ripetizioni. Ora il seed mescola un
  segreto derivato da una chiave casuale dell'autore, conservata privata nel suo profilo (la
  stessa consegna ricreata dallo stesso autore resta identica, così le bozze dei valutatori e la
  mappa restano valide), e seed, segreto e master seed stanno solo nella mappa dell'autore
  (versione 2, `OPERATIONS_SCHEMAS.md`); il piano pubblico porta `seed: 0`,
  pagine e domande sono numerate nell'ordine di presentazione, e il design pubblico non riporta
  randomizzazione né ripetizioni (`qicom/session/evaluation_package.py`,
  `qicom/session/qualitative_design.py`). L'aggregazione controlla le risposte di ogni consegna
  sulla struttura che quella consegna ha presentato, ricostruita dalla sua mappa sul pacchetto
  dell'autore o sulla mappa del pacchetto caricato, e riporta gli ID dell'autore
  (`qicom/session/evaluation_aggregation.py`). Una mappa versione 2 deve essere completa: una
  pagina, una domanda o un pannello che non mappa è rifiutato, perché con ID opachi il numero
  presentato nominerebbe un'altra sorgente. Le mappe versione 1 restano leggibili come prima.
  **Le consegne create con le versioni precedenti espongono comunque la randomizzazione a chi le
  ha ricevute**: per uno studio in corso, valutare se generarne di nuove.
- **Consegne in cieco e immagini non de-identificabili.** Un'immagine lasciata passare da un
  override quando l'autore aveva salvato il proprio pacchetto entrava nella consegna così
  com'era: il NIfTI non verificabile era riscansionato con l'elenco dei rifiuti scartato, e il
  DICOM non era de-identificato affatto (solo svuotato degli attributi di acquisizione). Ora ogni
  immagine è de-identificata di nuovo per la consegna, con UID propri per ogni caso; un rifiuto
  ferma la consegna prima di scrivere qualunque file e la finestra chiede all'autore se
  includere comunque quelle immagini, per questa consegna soltanto.
- **Consegna fallita a metà.** La mappa era scritta prima dello ZIP: se poi lo ZIP non arrivava a
  destinazione, restava la consegna precedente accanto alla mappa nuova, che nomina altre
  immagini. Ora la mappa precedente è messa da parte e rimessa al suo posto se la consegna non
  viene scritta; senza una mappa precedente, quella nuova è rimossa. Due consegne verso la stessa
  destinazione, anche contemporanee, scrivono la loro coppia una dopo l'altra (lock esclusivo per
  utente e macchina, `qicom/file_locks.py`), e la mappa registra l'hash del manifest della propria
  consegna (`delivery_manifest_sha256`).
- **Strumenti di utilità: NIfTI con la geometria di un'altra pila.** Per i multi-frame le posizioni
  erano riordinate lungo la normale anche quando il loader aveva lasciato l'ordine del file
  (posizioni ripetute), e per le serie il passo era ricavato da prima e ultima slice anche con una
  slice mancante: l'affine scritto non descriveva i pixel scritti, ed era dichiarato noto. Ora le
  slice sono collocate dalle posizioni che il loader consegna nell'ordine dei pixel; una pila che
  nessun affine descrive (passi irregolari, posizioni ripetute, frame con orientamenti diversi
  anche se le posizioni sono regolari) è scritta con qform/sform a 0, e una pila inclinata tiene il taglio solo
  nella sform, che il qform non può rappresentare (`qicom/io/geometry.py`).
- **CI: gate MIRP nel job di test principale.** Il job imposta `QICOM_RUN_IBSI_REFERENCE=1` per
  il gate PyRadiomics ma non installa MIRP né la sua cache, e il test MIRP lo trattava come un
  fallimento. Il gate MIRP ha ora una variabile propria, `QICOM_RUN_IBSI_MIRP`, impostata solo
  nei job che lo preparano.
- **Anonimizzazione DICOM: contenitore Part 10.** Il preambolo di 128 byte e il gruppo File Meta
  erano copiati dall'originale, con `PrivateInformation` e i suoi dati, in file dichiarati
  `PatientIdentityRemoved=YES`. Ora il contenitore è ricostruito come chiede PS3.15: preambolo
  azzerato, File Meta con i soli elementi richiesti da PS3.10, verifica sulla copia riletta
  (`qicom/io/dicom_anonymize.py`).
- **Anonimizzazione NIfTI: riconoscimento strutturale.** Bastava che i primi quattro byte
  valessero 348 o 540: un file senza magic, con testo identificativo dopo l'header, era
  esportato come verificato. Ora magic, dimensioni, datatype, offset dei voxel e completezza del
  payload devono essere coerenti; tutto ciò che sta tra header e voxel è azzerato e i byte dopo
  il payload sono eliminati (`qicom/io/nifti_anonymize.py`).
- **Progetti radiomici: tabelle riaperte diverse da quelle salvate.** Il CSV non conserva i tipi:
  una ROI di nome `"001"` tornava come l'intero `1`, il testo `"NA"` come valore mancante, e la
  tabella riaperta non aveva più l'hash registrato nella ricevuta. Ogni tabella è ora salvata
  anche in JSON tipizzato, che QICOM rilegge valore per valore; il CSV resta per gli altri
  strumenti. I progetti precedenti si leggono dal CSV tenendo come testo le colonne testuali note
  (`qicom/radiomics/project.py`).
- **Coorte radiomica su risultati obsoleti.** L'aggregazione accettava progetti marcati
  `results_stale` e tabelle che non corrispondevano più alla ricevuta, e ne calcolava la
  ripetibilità. Ora li rifiuta, con il motivo (`qicom/radiomics/cohort.py`).
- **RIQE nella CLI e nel batch.** Solo la GUI passava a RIQE l'orientamento letto dal loader: CLI
  e batch valutavano come assiale uno stack sagittale. Ora lo passano entrambi
  (`qicom/cli/main.py`, `qicom/batch.py`).
- **Valutazione qualitativa: il menu File spariva dopo il primo salvataggio.** Il blocco della
  finestra durante il salvataggio chiedeva a Qt la barra dei menu, che al posto di quella con il
  logo ne creava una nuova vuota: File, Vista, Strumenti e Aiuto sparivano, e per uscire dalla
  valutazione restava solo chiudere QICOM. La barra resta ora quella vera, e la voce del menu File
  che torna all'avvio si chiama «Chiudi e torna all'avvio», con la scorciatoia Ctrl+W
  (`qicom/gui/widgets/qualitative_main_window.py`).
- DICOM compressi in JPEG lossless (Process 14, SV1 e non) e JPEG-LS, il formato della maggior
  parte degli esami CT e MRI esportati da scanner e PACS: QICOM li rifiutava con «cannot decode
  with its bundled decoders», perché pydicom e Pillow da soli decodificano solo RLE, JPEG baseline
  e JPEG 2000. `python-gdcm` è ora una dipendenza di base (lock, attribuzione con le licenze di
  GDCM e delle librerie che incorpora in `packaging/attribution/notices/python-gdcm.txt`, smoke
  test del bundle che decodifica un frame). JPEG baseline e JPEG 2000 restano decodificati da
  Pillow anche se pydicom interpellerebbe prima GDCM, così i pixel dei file che si aprivano già
  non cambiano; GDCM decodifica solo ciò che Pillow non sa (`qicom/io/dicom_io.py`). Il messaggio
  per le sintassi ancora non decodificabili (HTJ2K) non suggerisce più di installare GDCM.
- Risultati quantitativi: la nota «N requested metric outcome(s) require attention» contava anche
  le metriche che il profilo non aveva selezionato (stato `not-requested`), per cui una corsa con
  sette metriche tutte calcolate segnalava 30 esiti da controllare. Ora conta solo le metriche
  richieste e non calcolate, e la nota resta nascosta quando non ce ne sono.

- Anonimizzazione DICOM nelle sequenze annidate: `dicom-anonymizer` applica le regole PS3.15 solo
  al livello radice, così nei multi-frame Enhanced (e in ogni sequenza annidata) restavano date
  d'acquisizione per frame, UID dello studio sorgente e riferimenti di derivazione, in file
  dichiarati `PatientIdentityRemoved=YES`. Le regole sono ora applicate a ogni elemento annidato,
  ogni UID di istanza, data, ora o nome di persona rimasto viene sostituito (in modo coerente
  all'interno del pacchetto), gli AE title di origine nel File Meta sono rimossi e la copia è
  verificata contro gli UID e le date dell'originale. I DICOMDIR sono riconosciuti esplicitamente
  e non vengono anonimizzati (`qicom/io/dicom_anonymize.py`).
- Anonimizzazione NIfTI incompleta: venivano azzerati solo `descrip` e `aux_file`. Ora sono azzerati
  anche `intent_name`, `db_name`/`data_type` (NIfTI-1) e `unused_str` (NIfTI-2), le estensioni
  dell'header (dove i convertitori copiano l'intero header DICOM, un JSON o la storia AFNI) sono
  rimosse, e un `.nii.gz` è sempre ricompresso senza il nome del file originale né la data
  nell'header gzip. Un file con suffisso NIfTI che non si riesce a verificare non viene più
  copiato in silenzio: segue lo stesso flusso dei DICOM non anonimizzabili. Anche il progetto
  ZIP quantitativo ora ripulisce gli header NIfTI (`qicom/io/nifti_anonymize.py`).
- Metadati delle bitmap nei pacchetti: il pacchetto di valutazione standard (e il progetto ZIP)
  copiava JPG e PNG con EXIF, XMP, commenti JPEG e chunk di testo PNG, mentre il dialogo dichiarava
  anonimizzata "ogni immagine"; solo la consegna in cieco li ripuliva. Ora sono rimossi senza
  perdita a livello di byte, mantenendo nome, formato e dati compressi del file (resta solo
  un'eventuale orientazione EXIF non predefinita e il profilo ICC con un nome neutro; i dati
  accodati dopo la fine dell'immagine sono eliminati) (`qicom/io/bitmap_anonymize.py`).
- Formula injection negli export CSV: solo gli XLSX e il CSV del CLI neutralizzavano le celle che
  iniziano con `=`, `+`, `-` o `@`. Ora lo fanno anche l'export CSV dell'aggregazione qualitativa
  (nomi e commenti scritti dai valutatori, input non fidato), il CSV del pannello Log, i CSV della
  radiomica, del batch e il tidy CSV. I valori numerici restano numerici; il tidy JSON resta la
  forma esatta per il round-trip.
- I NIfTI caricati restavano mappati in memoria sul file sorgente (`nib.load` con `mmap=True`): se
  una pipeline riscriveva il file, le metriche successive usavano i pixel nuovi mentre display e
  `data_range` restavano quelli vecchi, e un file troncato nel frattempo faceva terminare QICOM con
  SIGBUS, senza log. Ora i pixel sono letti in memoria al caricamento (`qicom/io/nifti_io.py`).
- Provenance: un `qicom/_build_identity.py` rimasto nel checkout dopo una build locale (è
  gitignored) aveva la precedenza su git anche fuori dal bundle, così ogni risultato calcolato
  in seguito dal sorgente veniva attribuito al commit di quella build, dichiarato pulito. Ora il
  modulo generato vale solo nel bundle frozen; dal sorgente si interroga il checkout e il modulo
  generato è solo un ripiego quando non c'è un repository (`qicom/build_identity.py`).
- All'avvio il launcher si chiudeva se `recent_projects.json` aveva una forma inattesa (lista,
  `null`, testo non UTF-8) o se un progetto recente stava in una cartella non più leggibile
  (`PermissionError`); ora quei casi danno un elenco vuoto o saltano la voce, e il file è scritto
  con un temporaneo univoco.
- Diagnostica per slice con "escludi l'aria" o con le ROI: le slice senza tessuto vengono saltate,
  e `worst_slice`, `outlier_slices` e il pulsante "slice peggiore" indicavano la posizione nella
  lista accorciata invece della slice del volume (con tre slice iniziali di sola aria, la 6 invece
  della 9). Ora riportano l'indice della slice nel volume (`StackAggregateResult.slice_indices`).
  Inoltre una sola slice invariata (PSNR infinito) faceva fallire l'intero calcolo: il riepilogo
  robusto ora usa le slice con valore finito. Una slice su cui la metrica non è definita (per
  esempio un riferimento costante nel tessuto) veniva tolta dal valore del volume con l'esito
  "calcolato" senza alcuna nota: ora l'esito dice su quante slice è stato calcolato e quali sono
  state escluse. I valori delle metriche non cambiano.
- Ripresa delle valutazioni in corso: il file locale era indicizzato solo su studio e computer, e
  tutte le consegne in cieco di uno studio condividono lo studio. Un secondo lettore sulla stessa
  postazione riprendeva risposte, piano di presentazione e identificativo di invio del primo; un
  file illeggibile (per esempio scritto da una versione più recente) veniva ignorato e poi
  sovrascritto al primo clic. Ora il file è indicizzato anche sulla consegna, QICOM chiede se la
  valutazione trovata è di chi sta rispondendo (No ne inizia una nuova) e un file illeggibile o
  non ripreso viene conservato a parte, con un avviso nel registro errori.
- Calculate premuto mentre "Apri progetto" legge l'archivio (l'attesa lascia attiva l'interfaccia)
  calcolava sulle immagini precedenti e i risultati venivano attribuiti al progetto appena aperto.
  Ora durante l'apertura il calcolo viene rifiutato con un messaggio, e ogni calcolo partito prima
  viene invalidato anche a lettura conclusa.
- Sicurezza degli input e dei download: un file di risposte XLSX (input non fidato) passa ora dal
  controllo d'inventario degli archivi prima di pandas (400 KB potevano diventare quasi 1 GB in
  memoria); l'import di un labelmap con molte etichette chiede conferma prima di allocare una
  maschera per etichetta; il download dei pesi learned rifiuta subito un `Content-Length` diverso
  dalla dimensione attesa, come già dichiarato in SECURITY.md (il contatore dei byte resta).
- Documentazione dell'accesso di rete della "Verifica IBSI" (risorse ufficiali IBSI scaricate su
  richiesta, con SHA-256 e dimensione fissati): About (4 lingue), SECURITY.md e
  `coverage-floor.json`, dove `ibsi_validation.py` diventa un trust boundary.
- Consegna in cieco: i DICOM copiati senza modifiche rivelavano il braccio dello studio attraverso
  produttore, kernel di ricostruzione, protocollo, dose e descrizioni. Ora nella consegna questi
  attributi sono svuotati (le sequenze di apparecchiature e algoritmi rimosse), i pixel restano
  invariati; gli header NIfTI sono ripuliti di nuovo, per i pacchetti salvati da versioni
  precedenti. Il pacchetto dell'autore non cambia.
- Le schede delle distanze NSS storiche (`brisque`, `niqe`) avvertono che con il modello sintetico
  predefinito aggiungere rumore può ridurre la distanza (un test lo documenta): non sono misure di
  rumore. Formula e direzione invariate.
- GUI e CLI avvisano quando il `data_range` supera di oltre 8 volte lo span dei valori delle
  immagini (tipico delle TC a 16 bit, dove SSIM e PSNR quasi non rispondono alle differenze). Il
  valore predefinito non cambia.
- Ricevute batch: un input mancante riceveva come "hash" lo SHA-256 del testo del suo percorso,
  come se fosse stato letto; ora è registrato con il prefisso `missing:`. I percorsi (pacchetto,
  risposte, mappe, progetti, cartella di output) stanno sotto `configuration.locations`, escluso
  dal `configuration_hash`, e il `result_hash` della coorte non include più la colonna
  `project_path`: lo stesso calcolo eseguito da un'altra cartella o macchina ha gli stessi hash.
  Le ricevute esistenti restano leggibili e invariate.
- Scritture persistenti: i temporanei avevano un nome fisso (`.<nome>.tmp`), quindi due finestre che
  salvavano lo stesso file scrivevano nello stesso temporaneo (archivio corrotto riportato come
  salvato) e un link simbolico piazzato con quel nome veniva seguito. Ora ogni scrittura usa un
  temporaneo univoco e non prevedibile, i dati sono sincronizzati su disco prima della
  rinomina e, su POSIX, anche la directory (`qicom/atomic_files.py`, usato da progetti,
  valutazioni, risposte, identità del valutatore, mappe di de-blinding, tidy, workbook, progetti
  radiomici, labelmap e verifica IBSI).
- Persistenza: preferenze illeggibili (danneggiate o di una versione più recente) venivano
  sovrascritte con i valori predefiniti alla prima modifica; ora il file viene conservato a parte
  prima del salvataggio. Una cache NSS troncata bloccava per sempre le distanze NSS; ora è scritta
  in modo atomico e una cache illeggibile viene ricalcolata. Le tabelle dei progetti radiomici sono
  rilette in modo esatto (`float_precision="round_trip"`): prima un valore su tre tornava diverso
  di qualche ULP e la tabella riaperta non corrispondeva al `result_hash` della sua ricevuta.
- Il progetto ZIP quantitativo non conservava ricevuta ed esiti delle metriche (viaggiano come
  attributi del DataFrame, persi nel manifest): riaperto altrove, esportava i numeri senza revisione
  di build, hash degli input, configurazione ed esiti. Ora il manifest ha il campo opzionale
  `calculation_records`, letto con default per i progetti precedenti (formato invariato, v1).
- Bundle demo: `qicom_demo.json` elenca in `run_specific_files` i file che registrano il momento
  della propria generazione; la procedura di confronto documentata vale per tutti gli altri.
- File danneggiati: un archivio troncato o con membri corrotti sollevava `BadZipFile`, e un manifest
  con campi del tipo sbagliato `TypeError`/`AttributeError`, che la GUI non gestiva: aperti dal
  launcher chiudevano QICOM senza messaggi. Ora sono errori di input con un messaggio, e il launcher
  torna alla scelta del flusso se l'apertura fallisce per qualunque motivo. Un database di sessione
  illeggibile viene conservato a parte (con i file `-wal`/`-shm`) e sostituito da uno nuovo, con un
  avviso nel registro errori; il database ha ora una versione (`PRAGMA user_version`) e uno scritto
  da una versione più recente viene rifiutato invece di essere usato alla cieca.
- RAM e CPU disponibili: su Linux la "RAM disponibile" era `MemFree` (senza la cache di pagina
  recuperabile: avvisi di memoria spuri e profilo `constrained` immotivato); su macOS era
  sconosciuta, quindi il controllo prima di caricare un volume non scattava mai. Ora QICOM usa
  `MemAvailable` (con il limite cgroup nei container) e `vm_stat` su macOS; il numero di CPU
  rispetta l'affinità del processo.
- Ogni errore mostrato dall'interfaccia comincia con una frase tradotta su cosa non è riuscito
  ("Impossibile aprire il progetto."), seguita dal dettaglio tecnico. Sono tradotti alla fonte, nelle
  quattro lingue, i motivi di rifiuto dell'anonimizzazione, i rifiuti degli archivi non sicuri e i
  messaggi sui file immagine illeggibili o non supportati; gli altri dettagli tecnici restano in
  inglese, sotto la frase tradotta.
- L'adattamento di un modello NSS locale mostra l'avanzamento per file e si può annullare.
- Interfaccia: il pannello di configurazione radiomica segue il cambio di lingua (i suoi 43 testi
  restavano nella lingua di apertura) e non ha più etichette fisse in inglese; i filtri dei file, i
  nomi delle metriche in heatmap, ROI e grafici, il passo di estrazione e altre stringhe visibili
  sono tradotti; la ricerca delle metriche trova anche il nome tradotto; il seme del region growing
  mostra la sua intensità; otto chiavi di traduzione inutilizzate sono state rimosse.
- Le descrizioni inglesi di BRISQUE, NIQE, NIQE/BRISQUE canonici ed EPI venivano da copie in
  `en.json` divergenti dalle schede YAML, che sono la fonte del testo inglese: le copie sono state
  rimosse.
- L'adattamento di un modello NSS locale avviene fuori dal thread dell'interfaccia; un range dei
  dati non valido è segnalato nella lingua dell'utente e ogni errore è mostrato invece di
  sfuggire al dialogo.
- Un file di identità del valutatore danneggiato (o di forma sbagliata, come `[]`) produceva un
  nuovo id, e ogni risposta salvata su quella postazione restava orfana; `[]` sollevava
  AttributeError. Ora il file viene messo da parte e, se le risposte salvate concordano su un id,
  quell'id viene ripreso.
- Una delivery in cieco veniva scritta prima della sua mappa di de-blinding: se la mappa falliva,
  restava una delivery che nessuno poteva più de-blindare. Ora la mappa viene confermata per prima.
- Una ricevuta con un identificativo con spazi (`" S1 "`) cambiava hash al ricaricamento, e
  `source_dirty: "false"` diventava `True`. Gli identificativi sono ora ripuliti alla costruzione
  e il flag deve essere un booleano.
- Con due coppie in parallelo il limite di un thread nativo valeva per OpenBLAS ma non per i pool
  OpenMP avviati dai worker, che lavoravano a piena larghezza ciascuno: ora il limite è applicato
  dentro ogni worker.
- La preparazione radiomica con normalizzazione "none" (il default) faceva due copie float64
  inutili del volume.
- La finestra radiomica non riceveva le eccezioni sfuggite a uno slot, che finivano solo nel log:
  ora compaiono nella barra di stato e nel bug report, insieme agli errori mostrati.
- I manifest dei progetti quantitativi e delle valutazioni sono JSON valido (RFC 8259): i numeri non
  finiti (una deviazione standard NaN, il PSNR +inf di immagini identiche) uscivano come token
  `NaN`/`Infinity`, rifiutati dai lettori rigorosi. Il progetto quantitativo passa al formato 2
  (`{"$float": "Infinity"}`); il formato 1 resta leggibile.
- I numeri di versione dei formati persistiti sono accettati solo come interi esatti: `true`, `3.0`,
  `"3"` o `3.7` passavano il controllo (`qicom.schema_version`).
- Una riesecuzione batch fallita lasciava i risultati della run precedente accanto a una ricevuta e
  un log nuovi e vuoti; ora ogni run rimuove i file scritti dalla precedente
  (`qicom_batch_outputs.json`) e nessun altro.
- Un progetto radiomico risalvato riceveva ogni volta un nuovo `created_at`.
- Il database di trending controlla la versione prima di creare tabelle e verifica l'hash di una
  baseline al caricamento (colonna additiva `canonical_json`).
- `hash_array` copiava l'intero array a ogni blocco quando non era contiguo (1,3 s invece di
  0,05 s su un volume 5000×5000 trasposto); l'hash è identico.
- Un'immagine più piccola della patch NSS veniva registrata come calcolo fallito (`failed`); ora è
  un input non valido, come ogni altra immagine troppo piccola per la finestra di una metrica.
- `qicom.runtime` dichiara che con due coppie in parallelo gli ultimi bit di BRISQUE, NIQE, NIQE
  canonico e RIQE, e quindi il `result_hash`, possono differire da un'esecuzione con una coppia
  sola: i valori coincidono alla precisione dichiarata.
- I nomi di membro duplicati, o uguali a meno di maiuscole, normalizzazione Unicode e punti o spazi
  finali, fanno rifiutare un archivio: su NTFS/APFS una slice sovrascriveva in silenzio l'altra.
  Su Windows sono rifiutati anche i nomi di dispositivo (`CON`, `LPT1`…) e i caratteri non ammessi.
- La verifica SHA-256 di un checkpoint learned restava in cache per tutta la sessione: un file
  sostituito dopo il primo caricamento veniva ricaricato senza controllo. Ora l'hash è verificato
  a ogni caricamento.
- Il filtro dei file del dialogo del modello NSS locale mostrava la chiave di traduzione
  `file_filter.images` invece di un filtro, e non selezionava nessun file. Un test verifica ora che
  ogni chiave letterale passata alla traduzione esista.
- Il pannello Log rileggeva l'intero file (fino a 5 MB) nel thread dell'interfaccia ogni 2 secondi,
  anche nascosto, e un carattere UTF-8 troncato da una chiusura non pulita faceva uscire
  un'eccezione dal timer. Ora legge solo le righe aggiunte, solo mentre è visibile, e tollera righe
  troncate o illeggibili.
- Esc o la chiusura del dialogo di download dei pesi learned bloccavano l'applicazione fino a 60 s
  con una connessione ferma. Ora il dialogo si chiude subito e il trasferimento si ferma al blocco
  successivo, senza attese nel thread dell'interfaccia.
- L'estrazione radiomica e l'aggregazione di coorte si possono annullare: il dialogo di avanzamento
  non aveva un pulsante Annulla e un'estrazione multi-ROI di molti minuti si interrompeva solo
  chiudendo QICOM. L'annullamento ha effetto al confine successivo (tra ROI, o tra immagine di
  riferimento e di confronto) e lascia invariati i risultati precedenti.
- Memoria al Calculate: alcune metriche raggiungono picchi di 14-40 volte un'immagine (Wasserstein-1,
  NMI, SSIM 3D), raddoppiati da due coppie in parallelo, e un `MemoryError` annullava l'intero
  calcolo. Ora un `MemoryError` è l'esito della sola metrica che l'ha provocato, QICOM avvisa
  quando la stima del picco supera la RAM disponibile e calcola le coppie una alla volta quando due
  in parallelo non ci starebbero (`qicom.memory.estimate_calculation_peak_bytes`).
- I motivi di un rifiuto dell'anonimizzazione non citano più il valore identificativo rimasto
  (prima "first: 'Cognome^Nome'"): nominano solo gli attributi, perché finiscono nel registro
  errori e nei report da inviare. Per lo stesso motivo il messaggio "più serie DICOM trovate" non
  cita più la descrizione libera delle serie (resta visibile solo nella scelta della serie).

- The coverage gate now computes statement coverage from line counters instead of labeling
  the combined line/branch percentage as statement coverage. Recorded floors are unchanged.

- Blinded qualitative deliveries now copy common raster images losslessly without text or
  identifying EXIF metadata. JPEG pixels are decoded once and stored in PNG; rendering metadata
  is retained. Burned-in text and arbitrary ICC profile content are outside this protection.
- Hidden-repeat schedules guarantee the requested number of intervening pages or reject an
  impossible design. Tail cases no longer silently shorten the requested spacing.

- **I test delle metriche learned tornano a trovare i pesi in CI.** Il profilo
  utente usa e getta che ogni test riceve reindirizza anche `TORCH_HOME`, e da
  quel momento nascondeva i checkpoint che il job CI scarica prima della suite
  con `packaging/prepare_learned_weights.py`: LPIPS e DISTS rifiutano di
  calcolare senza pesi verificati e non li scaricano mai durante un'analisi,
  quindi fallivano a ogni push. Su una macchina senza l'extra `learned` gli
  stessi test si saltano, perciò il guasto era invisibile in locale.
  `tests/conftest.py` ora collega i pesi già scaricati dentro il profilo
  temporaneo — le letture raggiungono i file reali, le scritture restano
  confinate — e `TestPreparedLearnedCheckpointsStayReachable` presidia
  l'invariante. Nessuna modifica al comportamento dell'applicazione né ai
  valori delle metriche.

- **Le build desktop per macOS e Windows tornano a completarsi.** Tre difetti
  indipendenti, tutti nel workflow di packaging e nessuno nell'applicazione:
  `pip-audit` veniva installato *dentro* l'ambiente che PyInstaller poi
  congelava, così le sue undici dipendenze (`requests`, `rich`, `certifi`,
  `urllib3`, …) finivano nel bundle e la verifica dell'inventario licenze lo
  rifiutava; l'indice CPU di PyTorch, che sostituisce PyPI anziché
  affiancarlo, risolveva `setuptools` a 78.1.0 (PYSEC-2025-49,
  PYSEC-2026-3447) facendo fallire l'audit su Windows e Linux; e `setuptools`,
  che PyInstaller impacchetta davvero perché `torch.utils.cpp_extension` lo
  importa, era escluso dall'inventario come se fosse un semplice installer.
  L'auditor ora vive in un ambiente separato e legge quello di build con
  `--path`, `setuptools` è inventariato con la sua licenza MIT e quelle dei
  pacchetti che vendorizza, e il nome derivato dalla directory
  `python3.<minor>/` non viene più scambiato per una distribuzione.

- **Le build con le metriche learned tornano a completarsi.** Altri due
  difetti indipendenti, di nuovo entrambi nel packaging e nessuno
  nell'applicazione. `torchvision` carica i suoi operatori compilati con
  `torch.ops.load_library`, su un percorso costruito da
  `torchvision.__file__`: un'indirezione che nessuna analisi statica può
  seguire, e che l'hook distribuito con PyInstaller compensa nominando
  `torchvision._C` — un nome che upstream non usa più, sostituito da
  `_C_stable` (e da `image_stable` per `torchvision.io`). Non veniva quindi
  raccolta alcuna libreria, e nel bundle `import torchvision` moriva con
  `operator torchvision::nms does not exist`, perché `_meta_registrations`
  registra incondizionatamente un kernel fake per quell'operatore: lo smoke
  test frozen falliva su ogni build learned, macOS compreso.
  `packaging/qicom.spec` ora raccoglie le librerie compilate che il
  `torchvision` installato spedisce davvero — per forma e non per nome, così
  che la prossima rinomina non possa ripetere il guasto in silenzio — e
  rifiuta di costruire un bundle learned se non ne trova nessuna. Il secondo
  difetto è il tetto `packaging<26` dell'extra `packaging`, che teneva quella
  distribuzione a 25.0: `pip check` esamina l'intero ambiente e non solo ciò
  che QICOM spedisce, e si rompeva sul `pipx` preinstallato sul runner
  Windows, che richiede `>=26`. Il tetto sale a `<27`.

- **LPIPS ritrova i propri pesi lineari dentro il bundle.** Superato il
  guasto di `torchvision`, il passo che verifica la preparazione esplicita
  dei checkpoint incontrava il difetto successivo: `lpips` individua i pesi
  che spedisce nel proprio pacchetto con `inspect.getfile(self.__init__)`,
  cioè dal nome di file del code object, che per un modulo servito
  dall'archivio PYZ è il percorso relativo registrato in fase di build.
  `os.path.abspath` lo risolveva quindi rispetto alla directory di lavoro e
  LPIPS moriva con `FileNotFoundError` su
  `<cwd>/lpips/weights/v0.1/alex.pth`: i pesi erano raccolti correttamente,
  li si cercava altrove. `lpips` viene ora raccolto come sorgente su disco,
  così l'import lo registra con un percorso assoluto. Poiché un modulo
  raccolto in questo modo non compare in `a.pure`, l'inventario dei moduli
  che alimenta `attribution.py check-bundle` rileggeva un pacchetto in meno e
  avrebbe smesso di verificarne la licenza in silenzio: ora legge anche i
  sorgenti raccolti come dati. Nessuna modifica ai valori delle metriche.

- Adding the green air sample in the Background step no longer jumps the
  window to the Analyze step: creating the circle is the first of that step's
  three actions (place it, detect, correct by hand), and the user was thrown
  out of the step they had only just started.

#### Release readiness

- **Dipendenze bloccate con hash.** Ogni bundle si installa dal lock del proprio profilo e della
  propria piattaforma (`requirements/lock/`, 12 file generati da `packaging/generate_locks.py`), con
  `pip --require-hashes`: due build dello stesso tag spediscono gli stessi file, e una release
  compromessa che rientra in un intervallo di versioni non entra nel bundle. Anche il gate IBSI usa
  il lock del bundle Linux. PyRadiomics, che pip non può verificare per hash, resta fissato al
  commit Git. `--check` nel gate statico tiene i lock coerenti con `pyproject.toml`.
- **Release e SBOM.** Un tag crea una GitHub Release in bozza con bundle, checksum SHA-256 e SBOM
  CycloneDX di ogni piattaforma, e le note della sezione del CHANGELOG; la pubblicazione resta una
  decisione del maintainer.
- **Build e attribuzioni.** NOTICE e il dialogo About indicavano `packaging/...`, un percorso che in
  un bundle non esiste: ora nominano i file dove si trovano davvero. Il wheel ridistribuisce il
  modello RIQE (CC BY 4.0) insieme alla sua attribuzione (`qicom/metrics/models/RIQE-MODEL.txt`).
  `metric_inventory.csv`, spedito nel bundle, non rimanda più a `paper/`, che non è distribuito.
  Le build dei tag non ripristinano la cache pip e ne conservano gli artefatti 90 giorni invece
  di 7. Una build locale avvisa che non è verificata quando usa la copia di riferimento delle
  licenze, ed esclude MIRP e ITK, che nessun profilo del bundle include.
- **Reference set IBSI nel bundle `full`.** `qicom/radiomics/reference_sets` era nel wheel ma non
  nella spec PyInstaller: un'applicazione congelata non poteva eseguire nessuna verifica IBSI. Un
  test confronta ora package-data e spec, e lo smoke frozen carica ogni manifest e il modello RIQE.
- **Gate "numerical results" non aggirabile.** Una voce sotto un sottotitolo `####`, un elenco con
  `*`, `+` o numerato, o il titolo in maiuscolo sfuggivano a `check_release_consistency.py`, e una
  versione stabile poteva uscire con modifiche ai numeri non dichiarate.
- **Ambiente di build.** setuptools, che finisce nel bundle, ha ora un tetto (`<85`) e le ricevute
  registrano le versioni di PyRadiomics, PyWavelets e torch (campo `backend_versions`, additivo).
  le release si installano ora da lock con hash (voce seguente).
- **Conformità IBSI di MIRP verificata in CI.** Il test dei sei manifest MIRP (2104 righe) saltava
  sempre: nessun job installava MIRP né preparava la cache delle risorse. Ora un job CI dedicato e
  un gate dei tag (`ibsi-mirp`, `ibsi-mirp-gate`) li eseguono in un ambiente separato, con il proprio
  audit delle dipendenze. Con `QICOM_RUN_IBSI_MIRP=1`, impostata solo in quei due job, un MIRP o
  una cache mancanti sono un fallimento, non uno skip; il job di test principale, che imposta
  `QICOM_RUN_IBSI_REFERENCE=1` per il gate PyRadiomics e non installa MIRP, lo salta con la causa
  (prima falliva).
- **Test che verificano davvero.** Gli stub statici di `QMessageBox` valgono ora solo per il modulo
  indicato e un dialogo statico senza risposta fa fallire il test invece di bloccare la suite;
  `faulthandler_timeout` indica il test bloccato. I test di pulizia dopo un archivio rifiutato
  guardano la cartella temporanea di sistema, dove l'estrazione avviene davvero. La coverage misura
  anche il codice eseguito nei QThread: `evaluation_export.py` risultava al 13% nella suite completa
  e al 99% da solo.
- `load_package_metadata_from_zip` segnalava un file che non è uno ZIP con un `BadZipFile` grezzo,
  che il dialogo di aggregazione non intercettava; ora è un `ValueError` leggibile come negli altri
  percorsi di apertura.
- **Tracciamento modifiche qualitativo corretto.** Il completamento asincrono
  dei media ripristinati da un archivio non rende più immediatamente
  “modificata” una valutazione appena aperta; rinominare un pannello, invece,
  incrementa ora la revisione del documento e non può più andare perso alla
  chiusura.
- **Packaging fail-closed.** Il confronto licenze ignora moduli della standard
  library e dati non importabili senza nascondere estensioni di terze parti; lo
  smoke frozen scrive il JSON su file anche con un eseguibile Windows senza
  console; un gate IBSI annullato non autorizza il package. Sui tag, versione,
  nome del ref, tag annotato e commit `HEAD` devono coincidere. L'inventario
  include anche il runtime PyInstaller e il file leggibile riproduce i testi di
  licenza/notice installati, invece di elencarne soltanto i percorsi.
- **Limite dell'export AI effettivo anche per un singolo campo enorme.** I
  testi controllati dal chiamante vengono troncati deterministicamente e il
  writer non restituisce mai un documento oltre `MAX_REPORT_BYTES`.
- **Identità sorgente sconosciuta dichiarata come tale.** Il pannello About non
  traduce più `revision=unknown` e `source_dirty=false` nell'affermazione non
  dimostrata “clean”.

- **Documentazione fuori data.** Revisione di tutti i documenti del progetto
  contro il codice: `OPERATIONS_SCHEMAS.md` descriveva la ricevuta schema 2
  dopo il rilascio della 3; la citazione nel README era ferma a "Version
  1.0.0" mentre `about.citation_text()` produceva già `1.1.0.dev0`; la sezione
  Test del README dichiarava una sola versione di Python dopo l'aggiunta della
  matrice; `requirements/packaging-base.txt` aveva perso la dipendenza
  `packaging` aggiunta all'extra corrispondente. `benchmarks/README.md`
  ignorava schema, `--compare` e la separazione dei due casi di export, e
  invocava PowerShell in un repository sviluppato su Linux.
  `packaging/LEGGIMI-TESTER.txt`, che viaggia dentro il bundle, spiegava
  SmartScreen e Gatekeeper ma non la terza cosa che un tester incontra: LPIPS
  e DISTS non disponibili al primo avvio. `CITATION.cff` portava la data di
  rilascio della 1.0.0 accanto alla versione `1.1.0.dev0`, affermando un
  rilascio mai avvenuto. Le note storiche in `benchmarks/` sono ora dichiarate
  tali.
- **Nuovi test contro il ritorno della deriva**: `tests/test_documentation_currency.py`
  (17), il mirroring `requirements/*.txt` ↔ `pyproject.toml` e la coerenza
  README ↔ `about.py`, che il README affermava già essere verificata e non lo era.

- **Esiti delle metriche persi attraverso la cache all-pairs.** Il valore e la
  sua causa viaggiavano su canali diversi — il valore al chiamante, la causa in
  un thread-local. In uno sweep N-immagini la cache calcola una metrica
  single-image una volta e la passa a ogni coppia: chi riusava un `None`
  cacheato non aveva nulla da riportare, e `unavailable`, `invalid` e `failed`
  finivano tutti come "non applicabile". Ora la computazione e il suo esito sono
  un unico oggetto immutabile. **Nessun valore numerico cambia.**
- **Export AI troncato solo a parole.** Oltre 200 righe il writer eliminava le
  *colonne* ed emetteva comunque ogni riga, dichiarando il report troncato: una
  analisi da 10.000 righe produceva un file da 10.000 righe che si diceva breve.
  Ora il limite è sulle righe, con selezione deterministica round-robin per
  metrica, budget byte esplicito, conteggi totale/emesso/omesso, digest
  sull'intero risultato e scrittura atomica.
- La lista dei progetti recenti è davvero best effort: un profilo utente non
  scrivibile costa la scorciatoia, non l'apertura del progetto.
- `Image.fromarray(..., mode=...)`, deprecato e rimosso in Pillow 13, eliminato
  ovunque il tipo dell'array determini già il modo. Output byte-identico.
- Nessun temporaneo lasciato dietro da uno ZIP fallito o annullato.

### ⚠️ Changes affecting numerical results

- **Serie di file DICOM Enhanced a frame singolo** (per esempio un multi-frame diviso in file):
  posizione, orientamento e rescale stanno nei functional group, che la lettura di una serie da
  cartella ignorava. Le slice venivano impilate per Instance Number, senza posizioni, e il range
  dei dati era calcolato senza il rescale per frame. Ora la lettura della serie legge anche i
  functional group (`qicom/io/dicom_io.py`) e la cartella si carica come il multi-frame da cui
  proviene. **Per queste serie possono cambiare l'ordine delle slice, il data range (e con esso
  PSNR e SSIM) e la geometria.** Le serie con posizione e orientamento in testa non cambiano.
- **Radiomica su DICOM multi-frame con i frame fuori ordine.** I pixel erano ordinati lungo la
  normale, ma origine e spaziatura tra le slice erano lette nell'ordine del file: frame a z = 2,
  0, 3, 1 mm davano origine 2 mm e spaziatura 0,333 mm invece di 0 e 1 mm. Ora il loader
  consegna le posizioni nello stesso ordine dei pixel e la geometria le usa
  (`qicom/io/dicom_io.py`, `qicom/radiomics/io.py`). **Cambiano tutte le feature dipendenti dalla
  geometria (forma, texture dopo il ricampionamento) per i volumi multi-frame memorizzati fuori
  ordine.** La copia NIfTI di un progetto salvato prima porta la geometria sbagliata e non può
  essere corretta da sola: reimportare la sorgente DICOM. Un volume con slice mancanti o
  irregolari (un passo che si discosta di oltre l'1% dalla spaziatura media) è ora rifiutato
  invece di ricevere una spaziatura media che non descrive nessuna slice, e così una pila con le
  slice spostate nel proprio piano (gantry inclinato, pila tagliata) oltre 0,01 mm: proiettare le
  posizioni sulla normale nascondeva lo spostamento. Anche un multi-frame con frame in
  orientamenti diversi è rifiutato: non è un volume.
- **RIQE in CLI e batch su stack non assiali**: il valore non compare più (esito
  `invalid/degenerate-input`), come già nella GUI.
- **Hash di configurazione uguale in GUI, CLI e batch.** Lo stesso calcolo aveva un hash diverso
  in ciascuno: la GUI registrava il modello di riferimento NSS e le unità dichiarate, il batch le
  unità solo se HU, batch e CLI le gamme per ruolo anche senza metriche che le leggano. Ora un solo
  costruttore (`qicom/metrics/run_configuration.py`) aggiunge ogni chiave solo per le metriche
  che la usano; il modello NSS, letto da una cache su disco, è letto solo se BRISQUE o NIQE sono
  richiesti, così una cache inaccessibile non blocca più un calcolo di sola MSE. I valori calcolati non cambiano; **cambia l'hash di configurazione delle ricevute
  CLI e batch** (e di quelle GUI con `data_range` intero, ora registrato come numero decimale).
- Il profilo quantitativo `complete` passa alla versione 1.2: include la nuova metrica `riqe`
  (era passato alla 1.1 per includere `brisque_canonical` e `niqe_canonical`). Nessun altro
  profilo cambia.
- **HaarPSI su una coppia piatta.** Nel runner una coppia costante viene normalizzata a zeri e
  piq restituiva +inf, registrato come HaarPSI valido. Ora è un input degenere
  (`invalid/degenerate-input`) e il valore non compare.
- **RIQE solo su slice assiali e su NIfTI che sono davvero CT.** Ogni NIfTI era considerato in HU:
  un volume MR veniva mostrato in HU e passato a RIQE. Inoltre un NIfTI in ordine RAS veniva
  affettato sagittalmente lungo l'asse 0 e valutato come se fosse assiale. Ora un NIfTI conta come
  CT solo se i suoi valori arrivano all'aria (≤ −500), e RIQE rifiuta le slice che la geometria del
  file (affine NIfTI o ImageOrientationPatient DICOM) indica come non assiali. Il nuovo campo
  `slice_orientations` entra nella configurazione, e quindi nell'hash, delle sole analisi con RIQE.
- **`evaluate_acceptance` giudica la coppia, non la prima ROI.** La tabella dei risultati elenca
  le ROI prima della coppia, e la regola leggeva la prima riga del metric_id: giudicava la ROI 1
  (SSIM 0,811) al posto dell'immagine (0,982). Con più ROI e nessun risultato sull'immagine intera
  la regola resta non valutata e lo dice.
- **CV e SNR su intensità con segno.** Con valori con segno (TC in HU) una ROI in grasso o aria
  ha media negativa, e CV (`std/media`) e SNR (`media/std`) diventavano negativi: fuori dal dominio
  dichiarato `[0, +inf)`, e un CV negativo risultava "migliore" di qualunque positivo. Ora entrambi
  usano il valore assoluto della media. **Cambiano i valori di `cv` e `snr` solo per ROI con media
  negativa.** Le ricevute registrano le nuove versioni di formula (`roi_cv_snr`,
  `masked_spatial_support`, `stack_summary`, `canonical_no_reference_range`).

- **Ordine delle slice negli stack di bitmap.** I file erano ordinati come stringhe, quindi
  `slice10` e `slice11` finivano tra `slice1` e `slice2`: il volume risultava rimescolato e due
  serie con convenzioni di nome diverse venivano accoppiate slice per slice in ordine diverso. Ora
  l'ordine è naturale (i numeri nei nomi contano come numeri). **Cambiano i risultati per slice e
  volumetrici degli stack di bitmap con numerazione senza zeri iniziali e almeno 10 slice.**

- **`data_range` predefinito di CLI e batch.** GUI, CLI e batch avevano tre copie della regola,
  e quella di CLI e batch non aveva il minimo di 1 della GUI: due immagini float in [0, 0.5]
  davano PSNR diversi di circa 6 dB secondo l'entry point. Ora tutte usano
  `combined_default_data_range`. **Cambiano i valori dipendenti da `data_range` (PSNR, SSIM, ...)
  di CLI e batch solo quando lo span congiunto delle due immagini è inferiore a 1 e non era
  indicato `data_range`.** Le ricevute batch registrano ora anche il profilo quantitativo, come
  quelle della GUI; l'aiuto del CLI descrive la regola reale.

- **SSIM, EPI e HFEN con "escludi l'aria".** Senza maschera le tre metriche sono native 3D su uno
  stack (finestra 7×7×7, filtri 3D); con la maschera diventavano in silenzio la media di valori 2D
  per slice, una grandezza diversa sotto lo stesso ID (con una maschera che non escludeva nulla
  HFEN raddoppiava). Ora con la maschera si calcola la stessa mappa 3D e la si restringe ai voxel di
  tessuto la cui intera finestra cade nella maschera: una maschera che copre tutto restituisce
  esattamente il valore senza maschera. Come senza maschera, il risultato è un valore per volume,
  senza distribuzione per slice, e la SSIM 3D di uno stack con meno di 7 slice è non valida.
  **Cambiano i valori di `ssim`, `epi` e `hfen` di ogni analisi con l'aria esclusa.** Le analisi
  senza esclusione e le ROI non cambiano.

- **BRISQUE e NIQE canonici normalizzano ogni immagine con il proprio range.** GUI, CLI e batch
  ricavano il `data_range` predefinito da entrambe le immagini, e la normalizzazione dei punteggi
  canonici lo usava per tutte e due: un partner più luminoso cambiava il punteggio del
  riferimento, contro quanto dichiarato nella metodologia. Ora ogni ruolo usa il range della
  propria immagine (quello riportato dal suo loader), registrato nella configurazione sotto
  `no_reference_data_range` e nella normalizzazione della ricevuta. **Cambiano i valori di
  `brisque_canonical` e `niqe_canonical` per ogni coppia in cui il range condiviso era più ampio
  di quello dell'immagine.** Le distanze NSS storiche (`brisque`, `niqe`), `blur` e RIQE non
  cambiano.

- **Ordine dei frame nei DICOM Enhanced multi-frame.** Le serie classiche sono ordinate per
  posizione lungo la normale al piano, mentre i frame di un oggetto Enhanced restavano nell'ordine
  del file (di acquisizione). Confrontando una serie classica con un Enhanced salvato nella
  direzione opposta, la slice i veniva confrontata con la N-1-i senza alcun avviso. Ora i frame
  sono ordinati con le posizioni dei gruppi funzionali (quando sono tutte distinte: i frame
  multi-eco o multi-fase che ripetono una posizione restano nell'ordine del file) e l'overlay
  riporta le posizioni per frame, così il controllo di coerenza della spaziatura vale anche per
  gli Enhanced. **Cambiano i risultati per slice e volumetrici di ogni Enhanced i cui frame erano
  memorizzati in ordine non crescente.**

- **Aggregazione qualitativa in cieco con più consegne.** Ogni consegna in cieco randomizza la
  propria presentazione (il seed deriva dal suo `delivery_id`), ma l'aggregazione applicava una
  sola mappa di de-blinding a tutte le risposte: i punteggi di ogni lettore tranne uno venivano
  attribuiti alle immagini sbagliate, e kappa, ICC, preferenze e AUC ne risultavano alterati. Ora
  ogni mappa si applica solo alle risposte della propria consegna (`apply_deblinding_maps`); le
  risposte di una consegna senza mappa, o di più consegne in cieco senza mappe, vengono rifiutate.
  Il dialogo di aggregazione accetta una mappa per consegna e il batch `aggregate-qualitative` il
  campo `deblinding_maps`. **Cambiano i risultati di ogni aggregazione multi-consegna con pannelli,
  pagine o domande randomizzati: va ripetuta.** Le aggregazioni di una sola consegna, o di
  pacchetti non in cieco, non cambiano.

- NMI uses float64 histogram counts instead of float32 histogram densities, removing bin-width
  rounding artifacts. Its receipt identifies `nmi: count-histogram-v2`; histogram bins and the
  entropy-ratio definition are unchanged. Recompute NMI when comparing with earlier reports.
- Masked SSIM and HFEN now erode ROI/tissue masks by the square filter footprint, including
  corner pixels. Diamond erosion allowed pixels outside irregular masks to affect the score.
  Receipts identify `masked_spatial_support: kernel-footprint-v2`; masked results may change
  or become unavailable when the complete kernel no longer fits the selected region.
- Radiomics CCC accuracy is computed directly as `2 * sd_x * sd_y / denominator`, so it remains
  defined for zero correlation and positive variance. CCC itself is unchanged; identical
  constant pairs retain an undefined accuracy term.
- Gwet AC2 v2 corrects weighted chance agreement and uses equally weighted nonempty-unit
  category proportions, including singleton units. Exported results include `ac2_method`.
- Deblinding maps questions and panel roles simultaneously from the original response labels,
  preventing swaps from collapsing distinct conditions and changing their aggregates.
- Qualitative descriptive response rates explicitly identify their denominator. With an
  assignment roster, completely absent responses are counted as missing; without one, the rate
  refers to received records and unrecorded missing assignments remain unknown.
- Task-based profile v2 corrects falling-edge rise distance and implements distinct PW, NPW,
  and NPWE expressions; NPWE requires an explicit eye filter. Detectability serialization v2
  preserves legacy v1 numerical values and identifies their effective PW formula. Nonzero edge
  angles are rejected because no slanted-edge correction is implemented.

- L'analisi qualitativa primaria passa dagli aggregati media/SD e kappa legacy a descrittive
  ordinali e alpha di Krippendorff; i confronti usano celle valutatore×caso, bootstrap a cluster
  e Holm. I fogli legacy restano disponibili, ma i nuovi risultati non sono numericamente
  confrontabili con report che trattavano le slice come osservazioni indipendenti.
- Il default della GUI quantitativa passa da tutte le metriche al profilo versionato Core rapido;
  le formule preesistenti non cambiano, ma una nuova sessione calcola un insieme più compatto se
  l'utente non seleziona un altro profilo.
- Gli aggregati di stack aggiungono mediana, IQR, MAD, P5/P95 e outlier Tukey; `value`, `std` e i
  CI legacy conservano la formula precedente.
- Le nuove metriche di errore condividono un solo residuo definito come `comparison - reference`;
  `residual_std` usa la deviazione standard di popolazione dopo la rimozione del bias.
- L'Indice QICOM è ora legato al profilo/versione e viene marcato parziale quando manca una
  componente obbligatoria; indici provenienti da profili/versioni diversi sono rifiutati come
  non confrontabili.
- Il profilo quantitativo Core rapido passa da 1.0 a 1.1 e include anche MSE; a parità di immagini
  le formule esistenti non cambiano, ma una nuova analisi Core produce una riga numerica in più.
- Il profilo radiomico predefinito passa a `qicom-ibsi-core@2.0`: il delta percentuale primario è
  ora quello simmetrico. `delta_percent` e `delta_classic_percent` conservano la formula classica,
  quindi gli export precedenti restano confrontabili usando quelle colonne.
- Le nuove normalizzazioni e resegmentation modificano intensità/maschera e quindi tutte le
  feature solo quando selezionate esplicitamente; restano disabilitate nel default. L'origine di
  bin `resegmentation-lower` cambia la discretizzazione solo se richiesta esplicitamente.
- L'unità esportata di `SurfaceVolumeRatio` è corretta da `mm²` a `mm⁻¹`; il valore numerico della
  feature non cambia.
- La regola di confronto IBSI passa da `official-rounded-difference-3-significant-digits` a
  `official-rounded-difference-3-significant-digits+absolute-near-zero-v2`
  (`reference_values.comparison_rule_version: 2` nel manifest del reference set). Un valore di
  riferimento pari a zero, o entro `relative_zero_threshold`, non ha una scala a tre cifre
  significative su cui troncare: la regola precedente ripiegava su `digits = 0` e troncava a zero
  *qualsiasi* differenza inferiore a 1.0, per cui `reference=0`, `calculated=0.4`, `tolerance=0`
  veniva riportato come PASS ufficiale. Ora vicino allo zero si confronta la differenza assoluta
  non troncata, quindi lo stesso caso è FAIL. L'errore relativo resta NA, con il motivo indicato
  esplicitamente nella riga della ricevuta. Il reference set ufficiale IBSI 1 (104 feature
  obbligatorie) non contiene valori di riferimento nulli: nessun verdetto pubblicato cambia, ma
  reference set con riferimenti nulli non possono più produrre un PASS silenzioso.

- L'alpha di Krippendorff ordinale passa alla versione 2 della formula (etichetta esportata
  `Krippendorff alpha (ordinal) v2`). La versione 1 usava una distanza quadratica fra ranghi
  uniformi, che ignora le frequenze marginali; la definizione ordinale corretta misura la massa
  delle categorie comprese fra le due, con metà della frequenza di ciascun estremo:
  `delta(c,k) = (somma_{g=c..k} n_g - (n_c + n_k)/2)^2`. Inoltre la disagreement osservata è ora
  pesata per unità con `1/(m_u - 1)` prima della media sui valori appaiabili: il conteggio
  aggregato delle coppie sovrappesava le unità con più valutatori, cioè proprio il caso creato
  dalle risposte mancanti. Su dati sbilanciati la differenza è ampia (esempio Likert asimmetrico:
  0.5272 → 0.2535; matrice canonica con mancanti: 0.8515 → 0.8049). I valori v1 non sono
  confrontabili. Gwet AC2 conserva i pesi quadratici dichiarati dal suo nome.

- I profili di accettazione verificano l'unità dichiarata dal risultato rispetto a quella della
  regola. Prima l'unità della regola veniva scritta nell'esito senza alcun controllo e il confronto
  usava il numero nudo: un risultato in `cm` poteva superare una soglia scritta in `mm`. Ora le
  unità devono coincidere letteralmente, senza alcuna conversione implicita; se non sono
  confrontabili la regola obbligatoria diventa `INCOMPLETE` e quella facoltativa `NOT-COMPARABLE`,
  entrambe con motivo tecnico. La tabella degli esiti aggiunge la colonna `result_unit` accanto a
  `unit`, così la discrepanza è visibile in GUI e negli export. Un risultato che non dichiara
  un'unità non viene più confrontato in silenzio.

- Le percentuali delle domande a scelta multipla sono ora quote dei rispondenti e non delle
  selezioni. Poiché un rispondente può spuntare più categorie, il totale delle selezioni supera il
  numero di rispondenti e la vecchia normalizzazione faceva sembrare ogni categoria più rara quanto
  più si spuntava: due rispondenti, uno con A+B e uno con A, davano A=67% e B=33% invece di A=100%
  e B=50%. `category_distribution` esporta ora anche `denominator` e `denominator_definition`.
  Risposte saltate, non valutabili e mancanti restano stati distinti ed escluse dal denominatore.
- Nuova `likert_distribution`: frequenze e percentuali per ciascuna categoria delle domande di
  rating, con denominatore dichiarato. È inclusa nell'export Excel qualitativo (foglio
  “Likert Distributions”) e nel batch qualitativo (`qualitative_likert_distribution.csv`).

## [1.0.0] — 2026-08-19

First release published under semantic versioning. QICOM provides three
independent workflows over DICOM, NIfTI and common 2D image formats:

- **Quantitative analysis** — full-reference metrics (MSE, MAE, RMSE, NMSE,
  PSNR, SSIM, MS-SSIM, FSIM, GMSD, HaarPSI, VIF, LPIPS, DISTS), no-reference
  indices (BRISQUE, NIQE, blur, edge/detail), ROI statistics (SNR, CNR, CV,
  mean/min/max), statistical measures (PCC, NMI), difference heatmaps with
  standard medical LUTs, line profiles, background-air exclusion, per-metric
  effort levels, and Excel export.
- **Qualitative analysis** — creation, blinded administration and aggregation
  of structured image evaluations, with inter-rater spread, reliability and
  AUC comparisons.
- **Radiomics** — paired 2D/3D feature extraction on reproducible,
  hash-identified profiles, semi-automatic segmentation, delta-radiomics and
  guarded ICC/CCC repeatability statistics.

All processing runs locally; see `SECURITY.md` for the security and privacy
model.
