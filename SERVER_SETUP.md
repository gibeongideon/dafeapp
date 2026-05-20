# DafeApp Server Setup

Production domain: `dafeapp.com`

Current production server IP: `192.34.61.66`

This project deploys DafeApp with Docker Compose on the server. GitHub Actions builds the Docker image from the `dev` branch, pushes it to GHCR, copies `docker-compose.prod.yml` and `Caddyfile` to `/opt/dafeapp`, then starts the stack on the server.

## 1. Point DNS to the Server

Create or update these DNS records:

```text
A    dafeapp.com      192.34.61.66
A    www.dafeapp.com  192.34.61.66
```

Wait for DNS propagation before expecting HTTPS certificates to issue successfully.

## 2. Bootstrap the Server

Run the one-time setup script from your local machine:

```bash
ssh root@192.34.61.66 "bash -s" < scripts/droplet-setup.sh
```

The script installs Docker and Docker Compose, creates `/opt/dafeapp`, and writes a starter `/opt/dafeapp/.env`.

The GitHub Actions deploy workflow also runs this bootstrap automatically if Docker or Docker Compose is missing. On a fresh server, the first workflow run may stop after bootstrap and ask you to edit `/opt/dafeapp/.env`; that is expected.

## 3. Configure `/opt/dafeapp/.env`

SSH into the server:

```bash
ssh root@192.34.61.66
nano /opt/dafeapp/.env
```

Use the domain values below:

```env
DEBUG=False
ALLOWED_HOSTS=dafeapp.com,www.dafeapp.com
CSRF_TRUSTED_ORIGINS=https://dafeapp.com,https://www.dafeapp.com
SITE_URL=https://dafeapp.com
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_SSL_REDIRECT=True
```

Also set these required secrets:

```env
SECRET_KEY=<strong-random-django-secret>
DB_PASSWORD=<strong-database-password>
DATABASE_URL=postgres://dafeapp:<same-db-password>@db:5432/dafeapp
FIELD_ENCRYPTION_KEY=<fernet-key>
```

Generate values if needed:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add provider and payment secrets as needed:

```env
DIGITALOCEAN_TOKEN=
GITHUB_CLIENT_ID=
GITHUB_SECRET=
PAYSTACK_SECRET_KEY=
PAYSTACK_PUBLIC_KEY=
PAYSTACK_CURRENCY=USD
```

## 4. Configure GitHub Actions Secrets

In GitHub, open the repository settings and set:

```text
DO_HOST=192.34.61.66
DO_USER=root
DO_SSH_KEY=<private SSH key that can access root@192.34.61.66>
GHCR_TOKEN=<GitHub token with package access>
FIELD_ENCRYPTION_KEY=<same Fernet key used in /opt/dafeapp/.env>
```

## 5. Deploy

Push to the `dev` branch:

```bash
git push origin dev:dev
```

The workflow in `.github/workflows/deploy.yml` will build and deploy the app.

If the workflow says Docker is missing, bootstrap the server manually:

```bash
ssh root@192.34.61.66 "bash -s" < scripts/droplet-setup.sh
```

If the workflow says `/opt/dafeapp/.env` contains placeholders, edit the env file on the server and rerun the workflow.

You can check the server manually:

```bash
ssh root@192.34.61.66
cd /opt/dafeapp
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f web
```

## 6. Create the Admin User

After the containers are running:

```bash
ssh root@192.34.61.66
cd /opt/dafeapp
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
```

This project uses email as the login field.

## 7. Expected URL

After DNS and deploy are complete:

```text
https://dafeapp.com
```

Caddy handles HTTPS automatically for `dafeapp.com` using the root `Caddyfile`.
