# Upstream sources and local modifications

This directory contains a modified fork of the [Tinker Python SDK](https://github.com/thinking-machines-lab/tinker) and a subset of the [Tinker Cookbook](https://github.com/thinking-machines-lab/tinker-cookbook). Both use Apache-2.0. It is not the unmodified official distribution.

| Component | License and attribution | Version/reference |
|---|---|---|
| Python SDK, `src/tinker/` | [LICENSE](LICENSE), Copyright 2025 Tinker | SDK metadata identifies 0.18.1; compared with official commit `931e2bd06dcc74d916c521afd83d58c4205df6be`. |
| Cookbook, `tinker_cookbook/` | [Cookbook LICENSE](tinker_cookbook/LICENSE), Copyright 2025 Thinking Machines Lab | Compared with official commit `9dfcc3a2cf44432d621a01005073dc6d76a1e87a`; this is not a confirmed import baseline. |

The inherited source did not record the exact upstream fork/import commits. We retain that uncertainty explicitly in [UPSTREAM.json](UPSTREAM.json), rather than assigning a comparison snapshot as the original source revision. The root `tinker-sources.json` records the inherited local source snapshots, a separate lineage step.

## Changes in this fork

- SDK client transport and runtime-scoped sampling/training routes, structured chat prompts and weight publication.
- `TinkerClient`, `RuntimeClient`, the local `ServerJob` lifecycle and public task catalog.
- Local backend storage/configuration and ROLL runtime integration.
- SWE-agent/Harbor ModelService recipes, public task preparation, isolated environments and documentation.

Original SDK transport/type/generated protobuf code and shared Cookbook abstractions retain their upstream origin. Modified upstream-derived files are marked with a change notice; new local extension files are recorded in the provenance manifest. Existing upstream notices remain intact. The local backend is a separate implementation, not a backend released by the two upstream repositories.

## Distribution

The combined `rl-rock` wheel includes the SDK license, Cookbook license and [NOTICE](NOTICE) in its license metadata. Maintain these files and source links when moving or trimming forked code. Build/test entrypoints and the current rollout/training protocol remain unchanged by attribution updates.
