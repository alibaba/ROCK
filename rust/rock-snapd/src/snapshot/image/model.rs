//! Image build commands and resolved immutable image identities.
//!
//! Validation belongs to these operations. HTTP decoding and response JSON
//! belong to the endpoint adapter.

use serde::Serialize;

use crate::snapshot::repository::PublicationState;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BuildImage {
    pub snapshot_id: String,
    pub oci_ref: String,
    pub oci_digest: String,
    pub platform: String,
    pub base_image_builder_version: String,
    pub base_image_build_key: String,
}

impl BuildImage {
    pub fn validate(&self) -> Result<(), String> {
        require_nonempty("snapshot_id", &self.snapshot_id, 4096)?;
        require_nonempty("oci_ref", &self.oci_ref, 4096)?;
        validate_prefixed_sha256(&self.oci_digest)?;
        require_nonempty("platform", &self.platform, 256)?;
        require_nonempty(
            "base_image_builder_version",
            &self.base_image_builder_version,
            256,
        )?;
        validate_prefixed_sha256(&self.base_image_build_key)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolveImage {
    pub oci_ref: String,
    pub platform: String,
}

impl ResolveImage {
    pub fn validate(&self) -> Result<(), String> {
        require_nonempty("oci_ref", &self.oci_ref, 4096)?;
        require_nonempty("platform", &self.platform, 256)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedImage {
    /// Effective locator, which may be a discovered accelerated sibling tag.
    pub oci_ref: String,
    /// Immutable platform manifest of the selected representation.
    pub oci_digest: String,
    pub oci_platform: String,
    pub base_image_builder_version: String,
    pub base_image_build_key: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImageBuildAccepted {
    pub identity: String,
    pub state: PublicationState,
}

fn require_nonempty(label: &str, value: &str, max: usize) -> Result<(), String> {
    if value.is_empty() || value.len() > max {
        Err(format!("{label} length must be 1..={max}"))
    } else {
        Ok(())
    }
}

fn validate_prefixed_sha256(value: &str) -> Result<(), String> {
    let Some(hex) = value.strip_prefix("sha256:") else {
        return Err("checksum must use sha256:<64 lowercase hex>".into());
    };
    if hex.len() == 64
        && hex
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(())
    } else {
        Err("checksum must contain 64 lowercase hex characters".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::snapshot::metadata::{canonical_json_bytes, sha256_identity};
    use crate::snapshot::services::background_uploader::SnapshotPublication;

    fn build_image() -> BuildImage {
        BuildImage {
            snapshot_id: "domain-image".into(),
            oci_ref: "registry.example/repo:tag".into(),
            oci_digest: format!("sha256:{}", "a".repeat(64)),
            platform: "linux/amd64".into(),
            base_image_builder_version: "builder-v1".into(),
            base_image_build_key: format!("sha256:{}", "b".repeat(64)),
        }
    }

    #[test]
    fn domain_image_commands_preserve_canonical_build_identity() {
        let request = build_image();
        assert_eq!(
            sha256_identity(&canonical_json_bytes(&request).unwrap()),
            "sha256:c457bbc3681dd60dd297ce535c871423288b7396610737de2523fcbc1e701ca9"
        );
        let publication = SnapshotPublication {
            snapshot_id: request.snapshot_id,
        };
        assert_eq!(
            canonical_json_bytes(&publication).unwrap(),
            b"{\"snapshot_id\":\"domain-image\"}\n"
        );
    }

    #[test]
    fn image_commands_retain_input_bounds_and_checksum_error_messages() {
        let request = build_image();
        request.validate().unwrap();
        let mut bad = request.clone();
        bad.snapshot_id.clear();
        assert_eq!(
            bad.validate().unwrap_err(),
            "snapshot_id length must be 1..=4096"
        );
        bad = request.clone();
        bad.oci_ref = "x".repeat(4097);
        assert_eq!(
            bad.validate().unwrap_err(),
            "oci_ref length must be 1..=4096"
        );
        bad = request.clone();
        bad.platform = "x".repeat(257);
        assert_eq!(
            bad.validate().unwrap_err(),
            "platform length must be 1..=256"
        );
        bad = request.clone();
        bad.base_image_builder_version.clear();
        assert_eq!(
            bad.validate().unwrap_err(),
            "base_image_builder_version length must be 1..=256"
        );
        bad = request.clone();
        bad.oci_digest = "a".repeat(64);
        assert_eq!(
            bad.validate().unwrap_err(),
            "checksum must use sha256:<64 lowercase hex>"
        );
        bad = request.clone();
        bad.base_image_build_key = format!("sha256:{}", "A".repeat(64));
        assert_eq!(
            bad.validate().unwrap_err(),
            "checksum must contain 64 lowercase hex characters"
        );
        let resolve = ResolveImage {
            oci_ref: request.oci_ref,
            platform: String::new(),
        };
        assert_eq!(
            resolve.validate().unwrap_err(),
            "platform length must be 1..=256"
        );
    }
}
