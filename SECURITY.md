# Sicurezza di QICOM

QICOM è uno strumento desktop locale per ricerca e controllo qualità, non un dispositivo medico.
Il modello di sicurezza considera non fidati i file immagine, gli archivi progetto/valutazione,
i file di risposte e ogni testo che può finire in un report.

## Garanzie implementate

- Import ZIP fail-closed contro traversal, percorsi assoluti, link simbolici, zip bomb e archivi
  oltre i limiti configurati. Sono rifiutati anche i membri duplicati o uguali a meno di maiuscole,
  normalizzazione Unicode e punti o spazi finali, che su NTFS/APFS finirebbero nello stesso file;
  su Windows anche i nomi di dispositivo (`CON`, `LPT1`…) e i caratteri non ammessi. I limiti sono per tipo di pacchetto (progetto, valutazione,
  radiomica, coorte, workbook), perché una tabella di coorte e un progetto multi-serie non
  presentano lo stesso rischio e un tetto condiviso lascia il piccolo di fatto senza budget. Il
  contatore agisce sui byte **realmente estratti**, non sulle dimensioni dichiarate nell'archivio,
  ed è preceduto da un controllo dello spazio libero con margine: un rifiuto non lascia file
  parziali dietro di sé.
- Validazione strutturale di immagini, manifest e risposte prima del calcolo o dell'aggregazione.
  Le risposte accettate in aggregazione possono arrivare come JSON QICOM, XLSX o CSV: tutti e tre
  i formati sono trattati come non fidati, con limite di dimensione del file e di numero di
  record, e vengono confrontati con il pacchetto di domande prima di entrare nelle statistiche.
