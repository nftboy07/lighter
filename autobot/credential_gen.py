"""
credential_gen.py — Unique username, strong password, and display-name generator.

All values are cryptographically random; passwords are never reused across calls.
"""

import secrets
import string
import random

# ─── Word lists ──────────────────────────────────────────────────────────────

_ADJECTIVES = [
    "Swift", "Bright", "Calm", "Dark", "Epic", "Fast", "Grand", "Happy",
    "Iron", "Jade", "Kind", "Lunar", "Misty", "Noble", "Olive", "Prime",
    "Quick", "Royal", "Solar", "Tidal", "Ultra", "Vivid", "Wild", "Xeno",
    "Young", "Zenith", "Arctic", "Blaze", "Cobalt", "Drift", "Ember",
    "Flint", "Gloom", "Haven", "Indie", "Jewel", "Karma", "Lumen", "Magne",
    "Nexus", "Onyx", "Pixel", "Quartz", "Raven", "Storm", "Terra", "Umbra",
    "Vault", "Wolfe", "Axiom", "Bolt", "Cipher", "Delta", "Echo", "Fable",
]

_NOUNS = [
    "Orbit", "Pulse", "Forge", "Ridge", "Crest", "Vale", "Peak", "Glyph",
    "Shard", "Blaze", "Trace", "Haze", "Core", "Dusk", "Dawn", "Flux",
    "Surge", "Void", "Rune", "Gate", "Link", "Node", "Path", "Seed",
    "Spark", "Wave", "Zone", "Arch", "Beam", "Cove", "Deck", "Edge",
    "Fern", "Glen", "Helm", "Isle", "Knot", "Lane", "Moor", "Nest",
    "Opal", "Pine", "Quill", "Reed", "Sage", "Thorn", "Urn", "Veil",
    "Wren", "Yarn", "Zinc", "Atlas", "Brine", "Cliff", "Dune", "Erin",
]

_FIRST_NAMES = [
    "Alex", "Blake", "Casey", "Dana", "Ellis", "Finley", "Gray", "Harper",
    "Indigo", "Jordan", "Kai", "Logan", "Morgan", "Noel", "Oakley", "Parker",
    "Quinn", "Reese", "Sage", "Taylor", "Uma", "Val", "Wren", "Xen",
    "Yael", "Zion", "Avery", "Bailey", "Corey", "Drew",
]

_LAST_NAMES = [
    "Stone", "Rivers", "Cross", "Banks", "Fields", "Lane", "Wells", "Moore",
    "Hayes", "Park", "Reid", "Shaw", "West", "Grant", "Cole", "Reed",
    "Nash", "Hunt", "Fox", "Boyd", "Wade", "King", "Bell", "Hall",
    "Ford", "Price", "Ray", "Day", "Kim", "Ash",
]

# Password character pools
_LOWER   = string.ascii_lowercase
_UPPER   = string.ascii_uppercase
_DIGITS  = string.digits
_SYMBOLS = "!#$%^&*-_=+"   # safe subset that most sites accept

_used_usernames: set[str] = set()
_used_passwords: set[str] = set()


# ─── Public API ───────────────────────────────────────────────────────────────

def generate_username(max_attempts: int = 100) -> str:
    """Return a unique AdjectiveNoun#### username."""
    for _ in range(max_attempts):
        adj  = secrets.choice(_ADJECTIVES)
        noun = secrets.choice(_NOUNS)
        num  = secrets.randbelow(9000) + 1000   # 1000–9999
        username = f"{adj}{noun}{num}"
        if username not in _used_usernames:
            _used_usernames.add(username)
            return username
    # Fallback: fully random hex suffix
    return f"User{secrets.token_hex(6)}"


def generate_password(length: int = 18) -> str:
    """
    Return a cryptographically strong password of *length* characters
    that satisfies upper, lower, digit, and symbol requirements.
    Guarantees uniqueness across the process lifetime.
    """
    for _ in range(200):
        # Guarantee at least one of each category
        mandatory = [
            secrets.choice(_UPPER),
            secrets.choice(_UPPER),
            secrets.choice(_LOWER),
            secrets.choice(_LOWER),
            secrets.choice(_DIGITS),
            secrets.choice(_DIGITS),
            secrets.choice(_SYMBOLS),
            secrets.choice(_SYMBOLS),
        ]
        pool = _LOWER + _UPPER + _DIGITS + _SYMBOLS
        rest = [secrets.choice(pool) for _ in range(length - len(mandatory))]
        chars = mandatory + rest
        secrets.SystemRandom().shuffle(chars)
        pw = "".join(chars)
        if pw not in _used_passwords:
            _used_passwords.add(pw)
            return pw
    raise RuntimeError("Could not generate unique password after 200 attempts")


def generate_display_name() -> str:
    """Return a random realistic first + last name."""
    return f"{secrets.choice(_FIRST_NAMES)} {secrets.choice(_LAST_NAMES)}"


def generate_credentials(email: str) -> dict:
    """
    Generate a complete set of registration credentials.

    Returns:
        {
            "email":        str,
            "username":     str,
            "password":     str,
            "display_name": str,
            "first_name":   str,
            "last_name":    str,
        }
    """
    first = secrets.choice(_FIRST_NAMES)
    last  = secrets.choice(_LAST_NAMES)
    return {
        "email":        email,
        "username":     generate_username(),
        "password":     generate_password(),
        "display_name": f"{first} {last}",
        "first_name":   first,
        "last_name":    last,
    }
