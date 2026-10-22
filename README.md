# QICOM — Quality Image Comparator

Applicazione desktop con tre flussi separati per immagini mediche: analisi quantitativa,
valutazione qualitativa e radiomica pairwise 2D/3D su DICOM/NIfTI; accanto, una raccolta di
strumenti di utilità (segmentazione, maschere, conversione, header DICOM, anonimizzazione).

> **Solo per uso di ricerca e controllo qualità.** QICOM non è un dispositivo medico e non è
> destinato alla diagnosi clinica né ad alcuna decisione riguardante la cura di un paziente.
> Questo avviso è mostrato anche nella finestra di avvio dell'applicazione
> (`qicom/gui/widgets/launcher_dialog.py`) e in **Aiuto ▸ Informazioni su QICOM**.

QICOM supports DICOM® medical images. DICOM® is the registered trademark of the National
Electrical Manufacturers Association (NEMA) for its standards publications relating to digital
communications of medical information. QICOM is an independent software project and is not
affiliated with, sponsored by, or endorsed by NEMA or the DICOM Standards Committee.

Ideato e sviluppato da Fabio Mattiussi a Lugano, Svizzera. Licenza [Apache-2.0](LICENSE).

## Documentazione

| Per | Vai a |
| --- | --- |
| Avviare e usare QICOM | [Avvio della GUI](#gui), [guida per i tester](packaging/LEGGIMI-TESTER.txt) |
| Capire le analisi | [Metriche](#metriche-implementate), [metodologia qualitativa](QUALITATIVE_METHODOLOGY.md), [metodologia radiomica](RADIOMICS_METHODOLOGY.md) |
| Condividere risultati e conoscere i limiti | [Export AI](#export-per-assistente-ai), [sicurezza e privacy](SECURITY.md), [uso previsto](#uso-previsto) |
| Consultare le novità | [Changelog](CHANGELOG.md) |
| Sviluppare QICOM | [Installazione](#installazione-sviluppo), [formati dei dati](OPERATIONS_SCHEMAS.md), [test](#test), [benchmark](benchmarks/README.md) |

## Installazione (sviluppo)

Servono **Python >=3.11** e, per la GUI, **PySide6-Essentials >=6.6,<7 (Qt 6)**.
La CI copre Python 3.11/3.12; i bundle usano 3.12 e includono l'interprete.
I target dei bundle sono Windows x64, macOS 14 Apple Silicon e Linux x64.
Windows 10 dal 1809/Windows 11 sono target attesi, non due ambienti desktop
verificati separatamente dal runner Windows Server. Su Linux il bundle dipende
anche dalla glibc e dalle librerie del sistema su cui viene costruito.
Minimi delle dipendenze, piattaforme testate e limiti delle prove sono distinti in
[Compatibilità e matrice di verifica](packaging/DISTRIBUTION.md#compatibility-boundary).
I profili hardware non abbassano questi requisiti e non esiste un fallback Qt 5.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Prima di qualsiasi dipendenza: è pip a scaricare ed eseguire i build backend,
# quindi è la prima cosa che deve essere affidabile. Fissare le versioni di ciò
# che installa non serve se l'installer stesso è vulnerabile.
python -m pip install --upgrade "pip>=26.2"

pip install -e ".[dev]"          # core + strumenti di sviluppo
pip install -e ".[gui]"          # + interfaccia grafica
pip install -e ".[radiomics]"   # + backend PyRadiomics del modulo dedicato
pip install -e ".[radiomics,mirp]"  # + backend MIRP opzionale per IBSI 1 completo
pip install -e ".[learned]"      # + metriche learned (LPIPS, DISTS, piq) — pesante, opzionale
```

`packaging/generate_constraints.py` registra il manifest esatto dell'ambiente
installato che PyInstaller sta per congelare: versioni complete (inclusi tag
locali come `+cpu`) e, per dipendenze VCS come PyRadiomics, repository e commit.
Non esegue una seconda risoluzione che potrebbe usare un indice diverso da
quello della build. Il file è evidenza della build, **non** un lock degli
artefatti con hash e non va usato con `--require-hashes`; l'integrità dei file
pubblicati è coperta separatamente da `SHA256SUMS`. Il workflow lo genera su
ciascuna piattaforma e lo pubblica accanto al bundle.

Per le metriche learned, su tutte le piattaforme è consigliata la build CPU-only di torch:

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
python packaging/prepare_learned_weights.py
```

Il secondo comando è intenzionalmente esplicito: scarica AlexNet/VGG16/DISTS, verifica per
ciascun file lo SHA-256 atteso e lo installa nella cache Torch. QICOM non scarica mai modelli
durante un'analisi; se un checkpoint manca o è alterato, la metrica viene omessa senza accesso
alla rete.

**I checkpoint non sono inclusi in nessun bundle**, learned compresi: pesano circa 760 MB, più
del resto dell'applicazione, e la policy è "su richiesta, con consenso esplicito". Un bundle
learned contiene le librerie (torch, torchvision, lpips, piq) e quindi le cinque metriche che non
usano pesi (MS-SSIM, FSIM, GMSD, VIF, HaarPSI) funzionano subito; LPIPS e DISTS restano
indisponibili finché l'utente non prepara i pesi, dal pulsante di download nella finestra
"Seleziona metriche" oppure con `python packaging/prepare_learned_weights.py`. Al primo avvio,
con la cache vuota, LPIPS e DISTS vengono quindi omesse con una causa dichiarata e senza alcun
accesso di rete: il workflow di packaging verifica esattamente questo comportamento sul bundle
congelato (`--smoke-test`, campo `learned_policy`).

In alternativa agli `extras` di `pyproject.toml`, sono disponibili file equivalenti in
`requirements/` (`base.txt`, `gui.txt`, `radiomics.txt`, `learned.txt`, `dev.txt`,
`packaging-base.txt` e `packaging.txt` per il profilo full).

### Requisiti hardware pratici

- Minimo pratico: 8 GB di RAM e 4 core logici per immagini 2D e stack moderati.
- Consigliato: 16 GB di RAM e 6–8 core logici per stack DICOM, confronti multipli e radiomica.
- Carichi pesanti: 32 GB di RAM per grandi volumi 3D, molte ROI o metriche learned. Le metriche
  learned restano opzionali e scaricano i checkpoint solo su richiesta esplicita.

Le metriche learned usano: `lpips` (ufficiale, backbone AlexNet) per LPIPS; `piq` per
MS-SSIM/FSIM/GMSD/VIF/DISTS. Tutte le funzioni in `reference_structural.py` importano
queste librerie in modo lazy: il modulo resta importabile (e SSIM resta utilizzabile)
anche senza l'extra `learned` installato; le singole funzioni che ne hanno bisogno
sollevano `ImportError` solo se effettivamente chiamate senza la libreria disponibile.

NSS feature distance e NSS patch-distribution distance (in `no_reference.py`) **non** fanno parte dell'extra `learned`: sono una
reimplementazione propria di QICOM delle statistiche di scena naturale (NSS) di Mittal et
al., scritta da zero con solo numpy/scipy (dipendenze base), senza dipendere da `pyiqa`.
Non includendo alcun modello pre-addestrato o dataset di terze
parti, il modello di riferimento "immagine naturale" usato di default è adattato su
immagini generate sinteticamente con spettro 1/f (`qicom/metrics/nss_reference.py`); per un
uso calibrato su un proprio corpus di riferimento, passare un `reference_model` ottenuto da
`fit_reference_model()`. I valori restituiti sono quindi un indice relativo, non
confrontabili con la scala BRISQUE/NIQE originale.

Sono disponibili anche **BRISQUE e NIQE canonici**, come metriche aggiuntive con ID
`brisque_canonical` e `niqe_canonical`. BRISQUE usa PIQ 0.8.0; NIQE è implementato
in QICOM a partire dall'articolo originale. I modelli ufficiali si scaricano solo
su richiesta da Seleziona metriche, con dimensione e SHA-256 verificati. In loro
assenza l'outcome è `unavailable-backend`: l'analisi non accede alla rete.
Si riportano due punteggi assoluti, uno per immagine, con valori inferiori migliori.
Queste metriche valutano l'immagine intera anche con esclusione dello sfondo attiva.
Definizioni, normalizzazione e verifiche sono in
[NO_REFERENCE_METHODOLOGY.md](NO_REFERENCE_METHODOLOGY.md); l'inventario corrente è
[metric_inventory.csv](metric_inventory.csv), distinto dall'inventario congelato dell'articolo
che descrive QICOM.

NIQE è stimato su fotografie e su immagini TC può classificare come più "naturale"
un'immagine più rumorosa. Per la TC è disponibile **RIQE** (`riqe`, Radiology Image
Quality Evaluator, modello `riqe-v1.0`,
[doi:10.5281/zenodo.23055559](https://doi.org/10.5281/zenodo.23055559)): la stessa
costruzione di NIQE con una mappatura HU fissa, maschere di campo di vista e corpo e un
modello stimato su TC a dose piena. Il modello (CC BY 4.0) è incluso nel pacchetto e
verificato con SHA-256: nessun download. RIQE valuta solo immagini dichiarate in unità
Hounsfield (in GUI: DICOM con Modality CT o NIfTI; in CLI `--hounsfield-units`) e rifiuta
slice senza aria (meno dell'1% del campo di vista ≤ −900 HU), quindi RM, bitmap o un NIfTI
non TC non ricevono punteggio. Come
nella sua validazione, valuta l'immagine di confronto sulle maschere del riferimento.
Serve a classificare un'immagine degradata rispetto alla sua sorgente entro lo stesso
protocollo; non va usato da solo per scegliere, confrontare o regolare un denoiser.

## Metriche implementate

| Modulo | Metriche | Note dimensionali |
|---|---|---|
| `reference_error` | MSE, MAE, RMSE, NMSE, PSNR, bias (`comparison - reference`), SD residua, mediana/P5/P95 dell'errore e dell'errore assoluto | native 3D; i riepiloghi voxel-wise sono descrittivi |
| `reference_statistical` | NMI, PCC, Wasserstein-1 | native 3D; Wasserstein-1 ignora la posizione spaziale |
| `edge_detail` | EPI, HFEN | native 3D |
| `reference_structural` | SSIM (native 3D) · MS-SSIM, FSIM, GMSD, VIF, HaarPSI, LPIPS, DISTS (slice-wise su 3D) | vedi vincoli di dimensione minima nelle schede (SSIM ≥7 per asse, MS-SSIM ≥161×161, HaarPSI ≥16×16) |
| `no_reference` | NSS feature distance, NSS patch-distribution distance (slice-wise su 3D, reimplementazione propria numpy/scipy) · Blur/VoL (native 3D, solo scipy) | La distanza NSS per patch richiede immagini ≥96×96; nessuna dipendenza dall'extra `learned` |
| `brisque_canonical`, `niqe_canonical` | BRISQUE (PIQ), NIQE (QICOM con MVG pristine LIVE) | slice-wise su 3D; BRISQUE ≥14×14 non costante, NIQE almeno due patch valide 96×96; modelli scaricati esplicitamente |
| `riqe` | RIQE (modello TC `riqe-v1.0`, incluso) | slice-wise su 3D; solo TC in HU dichiarate; almeno 72 patch 24×24 valide nel corpo; il confronto usa le maschere del riferimento |
| `roi_based` | SNR, CNR, Noise SD, Noise Var, CV, Mean, Min, Max | maschere ROI passate esplicitamente |

`run_all_metrics(img1, img2, roi_masks=None, config=None)` in `qicom/metrics/runner.py`
calcola tutte le metriche applicabili e restituisce un `pandas.DataFrame` con colonne
`metric_id, metric_name, category, image, scope, value, std, n_slices, better_direction,
target_value` — `scope` distingue sempre "per-slice" da "stack-aggregate".

Gli input numerici non finiti vengono rifiutati prima del calcolo. Le metriche non definite su
input degeneri (per esempio PCC/EPI su immagini costanti) vengono omesse, senza produrre NaN nel
report; gli errori di programmazione non sono invece nascosti come semplici metriche mancanti.

### Esclusione dell'aria di sfondo

In una slice tipica la maggior parte dei pixel è l'aria attorno al paziente: mediarla dentro una
metrica sull'immagine intera significa misurare in buona parte lo sfondo (l'errore risulta
diluito, SSIM/PSNR spinti verso "identiche" perché aria coincide con aria). `qicom/metrics/
background.py` costruisce la maschera dei tessuti (`build_tissue_mask`); l'aria *interna* al corpo
(polmone, gas intestinale) resta inclusa, perché a essere esclusa è solo l'aria attorno al
paziente.

Nella GUI il flusso è guidato in tre passi ed è semi-automatico più manuale: si crea un unico
**cerchio verde** (`DualImageView.add_air_roi`) e lo si posiziona su una zona di solo sfondo — è
l'unico punto da cui viene letto il livello dell'aria, mai una ROI di misura disegnata prima (una
ROI di rumore sta nell'aria di proposito, una di segnale mai); `Rileva aria` trasforma quel
campione in una soglia e colora subito sulle immagini tutto ciò che verrebbe escluso, con
tolleranza e soglia esatta modificabili; una **gomma** in stile paint rimette nella misura ciò che
la soglia ha preso per errore (tessuto scuro quanto l'aria), con `Annulla correzioni` per tornare
al risultato rilevato.

`run_all_metrics(..., tissue_mask=...)` la applica secondo cosa ciascuna metrica è davvero:

- metriche pixel-indipendenti (`reference_error`, `reference_statistical`): ricalcolate
  esattamente sui soli pixel selezionati, slice per slice (media/std/intervallo restano quelli
  della pila);
- SSIM/EPI/HFEN: solo i pixel il cui intero supporto del kernel cade dentro la maschera, come già
  avviene per le righe ROI;
- tutte le altre (VIF, FSIM, GMSD, MS-SSIM, HaarPSI, DISTS, LPIPS, NSS feature distance, NSS patch-distribution distance): non hanno una
  decomposizione per pixel da restringere, quindi ricevono la coppia con lo *sfondo soppresso*
  (stessa costante fuori maschera, ritaglio al bounding box del tessuto) — garanzia più debole,
  dichiarata come tale nell'interfaccia e nel resoconto di export.

Misure su ROI e metriche `roi_based` (SNR/CNR) non sono mai toccate: una ROI disegnata a mano dice
già quali pixel interessano, e la ROI di rumore/fondo è spesso posizionata nell'aria di proposito.

### Profili quantitativi, diagnostica e riproducibilità

Il workbench quantitativo usa per default il profilo versionato **Core rapido** invece di
selezionare ogni metrica. Restano disponibili Strutturale, Percettivo sperimentale,
Rumore/ROI QC, Completo, Fantoccio/task-based e Personalizzato. La selezione manuale non elimina
alcuna metrica: produce un profilo Personalizzato esplicito. L'Indice QICOM mostra profilo,
versione e composizione, è marcato sperimentale/non clinicamente validato e non è confrontabile
fra profili o versioni differenti; un indice con componenti obbligatorie mancanti è dichiarato
parziale.

Ogni calcolo produce inoltre:

- un registro degli esiti per tutte le metriche (`requested-and-computed`, `not-requested`,
  `not-applicable`, `failed`, `unavailable-backend`, `invalid/degenerate-input`, `cancelled`),
  visibile nel workbench e incluso nell'export;
- una ricevuta di riproducibilità versionata con hash di immagini, maschere, configurazione e
  risultati, versioni QICOM/backend, profilo/formule, warning preflight e unità statistica;
- una diagnostica preflight translation-only tramite phase cross-correlation 2D/3D, con shift in
  pixel e in mm quando lo spacing è noto. Non applica trasformazioni e non modifica i pixel;
- per stack, mediana, IQR, MAD, P5/P95, outlier Tukey 1,5×IQR e collegamento alla slice peggiore.
  I CI storici restano per compatibilità ma sono descritti come ottimistici, poiché slice e ROI
  dello stesso caso non sono unità indipendenti.

La mappa del bias usa `comparison - reference`, LUT blu-bianco-rosso centrata su zero, range
simmetrico e maschere tissue/ROI; l'export PNG salva a fianco il JSON della scala. La NPS usa
esclusivamente ROI dedicate `noise_texture_roi`, detrending e windowing dichiarati, correzione
dell'energia della finestra, PixelSpacing e frequenze mm⁻¹, più ROI/slice, curva radiale,
anisotropia, potenza integrata, fP, fA e conteggio dei campioni. I parametri completi sono
esportabili in JSON/CSV/Excel e lo strumento appartiene al profilo Rumore/ROI QC, non al Core.

I profili di accettazione sono file YAML/JSON dell'utente con hash, versione, applicabilità,
baseline e regole higher/lower/target/range; QICOM non distribuisce soglie cliniche. Le due distanze NSS
possono usare da Impostazioni un modello NSS locale adattato su immagini QA approvate, con nome,
versione, data, hash del corpus e parametri di fitting; punteggi ottenuti con modelli differenti
non sono confrontabili. Rise distance 10–90%, edge MTF/TTF/MTF50 e d′ PW/NPW/NPWE sono API
esperte del profilo Fantoccio/task-based. Richiedono rispettivamente un profilo, un bordo idoneo
esplicitamente dichiarato e task function/TTF/NPS configurate. Il bordo deve essere parallelo
all'asse delle righe, con transizione lungo le colonne: angoli diversi da zero sono rifiutati,
poiché non è implementata la proiezione slanted-edge. Il flag PASS controlla solo il CNR.

**Solo API Python.** La NPS (`qicom.metrics.nps`), i profili di accettazione
(`qicom.metrics.acceptance`), gli strumenti Fantoccio/task-based (`qicom.metrics.task_based`), il
trending (`qicom.metrics.trending`) e il robustness check radiomico
(`qicom.radiomics.robustness`) non sono raggiungibili dall'applicazione desktop: si usano da
script. Nell'app il profilo Rumore/ROI QC calcola le sole metriche ROI, e il profilo
Fantoccio/task-based non seleziona metriche; il workbench radiomico mostra e salva le tabelle di
robustness già prodotte, ma non le calcola.

La formula d′ v2 distingue PW (`sqrt(∫S/NPS df)`), NPW (`∫S df / sqrt(∫S·NPS df)`) e NPWE
(`∫S·E² df / sqrt(∫S·E⁴·NPS df)`), con `S = |task·TTF|²`. NPWE richiede `eye_filter`
esplicito, campionato sullo stesso asse: QICOM non assume geometria di visione o rumore interno.
L'integrazione è 1D sull'intervallo fornito, senza fattore radiale `2πf` o moltiplicatore per
frequenze negative; una NPS radiale 2D non va interpretata automaticamente come una NPS 1D.
Queste formule derivano dai modelli descritti da
[Solomon e Samei (2016)](https://doi.org/10.1117/1.JMI.3.3.035506), limitati qui al dominio 1D.
I vecchi risultati senza versione usavano la formula PW sotto entrambe le etichette NPW/NPWE:
`DetectabilityResult.from_dict()` conserva valore ed etichetta, assegna `formula_version=1`
e segnala `effective_observer="PW"`; i nuovi risultati hanno versione 2. Per riprodurre il
calcolo storico usare `observer="PW"`. Il profilo Fantoccio/task-based passa alla versione 2.0.

`qicom.provenance.StatisticalUnit` rappresenta `subject_id`, `case_id`, `acquisition_id`,
`timepoint`, `lesion_id`/`roi_id` e `reader_id`. In assenza di almeno tre unità indipendenti
identificate, le API statistiche restituiscono solo descrittive e una motivazione, senza
promuovere slice o ROI a osservazioni indipendenti.

## CLI

```bash
qicom-cli confronta riferimento.dcm confronto.dcm [--data-range 4095] [--output risultati.xlsx]
```

Carica le due immagini/volumi (DICOM, NIfTI, JPG/PNG/BMP tramite `qicom/io/`), calcola tutte
le metriche applicabili con `run_all_metrics` e stampa la tabella a schermo; `--output`
esporta anche in CSV, Excel o — con suffisso `.md` — nel report Markdown descritto in
[Export per assistente AI](#export-per-assistente-ai). Se `--data-range` non è indicato, viene
dedotto dal dtype/range dell'immagine di riferimento e sempre stampato esplicitamente (mai
assunto in silenzio).

## Export per assistente AI

QICOM può esportare i risultati come un singolo file Markdown autoconsistente
(`*_ai_assistant.md` dalla GUI, `--output report.md` dalla CLI) pensato per essere incollato in
una chat con un assistente AI e farsi spiegare i numeri. Accanto a ogni valore il file riporta la
definizione della metrica presa dalle schede `qicom/metrics/descriptors/*.yaml` — la stessa fonte
di verità del tooltip in interfaccia — così che il modello non debba indovinare il significato di
una metrica omonima ma diversa, e segue la lingua selezionata nell'applicazione.

**L'export Markdown non contatta un assistente generativo** e non usa chiavi API.
Le metriche learned opzionali usano invece modelli locali per misurare le immagini.
L'export si limita a scrivere un file sul disco; se, dove e a chi caricarlo è
una decisione — e una responsabilità — interamente dell'utente. Per questo il contenuto è deciso da
una whitelist esplicita (`qicom/reporting/llm_export.py`): entrano solo id/nome/categoria della
metrica, valore, intervalli di confidenza, unità, il testo del descrittore e la configurazione non
derivata dall'immagine (data range, 2D/3D, forma e dimensione delle ROI). Nomi e percorsi dei file,
header DICOM/NIfTI, identificativi di paziente/studio/serie, date e istituzione, nomi di operatore
o medico, hostname e utente non hanno alcuna via d'accesso al file, e i nomi delle ROI — testo
libero scritto dall'utente — sono contati, non riprodotti. Il file è Markdown leggibile: va aperto
e letto prima di inviarlo altrove.

Una sezione **Citazione** chiede al modello che un articolo, un report o una presentazione scritti
a partire dal file citino QICOM con il riferimento di **Aiuto ▸ Informazioni su QICOM ▸
Citazione**; quando fra i risultati c'è RIQE, anche lo studio (arXiv:2610.00384), la release del
modello (doi:10.5281/zenodo.23055559) e le attribuzioni che la sua licenza CC BY 4.0 richiede. I
riferimenti restano in inglese in ogni lingua.

Il file resta abbastanza piccolo da incollare in una chat: al massimo 200 righe di risultati e un
budget di byte esplicito. Quando l'analisi ne produce di più, le righe mostrate sono scelte in modo
deterministico, una per metrica a rotazione, così che ogni metrica compaia almeno una volta; il file
dichiara quante righe esistono, quante ne mostra e quante ne omette, e il digest di provenienza in
fondo copre **l'intero** insieme dei risultati, non l'estratto. Il file viene scritto in modo
atomico: un errore lascia il file precedente intatto, mai un report a metà che sembra completo.

## Sicurezza e privacy

- Tutta l'elaborazione delle immagini avviene localmente, sulla macchina su cui QICOM gira:
  nessun dato di immagine viene trasmesso, non esiste telemetria e nessuna analisi apre
  connessioni di rete. I download espliciti riguardano i checkpoint opzionali delle metriche
  learned e le risorse per la verifica IBSI; entrambi usano checksum fissati.
- I progetti portabili e i pacchetti di valutazione anonimizzano i DICOM per impostazione
  predefinita, rimuovono tag privati/identificativi e rigenerano coerentemente gli UID.
- Il riconoscimento è fail-closed: viene anonimizzato anche un dataset DICOM salvato senza
  preambolo PS3.10 (frequente negli export PACS e negli archivi più vecchi), e un file
  riconoscibile come DICOM ma non interpretabile blocca l'export invece di essere copiato
  tale e quale. Negli ZIP quantitativi i file non DICOM vengono copiati invariati e i nomi
  originali sono conservati: questi ZIP non garantiscono la rimozione di dati identificativi
  dagli header NIfTI o dai nomi dei file. La sanificazione NIfTI dei pacchetti qualitativi
  non si estende attualmente agli ZIP quantitativi.
- Il risultato viene verificato, non dato per buono: nessun valore con cui il file originale
  identificava una persona può sopravvivere in nessun punto della copia (sequenze annidate
  incluse). Un file che non supera questa verifica — per esempio un DICOMDIR, i cui record di
  directory non sono coperti dalle regole di attributo — viene bloccato in export invece di
  essere dichiarato anonimo.
- Un DICOM clinico con pixel ma senza `BurnedInAnnotation=NO` viene bloccato in export. È ammessa
  soltanto l'eccezione stretta per immagini Secondary Capture minimali (tipicamente JPG convertiti
  in DICOM) prive di attributi identificativi diretti; il file viene marcato esplicitamente come
  non verificato rispetto alle annotazioni nei pixel. QICOM non esegue OCR né rimozione del testo
  incorporato nei pixel.
- Se l'export viene bloccato da questi controlli, QICOM mostra il motivo e chiede se esportare
  comunque. Solo con un consenso esplicito i file rifiutati vengono inclusi, anonimizzati al meglio
  e marcati `PatientIdentityRemoved=NO` (non verificati); l'elenco resta nel registro errori.
- Gli errori si possono selezionare nella finestra Errori e salvare in un report di testo (versione,
  piattaforma, log recente con traceback) da allegare a una segnalazione. QICOM non invia nulla.
- Gli ZIP importati sono controllati prima dell'estrazione (path traversal, link simbolici,
  numero/dimensione file e rapporto di compressione); manifest e risposte sono limitati e
  validati prima dell'uso.
- File locali sensibili e log usano permessi owner-only sui sistemi POSIX. I log oscurano campi
  sensibili e il percorso home dell'utente.
- I testi esportati in Excel sono neutralizzati quando iniziano come formule; i nomi dei fogli
  sono validati e resi univoci.
- L'export Markdown per assistente AI è costruito su whitelist, non su blacklist: scrive solo i
  campi esplicitamente ammessi (valori, descrittori delle metriche, configurazione non derivata
  dall'immagine) e nessun dato proveniente da un header di immagine. Una suite di test lo verifica
  a partire dalla whitelist stessa. QICOM non carica nulla: l'invio del file, se avviene, è un
  gesto dell'utente.

Vedi anche [SECURITY.md](SECURITY.md) per il modello di minaccia e i limiti operativi.

## Livello dati/sessione

`qicom/session/` (nessuna dipendenza GUI) fornisce:
- **`models.py`**: `Project`, `Analysis`, `RoiDefinition`, `Annotation` (dataclass immutabili).
- **`storage.py`**: `SessionStorage`, persistenza SQLite locale (default via `platformdirs`,
  cross-platform) con CRUD completo; `save_analysis(analysis, results)` salva atomicamente
  analisi, risultati, ROI e annotazioni in una sola transazione SQLite.
- **`history.py`**: `AnalysisHistory`/`AnalysisBundle`, facciata di sola lettura per
  recuperare/riaprire analisi passate (data/ora, immagini, metriche, ROI, annotazioni).
- **`commands.py`**: `Command`/`CommandHistory`, motore undo/redo in memoria (Ctrl+Z/Ctrl+Y),
  pronto per i comandi concreti della GUI.
- **`app_logging.py`**: log applicativo JSON-lines su file rotante (+ console), rileggibile
  dalla GUI o esportabile. Vengono registrati avvio/chiusura della sessione, ogni operazione
  in background (nome, durata e — in caso di errore — l'eccezione con il traceback) e ogni
  calcolo/salvataggio/caricamento/export, con forme e conteggi ma senza percorsi file né
  testo derivato dal paziente.

## GUI

```bash
qicom-gui
# oppure, senza installare lo script:
python -m qicom.gui.app
```

`qicom/gui/` (PySide6 + pyqtgraph) contiene solo presentazione/orchestrazione - nessuna logica
di calcolo propria. Le tre finestre condividono un workbench orientato alle attività: una barra
laterale espone una fase alla volta e lascia il viewer libero dai pannelli non pertinenti.

- **Quantitativo**: `Importa → Ispeziona → Misura → Analizza → Risultati → Mappe di calore →
  Esporta`. ROI/linee/annotazioni, calcolo e grafici non competono più contemporaneamente per lo
  spazio; al termine del calcolo si passa automaticamente ai risultati. `Importa` offre sia il
  caricamento immagini sia l'import di un progetto salvato; `Risultati` ed `Esporta` nascondono
  del tutto il canvas (numeri, grafici e resoconto scritto sono l'unico contenuto utile lì),
  mentre `Mappe di calore` fa l'opposto: immagini a piena dimensione e la sola tabella delle
  metriche mappabili; le heatmap si colorano con LUT standard dell'imaging medicale (Inferno,
  Magma, Viridis percettivamente uniformi, Hot Iron della palette DICOM, più scala di grigi). Il
  verso della scala segue la metrica: per le mappe di differenza (MSE/MAE/NMSE) lo zero è
  trasparente, per SSIM — che vale 1.0 dove le immagini coincidono — la LUT viene applicata al
  contrario, così a risaltare sono comunque le zone compromesse (legenda inclusa).
  `Analizza` ospita anche l'esclusione dell'aria di sfondo (vedi sopra). `Esporta` elenca per
  iscritto cosa è stato analizzato, cosa è stato
  generato e quali file verranno scritti, con due opzioni di esclusione (immagini delle heatmap e
  immagini chiave con le ROI, entrambe attive per default). Cronologia, log ed errori non sono
  più una fase: stanno in `Visualizza → Pannelli`, e gli errori non letti restano segnalati da un
  indicatore nella status bar.
- **Qualitativo**: il ruolo autore usa
  `Immagini → Domande → Tutte le pagine → Anteprima → Salva`. I primi due passi sono una
  passata guidata su **una** pagina — carichi le sue immagini, poi scrivi le domande che
  riguardano quelle immagini — e ogni passo termina con un pulsante `Avanti` che porta al
  successivo, così il percorso non va scoperto cliccando sulle linguette. `Tutte le pagine` è la
  vista d'insieme: una riga per pagina con quante immagini e quante domande ha, da cui si apre,
  duplica, rinomina o elimina una coppia già creata, o se ne inizia un'altra (che riporta al
  passo `Immagini`, sempre lo stesso, non una sezione in più). Il valutatore vede soltanto
  `Valuta → Completa`, senza strumenti di authoring.
- **Radiomica**: `Immagini → ROI/VOI → Pre-processing → Estrazione → Risultati → Esporta`, con
  configurazione e segmentazione mostrate come pannelli contestuali separati.
- Menu e barra applicazione sono uniformi (`File`, `Modifica` dove applicabile, `Visualizza`,
  `Strumenti`, `Aiuto`); tema, lingua e formato numerico non occupano azioni operative.
- La tabella quantitativa usa intestazioni tradotte e leggibili e include un filtro testuale su
  tutte le colonne; le chiavi tecniche originali restano inalterate nei DataFrame e negli export.

Componenti principali:
- **`app.py`**: bootstrap `QApplication` + `configure_logging()`.
- **`main_window.py`**: chrome del workbench quantitativo (menu, navigazione, dock contestuali,
  status bar); costruisce un `AppController` e resta un guscio sottile sulla business logic.
- **`controller.py`** (`AppController`): orchestra caricamento immagini, calcolo metriche
  (in background thread, `workers.py`), `CommandHistory` (undo/redo, Ctrl+Z/Ctrl+Y),
  `SessionStorage`/`AnalysisHistory` (salva/riapri analisi passate).
- **`commands.py`**: sottoclassi concrete di `qicom.session.commands.Command` per ogni azione
  annullabile (ROI, window/level, annotazioni).
- **`widgets/dual_image_view.py`**: due pannelli `pg.ImageView` sincronizzati (slice, ROI
  cerchio/rettangolo/poligono duplicate su entrambi i pannelli, annotazioni testuali).
- **`widgets/background_panel.py`**: esclusione dell'aria nella fase `Analizza`, in tre passi
  (cerchio verde di campionamento → `Rileva aria` → gomma per le correzioni a mano), con
  tolleranza/soglia modificabili e resoconto della percentuale esclusa. Il passo è dichiarato
  facoltativo da un avviso fisso in cima alla colonna e si attiva da un interruttore a tutta
  larghezza, non da una casella di spunta: saltarlo è una scelta legittima (le metriche fanno
  allora la media su tutta l'immagine, aria compresa), ma va fatta consapevolmente. Il campione
  d'aria è **uno solo** — posato il cerchio, il pulsante che lo crea si disattiva.
- **`widgets/evaluation_setup_panel.py`**: il primo passo del compositore qualitativo — anonimato
  delle risposte, istruzioni generali, randomizzazione e cecità, ripetizioni nascoste, statistiche
  e tempi. Nulla qui riguarda una singola pagina o domanda: è il comportamento dell'intera
  valutazione, deciso prima che esista qualcosa a cui applicarlo.
- **`widgets/page_overview_panel.py`**: la vista d'insieme del compositore di form qualitativi —
  una riga per pagina con il numero di immagini e di domande, e le azioni per aprirla,
  duplicarla, rinominarla o eliminarla.
- **`widgets/wizard_step_bar.py`**: la barra `Indietro`/`Avanti` in fondo a ogni passo di
  costruzione, con l'indicazione della pagina su cui si sta lavorando.
- **`widgets/roi_manager.py`**, **`widgets/annotation_manager.py`**, **`widgets/history_panel.py`**,
  **`widgets/log_panel.py`**, **`widgets/results_table.py`**, **`widgets/concat_dialog.py`**,
  **`widgets/import_panel.py`**, **`widgets/export_summary_panel.py`**,
  **`widgets/toolbar.py`**: pannelli dock/dialoghi, ciascuno presentazione pura.
- **`widgets/workflow_rail.py`**: navigazione condivisa e traducibile; segnala la fase selezionata
  alla finestra proprietaria senza conoscere controller o dati scientifici. È una striscia di
  linguette orizzontali in stile browser, disegnata a mano (`QPainterPath`: le spalle curve non
  sono esprimibili in QSS) e ospitata in una barra strumenti in cima alla finestra: la linguetta
  attiva è riempita del colore della superficie sottostante e vi si salda, le altre restano
  staccate sullo sfondo arretrato. Una sola fase è visibile alla volta — sono i passi di un unico
  flusso, non documenti aperti in parallelo.
- **`i18n/`**: traduzione runtime via dizionari JSON (`locales/{en,it,fr,de,es,pt,zh-Hans,ja}.json`), non
  `.ts`/`.qm` Qt (nessun passo di build); cambio lingua a caldo, senza riavvio.
- **`theming.py`**: QSS chiaro/grigio/scuro (`themes/{light,gray,dark}.qss`) applicati sia alla chrome Qt
  sia (esplicitamente, via `pg.setConfigOption` + refresh dei canvas già costruiti) ai
  pannelli pyqtgraph, che QSS da solo non raggiunge.

Prima di ogni calcolo QICOM confronta i metadati fisici delle immagini caricate e
segnala — nel pannello Errori e nel log, senza mai bloccare il calcolo — interpretazione
fotometrica, pixel spacing, orientamento o passo fra slice discordanti, e serie con slice
non equispaziate. Un'immagine MONOCHROME1 (visualizzata invertita rispetto a un PACS) è
inoltre marcata come tale nell'overlay del pannello. I pixel restano sempre quelli
memorizzati: QICOM non inverte e non ricampiona nulla per conto proprio.

I caricamenti e i calcoli asincroni hanno un identificatore di generazione: un risultato vecchio
non può sovrascrivere immagini o risultati richiesti più recentemente. In chiusura, l'opzione
“Salva” persiste il progetto portatile modificabile, non la sola fotografia dei risultati.

## Valutazioni qualitative e statistica

L'autore, nel primo passo **Impostazioni**, può marcare la valutazione come **a risposte anonime**: agli
valutatori non viene chiesto alcun nome, nessun nome viene registrato, e ogni consegna è
identificata solo dal proprio **id di consegna** — un timestamp UTC più un suffisso casuale,
generato una volta per set di risposte inviato. È una chiave diversa dall'identità del computer
(`load_or_create_evaluator_identity`, che serve solo a ritrovare una valutazione interrotta su
quella macchina): più persone che rispondono dalla stessa postazione producono consegne distinte,
e la stessa persona che ne invia due ne produce comunque due. Chi aggrega vede quante ne sono
arrivate e decide: se ne attendeva 10 e ne riceve 20, qualcuno ha risposto più volte.

Il file che un valutatore salva a fine valutazione contiene **solo le sue risposte** (un unico
foglio `Raw Answers`). Le statistiche non stanno lì: su un singolo valutatore sarebbero degeneri
— una "media" su un solo giudice è il suo stesso punteggio, e concordanza inter-valutatore e ICC
non sono definite — e sono compito del passo di aggregazione, che le calcola fra valutatori. Quel
foglio è anche esattamente ciò che il modulo di aggregazione rilegge quando carichi l'`.xlsx`
invece del `.json`.

Dal launcher, **Aggregate Results** apre uno spazio di lavoro a quattro passi (le stesse
`WorkflowRail` delle finestre principali): si carica il pacchetto con le domande inviate, poi i
file di risposte ricevuti — `.json` di QICOM, il report Excel che ogni valutatore salva a fine
valutazione, o un CSV degli stessi record. Ogni riga dell'elenco è una valutazione con una
bandierina verde o rossa: la verifica è strutturale (pagine, domande, testo delle domande e
immagini di confronto realmente definiti nel pacchetto, oltre al test id quando il file lo
dichiara), così un report che risponde a un questionario diverso viene mostrato con il motivo
dell'incongruenza ed escluso dall'analisi invece di far fallire l'aggregazione. I passi
successivi calcolano e mostrano in QICOM le statistiche e il grafico dei punteggi medi, e
esportano tutto in Excel (con grafici nativi) o in una cartella di CSV.

Le risposte aggregate devono riferirsi a pagine, domande, pannelli, slice e intervalli di
punteggio realmente definiti nel pacchetto; record ed evaluator duplicati non vengono aggregati.
Il report include media/deviazione, Cohen/Fleiss kappa, ICC(2,1) e confronti appaiati. Per questi ultimi QICOM media prima le slice per valutatore, abbina i confronti sullo
stesso valutatore, calcola la probabilità di superiorità e usa il Wilcoxon signed-rank. Non tratta
le slice spazialmente correlate come soggetti indipendenti. Il foglio `Methodology` del report
documenta unità statistica e limiti degli intervalli bootstrap.

### Disegno qualitativo v4

Il primo passo **Impostazioni** configura istruzioni generali, randomizzazione di
casi/domande/pannelli, controbilanciamento, seed, etichette A/B/C, pagine di calibrazione,
ripetizioni nascoste e raccolta opzionale dei tempi. Sono scelte che valgono per l'intera
valutazione e che cambiano ciò che ogni valutatore vede, quindi si fanno prima di costruire le
pagine, non mentre si scrivono le domande. **Crea consegna in cieco**
produce un ZIP pubblico con nomi generici e una mappa JSON di decodifica separata, riservata
all'autore. Seed e ordine sono salvati nella consegna e nelle risposte: la ripresa non cambia
mai ciò che il valutatore vede.

Chi valuta accede con un proprio utente locale (ID e password) per poter sospendere e riprendere:
le sue valutazioni, in sospeso e concluse, sono cifrate su quel computer e visibili solo a lui.
Senza accesso si valuta in una sola sessione e nulla viene conservato. L'autore può inoltre
proteggere con un codice di accesso la copia da inviare e la consegna in cieco: senza il codice non
si aprono, non si modificano e non se ne aggregano le risposte, che tornano cifrate con lo stesso
codice. Limiti in `SECURITY.md`.

Le scale numeriche supportano ancore testuali e commenti obbligatori sotto soglia. Sono distinti
assenza, salto esplicito e **Non valutabile**. Sono disponibili anche 2-AFC forzata A/B,
preferenza appaiata A/B/nessuna differenza, categorie, ranking e testo aperto.

Il report usa mediana/IQR, distribuzioni, top/bottom box, risposte divergenti e tassi di risposta.
La concordanza primaria è l'alpha ordinale di Krippendorff; Gwet AC2 è dichiarata avanzata.
I confronti mediano prima le slice nella cella valutatore×caso, applicano bootstrap indipendente
dei cluster valutatore e caso e riportano anche Wilcoxon con correzione di Holm. Ripetizioni, QC
descrittivo, completezza e precisione hanno fogli separati; nessun flag esclude o ricentra
automaticamente un valutatore. Dettagli in
[QUALITATIVE_METHODOLOGY.md](QUALITATIVE_METHODOLOGY.md).

## Radiomica pairwise

Dal launcher, **Radiomica** apre una finestra e un formato progetto indipendenti dai moduli
quantitativo e qualitativo. Accetta esclusivamente un’immagine/serie DICOM o un file NIfTI per
ciascuno dei due ruoli (riferimento e confronto); JPG/PNG/BMP sono rifiutati perché privi della
geometria fisica necessaria.

Il flusso dedicato comprende:

- resampling centrato in coordinate fisiche a voxel isotropici configurabili, con B-spline di
  default per l’immagine e nearest-neighbour per la maschera;
- discretizzazione IBSI a numero di bin o ampiezza fissa. Nel secondo caso QICOM sostituisce
  durante l’estrazione l’ancoraggio a zero predefinito di PyRadiomics con l’ancoraggio al minimo
  della ROI richiesto dal profilo IBSI;
- normalizzazione, clipping e resegmentation espliciti e inclusi nell’hash del profilo; il default
  resta `none`, senza modifiche silenziose ai valori;
- ROI/VOI manuali sincronizzate, thresholding e region growing senza deep learning;
- import NIfTI multi-label con controllo rigoroso di size/spacing/origin/direction, conferma
  esplicita prima del resampling nearest-neighbour, ed export originale/preprocessato con sidecar;
- shape 2D/3D, first-order, GLCM, GLRLM, GLSZM, GLDM e NGTDM su Original, Wavelet e/o LoG;
- delta assoluto, percentuale classica e simmetrica per ogni feature; la simmetrica è primaria
  nel profilo 2.0, mentre `delta_percent` conserva il valore classico per compatibilità;
- accordo pairwise esclusivamente descrittivo: nessuna ROI/lesione dello stesso soggetto viene
  trattata come unità indipendente e l’ICC/CCC formale è riservato all’aggregatore di coorte;
- QC separato delle maschere (Dice, Jaccard, RVD, centroid shift, HD95, ASSD, overlap fisico,
  bordi, voxel e hash) e delle feature;
- robustness check con perturbazioni fisiche per famiglia e sweep configurabile del
  preprocessing, senza ICC sul singolo caso e senza selezione clinica automatica (solo API);
- aggregatore multi-progetto che legge le sole tabelle, verifica gli hash dei profili e calcola
  ICC/CCC soltanto con almeno tre unità indipendenti dichiarate; include Pearson/Spearman,
  effective N, associazioni col volume e cluster di ridondanza esplorativi;
- progetto ZIP portabile con copie NIfTI derivate, maschere, configurazione e risultati; export
  Excel/CSV/HTML con sezioni separate per feature, delta, ricevuta, registro esiti, QC,
  robustness, sweep, validazione e metodologia.

Nella pagina Immagini, **Verifica QICOM con fantoccio IBSI** apre la quarta modalità dedicata. Il
manifest condiviso con la CI scarica solo dopo consenso immagine, maschera e workbook ufficiali,
ne verifica gli SHA-256 fissati e li riusa offline. La verifica attuale confronta 104 feature del
digital phantom IBSI-1 ed è circoscritta a formule, aggregazione 3D, caricamento/geometria NIfTI,
label, discretizzazione ed estrazione dichiarate: non è una verifica full-pipeline DICOM e non è
una certificazione. PASS richiede che tutte le feature obbligatorie siano confrontabili e dentro
tolleranza; una feature mancante produce INCOMPLETE. Una copia modificabile diventa
`derived-experiment` e perde il verdetto di riferimento.

Ogni riga riporta backend, hash del profilo e stato di copertura. La ricevuta comune salva versioni,
hash di input/maschere/configurazione/risultati, warning, esiti e unità statistica. Sono fuori scope
LASSO, Rad-Score, ROC/AUC, modelli predittivi, registrazione automatica e soglie cliniche.
Metodologia completa in [RADIOMICS_METHODOLOGY.md](RADIOMICS_METHODOLOGY.md); schemi, batch,
trending, HTML e demo in [OPERATIONS_SCHEMAS.md](OPERATIONS_SCHEMAS.md).

**Limiti noti di questo prototipo** (segnalati esplicitamente, non nascosti): il freehand ROI
è un poligono a vertici (`pg.PolyLineROI`), non un tratto continuo; la concatenazione stack è
solo manuale (indice di partenza per ciascuna immagine + `numpy.concatenate` — l'ordinamento
automatico via metadati DICOM/NIfTI richiederebbe un'estensione del core). DICOM SEG e RTSTRUCT
non sono inclusi in questa iterazione.

## Strumenti di utilità

Dal launcher, **Strumenti di utilità** apre una finestra con il catalogo a sinistra e lo strumento
a destra; lo stesso catalogo è nel menu `Strumenti` di ogni finestra di lavoro. Gli strumenti
leggono le sorgenti senza modificarle: ogni risultato è una copia, e un insieme di file compare
nella cartella di destinazione (nuova o vuota) tutto insieme o per niente. Il NIfTI scritto da
uno strumento è posizionato dalla geometria della sorgente (`qicom/io/geometry.py`) e non copia
alcun campo dell'header. Ogni elaborazione e ogni salvataggio mostra la percentuale di
avanzamento in una finestra propria; nell'area dell'immagine la rotella scorre le slice
(Ctrl+rotella ingrandisce), il trascinamento con il tasto sinistro regola finestra e livello, il
tasto centrale sposta l'immagine e il destro la ingrandisce, e un clic, o un file trascinato,
carica l'immagine; un pulsante colorato indica a ogni momento il passo successivo.

| Strumento | Cosa fa |
| --- | --- |
| Segmentazione semi-automatica | pagine guidate a schede (immagine, etichetta, campione, selezione, applica, salva) ripetute per ogni regione: ROI campionata, intervallo di intensità, anteprima, poi «togli», «mantieni solo» o «assegna»; preset con intervalli HU indicativi per la CT; annulla/ripeti; salva NIfTI + tabella JSON delle etichette |
| Operazioni e confronto tra maschere | unione, intersezione, differenza, XOR, riempimento delle slice vuote, Dice/Jaccard/volumi/Hausdorff in mm |
| Converti immagini | DICOM, NIfTI o pile di bitmap in NIfTI, slice PNG a 8 o 16 bit (finestra a schermo) o TIFF (valori reali), in una cartella nuova `<nome>_<formato>` dentro quella scelta |
| Ricampiona | a una spaziatura o sulla griglia di un riferimento, per coordinate paziente (non registra) |
| Aritmetica tra immagini | differenza, differenza assoluta, somma, media, rapporto |
| Degradazione controllata | un effetto alla volta (rumore, sfocatura, perdita di risoluzione, JPEG) con elenco degli effetti rimovibili, da un seed, con provenance riproducibile |
| Ispeziona e modifica tag DICOM | header completo con sequenze e tag privati, ricerca ed export CSV; copie modificate in una cartella nuova, tag dei pixel bloccati, tag delle misure con conferma |
| Confronta header DICOM | due header elemento per elemento, interi; filtri «solo differenze» e «nascondi UID» |
| Controlla e organizza cartelle DICOM | studi e serie, istanze mancanti, spaziatura irregolare; copia ordinata senza nomi identificativi |
| Decomprimi DICOM | un DICOM compresso (JPEG lossless, JPEG-LS, JPEG 2000, RLE…) riscritto non compresso, un file per frame, nella cartella nuova `<nome>` dentro quella scelta; i multi-frame ricevono UID di istanza e di serie nuovi, gli Enhanced restano Enhanced |
| Anonimizza immagini | stesse regole fail-closed degli export; albero dei file prima di scrivere; copia in `<cartella>_anonymized` con le stesse sottocartelle e gli stessi nomi (i nomi non sono de-identificati), oppure, come opzione, con nomi generici in `anonymized` |

La segmentazione separa classi di intensità, non organi; gli intervalli HU dei preset sono punti
di partenza da regolare, non soglie cliniche. I limiti di privacy di ciascuno strumento sono in
[SECURITY.md](SECURITY.md), i formati scritti in [OPERATIONS_SCHEMAS.md](OPERATIONS_SCHEMAS.md).

## Operazioni locali e headless

`qicom-cli` offre `batch-quantitative`, `aggregate-qualitative` e
`batch-radiomics-cohort` da configurazione JSON, con progress, log, exit code, isolamento degli
errori, tidy CSV/JSON e ricevute. Il menu Strumenti della radiomica espone anche l’aggregazione
multi-progetto. `TrendStore` mantiene baseline versionate, variazioni, Shewhart, EWMA opzionale e
limiti utente distinti dai limiti statistici.

### Bundle sintetico (demo e riproduzione bug)

QICOM genera una cartella autonoma di dati di esempio deterministici che copre i tre moduli, con
risultati, ricevute di riproducibilità e report HTML locale. Tutto deriva dal seed fisso `20260517`:
la stessa build produce sempre gli stessi file, e `qicom_demo.json` elenca versione QICOM,
piattaforma e uno SHA-256 per file.

```bash
qicom-cli demo ./QICOM-synthetic-demo
```

Nell'app: **Impostazioni → Diagnostica**, oppure **Aiuto → Segnala un Bug**, che è il punto in cui
serve davvero — allegando il bundle a una segnalazione, chi la riceve lo rigenera e confronta gli
hash, senza che nessun dato di paziente debba viaggiare. Non contiene PHI e non è una validazione
clinica o IBSI. La cartella di destinazione non viene mai sovrascritta (dalla GUI il nome libero
successivo, `-2`, `-3`, … , viene scelto automaticamente).

## Test

Per preparare un ambiente di sviluppo completo e verificare prima del push gli stessi
controlli usati da GitHub:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python packaging/bootstrap_environment.py
python -m pip install -e ".[gui,learned,radiomics,dev]"
python packaging/check_ci.py
```

Il bootstrap aggiorna prima `pip`, poi `setuptools` usando il requisito dichiarato in
`[build-system]`. Il solo `pip install -e .` prepara gli strumenti di compilazione in un
ambiente isolato e può lasciare un `setuptools` obsoleto nell'ambiente poi sottoposto ad
audit. `wheel` resta nell'ambiente di compilazione isolato, per evitare che PyInstaller
lo raccolga nell'eseguibile. Anche i job GitHub creano un venv dedicato: i pacchetti
preinstallati sul runner non entrano nell'ambiente verificato o distribuito.

Il comando di verifica controlla versioni e compatibilità delle dipendenze, esegue
`pip-audit`, lint, formattazione, mypy, i gate del repository e la suite con coverage.
Prepara inoltre i checkpoint learned verificati e abilita il confronto con i riferimenti
IBSI ufficiali. Richiede rete per l'audit e per le risorse non ancora in cache; questi
accessi appartengono alla verifica di sviluppo. I rapporti finiscono in `build/audit/`,
`build/coverage.json` e `build/ibsi-ci/`. Un errore interrompe il comando con esito non zero.

Per il profilo di test senza learned, usare un **venv separato**, installare
`.[gui,radiomics,dev]` dopo il bootstrap ed eseguire
`python packaging/check_ci.py --profile base`. Questo profilo esegue la suite e controlla
anche la raccolta con `python -m pytest`; la coverage completa resta nel profilo full.
Su Linux servono anche le librerie Qt elencate nel passo `Install Qt runtime dependencies`
di `.github/workflows/ci.yml`.

Le fasi `--phase audit`, `--phase static` e `--phase test --profile full|base` sono
richiamate direttamente dalla CI. Il controllo pre-push verifica l'interprete corrente:
la matrice 3.11/3.12 resta necessaria. Non installa hook Git e non esegue commit o push.

La CI è configurata per eseguire la suite su **Linux, Python 3.11 e 3.12**: il job base
include GUI e radiomica ma omette learned; il job completo include anche torch e gira su 3.12.
Windows e macOS hanno build native con smoke test, non la stessa suite completa.
La CI gira anche ogni lunedì per rilevare
nuove vulnerabilità senza attendere un commit. L'audit precede i test e il rapporto JSON
viene conservato come artifact per sette giorni anche in caso di vulnerabilità.
I singoli gate restano eseguibili separatamente:

```bash
python packaging/check_complexity.py           # nessuna nuova funzione oltre C901 20 (ruff)
python packaging/check_release_consistency.py  # versione coerente e changelog che la descrive
python packaging/check_coverage.py --coverage-json build/coverage.json
pytest -q tests/radiomics/test_ibsi_validation.py   # riferimento IBSI, obbligatorio sui tag
```

Le dipendenze dichiarate hanno limiti superiori per evitare aggiornamenti major impliciti.
Il congelamento per sistema operativo non è più solo un'intenzione: il workflow di packaging
esegue `packaging/generate_constraints.py` nello stesso ambiente della build e pubblica il
manifest di versioni e origini VCS installate accanto al bundle (vedi la sezione Installazione).

## Packaging cross-platform

Eseguibile standalone via [PyInstaller](https://pyinstaller.org/), spec in
`packaging/qicom.spec`:

```bash
# Profilo base: GUI quantitativa + qualitativa
pip install -e ".[gui,packaging]"
QICOM_PACKAGE_PROFILE=base pyinstaller packaging/qicom.spec

# Profilo full: aggiunge il workflow radiomico
pip install -e ".[gui,radiomics,packaging]"
QICOM_PACKAGE_PROFILE=full pyinstaller packaging/qicom.spec

# Smoke test del bundle (aggiungere .exe su Windows)
dist/qicom-full/qicom --smoke-test
```

Note:
- **PyInstaller non compila incrociato**: va eseguito su ciascun sistema operativo target
  (Windows, macOS, Linux) per produrne il build - un build Linux gira solo su Linux, ecc.
- Il profilo `base` esclude PyRadiomics/SimpleITK; il profilo `full`, predefinito, li include.
  Gli output sono rispettivamente `dist/qicom-base/` e `dist/qicom-full/`.
- Entrambi i profili usano `PySide6-Essentials`: QICOM non richiede il wheel Addons
  (WebEngine, Multimedia, Qt 3D e altri moduli non usati).
- Per impostazione predefinita lo spec **esclude** i backend delle metriche learned (`torch`,
  `torchvision`, `lpips`, `piq`) — build più leggero e veloce, corrisponde all'extra
  `gui,radiomics` (non `learned`) di `pyproject.toml`. Ogni altra metrica (incluse le distanze NSS, che non
  ne hanno più bisogno), il caricamento immagini, ROI, sessione/cronologia ed export restano
  perfettamente funzionanti; chiamare una metrica learned esclusa solleva `ImportError` con il
  nome della libreria mancante, esattamente come in un'installazione di sviluppo senza
  l'extra `learned`.
- Per includere anche le metriche learned nel build: `pip install -e ".[gui,radiomics,learned,packaging]"`
  poi `python packaging/prepare_learned_weights.py` e
  `QICOM_PACKAGE_PROFILE=full QICOM_INCLUDE_LEARNED_METRICS=1 pyinstaller packaging/qicom.spec`
  — build molto più
  grande (`torch` domina sia le dimensioni sia il tempo di build) e più lento da produrre.
- Lo spec usa raccolte mirate e include i file dati necessari (`gui/i18n/locales/*.json`, `gui/themes/*.qss`,
  `metrics/descriptors/*.yaml`, icone e il `THIRD_PARTY_LICENSES.txt` generato per quel profilo),
  quindi risolti correttamente sia in un checkout sia nel bundle PyInstaller. Inventario e SBOM
  includono anche PyInstaller: il suo bootloader/runtime viene redistribuito nell'app congelata,
  anche se le altre dipendenze di packaging restano strumenti di build.
- Il workflow `.github/workflows/package.yml` configura build native per Linux x64, Windows
  x64 e macOS 14 arm64 ed esegue `--smoke-test` sul frozen prima di caricare gli artefatti.
  Le piattaforme effettivamente prodotte dipendono dalla selezione e dall'esito del singolo run;
  la [matrice di verifica](packaging/DISTRIBUTION.md#build-and-verification-matrix) ne precisa
  la copertura. L'eseguibile Windows
  incorpora nome del prodotto, descrizione, autore,
  copyright e versione; ogni bundle contiene `SHA256SUMS` e ogni archivio viene pubblicato con
  un sidecar `.sha256`, utili per verificare e autorizzare esattamente la build ricevuta. I
  checksum non autenticano però il publisher. Gli artefatti restano non firmati: firma Windows
  Authenticode e firma/notarizzazione Apple richiedono certificati e credenziali del
  distributore.

## Uso previsto, attribuzione e licenza

### Uso previsto

QICOM è destinato esclusivamente a uso di ricerca e controllo di qualità. Non è un dispositivo
medico. I numeri che produce descrivono immagini, non pazienti: stabilire se un confronto sia
significativo, e interpretarne il risultato, resta responsabilità dell'utente qualificato che lo
ha impostato.

Due limiti da dichiarare sempre insieme alle metriche:

- **Nessuna registrazione.** QICOM confronta le immagini elemento per elemento e non le registra
  mai. Ogni metrica full-reference (MSE, MAE, RMSE, PSNR, SSIM e le altre) presuppone quindi che i
  due input corrispondano già spazialmente. `qicom/io/consistency.py` segnala le discordanze
  leggibili dagli header DICOM — interpretazione fotometrica, pixel spacing, orientamento, passo
  fra slice — ma non può rilevare un disallineamento che gli header non descrivono.
- **IBSI: aligned, non certificato.** Il modulo di radiomica segue le definizioni di riferimento
  IBSI e applica la convenzione IBSI dove PyRadiomics se ne discosta, ma non è certificato IBSI:
  IBSI non gestisce alcuno schema di certificazione.

### Attribuzione DICOM

Le frasi seguenti sono riportate testualmente, in inglese, in [NOTICE](NOTICE), in
`qicom/about.py` e nella scheda **Note legali e DICOM** della finestra Informazioni:

> QICOM supports DICOM® medical images.
>
> DICOM® is the registered trademark of the National Electrical Manufacturers Association (NEMA)
> for its standards publications relating to digital communications of medical information.
>
> QICOM is an independent software project and is not affiliated with, sponsored by, or endorsed
> by NEMA or the DICOM Standards Committee.
>
> QICOM reads DICOM files; it implements no DICOM network or storage service, claims no
> conformance to the DICOM Standard, and publishes no DICOM Conformance Statement.

### Licenza

Apache-2.0: vedi [LICENSE](LICENSE) e [NOTICE](NOTICE). I componenti di terze parti distribuiti
con l'applicazione restano soggetti alle rispettive licenze. La build genera l'inventario per il
profilo effettivo e riproduce nel proprio `THIRD_PARTY_LICENSES.txt` i testi dichiarati dalle
distribuzioni installate; [packaging/THIRD_PARTY_LICENSES.txt](packaging/THIRD_PARTY_LICENSES.txt)
resta la copia di riferimento del repository.

### Come citare QICOM

```
Mattiussi, F. (2026). QICOM — Quality Image COMparator (Version 1.1.0.dev0)
[Computer software]. https://github.com/Metallogik/QICOM
```

Il repository contiene [CITATION.cff](CITATION.cff); la voce BibTeX equivalente si copia da
**Aiuto ▸ Informazioni su QICOM ▸ Citazione**. Un DOI verrà aggiunto in entrambi i posti quando la
prima release sarà archiviata.

### Versioning e changelog

QICOM segue il [Semantic Versioning](https://semver.org/). [CHANGELOG.md](CHANGELOG.md) dedica una
sezione a sé — *Changes affecting numerical results* — a ogni modifica per cui **gli stessi input
producono numeri diversi**: formule, preprocessing, discretizzazione radiomica, maschere,
resampling o default che entrano in un calcolo. Non finiscono mai in un generico "bug fixes".
Le API Python obsolete, le sostituzioni e la finestra minima di rimozione sono registrate in
[DEPRECATIONS.md](DEPRECATIONS.md); nessuna API pubblica 1.x viene rimossa prima della 2.0.

Una versione nomina una release; **non** identifica il codice che ha prodotto un risultato. Fra due
build che portano lo stesso numero può esserci un numero qualsiasi di commit. Per questo la ricevuta
di riproducibilità registra anche `build_revision` — il commit esatto — e `source_dirty`, e il
bundle sa dichiararli senza avere un `.git` a disposizione. La strategia completa (una versione,
quattro file, chi la fa rispettare, e perché `main` porta una versione `.dev`) è in
[VERSIONING.md](VERSIONING.md). I requisiti di distribuzione, firma e notarizzazione inclusi, sono in
[packaging/DISTRIBUTION.md](packaging/DISTRIBUTION.md).

### Informazioni nell'applicazione

**Aiuto ▸ Informazioni su QICOM** — presente in tutte e tre le finestre principali e raggiungibile
dal pulsante ⓘ del launcher — raccoglie versione e build, autore e luogo, licenza, repository e
contatto di supporto, uso previsto (cosa fa e cosa non fa QICOM), attribuzione DICOM, dichiarazione
di privacy e citazione copiabile. Tutti i dati provengono da `qicom/about.py`, unica fonte condivisa
con `pyproject.toml`, `CITATION.cff` e questo README: la coerenza fra loro è verificata da
`tests/test_about.py`.

### Backend MIRP e riferimenti IBSI 1

Il backend predefinito resta PyRadiomics. Il backend opzionale MIRP 2.7.0 aggiunge
local intensity, intensity histogram, intensity-volume histogram, GLDZM e le aggregazioni
2D/2.5D/3D. Si seleziona nel pannello di configurazione radiomica e supporta immagini Original.
I risultati registrano backend, versione e configurazione; i profili PyRadiomics esistenti
mantengono la loro serializzazione e il loro hash.

La finestra di verifica IBSI permette di selezionare il sottoinsieme PyRadiomics originale
(104 righe) oppure i sei manifest MIRP: fantoccio digitale (482 righe), CT A/B (406 ciascuno)
e CT C/D/E (270 ciascuno). I manifest coprono tutte le righe con riferimento e tolleranza
numerici dei sei fogli IBSI 1. Le 1.622 righe CT includono 300 diagnostiche di preprocessing;
A–E sono configurazioni dello stesso caso. I valori e le tolleranze sono letti dal workbook
ufficiale verificato tramite hash. La verifica non richiede rete se la cache è già preparata.

Per le estrazioni MIRP, QICOM calcola Moran I e Geary C con somme esatte delle distanze
inverse tramite convoluzione FFT, senza campionamento casuale. Le ricevute distinguono
questi due output e le diagnostiche dagli altri valori estratti con MIRP.

MIRP è distribuito separatamente sotto EUPL-1.2. Citazione richiesta:
[Zwanenburg e Löck, MIRP (JOSS, 2024)](https://doi.org/10.21105/joss.06413).
I dati CT di riferimento sono CC BY-NC 3.0; URL, hash e attribuzioni sono nei manifest,
mentre i file immagine non sono inclusi nella distribuzione QICOM.
