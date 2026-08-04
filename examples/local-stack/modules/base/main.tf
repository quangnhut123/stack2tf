variable "name" { type = string }

# terraform_data is built-in (no provider/credentials needed).
resource "terraform_data" "x" {
  input = var.name
}

# unknown until apply -> demonstrates placeholder derivation downstream
output "id" { value = terraform_data.x.output }

# known at plan -> demonstrates real value propagation downstream
output "name_out" { value = var.name }
