# gnrasgiserve — Guide for GenroPy Developers

## What is it

`gnrasgiserve` is the ASGI replacement for `gnrwsgiserve`. It starts a
GenroPy site exactly like the traditional command, but uses **uvicorn**
(ASGI) instead of **werkzeug** (WSGI). The GenroPy site runs unmodified
behind a transparent ASGI gateway.

If you know how to use `gnrwsgiserve`, you already know how to use
`gnrasgiserve`: same options, same behavior, same startup flow.

## Prerequisites

1. **GenroPy installed** and configured (`~/.gnr/environment.xml` present)
2. **An existing GenroPy site** (with `root.py` in the site directory)
3. **genropy-asgi installed**:

```bash
pip install -e /path/to/genro-asgi
pip install -e /path/to/genro-asgi/contrib/genropy_asgi
```

After installation, the `gnrasgiserve` command is available in your PATH.

## Basic usage

```bash
gnrasgiserve <site_name>
```

Where `<site_name>` is the GenroPy instance name (the same you use with
`gnrwsgiserve` or `gnr web serve`).

### Examples

```bash
# Start "test_invoice_pg" on the default port (8080)
gnrasgiserve test_invoice_pg

# Specify port
gnrasgiserve test_invoice_pg -p 9000

# Enable auto-reload (restarts when you modify files)
gnrasgiserve test_invoice_pg --reload

# Open browser automatically
gnrasgiserve test_invoice_pg -o

# Custom port + host
gnrasgiserve test_invoice_pg -p 9000 -H 127.0.0.1
```

Once started, the site is reachable at `http://<host>:<port>/index`.

## Full options reference

| Option | Description | Default |
| ------ | ----------- | ------- |
| `site_name` | GenroPy instance name (positional) | — |
| `-s`, `--site` | Site name (alternative to positional) | — |
| `-p`, `--port` | Listening port | `8080` |
| `-H`, `--host` | Listening address | `0.0.0.0` |
| `--reload` | Enable file monitor (auto-restart on changes) | off |
| `--noreload` | Explicitly disable file monitor | — |
| `--nodebug` | Disable debug mode | — |
| `-o`, `--open` | Open browser on startup | off |
| `-c`, `--config` | Path to gnrserve directory (overrides `~/.gnr`) | `~/.gnr` |
| `-n`, `--noclean` | Skip clean restart | off |
| `--restore PATH` | Restore from a backup path | — |
| `--source_instance` | Import data from another instance | — |
| `--remote_edit` | Enable remote editing | off |
| `--remotedb [NAME]` | Use a remote database (via gnrdaemon) | — |
| `--ssl` | Enable SSL | off |
| `--ssl_cert` | Path to SSL certificate | — |
| `--ssl_key` | Path to SSL key | — |
| `--counter` | Startup counter | — |

## Remote database (--remotedb)

To develop locally while connecting to a database on a remote server:

```bash
gnrasgiserve test_invoice_pg --remotedb myremote
```

**Prerequisites**:

1. The **gnrdaemon** must be running (it creates the SSH tunnel)
2. The remote DB configuration must be in `~/.gnr/instanceconfig/default.xml`:

```xml
<GenRoBag>
    <remote_db>
        <myremote ssh_host="1.2.3.4" ssh_user="root"
                  host="127.0.0.1" port="5432"
                  dbname="mydb" user="dbuser"
                  password="dbpassword"/>
    </remote_db>
</GenRoBag>
```

If you pass `--remotedb` without a name, the site name is used as the
remote name.

## How it works internally

The startup flow is identical to `gnrwsgiserve` for the first 6 steps:

```
1. Parse CLI arguments
2. Read GenroPy configuration (~/.gnr/environment.xml)
3. Resolve site path (PathResolver)
4. Load site's siteconfig
5. Merge options (CLI > env vars > siteconfig > defaults)
6. Create GnrWsgiSite (the GenroPy application)
```

Step 7 is the difference: instead of passing the site to werkzeug, it
mounts it as the main application on an `AsgiServer` (served by uvicorn)
through `GenropyProxy`, an `AsgiApplication` that wraps the site.

```
gnrwsgiserve:  GnrWsgiSite → werkzeug (WSGI)
gnrasgiserve:  GnrWsgiSite → GenropyProxy → AsgiServer → uvicorn (ASGI)
```

The GenroPy site is not modified in any way. The proxy translates ASGI
requests into WSGI calls (PEP 3333) and vice versa.

## Option resolution order

Options follow this priority (highest to lowest):

1. **CLI arguments** (`-p 9000`)
2. **Environment variables** (`GNR_WSGI_OPT_port=9000`)
3. **Site's siteconfig** (`wsgi?port` in siteconfig.xml)
4. **Defaults** (host=0.0.0.0, port=8080, debug=true)

## Differences from gnrwsgiserve

| Aspect | gnrwsgiserve | gnrasgiserve |
| ------ | ------------ | ------------ |
| WSGI server | werkzeug | uvicorn (via ASGI gateway) |
| Protocol | WSGI | ASGI (with WSGI bridge) |
| WebSocket | Not supported | Supported (native ASGI) |
| CLI options | Identical | Identical |
| Configuration | Identical | Identical |
| GenroPy site | Unmodified | Unmodified |
| Command | `gnrwsgiserve site` | `gnrasgiserve site` |

## Troubleshooting

### "site name is required"
You did not pass the site name. Use:
```bash
gnrasgiserve mysite
```

### "Error: no ~/.gnr/ or /etc/gnr/ found"
GenroPy is not configured. Create `~/.gnr/environment.xml` with your
GenroPy environment paths.

### "Error: no root.py in the site provided"
The site exists in the configuration but has no `root.py` file. Verify
the path with:
```bash
gnr app sitepath <site_name>
```

### Site does not respond

Check that the port is not already in use:
```bash
lsof -i :<port>
```

### --remotedb does not work

1. Verify gnrdaemon is running: `ps aux | grep gnrdaemon`
2. Check the daemon's actual listening port: `lsof -i -P -n | grep <PID>`
3. Verify the configuration in `~/.gnr/instanceconfig/default.xml`
   is correct

## GNR_CURRENT_SITE environment variable

If you set `GNR_CURRENT_SITE`, you can omit the site name:

```bash
export GNR_CURRENT_SITE=test_invoice_pg
gnrasgiserve    # uses test_invoice_pg
```
