//! Shared typed physical-transport failures.

use thiserror::Error;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum RangeReadError {
    #[error("object or requested version was not found")]
    NotFound,
    #[error("object range is invalid or exceeds the object")]
    InvalidRange,
    #[error("provider failed [{class}]: {message}")]
    Failure {
        class: RangeReadFailureClass,
        message: String,
    },
    #[error(
        "preferred source failed ({preferred_source}); provider fallback failed: {final_error}"
    )]
    Fallback {
        preferred_source: RangeReadFallbackSource,
        final_error: Box<RangeReadError>,
    },
}

/// Stable provider-side failure taxonomy used for retry and fail-closed
/// decisions.  Human-readable messages are diagnostic only and must never be
/// parsed to decide whether a failed immutable read is safe to retry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RangeReadFailureClass {
    Transient,
    Authorization,
    Integrity,
    Permanent,
}

impl std::fmt::Display for RangeReadFailureClass {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Transient => "transient",
            Self::Authorization => "authorization",
            Self::Integrity => "integrity",
            Self::Permanent => "permanent",
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RangeReadFallbackClass {
    Unavailable,
    Timeout,
    Transport,
    NotFound,
    Integrity,
}

impl std::fmt::Display for RangeReadFallbackClass {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::Unavailable => "unavailable",
            Self::Timeout => "timeout",
            Self::Transport => "transport",
            Self::NotFound => "not_found",
            Self::Integrity => "integrity",
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RangeReadFallbackSource {
    pub class: RangeReadFallbackClass,
    pub message: String,
}

impl std::fmt::Display for RangeReadFallbackSource {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}: {}", self.class, self.message)
    }
}

impl RangeReadError {
    pub fn failure(class: RangeReadFailureClass, message: impl Into<String>) -> Self {
        Self::Failure {
            class,
            message: message.into().chars().take(512).collect(),
        }
    }

    pub fn fallback(
        class: RangeReadFallbackClass,
        message: impl Into<String>,
        final_error: RangeReadError,
    ) -> Self {
        Self::Fallback {
            preferred_source: RangeReadFallbackSource {
                class,
                message: message.into().chars().take(512).collect(),
            },
            final_error: Box::new(final_error),
        }
    }

    /// The authoritative provider result after all preferred-source fallback.
    /// Callers making correctness decisions must classify this typed value,
    /// never the combined human-readable error text.
    pub fn into_final_error(self) -> Self {
        match self {
            Self::Fallback { final_error, .. } => final_error.into_final_error(),
            error => error,
        }
    }

    /// Recover the typed provider error after the generic residency registry
    /// has serialized it through `Display`.  This parses only our fixed
    /// envelope and class token; the diagnostic message is never inspected.
    pub fn from_stable_display(value: &str) -> Option<Self> {
        if value == Self::NotFound.to_string() {
            return Some(Self::NotFound);
        }
        if value == Self::InvalidRange.to_string() {
            return Some(Self::InvalidRange);
        }
        let value = value.strip_prefix("provider failed [")?;
        let (class, message) = value.split_once("]: ")?;
        let class = match class {
            "transient" => RangeReadFailureClass::Transient,
            "authorization" => RangeReadFailureClass::Authorization,
            "integrity" => RangeReadFailureClass::Integrity,
            "permanent" => RangeReadFailureClass::Permanent,
            _ => return None,
        };
        Some(Self::failure(class, message))
    }
}
