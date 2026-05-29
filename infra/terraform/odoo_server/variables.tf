variable "cloud_provider" {
  description = "Cloud provider: DIGITALOCEAN or AWS"
  type        = string
}

variable "name" {
  description = "Base server name"
  type        = string
}

variable "odoo_version" {
  description = "Odoo major version (18 or 19)"
  type        = string
}

variable "region" {
  description = "Region for the server"
  type        = string
}

variable "size" {
  description = "Instance size slug/type"
  type        = string
}

variable "organization_id" {
  description = "Organization identifier from DafeApp"
  type        = number
}

variable "do_image" {
  description = "DigitalOcean image slug"
  type        = string
  default     = "ubuntu-22-04-x64"
}

variable "aws_region" {
  description = "Optional explicit AWS region override"
  type        = string
  default     = ""
}
