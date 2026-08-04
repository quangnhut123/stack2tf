component "base" {
  source = "./modules/base"
  inputs = {
    name = var.name
  }
}

component "consumer" {
  source     = "./modules/consumer"
  depends_on = [component.base]
  inputs = {
    name          = var.name
    upstream_id   = component.base.id
    upstream_name = component.base.name_out
  }
}
