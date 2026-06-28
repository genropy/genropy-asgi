# genropy-asgi — Usage Guide

## Installation

```bash
pip install -e /path/to/genro-asgi
pip install -e /path/to/genro-asgi/contrib/genropy_asgi
```

## Mode 1: Script only (no YAML)

```python
# run.py
from genropy_asgi import GenropyAsgiServer
from genro_asgi import OpenApiApplication
from sourcerer_core.api import SourcererAPI

server = GenropyAsgiServer()
server.register_db("default", server.gnr_apps["sourcerer"].db)

# But since GenropyAsgiServer reads databases from config,
# and we have no config, we create GnrApp manually:
from gnr.app.gnrapp import GnrApp
gnr_app = GnrApp("sourcerer")

api = SourcererAPI(gnr_app.db, voyage_api_key="your-key")
app = OpenApiApplication(routing_class=api, docs="swagger")

from genro_asgi import AsgiServer
server = AsgiServer()
server.mount("api", app)
server.run()
```

Simpler — the server runs on 127.0.0.1:8000 with defaults.

## Mode 2: With config.yaml (recommended)

### Directory structure

```
my_server/
  config.yaml
  run.py
```

### config.yaml

```yaml
server:
  host: "0.0.0.0"
  port: 8082

databases:
  default:
    genropy_app: "sourcerer"

apps:
  api:
    module: "sourcerer_core.api.sourcerer_api:SourcererAPI"
    db_name: default
    voyage_api_key: "your-key-here"
    docs: swagger
```

### run.py

```python
from genropy_asgi import GenropyAsgiServer
from genro_asgi import OpenApiApplication
from sourcerer_core.api import SourcererAPI

server = GenropyAsgiServer(server_dir=".")

# Create API with db from registry
api = SourcererAPI(server.get_db("default"))
app = OpenApiApplication(routing_class=api, docs="swagger")
server.mount("api", app)

server.run()
```

### Run

```bash
cd my_server
python run.py
```

### Endpoints

- `http://localhost:8082/api/docs` — Swagger UI
- `http://localhost:8082/api/openapi` — OpenAPI 3.1 JSON schema
- `http://localhost:8082/api/api/code/search_symbols?query=Router` — API endpoint

## Mode 3: Fully declarative

The config.yaml handles everything — no manual wrapping needed:

```python
from genropy_asgi import GenropyAsgiServer
server = GenropyAsgiServer(server_dir=".")
server.run()
```

```yaml
server:
  host: "0.0.0.0"
  port: 8082

databases:
  default:
    genropy_app: "sourcerer"

apps:
  api:
    module: "sourcerer_core.api.sourcerer_api:SourcererAPI"
    db_name: default
    voyage_api_key: "your-key-here"
    docs: swagger
```

The server auto-detects whether the declared class is an `AsgiApplication`
subclass or a plain `RoutingClass`. Plain routing classes are automatically
wrapped in `OpenApiApplication`:

- `docs` controls the documentation style (`swagger`, `redoc`, or `off`)
- `db_name` and `base_dir` are forwarded to the wrapper
- All other kwargs are passed to the `RoutingClass` constructor

If `docs` is omitted, the default is `off` (no Swagger UI), but the
wrapping still occurs so that `base_dir`, `db_name`, and lifecycle
hooks are available.

## What GenropyAsgiServer does

GenropyAsgiServer extends AsgiServer with one capability: reading
`genropy_app` entries from the `databases` config section.

For each entry like:

```yaml
databases:
  default:
    genropy_app: "sourcerer"
```

It creates a `GnrApp("sourcerer")` and registers `gnr_app.db` in
the server's `db_registry` under the given name ("default").

Apps and routing classes access the db via:
- `server.get_db("default")` — explicit
- `request.db` — automatic (resolves via app's db_name)
- `self.db` — via DbRoutingClass chain (walks up _routing_parent)
