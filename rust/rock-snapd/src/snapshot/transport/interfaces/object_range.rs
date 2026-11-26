//! Physical immutable object ranges, without snapshot formats or VM identity.

use crate::snapshot::transport::RangeReadError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PhysicalRangeRequest<'a> {
    /// Explicit namespace when it is carried by the authenticated source.
    /// A transport already bound to one namespace may accept `None`.
    pub namespace: Option<&'a str>,
    pub object_key: &'a str,
    /// Identity of the complete object, not a digest of this partial response.
    pub object_sha256: &'a str,
    /// Complete physical object size, when present in authenticated metadata.
    /// This is mandatory for registry blobs and is never inferred from a range.
    pub object_size: Option<u64>,
    pub object_version: Option<&'a str>,
    pub offset: u64,
    pub length: u64,
}

pub use crate::snapshot::transport::PhysicalRangeReader;

impl PhysicalRangeRequest<'_> {
    /// Validate common object identity and bounded geometry before any I/O.
    /// A partial read cannot establish the complete object's SHA; formats
    /// still authenticate their decoded DATA independently.
    pub fn validate(&self) -> Result<(), RangeReadError> {
        let digest = self
            .object_sha256
            .strip_prefix("sha256:")
            .unwrap_or(self.object_sha256);
        if self.object_key.is_empty()
            || self.namespace.is_some_and(str::is_empty)
            || self.object_version.is_some_and(str::is_empty)
            || digest.len() != 64
            || !digest
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err(RangeReadError::failure(
                crate::snapshot::transport::RangeReadFailureClass::Integrity,
                "physical object identity is incomplete or invalid",
            ));
        }
        if self.length == 0
            || self
                .offset
                .checked_add(self.length)
                .is_none_or(|end| self.object_size.is_some_and(|size| end > size))
        {
            return Err(RangeReadError::InvalidRange);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request() -> PhysicalRangeRequest<'static> {
        PhysicalRangeRequest {
            namespace: Some("repository"),
            object_key: "object",
            object_sha256:
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            object_size: Some(1024),
            object_version: Some("version"),
            offset: 1000,
            length: 24,
        }
    }

    #[test]
    fn physical_identity_and_exact_object_bounds_are_required() {
        assert!(request().validate().is_ok());
        for invalid in [
            PhysicalRangeRequest {
                namespace: Some(""),
                ..request()
            },
            PhysicalRangeRequest {
                object_key: "",
                ..request()
            },
            PhysicalRangeRequest {
                object_sha256: "not-a-digest",
                ..request()
            },
            PhysicalRangeRequest {
                object_version: Some(""),
                ..request()
            },
        ] {
            assert!(matches!(
                invalid.validate(),
                Err(RangeReadError::Failure {
                    class: crate::snapshot::transport::RangeReadFailureClass::Integrity,
                    ..
                })
            ));
        }
        for invalid in [
            PhysicalRangeRequest {
                length: 0,
                ..request()
            },
            PhysicalRangeRequest {
                length: 25,
                ..request()
            },
            PhysicalRangeRequest {
                offset: u64::MAX,
                ..request()
            },
        ] {
            assert_eq!(invalid.validate(), Err(RangeReadError::InvalidRange));
        }
    }
}
