"""Parses a settings mapping into normalised values."""

DEFAULT_PORT = 8080


def parse_port(settings):
    value = settings.get("port", DEFAULT_PORT)
    return int(value)


def parse_hosts(settings):
    value = settings.get("hosts")
    if value is None:
        return []
    return list(value)


def parse_debug(settings):
    return bool(settings.get("debug", False))


def parse_name(settings):
    value = settings.get("name")
    if value is None:
        return None
    return value.strip()


def parse_all(settings):
    return {
        "port": parse_port(settings),
        "hosts": parse_hosts(settings),
        "debug": parse_debug(settings),
        "name": parse_name(settings),
    }
