CREATION OF SERVER
Distribution
ubuntu 24.04
Architecture
x86_64
Support Tier
Tier 1
Check Summary
26 passed
0 failed
0 warnings

Check Results (26)
ssh connection
SSH connection established

root access
Running as root user

memory
2GB RAM available (minimum: 2GB)

disk space
52GB available on /var (minimum: 10GB)

cpu cores
2 CPU cores available

architecture
Architecture: x86_64

inotify watches
inotify watch limit: 524288 (recommended: 524288)

os detection
Ubuntu 24.04.3 LTS - Full Support - Auto-installation available

docker installed
Docker is installed (version 29.4.0)

docker version
Docker version 29.4.0 meets minimum (20.10)

docker running
Docker daemon is running and accessible

docker compose
Docker Compose v2 is available (5.1.3)

port 80
Port 80 (HTTP) is available

port 443
Port 443 (HTTPS) is available

port 8069
Port 8069 (Odoo) is available

dns resolution
DNS resolution is working

docker hub
Docker Hub is reachable

package repos
Package repositories are reachable

package curl
curl is installed

package wget
wget is installed

package git
git is installed

package zip
zip is installed

package unzip
unzip is installed

swap
1GB swap configured

external port 80
Port 80 (HTTP) is reachable externally (no service listening yet — normal before first deploy)

external port 443
Port 443 (HTTPS) is reachable externally (no service listening yet — normal before first deploy)







CREATION OF ODOO DOCKER INSTANCE STEPS

5:21:08 PM
[info]
Deployment initiated
5:21:09 PM
[info]
Initializing deployment...
5:21:09 PM
[info]
Deploying rapid-palm to 64.227.170.79
5:21:09 PM
[info]
Connecting to 64.227.170.79...
5:21:10 PM
[info]
SSH connection established (1.1s)
5:21:13 PM
[info]
Creating network oecsh_net_d20d9dd4-e9ee-4e28-af8d-fab7dec06966...
5:21:15 PM
[info]
Network created (1.6s)
5:21:15 PM
[info]
Configuring DNS record...
5:21:17 PM
[info]
DNS record created: rapid-palm-prod-8633.apps.oec.sh (2.0s)
5:21:17 PM
[info]
Creating PostgreSQL container...
5:21:43 PM
[info]
PostgreSQL container running (26.5s)
5:21:43 PM
[info]
Waiting for PostgreSQL to be ready...
5:21:44 PM
[info]
PostgreSQL ready
5:21:44 PM
[info]
Optimizing PostgreSQL with PGTune...
5:21:47 PM
[info]
PostgreSQL optimization complete (3.3s)
5:21:48 PM
[info]
No platform repos configured
5:21:48 PM
[info]
No organization repos configured
5:21:48 PM
[info]
No project-additional repos configured
5:21:48 PM
[info]
No module repositories configured
5:21:48 PM
[info]
No project repository configured, skipping
5:21:48 PM
[info]
Pulling Odoo 19.0 image...
5:22:47 PM
[info]
Image ready (58.3s)
5:22:47 PM
[info]
Generating Odoo configuration...
5:22:48 PM
[info]
Auto-config: Ignored platform-locked parameters in user config: proxy_mode. These are controlled by the deployment infrastructure.
5:22:48 PM
[info]
Auto-config: Migrated 'without_demo=all' to 'with_demo=False' for Odoo 19.0
5:22:48 PM
[info]
Starting Odoo container...
5:23:30 PM
[info]
Container started: cf7573ec0ecd (41.7s)
5:23:30 PM
[info]
Scanning all repos for apt.txt and requirements.txt...
5:23:30 PM
[info]
No repositories to check for dependencies
5:23:30 PM
[info]
No dependencies found (0.0s)
5:23:30 PM
[info]
Initializing Odoo database (this may take 1-2 minutes)...
5:23:31 PM
[info]
Running Odoo database initialization...
5:24:26 PM
[info]
Database initialized successfully
5:24:26 PM
[info]
Database initialized successfully (55.6s)
5:24:26 PM
[info]
Verifying DNS propagation (may take up to 3 minutes)...
5:24:26 PM
[info]
DNS propagation verified successfully (0.2s)
5:24:26 PM
[info]
Configuring Traefik routing...
5:25:08 PM
[info]
Traefik configured (42.1s)
5:25:08 PM
[info]
Performing health check: verifying container, database, and HTTP response...
5:25:12 PM
[info]
Health check passed! (3.9s)
5:25:12 PM
[info]
Deployment completed! Total: 243.1s (4.1 min)

