# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.1] - 2026-03-28

### Fixed

- **OSS HTTPS Connection** - OSS client now uses HTTPS by default
  - Added `secure: true` to `ali-oss` client initialization in `setupOss()`
  - Previously, OSS client defaulted to HTTP protocol, causing connection refused errors
  - OSS buckets typically require HTTPS connections for security

## [1.3.0] - 2026-03-28

### Added

- **OSS File Download** - New `downloadFile()` method to download files from sandbox via OSS
  - Downloads remote files from sandbox to local machine using OSS as intermediate storage
  - Automatically installs ossutil in sandbox for OSS operations
  - Generates unique object names to avoid conflicts

- **Enhanced File Upload with Upload Mode** - `uploadByPath()` now accepts `uploadMode` parameter
  - `auto`: Automatically choose upload method based on file size (>1MB) and OSS availability
  - `direct`: Force direct HTTP upload
  - `oss`: Force OSS upload for large files

- **OSS STS Credentials Management**
  - `getOssStsCredentials()`: Fetch STS token from sandbox `/get_token` API
  - `isTokenExpired()`: Check token expiration with 5-minute buffer
  - Automatic token refresh support via `refreshSTSToken`

- **New Types**
  - `DownloadFileResponse`: Response type for download operations
  - `OssCredentials`: STS credentials structure
  - `UploadMode`: Enum for upload mode selection (`'auto' | 'direct' | 'oss'`)

### New Constants

- `ENSURE_OSSUTIL_SCRIPT`: Script to install ossutil in sandbox

## [1.2.7] - 2026-03-11

### Fixed

- HTTP errors now preserve `response` property for status code detection
  - Previously, `HttpUtils.post()`, `get()`, and `postMultipart()` wrapped errors in generic `Error` objects, losing HTTP status code information
  - Now re-throws original `AxiosError`, allowing callers to access `error.response.status` (e.g., 401, 403, 500)
  - Consistent with Python SDK behavior

## [1.2.4] - 2026-02-16

### Added

- `HttpResponse` interface with `status`, `result`, `error`, and `headers` fields
- Response header extraction in `HttpUtils` methods (`get`, `post`, `postMultipart`)
- New fields in `SandboxStatusResponse`: `cluster`, `requestId`, `eagleeyeTraceid`
- Header info extraction in `Sandbox.getStatus()` for debugging and tracing

### Changed

- `HttpUtils.get()`, `post()`, `postMultipart()` now return `HttpResponse<T>` instead of `T`
- Improved error messages to include backend `error` field when available
- Updated `EnvHubClient` to adapt to new `HttpResponse` return type

## [1.2.1] - 2025-02-12

### Added

- Initial TypeScript SDK release based on Python SDK `rl-rock`
- Apache License 2.0
- **Sandbox Module**
  - `Sandbox` class for managing remote container sandboxes
  - `SandboxGroup` class for batch sandbox operations
  - `Deploy` class for deploying working directories
  - `FileSystem` class for file operations (chown, chmod, uploadDir)
  - `Network` class for network acceleration configuration
  - `Process` class for script execution
  - `RemoteUser` class for user management
  - `RuntimeEnv` framework for Python/Node.js runtime management
  - `SpeedupType` enum for acceleration types (APT, PIP, GitHub)
- **EnvHub Module**
  - `EnvHubClient` for environment registration and management
  - `RockEnvInfo` schema for environment information
- **Envs Module**
  - `RockEnv` class with Gym-style interface (step, reset, close)
  - `make()` factory function
- **Model Module**
  - `ModelClient` for LLM communication
  - `ModelService` for local model service management
- **Common Module**
  - `Codes` enum for status codes
  - Exception classes (`RockException`, `BadRequestRockError`, etc.)
- **Utils Module**
  - `HttpUtils` class with axios backend
  - `retryAsync` and `withRetry` decorators
  - `deprecated` and `deprecatedClass` decorators
- **Logger**
  - Winston-based logging with timezone support
- **Types**
  - Zod schemas for request/response validation
  - Full TypeScript type definitions

### Technical Details

- Built with TypeScript 5.x
- Dual ESM/CommonJS module support via tsup
- Tested with Jest (59 test cases)
- Dependencies: axios, zod, winston, ali-oss

## [Unreleased]

### Planned

- Agent framework (RockAgent, SWEAgent, OpenHands agent)
- More comprehensive test coverage
- Documentation improvements
- Performance optimizations
