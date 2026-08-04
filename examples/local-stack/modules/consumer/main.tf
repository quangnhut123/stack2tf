variable "name" { type = string }
variable "upstream_id" {}       # comes from base.id (unknown at plan)
variable "upstream_name" {}     # comes from base.name_out (known at plan)

resource "terraform_data" "y" {
  input = "${var.name}:${var.upstream_name}"
}

output "done" { value = terraform_data.y.id }
