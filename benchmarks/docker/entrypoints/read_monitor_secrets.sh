# Read the monitor's two secrets and export them. SOURCED, never executed.
#
#     . /lab/entrypoints/read_monitor_secrets.sh <file>   || exit 1
#
# The file is DATA, not a script. It used to be read with `.` — the shell then
# executes it, so a password holding a space, a `#`, a `$`, a quote or a
# backslash changes meaning or breaks the parse, and a crafted value would run
# as a command. Here each line must match one fixed name and a base64 payload,
# nothing is evaluated, and what the file holds never reaches a shell parser.
#
#     GNR_ASGI_STORAGE_KEY_B64=<base64>
#     GNR_ASGI_ADMIN_PASSWORD_B64=<base64>
#
# Encode with no trailing newline in the value:
#     printf %s "$secret" | base64 | tr -d '\n'
#
# A missing file, a line that does not match, a payload that will not decode or
# one that decodes to nothing: the caller is told, and the bridge does not
# start. The monitor is never open because a secret was unreadable.

read_monitor_secret () {
    # One name's base64 payload, decoded, or nothing at all.
    local file="$1" name="$2" payload
    payload=$(sed -n "s/^${name}_B64=\\([A-Za-z0-9+/=]\\{1,\\}\\)\$/\\1/p" "$file" | head -1)
    [ -n "$payload" ] || return 1
    printf '%s' "$payload" | base64 -d 2>/dev/null
}

read_monitor_secrets () {
    local file="${1:-}" name value
    if [ -z "$file" ] || [ ! -r "$file" ]; then
        echo "il file dei segreti del monitor non è leggibile: ${file:-(nessun path)}" >&2
        return 1
    fi
    for name in GNR_ASGI_STORAGE_KEY GNR_ASGI_ADMIN_PASSWORD; do
        value=$(read_monitor_secret "$file" "$name") || value=""
        if [ -z "$value" ]; then
            echo "${name}_B64 assente, malformata o vuota in $file" >&2
            return 1
        fi
        # Assigned by name, not through eval. There are two names and they are
        # both written here, so nothing about a secret ever reaches a parser.
        case "$name" in
            GNR_ASGI_STORAGE_KEY)
                GNR_ASGI_STORAGE_KEY="$value"; export GNR_ASGI_STORAGE_KEY ;;
            GNR_ASGI_ADMIN_PASSWORD)
                GNR_ASGI_ADMIN_PASSWORD="$value"; export GNR_ASGI_ADMIN_PASSWORD ;;
        esac
    done
    return 0
}
