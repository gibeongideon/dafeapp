locals {
  do_instance_id  = length(digitalocean_droplet.odoo) > 0 ? tostring(digitalocean_droplet.odoo[0].id) : ""
  do_public_ip    = length(digitalocean_droplet.odoo) > 0 ? digitalocean_droplet.odoo[0].ipv4_address : ""
  aws_instance_id = length(aws_instance.odoo) > 0 ? aws_instance.odoo[0].id : ""
  aws_public_ip   = length(aws_instance.odoo) > 0 ? aws_instance.odoo[0].public_ip : ""
}

output "instance_id" {
  value = local.do_instance_id != "" ? local.do_instance_id : local.aws_instance_id
}

output "public_ip" {
  value = local.do_public_ip != "" ? local.do_public_ip : local.aws_public_ip
}
