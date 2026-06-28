# Architecture

## Overview

genropy-asgi sits between genro-asgi (ASGI framework) and GenroPy (legacy framework).
It bridges the two worlds without either knowing about the other.

```
┌─────────────────────────────────────────────┐
│              Your Project                    │
│  (SourcererAPI, MyAPI, etc.)                │
│  RoutingClass with @route() methods         │
├─────────────────────────────────────────────┤
│          OpenApiApplication                  │
│  Wraps RoutingClass, adds /openapi + /docs  │
├─────────────────────────────────────────────┤
│        GenropyAsgiServer                     │  ← genropy-asgi
│  Reads genropy_app from config              │
│  Creates GnrApp, registers db               │
├─────────────────────────────────────────────┤
│            AsgiServer                        │  ← genro-asgi
│  db_registry, middleware, routing            │
├─────────────────────────────────────────────┤
│          ASGI (uvicorn)                      │
└─────────────────────────────────────────────┘
```

## Database flow

### Config-driven

```yaml
databases:
  default:
    genropy_app: "sourcerer"
```

1. `GenropyAsgiServer.__init__()` calls `super().__init__()` (AsgiServer setup)
2. `_load_databases()` reads the `databases` config section
3. For each `genropy_app` entry: creates `GnrApp(app_name)`
4. Registers `gnr_app.db` in `server.db_registry`
5. Apps/routing classes access it via `server.get_db(name)`

### DbRoutingClass chain

When using `DbRoutingClass` from genro-routes, the db property walks up
the `_routing_parent` chain automatically:

```
SubModule.db → (no local _db) → parent.db
    ↑
RootAPI.db → (has _db from constructor) → returns it
```

If no node in the routing chain has a local db, it falls through to the
server level where `db_registry` provides it.

## Why a separate package

- `genro-asgi` is framework-agnostic with minimal dependencies
- GenroPy is a large legacy framework
- Keeping the integration separate means:
  - genro-asgi stays clean
  - Projects without GenroPy are not affected
  - The integration can evolve independently

## Subclassing GenropyAsgiServer

For project-specific needs (custom auth, shared resources):

```python
from genropy_asgi import GenropyAsgiServer

class MyServer(GenropyAsgiServer):
    __slots__ = ("my_resource",)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.my_resource = self._setup_resource()

    def authenticate(self, scope):
        # Custom auth using GenroPy db
        db = self.get_db("default")
        token = extract_token(scope)
        return validate(db, token)
```
