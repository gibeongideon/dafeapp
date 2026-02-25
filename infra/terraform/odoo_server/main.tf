terraform {
  required_version = ">= 1.5.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

provider "aws" {
  region = var.aws_region != "" ? var.aws_region : var.region
}

locals {
  is_do  = upper(var.provider) == "DIGITALOCEAN"
  is_aws = upper(var.provider) == "AWS"
}

resource "random_id" "suffix" {
  byte_length = 2
}

resource "digitalocean_droplet" "odoo" {
  count  = local.is_do ? 1 : 0
  name   = "${var.name}-${var.odoo_version}-${random_id.suffix.hex}"
  region = var.region
  size   = var.size
  image  = var.do_image

  monitoring = true
  backups    = false
  ipv6       = false
}

resource "digitalocean_firewall" "odoo" {
  count = local.is_do ? 1 : 0
  name  = "${var.name}-${var.odoo_version}-fw-${random_id.suffix.hex}"

  droplet_ids = [digitalocean_droplet.odoo[0].id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}

data "aws_vpc" "default" {
  count   = local.is_aws ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = local.is_aws ? 1 : 0
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
}

data "aws_ami" "ubuntu" {
  count       = local.is_aws ? 1 : 0
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_security_group" "odoo" {
  count       = local.is_aws ? 1 : 0
  name        = "${var.name}-${var.odoo_version}-sg-${random_id.suffix.hex}"
  description = "Firewall for Odoo server"
  vpc_id      = data.aws_vpc.default[0].id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "odoo" {
  count                       = local.is_aws ? 1 : 0
  ami                         = data.aws_ami.ubuntu[0].id
  instance_type               = var.size
  subnet_id                   = data.aws_subnets.default[0].ids[0]
  vpc_security_group_ids      = [aws_security_group.odoo[0].id]
  associate_public_ip_address = true

  tags = {
    Name        = "${var.name}-${var.odoo_version}"
    DafeApp     = "true"
    OdooVersion = var.odoo_version
  }
}
