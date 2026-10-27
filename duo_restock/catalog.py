"""Built-in iPhone Duo part numbers, so users pick capacity + color instead of
hunting for part numbers in browser dev tools.

Source: Apple buy-page product data per region (snapshot October 2026). If Apple
adds a color or changes numbers, put the `part` straight into config.toml.
"""

CAPACITIES = ["256gb", "512gb", "1tb", "2tb"]
COLORS = {"nightsky": "Night Sky", "starwhite": "Star White"}

DUO_PARTS: dict[str, dict[tuple[str, str], str]] = {
    "us": {
        ("256gb", "nightsky"): "MK1F4LL/A",
        ("256gb", "starwhite"): "MK1E4LL/A",
        ("512gb", "nightsky"): "MK1H4LL/A",
        ("512gb", "starwhite"): "MK1G4LL/A",
        ("1tb", "nightsky"): "MK1L4LL/A",
        ("1tb", "starwhite"): "MK1J4LL/A",
        ("2tb", "nightsky"): "MK1R4LL/A",
        ("2tb", "starwhite"): "MK1Q4LL/A",
    },
    "cn": {
        ("256gb", "nightsky"): "MK2N4CH/A",
        ("256gb", "starwhite"): "MK2M4CH/A",
        ("512gb", "nightsky"): "MK2Q4CH/A",
        ("512gb", "starwhite"): "MK2P4CH/A",
        ("1tb", "nightsky"): "MK2U4CH/A",
        ("1tb", "starwhite"): "MK2T4CH/A",
        ("2tb", "nightsky"): "MK2W4CH/A",
        ("2tb", "starwhite"): "MK2V4CH/A",
    },
    "hk": {
        ("256gb", "nightsky"): "MK2E4ZA/A",
        ("256gb", "starwhite"): "MK2D4ZA/A",
        ("512gb", "nightsky"): "MK2G4ZA/A",
        ("512gb", "starwhite"): "MK2F4ZA/A",
        ("1tb", "nightsky"): "MK2J4ZA/A",
        ("1tb", "starwhite"): "MK2H4ZA/A",
        ("2tb", "nightsky"): "MK2L4ZA/A",
        ("2tb", "starwhite"): "MK2K4ZA/A",
    },
    "tw": {
        ("256gb", "nightsky"): "MK2E4ZP/A",
        ("256gb", "starwhite"): "MK2D4ZP/A",
        ("512gb", "nightsky"): "MK2G4ZP/A",
        ("512gb", "starwhite"): "MK2F4ZP/A",
        ("1tb", "nightsky"): "MK2J4ZP/A",
        ("1tb", "starwhite"): "MK2H4ZP/A",
        ("2tb", "nightsky"): "MK2L4ZP/A",
        ("2tb", "starwhite"): "MK2K4ZP/A",
    },
    "jp": {
        ("256gb", "nightsky"): "MK254J/A",
        ("256gb", "starwhite"): "MK244J/A",
        ("512gb", "nightsky"): "MK274J/A",
        ("512gb", "starwhite"): "MK264J/A",
        ("1tb", "nightsky"): "MK294J/A",
        ("1tb", "starwhite"): "MK284J/A",
        ("2tb", "nightsky"): "MK2C4J/A",
        ("2tb", "starwhite"): "MK2A4J/A",
    },
    "sg": {
        ("256gb", "nightsky"): "MK2E4X/A",
        ("256gb", "starwhite"): "MK2D4X/A",
        ("512gb", "nightsky"): "MK2G4X/A",
        ("512gb", "starwhite"): "MK2F4X/A",
        ("1tb", "nightsky"): "MK2J4X/A",
        ("1tb", "starwhite"): "MK2H4X/A",
        ("2tb", "nightsky"): "MK2L4X/A",
        ("2tb", "starwhite"): "MK2K4X/A",
    },
    "ca": {
        ("256gb", "nightsky"): "MK254VC/A",
        ("256gb", "starwhite"): "MK244VC/A",
        ("512gb", "nightsky"): "MK274VC/A",
        ("512gb", "starwhite"): "MK264VC/A",
        ("1tb", "nightsky"): "MK294VC/A",
        ("1tb", "starwhite"): "MK284VC/A",
        ("2tb", "nightsky"): "MK2C4VC/A",
        ("2tb", "starwhite"): "MK2A4VC/A",
    },
    "uk": {
        ("256gb", "nightsky"): "MK2E4QN/A",
        ("256gb", "starwhite"): "MK2D4QN/A",
        ("512gb", "nightsky"): "MK2G4QN/A",
        ("512gb", "starwhite"): "MK2F4QN/A",
        ("1tb", "nightsky"): "MK2J4QN/A",
        ("1tb", "starwhite"): "MK2H4QN/A",
        ("2tb", "nightsky"): "MK2L4QN/A",
        ("2tb", "starwhite"): "MK2K4QN/A",
    },
    "au": {
        ("256gb", "nightsky"): "MK2E4X/A",
        ("256gb", "starwhite"): "MK2D4X/A",
        ("512gb", "nightsky"): "MK2G4X/A",
        ("512gb", "starwhite"): "MK2F4X/A",
        ("1tb", "nightsky"): "MK2J4X/A",
        ("1tb", "starwhite"): "MK2H4X/A",
        ("2tb", "nightsky"): "MK2L4X/A",
        ("2tb", "starwhite"): "MK2K4X/A",
    },
}
DUO_PARTS["hk-zh"] = DUO_PARTS["hk"]


def normalize(capacity: str, color: str) -> tuple[str, str]:
    cap = capacity.lower().replace(" ", "")
    col = color.lower().replace(" ", "").replace("-", "").replace("_", "")
    return cap, col


def lookup(region: str, capacity: str, color: str) -> str | None:
    return DUO_PARTS.get(region, {}).get(normalize(capacity, color))


def display_name(capacity: str, color: str) -> str:
    cap, col = normalize(capacity, color)
    return f"iPhone Duo {cap.upper()} {COLORS.get(col, color)}"
