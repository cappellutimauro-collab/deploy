# Deploy Vercel - Rehab Studio App

Questa cartella contiene solo i file sorgente fondamentali da caricare su GitHub.

## File inclusi

- `prova.py`: applicazione principale.
- `api/index.py`: adapter per Vercel Python Functions.
- `vercel.json`: routing Vercel.
- `static/`: CSS, JS, immagini e PWA assets.
- `rehab_app/`: moduli interni.
- `requirements.txt`: dipendenze Python.
- `runtime.txt`: runtime Python.
- `docs/`: note release e sicurezza.
- `tests/`: test automatici.

## File esclusi

Non caricare mai database, chiavi o dati reali:

- `fisio_app.sqlite3`
- `app_secret.key`
- `stripe_secret_key.txt`
- `stripe_webhook_secret.txt`
- `email_settings.json`
- `email_outbox/`
- `consensi informati/`
- `backups/`
- `runtime-data/`

## Variabili ambiente consigliate su Vercel

Impostale dalla dashboard Vercel, non nel codice:

- `FISIO_SECRET`: chiave lunga casuale per firmare sessioni e token.
- `STRIPE_SECRET_KEY`: chiave privata Stripe.
- `STRIPE_WEBHOOK_SECRET`: webhook secret Stripe.
- Config SMTP, se decidi di portarli su env invece che impostarli da pannello app.

## Nota importante su Vercel

Vercel Functions hanno filesystem read-only, con solo `/tmp` scrivibile in modo temporaneo. Questo significa che SQLite locale, consensi salvati su file, loghi caricati e configurazioni locali non sono persistenti in produzione.

Per una produzione vera servono:

- database esterno persistente;
- storage esterno per documenti, loghi e consensi;
- secret management tramite variabili ambiente;
- webhook Stripe configurato sul dominio pubblico.

Questo pacchetto serve per avviare il deploy e testare l'app su Vercel, ma la persistenza sanitaria reale va completata prima di aprirla al pubblico.