- Anonimizzazione DICOM predefinita negli export portabili, con coerenza degli UID e blocco dei
  file clinici che non dichiarano esplicitamente assenza di annotazioni identificative nei pixel.
  I soli Secondary Capture minimali senza attributi identificativi diretti sono accettati anche
  quando la dichiarazione manca, per supportare immagini JPG convertite in DICOM. Le regole del
  profilo PS3.15 sono applicate anche a ogni elemento delle sequenze annidate (gruppi funzionali
  dei multi-frame Enhanced, catene di derivazione, riferimenti allo studio sorgente); ogni UID di
  istanza, data, ora o nome di persona rimasto viene sostituito, e la copia esportata è verificata
  contro gli UID e le date dell'originale prima di dichiarare `PatientIdentityRemoved=YES`. I
  DICOMDIR non vengono anonimizzati: i loro record possono nominare pazienti nei percorsi.
  Il contenitore Part 10 è ricostruito, non ereditato (PS3.15): il preambolo di 128 byte è
  azzerato e il gruppo File Meta tiene solo gli elementi richiesti da PS3.10 (classe e istanza
  SOP, sintassi di trasferimento, identità dell'implementazione); `PrivateInformation`, AE title e
  indirizzi di origine non sopravvivono. La copia riletta è verificata anche su questo.
- Un file con suffisso NIfTI è riconosciuto solo se la struttura è coerente: magic di file
  singolo, dimensioni, datatype con dimensione del voxel nota, offset dei voxel oltre
  l'extender e payload presente per intero. Tutto ciò che sta tra header e voxel è azzerato, i
  byte dopo il payload sono eliminati; un file che non supera la verifica è rifiutato come gli
  altri non verificabili.
- Consegne in cieco: ogni immagine è de-identificata di nuovo per la consegna, e un override
  accettato dall'autore quando ha salvato il proprio pacchetto non si trasmette: le immagini
  rifiutate fermano la consegna prima di scrivere qualunque file, finché l'autore non sceglie
  esplicitamente, per quella consegna, di includerle. Il pacchetto del valutatore non contiene
  nulla che permetta di ricostruire la randomizzazione: il seed mescola un segreto derivato da
  una chiave casuale dell'autore, conservata privata nel suo profilo
  (`qualitative_blinding_key.json`), e seed, segreto e master seed stanno solo nella mappa;
  pagine e domande sono numerate nell'ordine di presentazione, e il design pubblico non riporta
  randomizzazione né ripetizioni nascoste. Le consegne create prima di questa versione
  contengono seed e master seed: chi le ha ricevute può ricostruire quale pannello era quale e
  quali pagine erano ripetizioni (vedi `CHANGELOG.md`).
- Le copie di JPG e PNG nei pacchetti perdono EXIF, XMP, commenti e chunk di testo senza perdita
  di qualità (stessi dati compressi, stesso nome e formato); il testo impresso nei pixel resta
  fuori dalla portata di qualunque rimozione di metadati.
- Scritture atomiche per archivi/JSON e transazioni SQLite per analisi, risultati, ROI e note.
- Protezione da formula injection negli export Excel e CSV (le celle di testo che iniziano con
  `=`, `+`, `-` o `@` ricevono un apostrofo iniziale) e nomi foglio validi/univoci.
- Due soli accessi di rete, entrambi avviati esplicitamente dall'utente e mai da un'analisi: il
  download dei pesi learned (sotto) e lo scaricamento delle risorse ufficiali IBSI per la
  "Verifica IBSI" (raw.githubusercontent.com e ibsi.radiomics.hevs.ch), con SHA-256 e dimensione
  fissati nel manifest e conferma prima del download.
- Nessun download implicito di pesi learned durante un'analisi; i checkpoint ammessi sono
  verificati tramite SHA-256 **e** dimensione fissata. Ogni trasferimento è limitato in due modi
  indipendenti: `Content-Length` quando il server lo dichiara, e un contatore sui byte ricevuti —
  un server disposto a mandare il file sbagliato è disposto a dichiararne la lunghezza sbagliata.
  Un sovraccarico, un digest errato o un'interruzione eliminano il temporaneo e lasciano intatta
  la copia già verificata. L'hash di un checkpoint è ricontrollato a ogni caricamento del modello,
  non una volta per sessione.
- Ogni risultato porta nella ricevuta il commit esatto da cui è stato prodotto (`build_revision`)
  e se quel checkout era modificato (`source_dirty`): una versione nomina una release, non
  identifica il software che ha calcolato i numeri.

## Limiti operativi

- Gli ZIP quantitativi conservano i nomi originali dei file. Gli header NIfTI sono ripuliti come
  nei pacchetti qualitativi (campi di testo, estensioni, nome e data nell'header gzip), ma i nomi
  dei file e il testo libero dei manifest richiedono una verifica prima della condivisione.
- `BurnedInAnnotation=NO` è una dichiarazione del produttore DICOM, non una verifica OCR dei
  pixel. Prima di condividere dati reali resta necessaria la procedura privacy dell'organizzazione.
- Nei Secondary Capture minimali accettati senza `BurnedInAnnotation`, QICOM annota nel metodo di
  de-identificazione che il contenuto dei pixel non è stato verificato. L'assenza di tag anagrafici
  non garantisce che il JPG originario non contenga testo identificativo visibile.
- Quando un'immagine non si può anonimizzare, per qualunque motivo, l'export non scrive nulla:
  QICOM raccoglie tutte le immagini interessate e dice all'utente quali sono e perché, e che può
  comunque procedere senza anonimizzazione per quelle immagini (Annulla resta il pulsante
  predefinito). Solo questa scelta esplicita le esporta: anonimizzate al meglio ma **non
  verificate**, con `PatientIdentityRemoved=NO` e un `DeidentificationMethod` che nomina
  l'override, elencate nel registro errori; un file che QICOM non riesce a leggere o riscrivere
  esce invariato. La responsabilità della condivisione passa all'utente. I motivi mostrati e
  registrati nominano gli attributi, mai i valori identificativi.
- Il report errori (finestra Errori, Aiuto ▸ Segnala un bug) è un file di testo scritto solo su
  richiesta e mai inviato da QICOM. Contiene i messaggi di errore scelti, i dati di build e la coda
  del log applicativo; i messaggi possono citare nomi di file, quindi va letto prima dell'invio.
- I file immagine sono decodificati da librerie native di terze parti. Aggiornare regolarmente le
  dipendenze, eseguire `pip-audit` e ricostruire gli eseguibili dopo gli aggiornamenti di
  sicurezza. La CI esegue `pip-audit` sia sull'ambiente di test sia su quello che produce il
  bundle, e aggiorna l'installer prima di qualunque dipendenza: è pip a scaricare ed eseguire i
  build backend, quindi fissare le versioni di ciò che installa non serve se l'installer stesso
  è vulnerabile.
- Il JPEG lossless e il JPEG-LS dei DICOM sono decodificati da GDCM (`python-gdcm`), che incorpora
  copie proprie, più vecchie di quelle di Pillow, di libjpeg 6b, CharLS e OpenJPEG 2.3. QICOM gli
  affida solo le sintassi che Pillow non decodifica: JPEG baseline e JPEG 2000 restano a Pillow
  (`qicom/io/dicom_io.py`). Il wheel Linux include anche OpenSSL 1.1.1k (build RHEL 8, ramo non
  più supportato upstream; su macOS è OpenSSL 3.5, su Windows è assente), collegato al nucleo di
  GDCM e quindi caricato con esso, anche se QICOM usa GDCM solo per decodificare pixel e non apre
  connessioni. `pip-audit` non vede queste librerie native: un avviso di sicurezza su GDCM o sulle
  librerie che incorpora va seguito a mano, aggiornando `python-gdcm` e rigenerando i lock.
- Gli artefatti CI non sono firmati. Una distribuzione pubblica deve aggiungere Authenticode su
  Windows e firma/notarizzazione Apple su macOS usando credenziali conservate nel sistema CI —
  vedi [packaging/DISTRIBUTION.md](packaging/DISTRIBUTION.md) per la sequenza esatta e per il
  motivo per cui la firma va inserita **prima** del calcolo dei checksum.
- Ogni bundle è accompagnato dall'inventario delle licenze (incluso il runtime PyInstaller e con
  i testi di licenza/notice riprodotti), da un SBOM CycloneDX e dal manifest delle versioni e
  origini VCS realmente installate sulla piattaforma di build, tutti coperti dallo stesso
  `SHA256SUMS`. Il manifest dell'ambiente non è un lock dei wheel con hash; la build fallisce se
  contiene una distribuzione che l'inventario non descrive.
- Ogni bundle CI contiene `SHA256SUMS` per i file regolari e viene pubblicato insieme al checksum
  SHA-256 dell'archivio. Questi valori identificano la build e permettono di confrontarla con
  quella approvata, ma non autenticano il publisher: finché manca una firma attendibile devono
  essere ottenuti o confermati tramite un canale aziendale fidato.
- Utenti valutatore (Esegui valutazione): le valutazioni sospese e concluse di ogni utente sono
  cifrate con AES-256-GCM, a ogni salvataggio automatico e non solo alla sospensione, con una
  chiave casuale dell'utente. Quella chiave è conservata cifrata con una chiave derivata dalla
  password (scrypt, N=2^17, r=8, p=1); la password non è salvata. Ogni file è legato al suo utente
  e al suo nome: copiato nella cartella di un altro, o scambiato, non si apre. Dentro QICOM un
  utente vede, riprende e rimuove solo le proprie valutazioni.
  - Limiti: la cifratura impedisce di leggere o alterare senza che ce ne si accorga, non di
    cancellare. Chi condivide l'account del sistema operativo può eliminare i file degli altri
    dalla cartella dati; contro questo servono account del sistema operativo separati.
  - La password minima è di 5 caratteri, senza requisiti di complessità, per scelta. Chi copia i
    file su un altro computer può provare password senza il ritardo della finestra di accesso:
    scrypt rende ogni tentativo costoso, ma una password corta resta indovinabile.
  - Nessun recupero della password: dimenticarla rende illeggibili le valutazioni sospese di
    quell'utente (il pacchetto si può rifare da capo).
  - Restano in chiaro gli ID utente, il report e il file risposte che il valutatore salva per
    l'autore, e le immagini del pacchetto estratte temporaneamente durante la valutazione. La
    chiave resta in memoria finché la finestra di valutazione è aperta.
  - Senza accesso («Continua senza accedere») non si conserva nulla su disco: niente sospensione né
    ripresa, e chiudere prima di salvare il report perde le risposte, dopo una conferma.
  - Le bozze in chiaro di versioni precedenti non vengono assegnate a nessuno in automatico: le
    recupera solo chi, dopo l'accesso, riapre lo stesso pacchetto e conferma che sono sue. Sono poi
    cifrate nel suo utente e la copia in chiaro viene cancellata; su SSD la cancellazione di un
    file non garantisce che i blocchi precedenti spariscano dal disco.
- Codice di accesso (facoltativo, passo Pubblica): la copia da inviare ai valutatori, la consegna
  in cieco e la sua mappa di decodifica sono cifrate (AES-256-GCM, chiave derivata dal codice con
  scrypt) e si aprono, si modificano e si aggregano solo con il codice, dentro QICOM e fuori. Le
  risposte salvate per un pacchetto protetto sono cifrate con lo stesso codice.
  - Il codice è unico per tutti i valutatori: stabilisce chi può aprire la valutazione, non chi ha
    risposto, e chi lo conosce può inoltrarlo.
  - Minimo 10 caratteri; QICOM ne propone uno generato di 16 (circa 79 bit). Chi ha il file può
    provare codici sul proprio computer senza limiti: un codice breve o prevedibile resta
    indovinabile.
  - Nessun recupero: perso il codice, i file protetti non si aprono più (la copia di lavoro
    dell'autore resta senza codice).
  - Va comunicato su un canale diverso da quello del file.
  - Durante la valutazione o l'aggregazione il pacchetto è decifrato in una cartella temporanea,
    eliminata alla chiusura; un arresto improvviso può lasciarla sul disco.
- Strumenti di utilità. Leggono le sorgenti e non le modificano mai: ogni risultato è una copia, e
  un insieme di file compare nella cartella di destinazione tutto insieme o per niente; la
  cartella deve essere nuova o vuota, reale (non un collegamento) e diversa da quella delle
  sorgenti. Le scansioni di cartelle non seguono i collegamenti.
  - Ispezione e confronto dei tag mostrano l'header così com'è, dati del paziente compresi, e
    l'export CSV della tabella **non è anonimizzato**. Le celle di ogni CSV sono protette dalla
    formula injection.
  - L'editor di tag scrive copie in una cartella nuova; i tag che definiscono i pixel e il gruppo
    0002 non si modificano, quelli che cambiano le misure chiedono conferma. Il registro delle
    modifiche contiene i nuovi valori e gli hash, mai i valori precedenti. Una copia modificata
    resta identificabile quanto la sorgente, salvo ciò che l'utente ha cambiato.
  - L'anonimizzatore applica le stesse regole fail-closed degli export (sopra): un file che non
    si può verificare ferma tutto prima di scrivere, e solo la scelta esplicita dell'utente lo
    scrive come non verificato. La copia mantiene l'albero delle cartelle e i nomi dei file
    dell'originale, in `<cartella>_anonymized`: i nomi **non sono de-identificati**, e una
    cartella o un file che porta il nome di un paziente lo porta anche nella copia. Lo strumento
    lo scrive prima di procedere; i nomi vanno controllati prima di condividere la copia. Per
    le esportazioni i cui nomi identificano il paziente, un'opzione sostituisce cartelle e file
    con nomi generici (`folder_001`, `image_0001.dcm`, ...) in una cartella `anonymized`, senza
    tabella di corrispondenza.
  - La copia organizzata dei DICOM nomina le cartelle per ordine di studio, numero di serie e
    modalità, mai per paziente, ma i file **non sono anonimizzati**.
  - La decompressione DICOM riscrive l'header completo in ogni copia, dati del paziente compresi:
    le copie **non sono anonimizzate**, e la cartella prende il nome del file sorgente. I pixel
    sono decodificati dagli stessi decoder del loader (Pillow, poi GDCM) e i limiti di sicurezza
    di GDCM descritti sopra valgono anche qui.
  - Conversioni, ricampionamenti, degradazioni, risultati aritmetici, maschere e segmentazioni
    portano solo pixel e geometria: nessun campo dell'header né testo della sorgente. La
    conversione crea la sua cartella con il nome dell'immagine sorgente (`<nome>_<formato>`).
- Il database locale e i progetti non sono cifrati a riposo. Usare cifratura del disco e controlli
  di accesso del sistema operativo quando contengono dati sensibili.

## Segnalazione

Non includere immagini cliniche, dati personali, chiavi o credenziali in una segnalazione. Fornire
una riproduzione minima con dati sintetici e la versione di QICOM e del sistema operativo.
