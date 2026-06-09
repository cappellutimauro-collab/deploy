# Rehab Philosophy - Checklist Produzione

## Variabili richieste
- `APP_ENV=production`
- `FISIO_SECRET=<segreto lungo e random>`
- `ADMIN_EMAIL=<email admin>`
- `ADMIN_PASSWORD=<password iniziale forte>`
- `APP_BASE_URL=https://tuo-dominio.it`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USER`, `SMTP_PASSWORD`
- `STRIPE_SECRET_KEY` e, quando attivi il webhook, `STRIPE_WEBHOOK_SECRET`

## Infrastruttura
- HTTPS obbligatorio con reverse proxy o hosting gestito.
- Database e cartella `consensi informati` su volume persistente e con backup automatici.
- Proteggere `app_secret.key`, `stripe_secret_key.txt`, `email_settings.json` e backup.
- Configurare webhook Stripe su `/stripe/webhook`.

## Note
- Il codice include CSRF, rate limiting base, cookie sicuri dietro HTTPS, security headers e backup SQLite giornaliero locale.
- Per dati sanitari reali valuta cifratura a riposo con storage/volume cifrato o SQLCipher.
- Prima del pubblico far validare informativa privacy/consenso da consulente privacy.


## Avvio in hosting
- Procfile: `web: python prova.py`
- Docker: build con `docker build -t rehab-philosophy .` e run con variabili env + volume persistente.
- L'app legge automaticamente `PORT`; in produzione usa host `0.0.0.0` e non apre il browser.

## Test locali
- `python -m py_compile prova.py`
- `python -m unittest discover -s tests`

## Da non committare/pubblicare
- `fisio_app.sqlite3`
- `app_secret.key`
- `stripe_secret_key.txt`
- `email_settings.json`
- cartelle `consensi informati`, `backups`, `email_outbox`
