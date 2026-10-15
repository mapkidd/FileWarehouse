"""Asset Warehouse - Crates, Pallets and Manifests.

Crate    (.crate)    compressed + encrypted bundle of files
Pallet   (.pallet)   hierarchical, encrypted holder of crates
Manifest (.manifest) key used to encrypt / decrypt crates and pallets
"""

from .errors import (
    WarehouseError, FormatError, KeyMissingError, PassphraseRequiredError,
    IntegrityError,
)
from .manifest import Manifest, Keyring
from .crate import pack_crate, CrateReader
from .pallet import PalletBuilder, PalletReader

__version__ = "0.2.0"

__all__ = [
    "WarehouseError", "FormatError", "KeyMissingError", "PassphraseRequiredError",
    "IntegrityError", "Manifest", "Keyring", "pack_crate",
    "CrateReader", "PalletBuilder", "PalletReader",
]
