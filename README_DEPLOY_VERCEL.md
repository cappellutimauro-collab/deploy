# Deploy Vercel - Rehab Studio App

Questa cartella contiene solo i file sorgente fondamentali da caricare su GitHub.

## File inclusi

- `prova.py`: applicazione principale.
- `api/index.py`: adapter per Vercel Python Functions.
- `vercel.json`: routing Vercel.
- `static/`: CSS, JS, immagini e PWA assets.
- `rehab_app/`: moduli interni.
- `requirements.txt`: dipendenze Python.
- `.python-version`: runtime Python consigliata.

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
- `DATABASE_URL`: connection string Supabase/Postgres dello studio. Usa la Transaction Pooler string.
- `STRIPE_SECRET_KEY`: chiave privata Stripe.
- `STRIPE_WEBHOOK_SECRET`: webhook secret Stripe.
- Config SMTP, se decidi di portarli su env invece che impostarli da pannello app.

## Stripe

Per ogni studio configura Stripe su Vercel con:

1. `STRIPE_SECRET_KEY`: chiave privata `sk_test_...` o `sk_live_...`.
2. `STRIPE_WEBHOOK_SECRET`: secret `whsec_...` del webhook.
3. Webhook endpoint Stripe:
   - URL: `https://dominio-studio.vercel.app/stripe/webhook`
   - eventi: `checkout.session.completed`, `checkout.session.async_payment_succeeded`
4. Metodi pagamento: abilitali dalla Dashboard Stripe. L'app non forza `payment_method_types`, quindi Checkout puo mostrare carta, Apple Pay, Google Pay, PayPal o altri metodi se disponibili per account, paese, valuta, dominio e dispositivo.

Per Apple Pay/Google Pay puo essere necessaria la verifica dominio in Stripe.

## Supabase per singolo studio

Per ogni nuovo studio:

1. Crea manualmente un progetto Supabase intestato allo studio.
2. Copia la connection string `Transaction pooler`.
3. Inseriscila su Vercel come `DATABASE_URL`.
4. Fai redeploy.
5. Apri l'app e completa il setup studio.

La password Supabase non deve mai essere salvata nel repository GitHub.

## Nota importante su Vercel

Vercel Functions hanno filesystem read-only, con solo `/tmp` scrivibile in modo temporaneo. Per questo in produzione l'app usa `DATABASE_URL` quando presente. SQLite resta disponibile solo per uso locale/offline.

Per una produzione vera servono:

- database esterno persistente, ora gestito tramite Supabase/Postgres;
- storage esterno dedicato se in futuro vuoi scaricare o conservare i PDF firmati fuori dal DB;
- secret management tramite variabili ambiente;
- webhook Stripe configurato sul dominio pubblico.

Questo pacchetto serve per avviare il deploy e testare l'app su Vercel, ma la persistenza sanitaria reale va completata prima di aprirla al pubblico.
