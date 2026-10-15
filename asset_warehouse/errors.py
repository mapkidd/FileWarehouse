class WarehouseError(Exception):
    """Base class for all Asset Warehouse errors."""


class FormatError(WarehouseError):
    """File is not a valid crate / pallet / manifest, or is corrupt."""


class IntegrityError(WarehouseError):
    """Data failed authentication or checksum verification."""


class KeyMissingError(WarehouseError):
    """The manifest needed to decrypt a container is not loaded."""

    def __init__(self, key_id, manifest_name=None):
        self.key_id = key_id
        self.manifest_name = manifest_name
        label = f"'{manifest_name}' " if manifest_name else ""
        super().__init__(f"Manifest {label}(key id {key_id}) is required but not loaded")


class PassphraseRequiredError(WarehouseError):
    """Manifest is passphrase-protected and no / wrong passphrase was given."""
