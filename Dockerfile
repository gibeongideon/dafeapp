FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps: postgres client, build tools, SSH, Ansible, Terraform
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl gnupg lsb-release \
    openssh-client sshpass rsync \
    ansible \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source (lvenv/, var/, .env excluded via .dockerignore)
COPY . .

EXPOSE 8000

CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "dafeapp.asgi:application"]
