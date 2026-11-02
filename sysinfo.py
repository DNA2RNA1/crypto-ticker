"""System facts for the phone page's Status section (Linux; empty off a Pi)."""


def wifi(path="/proc/net/wireless"):
    """Signal of the first wireless interface, or None (wired, no Wi-Fi, not Linux).

    /proc/net/wireless looks like:
        Inter-| sta-|   Quality        |   Discarded packets ...
         face | tus | link level noise |  nwid  crypt   frag ...
         wlan0: 0000   54.  -56.  -256        0      0      0 ...
    """
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()[2:]
    except OSError:
        return None
    for line in lines:
        if ":" not in line:
            continue
        iface, _, rest = line.partition(":")
        parts = rest.split()
        if len(parts) < 3:
            continue
        try:
            link = float(parts[1].rstrip("."))
            level = float(parts[2].rstrip("."))
        except ValueError:
            continue
        if level > 0:  # some drivers report 0-100 or 256-based units instead of dBm
            level = level - 256 if level > 63 else None
        quality = max(0, min(100, round(link * 100 / 70)))  # Pi Wi-Fi reports link out of 70
        if level is not None:
            label = "excellent" if level >= -55 else "good" if level >= -67 else "fair" if level >= -75 else "weak"
        else:
            label = "excellent" if quality >= 80 else "good" if quality >= 60 else "fair" if quality >= 40 else "weak"
        return {"interface": iface.strip(), "dbm": level, "quality": quality, "label": label}
    return None


def system_uptime(path="/proc/uptime"):
    """Seconds since the Pi booted, or None."""
    try:
        with open(path) as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
