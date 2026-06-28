# Config Reference

GenropyAsgiServer reads the standard genro-asgi `config.yaml` plus
the `databases` section for GenroPy app connections.

## Full example

```yaml
server:
  host: "0.0.0.0"
  port: 8082
  reload: false

databases:
  default:
    genropy_app: "sourcerer"
  analytics:
    genropy_app: "analytics_app"

middleware:
  cors: on
  errors: on

plugins:
  auth:
    rule: "developer|owner"

apps:
  api:
    module: "my_project.api:MyAPI"
    db_name: default

openapi:
  title: "My API"
  version: "1.0.0"
```

## databases section

Each key is a logical name for the db_registry.

### GenroPy connection

```yaml
databases:
  default:
    genropy_app: "sourcerer"    # GnrApp instance name
```

Creates `GnrApp("sourcerer")` and registers `gnr_app.db` as `"default"`.

### Multiple databases

```yaml
databases:
  main:
    genropy_app: "myapp"
  reporting:
    genropy_app: "reports"
```

Access: `server.get_db("main")`, `server.get_db("reporting")`.

## apps section — db_name

Apps can declare which database they use:

```yaml
apps:
  api:
    module: "my_project.api:MyAPI"
    db_name: default          # resolved from db_registry
```

When `request.db` is accessed, it looks up the app's `db_name` in the registry.

## All inherited sections

All standard genro-asgi config sections work: `server`, `middleware`,
`plugins`, `apps`, `sys_apps`, `openapi`. See genro-asgi documentation
for details.
