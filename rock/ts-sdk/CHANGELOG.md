# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
